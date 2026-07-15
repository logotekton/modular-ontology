from __future__ import annotations

import base64
import gzip
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import io
from concurrent.futures import ThreadPoolExecutor

import requests
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    ADMIN_FOLDER,
    DATA_DIR,
    DATABASE_FOLDER,
    DB_PATH,
    EPHEMERAL_STORAGE,
    IFC_MODELS_FOLDER,
    LEGACY_ONTOLOGY_PACKS_FOLDER,
    MCP_TOKENS_FILE,
    ONTOLOGY_PACKS_FOLDER,
    PROJECTS_FOLDER,
    env,
)
from .query_snapshot import (
    QUERY_DATABASE_FILENAME,
    build_query_database,
    compress_query_database,
    validate_query_database,
)

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_TTL_SECONDS = 1800
# Simple/multipart uploads hold the whole payload in memory and cannot resume,
# so anything larger goes through a resumable upload session.
RESUMABLE_UPLOAD_THRESHOLD = 5 * 1024 * 1024
RESUMABLE_UPLOAD_CHUNK_SIZE = 16 * 1024 * 1024  # must be a multiple of 256 KiB
DATABASE_FILENAME = "modular_ontology.sqlite3"
LEGACY_DATABASE_FILENAME = "mod" + "dular_" + "graph.sqlite3"
PROJECT_IFC_FOLDER = "ifc-models"
PROJECT_PACKS_FOLDER = "ontology-packs"
PROJECT_README_FILENAME = "README.md"
COMMON_PROJECT_ID = "_Common"
COMMON_PROJECT_PACK_LINKS_KEY = "__common__"
PROJECT_PACK_LINKS_FILENAME = ".drive-project-pack-links.json"
PROJECT_FOLDERS_FILENAME = ".drive-project-folders.json"
DRIVE_FILE_CACHE_FILENAME = ".google-drive-file-cache.json"
PROJECT_SYNC_MARKER_FOLDER = ".google-drive-project-sync"
_DB_SYNC_LOCK = threading.Lock()

# Drive가 지수 백오프 재시도를 요구하는 일시적 오류 (429 rate limit, 5xx)
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
_RETRY_ATTEMPTS = 4


# 커넥션 풀 공유 세션 — 호출마다 새 TCP+TLS 핸드셰이크를 하던 urllib 직접 호출 대체.
# 스레드 안전(내부 urllib3 풀)이므로 프로젝트 병렬 동기화에서도 공유한다.
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16))


class _HttpResponse:
    """호출부가 기대하는 urllib 응답 인터페이스(read/headers/컨텍스트)를 requests 위에 제공."""

    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self.headers = response.headers

    def read(self, size: int | None = None) -> bytes:
        if size is None:
            return self._response.content
        return self._response.raw.read(size, decode_content=True)

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        self._response.close()
        return False


def _urlopen_with_retry(request: urllib.request.Request, *, timeout: float) -> _HttpResponse:
    """일시적 HTTP/네트워크 오류에 지수 백오프로 재시도하는 실행기.

    urllib.request.Request를 받아 공유 requests 세션으로 실행한다(커넥션 재사용).
    308(Resume Incomplete)은 호출부의 재개 로직이 기대하므로 재시도 없이
    urllib.error.HTTPError로 즉시 전파한다. 429/5xx는 백오프 재시도.
    """
    delay = 1.0
    last_error: Exception | None = None
    method = request.get_method()
    headers = dict(request.header_items())
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = _HTTP_SESSION.request(
                method,
                request.full_url,
                headers=headers,
                data=request.data,
                timeout=timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            last_error = exc
        else:
            status = response.status_code
            if status < 300:
                return _HttpResponse(response)
            error = urllib.error.HTTPError(
                request.full_url, status, response.reason or "", response.headers, io.BytesIO(response.content)
            )
            response.close()
            if status not in _RETRYABLE_HTTP_CODES:
                raise error
            last_error = error
        if attempt < _RETRY_ATTEMPTS - 1:
            time.sleep(delay)
            delay = min(delay * 2.0, 8.0)
    assert last_error is not None
    raise last_error


@dataclass(frozen=True)
class DriveItem:
    id: str
    name: str
    mime_type: str
    modified_time: str = ""
    size: int | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


class GoogleDriveClient:
    def __init__(
        self,
        api_key: str | None = None,
        access_token: str | None = None,
        token_provider=None,
    ) -> None:
        self.api_key = api_key
        self._access_token = access_token
        # 요청 시점마다 유효 토큰을 반환 — 1시간 이상 걸리는 동기화 중 만료 대응
        self._token_provider = token_provider
        self._provider_token_cache: tuple[float, str] | None = None
        self._list_connection_primed = False

    @classmethod
    def from_env(cls) -> "GoogleDriveClient":
        access_token = env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_ACCESS_TOKEN") or os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN")
        if access_token:
            return cls(access_token=str(access_token))

        if env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_USE_GCLOUD_AUTH") == "1":
            return cls(access_token=_gcloud_access_token())

        service_account = _load_service_account_info()
        if service_account:
            return cls(token_provider=_service_account_token_provider(service_account))

        api_key = env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_API_KEY") or os.environ.get("GOOGLE_DRIVE_API_KEY")
        if api_key:
            return cls(api_key=str(api_key))

        raise RuntimeError(
            "Google Drive sync needs one of MODULAR_ONTOLOGY_GOOGLE_SERVICE_ACCOUNT_JSON, "
            "MODULAR_ONTOLOGY_GOOGLE_DRIVE_ACCESS_TOKEN, or MODULAR_ONTOLOGY_GOOGLE_DRIVE_API_KEY."
        )

    def list_children(self, folder_id: str) -> list[DriveItem]:
        items: list[DriveItem] = []
        page_token: str | None = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size)",
                "pageSize": "1000",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            if not self._list_connection_primed:
                payload = self._request_json_fresh("files", params)
                self._list_connection_primed = True
            else:
                try:
                    payload = self._request_json("files", params)
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                    payload = self._request_json_fresh("files", params)
            for raw in payload.get("files", []):
                items.append(
                    DriveItem(
                        id=str(raw["id"]),
                        name=str(raw["name"]),
                        mime_type=str(raw["mimeType"]),
                        modified_time=str(raw.get("modifiedTime", "")),
                        size=int(raw["size"]) if raw.get("size") else None,
                    )
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                return items

    def download_file(self, file_id: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        params = {"alt": "media", "supportsAllDrives": "true"}
        url = self._url(f"files/{file_id}", params)
        request = urllib.request.Request(url, headers=self._headers())
        temp = target.with_name(f".{target.name}.tmp")
        with _urlopen_with_retry(request, timeout=60) as response:
            with temp.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
        os.replace(temp, target)

    def download_gzip_file(self, file_id: str, target: Path) -> None:
        """Stream a gzip-compressed Drive file directly into its uncompressed target."""

        target.parent.mkdir(parents=True, exist_ok=True)
        params = {"alt": "media", "supportsAllDrives": "true"}
        url = self._url(f"files/{file_id}", params)
        request = urllib.request.Request(url, headers=self._headers())
        temp = target.with_name(f".{target.name}.tmp")
        temp.unlink(missing_ok=True)
        try:
            with _urlopen_with_retry(request, timeout=120) as response:
                with gzip.GzipFile(fileobj=response, mode="rb") as compressed, temp.open("wb") as stream:
                    shutil.copyfileobj(compressed, stream, length=1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
            validate_query_database(temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def update_file(self, file_id: str, source: Path, mime_type: str | None = None) -> dict[str, Any]:
        content_type = mime_type or _guess_mime_type(source)
        size = source.stat().st_size
        if size > RESUMABLE_UPLOAD_THRESHOLD:
            return self._resumable_upload(f"files/{file_id}", "PATCH", None, source, content_type, size)
        params = {"uploadType": "media", "supportsAllDrives": "true"}
        url = self._upload_url(f"files/{file_id}", params)
        data = source.read_bytes()
        request = urllib.request.Request(
            url,
            data=data,
            headers={**self._headers(), "Content-Type": content_type},
            method="PATCH",
        )
        with _urlopen_with_retry(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def create_file(self, parent_id: str, source: Path, name: str | None = None, mime_type: str | None = None) -> dict[str, Any]:
        boundary = f"modular-{uuid.uuid4().hex}"
        file_name = name or source.name
        content_type = mime_type or _guess_mime_type(source)
        metadata = {"name": file_name, "parents": [parent_id]}
        size = source.stat().st_size
        if size > RESUMABLE_UPLOAD_THRESHOLD:
            return self._resumable_upload("files", "POST", metadata, source, content_type, size)
        metadata_bytes = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        file_bytes = source.read_bytes()
        body = b"".join(
            [
                f"--{boundary}\r\n".encode("ascii"),
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
                metadata_bytes,
                b"\r\n",
                f"--{boundary}\r\n".encode("ascii"),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            ]
        )
        url = self._upload_url("files", {"uploadType": "multipart", "supportsAllDrives": "true"})
        request = urllib.request.Request(
            url,
            data=body,
            headers={**self._headers(), "Content-Type": f"multipart/related; boundary={boundary}"},
            method="POST",
        )
        with _urlopen_with_retry(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))

    def _resumable_upload(
        self,
        path: str,
        method: str,
        metadata: dict[str, Any] | None,
        source: Path,
        content_type: str,
        size: int,
    ) -> dict[str, Any]:
        params = {"uploadType": "resumable", "supportsAllDrives": "true"}
        url = self._upload_url(path, params)
        headers = {
            **self._headers(),
            "X-Upload-Content-Type": content_type,
            "X-Upload-Content-Length": str(size),
        }
        body = None
        if metadata is not None:
            body = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=UTF-8"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with _urlopen_with_retry(request, timeout=60) as response:
            session_url = response.headers.get("Location")
        if not session_url:
            raise RuntimeError("Google Drive did not return a resumable upload session URL.")

        offset = 0
        stalled_responses = 0
        with source.open("rb") as stream:
            while offset < size:
                stream.seek(offset)
                chunk = stream.read(RESUMABLE_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    raise RuntimeError(f"Source file shrank during resumable upload: {source}")
                chunk_end = offset + len(chunk) - 1
                chunk_request = urllib.request.Request(
                    session_url,
                    data=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{chunk_end}/{size}",
                    },
                    method="PUT",
                )
                try:
                    with _urlopen_with_retry(chunk_request, timeout=300) as response:
                        return json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    if exc.code != 308:
                        raise
                    # 308 Resume Incomplete: Drive reports the confirmed range.
                    confirmed = str(exc.headers.get("Range") or "")
                    if confirmed.startswith("bytes=0-"):
                        next_offset = int(confirmed.removeprefix("bytes=0-")) + 1
                    else:
                        # No Range means Drive has not confirmed this chunk; retry it.
                        next_offset = offset
                    if next_offset == offset:
                        stalled_responses += 1
                        if stalled_responses >= 3:
                            raise RuntimeError(f"Google Drive resumable upload made no progress for {source.name}.")
                    else:
                        stalled_responses = 0
                    offset = next_offset
        raise RuntimeError(f"Google Drive resumable upload ended without a completion response: {source.name}")

    def create_folder(self, parent_id: str, name: str) -> DriveItem:
        metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        data = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        url = self._url("files", {"fields": "id,name,mimeType", "supportsAllDrives": "true"})
        request = urllib.request.Request(
            url,
            data=data,
            headers={**self._headers(), "Content-Type": "application/json; charset=UTF-8"},
            method="POST",
        )
        with _urlopen_with_retry(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return DriveItem(id=str(payload["id"]), name=str(payload["name"]), mime_type=str(payload["mimeType"]))

    def upload_file_by_name(
        self,
        folder_id: str,
        source: Path,
        name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        file_name = name or source.name
        children = _children_by_name(self, folder_id)
        existing = children.get(file_name)
        if existing and existing.is_folder:
            raise RuntimeError(f"Google Drive item {file_name!r} is a folder, not a file.")
        if existing:
            result = self.update_file(existing.id, source, mime_type)
            return {"status": "updated", "id": existing.id, "name": file_name, "result": result}
        result = self.create_file(folder_id, source, file_name, mime_type)
        return {"status": "created", "id": result.get("id"), "name": file_name, "result": result}

    def _request_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = self._url(path, params)
        request = urllib.request.Request(url, headers=self._headers())
        with _urlopen_with_retry(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _request_json_fresh(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(4):
            response = requests.get(
                self._url(path, params),
                headers=self._headers(),
                timeout=30,
            )
            try:
                if response.status_code < 300:
                    return response.json()
                error = urllib.error.HTTPError(
                    response.url,
                    response.status_code,
                    response.reason or "",
                    response.headers,
                    io.BytesIO(response.content),
                )
                if response.status_code != 404 or attempt >= 3:
                    raise error
            finally:
                response.close()
            time.sleep(0.5 * (2**attempt))
        raise RuntimeError("Unreachable Drive retry state")

    def _url(self, path: str, params: dict[str, str]) -> str:
        all_params = dict(params)
        if self.api_key:
            all_params["key"] = self.api_key
        return f"{DRIVE_API}/{path}?{urllib.parse.urlencode(all_params)}"

    def _upload_url(self, path: str, params: dict[str, str]) -> str:
        return f"{DRIVE_UPLOAD_API}/{path}?{urllib.parse.urlencode(params)}"

    def _headers(self) -> dict[str, str]:
        if self._token_provider is not None:
            now = time.monotonic()
            if self._provider_token_cache is None or now - self._provider_token_cache[0] >= 3000:
                self._provider_token_cache = (now, str(self._token_provider()))
            return {"Authorization": f"Bearer {self._provider_token_cache[1]}"}
        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}
        return {}


def google_drive_sync_enabled() -> bool:
    return bool(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID"))


def google_drive_sync_status(*, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    marker = _sync_marker(data_dir)
    if not marker.exists():
        return {"status": "not-synced", "downloaded": [], "missing": []}
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "downloaded": [], "missing": []}


def sync_google_drive_storage(
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
    data_dir: Path = DATA_DIR,
    force: bool = False,
    include_shared_packs: bool = True,
) -> dict[str, Any]:
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id:
        return {"status": "skipped", "reason": "MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set."}

    ttl = int(str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_SYNC_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))))
    marker = _sync_marker(data_dir)
    if not force and ttl > 0 and marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            if time.time() - float(previous.get("synced_at", 0)) < ttl:
                return {"status": "cached", **previous}
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    client = client or GoogleDriveClient.from_env()
    root = _children_by_name(client, root_folder_id)
    downloaded: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []
    file_cache = _load_drive_file_cache(data_dir)

    admin = root.get(ADMIN_FOLDER)
    if admin and admin.is_folder:
        downloaded.extend(
            _download_named_files(
                client,
                admin.id,
                data_dir / ADMIN_FOLDER,
                {"users.json", "mcp_remote.json", "mcp_tokens.json"},
                data_dir=data_dir,
                file_cache=file_cache,
                skipped=skipped,
            )
        )
    else:
        missing.append(ADMIN_FOLDER)

    database = root.get(DATABASE_FOLDER)
    if database and database.is_folder:
        downloaded.extend(
            _download_database_file(
                client,
                database.id,
                data_dir / DATABASE_FOLDER,
                data_dir=data_dir,
                file_cache=file_cache,
                skipped=skipped,
            )
        )
    else:
        missing.append(DATABASE_FOLDER)

    projects = root.get(PROJECTS_FOLDER)
    project_root_found = bool(projects and projects.is_folder)
    if projects and projects.is_folder:
        project_downloads, project_pack_links = _download_project_assets(
            client,
            projects.id,
            data_dir,
            warnings=warnings,
            file_cache=file_cache,
            skipped=skipped,
            # _Common은 프로젝트 모델의 일부(모든 프로젝트에 공통 팩 배포)라 항상 동기화.
            # include_shared_packs는 레거시 전역 팩 폴더(02/04_Ontology_Packs)에만 적용.
            include_common_packs=True,
        )
        downloaded.extend(project_downloads)
        _write_project_pack_links(data_dir, project_pack_links)
    else:
        _write_project_pack_links(data_dir, {})
        _write_project_folders(data_dir, [])
        missing.append(PROJECTS_FOLDER)

    pack_roots = (
        [
            item
            for item in (root.get(ONTOLOGY_PACKS_FOLDER), root.get(LEGACY_ONTOLOGY_PACKS_FOLDER))
            if item and item.is_folder
        ]
        if include_shared_packs
        else []
    )
    if pack_roots:
        for packs in pack_roots:
            pack_folders = _children_by_name(client, packs.id)
            indexed = pack_folders.get("indexed")
            if indexed and indexed.is_folder:
                downloaded.extend(
                    _download_zip_files(
                        client,
                        indexed.id,
                        data_dir / ONTOLOGY_PACKS_FOLDER / "indexed",
                        warnings=warnings,
                        data_dir=data_dir,
                        file_cache=file_cache,
                        skipped=skipped,
                    )
                )
            elif packs.name == ONTOLOGY_PACKS_FOLDER and not project_root_found:
                missing.append(f"{ONTOLOGY_PACKS_FOLDER}/indexed")
    elif not project_root_found:
        missing.append(ONTOLOGY_PACKS_FOLDER)

    ifc_models = root.get(IFC_MODELS_FOLDER)
    if ifc_models and ifc_models.is_folder:
        downloaded.extend(
            _download_ifc_metadata_files(
                client,
                ifc_models.id,
                data_dir / IFC_MODELS_FOLDER,
                warnings=warnings,
                data_dir=data_dir,
                file_cache=file_cache,
                skipped=skipped,
            )
        )

    _write_drive_file_cache(data_dir, file_cache)
    result = {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "skipped": skipped,
        "missing": missing,
        "warnings": warnings,
        "sharedPacksIncluded": include_shared_packs,
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def sync_google_drive_project_storage(
    project_id: str,
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
    data_dir: Path = DATA_DIR,
    force: bool = False,
) -> dict[str, Any]:
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id:
        return {"status": "skipped", "reason": "MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set.", "scope": "project"}

    safe_project_id = _safe_drive_filename(project_id.strip())
    ttl = int(str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_PROJECT_SYNC_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))))
    marker = _project_sync_marker(data_dir, safe_project_id)
    if not force and ttl > 0 and marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            if time.time() - float(previous.get("synced_at", 0)) < ttl:
                return {"status": "cached", **previous}
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    client = client or GoogleDriveClient.from_env()
    root = _children_by_name(client, root_folder_id)
    projects = root.get(PROJECTS_FOLDER)
    if not projects or not projects.is_folder:
        return {
            "status": "missing",
            "scope": "project",
            "projectId": safe_project_id,
            "downloaded": [],
            "skipped": [],
            "missing": [PROJECTS_FOLDER],
            "warnings": [],
        }

    warnings: list[str] = []
    project_item: DriveItem | None = None
    for item in client.list_children(projects.id):
        if not item.is_folder:
            continue
        item_project_id = _safe_drive_filename_or_none(item.name, warnings, f"{PROJECTS_FOLDER} project folder")
        if item_project_id == safe_project_id:
            project_item = item
            break

    if project_item is None:
        return {
            "status": "missing",
            "scope": "project",
            "projectId": safe_project_id,
            "downloaded": [],
            "skipped": [],
            "missing": [f"{PROJECTS_FOLDER}/{safe_project_id}"],
            "warnings": warnings,
        }

    skipped: list[str] = []
    file_cache = _load_drive_file_cache(data_dir)
    downloaded, pack_ids, _packs_folder_found = _download_single_project_assets(
        client,
        project_item,
        safe_project_id,
        data_dir,
        warnings=warnings,
        file_cache=file_cache,
        skipped=skipped,
    )
    _merge_project_pack_links(data_dir, {safe_project_id: pack_ids})
    _write_drive_file_cache(data_dir, file_cache)

    result = {
        "status": "synced",
        "scope": "project",
        "projectId": safe_project_id,
        "projectFolderId": project_item.id,
        "projectName": project_item.name,
        "synced_at": time.time(),
        "downloaded": downloaded,
        "skipped": skipped,
        "missing": [],
        "warnings": warnings,
        "packIds": pack_ids,
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def sync_google_drive_users_file(
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
    data_dir: Path = DATA_DIR,
) -> dict[str, Any]:
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id:
        return {"status": "skipped", "reason": "MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set."}

    client = client or GoogleDriveClient.from_env()
    root = _children_by_name(client, root_folder_id)
    admin = root.get(ADMIN_FOLDER)
    if not admin or not admin.is_folder:
        return {"status": "synced", "synced_at": time.time(), "downloaded": [], "missing": [ADMIN_FOLDER], "scope": "users"}

    downloaded = _download_named_files(client, admin.id, data_dir / ADMIN_FOLDER, {"users.json"})
    return {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "missing": [] if downloaded else [f"{ADMIN_FOLDER}/users.json"],
        "scope": "users",
    }


def sync_google_drive_mcp_tokens_file(
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
    data_dir: Path = DATA_DIR,
) -> dict[str, Any]:
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id:
        return {"status": "skipped", "reason": "MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set."}

    client = client or GoogleDriveClient.from_env()
    root = _children_by_name(client, root_folder_id)
    admin = root.get(ADMIN_FOLDER)
    if not admin or not admin.is_folder:
        return {
            "status": "synced",
            "synced_at": time.time(),
            "downloaded": [],
            "missing": [ADMIN_FOLDER],
            "scope": "mcp_tokens",
        }

    downloaded = _download_named_files(client, admin.id, data_dir / ADMIN_FOLDER, {"mcp_tokens.json"})
    return {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "missing": [] if downloaded else [f"{ADMIN_FOLDER}/mcp_tokens.json"],
        "scope": "mcp_tokens",
    }


def write_back_google_drive_file(
    source: Path,
    drive_folder_path: list[str],
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
    name: str | None = None,
    mime_type: str | None = None,
    create_folders: bool = False,
) -> dict[str, Any]:
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id:
        return {"status": "skipped", "reason": "MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set."}
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(str(source))
    client = client or GoogleDriveClient.from_env()
    folder_id = _resolve_folder_path(client, root_folder_id, drive_folder_path, create_missing=create_folders)
    uploaded = client.upload_file_by_name(folder_id, source, name or source.name, mime_type)
    return {
        "status": "written",
        "source": str(source),
        "drivePath": "/".join([*drive_folder_path, name or source.name]),
        "upload": uploaded,
        "written_at": time.time(),
    }


def write_back_users_file(*, data_dir: Path = DATA_DIR, client: GoogleDriveClient | None = None) -> dict[str, Any]:
    return write_back_google_drive_file(
        data_dir / ADMIN_FOLDER / "users.json",
        [ADMIN_FOLDER],
        client=client,
        name="users.json",
        mime_type="application/json; charset=utf-8",
    )


def write_back_mcp_tokens_file(*, data_dir: Path = DATA_DIR, client: GoogleDriveClient | None = None) -> dict[str, Any]:
    return write_back_google_drive_file(
        Path(env("MODULAR_ONTOLOGY_MCP_TOKENS_FILE", MCP_TOKENS_FILE)),
        [ADMIN_FOLDER],
        client=client,
        name="mcp_tokens.json",
        mime_type="application/json; charset=utf-8",
    )


def _write_back_query_database_file(*, client: GoogleDriveClient | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="modular-ontology-query-") as temp_dir:
        query_db_path = Path(temp_dir) / QUERY_DATABASE_FILENAME.removesuffix(".gz")
        query_gzip_path = Path(temp_dir) / QUERY_DATABASE_FILENAME
        query_stats = build_query_database(DB_PATH, query_db_path)
        compression = compress_query_database(query_db_path, query_gzip_path)
        compact_result = write_back_google_drive_file(
            query_gzip_path,
            [DATABASE_FOLDER],
            client=client,
            name=QUERY_DATABASE_FILENAME,
            mime_type="application/gzip",
        )
    compact_status = str(compact_result.get("status") or "unknown")
    return {
        "status": "synced" if compact_status in {"created", "updated", "written", "synced"} else compact_status,
        "queryDatabase": compact_result,
        "queryDatabaseStats": {**query_stats, **compression},
    }


def write_back_query_database_file(*, client: GoogleDriveClient | None = None) -> dict[str, Any]:
    """Publish only the compact serverless MCP query snapshot."""

    with _DB_SYNC_LOCK:
        _checkpoint_sqlite(DB_PATH, truncate=True)
        return _write_back_query_database_file(client=client)


def write_back_database_file(*, client: GoogleDriveClient | None = None) -> dict[str, Any]:
    with _DB_SYNC_LOCK:
        _checkpoint_sqlite(DB_PATH, truncate=True)
        full_result = write_back_google_drive_file(
            DB_PATH,
            [DATABASE_FOLDER],
            client=client,
            name=DATABASE_FILENAME,
            mime_type="application/vnd.sqlite3",
        )
        query_result = _write_back_query_database_file(client=client)
        compact_result = query_result["queryDatabase"]
        statuses = {str(full_result.get("status")), str(compact_result.get("status"))}
        successful_statuses = {"created", "updated", "written", "synced"}
        status = "synced" if statuses <= successful_statuses else "error"
        return {
            "status": status,
            "database": full_result,
            "queryDatabase": compact_result,
            "queryDatabaseStats": query_result["queryDatabaseStats"],
        }


def write_back_pack_file(
    pack_path: Path,
    *,
    project_id: str | None = None,
    client: GoogleDriveClient | None = None,
) -> dict[str, Any]:
    if not project_id:
        return {
            "status": "skipped",
            "reason": "Project-scoped pack write-back needs a project_id.",
            "source": str(pack_path.resolve()),
        }
    safe_project_id = _safe_drive_filename(project_id)
    name = pack_path.name
    scoped_prefix = f"{safe_project_id}__"
    if name.startswith(scoped_prefix):
        name = name[len(scoped_prefix) :]
    return write_back_google_drive_file(
        pack_path,
        [PROJECTS_FOLDER, safe_project_id, PROJECT_PACKS_FOLDER],
        client=client,
        name=name,
        mime_type="application/zip",
        create_folders=True,
    )


def write_back_ifc_file(
    ifc_path: Path,
    project_id: str,
    *,
    client: GoogleDriveClient | None = None,
) -> dict[str, Any]:
    safe_project_id = _safe_drive_filename(project_id)
    return write_back_google_drive_file(
        ifc_path,
        [PROJECTS_FOLDER, safe_project_id, PROJECT_IFC_FOLDER],
        client=client,
        name=ifc_path.name,
        mime_type=_guess_mime_type(ifc_path),
        create_folders=True,
    )


def write_back_ifc_metadata_file(
    metadata_path: Path,
    project_id: str,
    *,
    client: GoogleDriveClient | None = None,
) -> dict[str, Any]:
    safe_project_id = _safe_drive_filename(project_id)
    return write_back_google_drive_file(
        metadata_path,
        [PROJECTS_FOLDER, safe_project_id, "metadata"],
        client=client,
        name=metadata_path.name,
        mime_type="application/json; charset=utf-8",
        create_folders=True,
    )


def ensure_project_drive_folders(
    project_id: str,
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
) -> dict[str, Any]:
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id:
        return {"status": "skipped", "reason": "MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set."}
    safe_project_id = _safe_drive_filename(project_id)
    client = client or GoogleDriveClient.from_env()
    projects_folder_id = _resolve_folder_path(client, root_folder_id, [PROJECTS_FOLDER], create_missing=True)
    project_folder_id = _resolve_folder_path(client, root_folder_id, [PROJECTS_FOLDER, safe_project_id], create_missing=True)
    ensured_paths = [f"{PROJECTS_FOLDER}/{safe_project_id}"]
    for folder_name in (PROJECT_IFC_FOLDER, PROJECT_PACKS_FOLDER):
        _resolve_folder_path(client, project_folder_id, [folder_name], create_missing=True)
        ensured_paths.append(f"{PROJECTS_FOLDER}/{safe_project_id}/{folder_name}")
    readme_result = _ensure_project_readme_file(client, project_folder_id, safe_project_id)
    return {
        "status": "ensured",
        "projectId": safe_project_id,
        "projectsFolderId": projects_folder_id,
        "projectFolderId": project_folder_id,
        "paths": ensured_paths,
        "files": [f"{PROJECTS_FOLDER}/{safe_project_id}/{PROJECT_README_FILENAME}"],
        "readme": readme_result,
        "written_at": time.time(),
    }


def _ensure_project_readme_file(client: GoogleDriveClient, project_folder_id: str, project_id: str) -> dict[str, Any]:
    existing = _children_by_name(client, project_folder_id).get(PROJECT_README_FILENAME)
    if existing and existing.is_folder:
        raise RuntimeError(f"Google Drive item {PROJECT_README_FILENAME!r} is a folder, not a file.")
    if existing:
        return {"status": "exists", "id": existing.id, "name": PROJECT_README_FILENAME}

    content = "\n".join(
        [
            f"# {project_id}",
            "",
            "Modular Ontology project workspace.",
            "",
            "## Folders",
            "",
            f"- `{PROJECT_IFC_FOLDER}`: IFC, IFCZIP, ZIP, and XKT model files.",
            f"- `{PROJECT_PACKS_FOLDER}`: project ontology pack ZIP files.",
            "",
        ]
    )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as temp:
            temp.write(content)
            temp_path = Path(temp.name)
        return client.upload_file_by_name(
            project_folder_id,
            temp_path,
            PROJECT_README_FILENAME,
            "text/markdown; charset=utf-8",
        )
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def _children_by_name(client: GoogleDriveClient, folder_id: str) -> dict[str, DriveItem]:
    children: dict[str, DriveItem] = {}
    for item in client.list_children(folder_id):
        existing = children.get(item.name)
        if not existing or _prefer_drive_item(item, existing):
            children[item.name] = item
    return children


def _prefer_drive_item(candidate: DriveItem, current: DriveItem) -> bool:
    if candidate.is_folder != current.is_folder:
        return candidate.is_folder
    candidate_time = _drive_time_to_epoch(candidate.modified_time) or 0
    current_time = _drive_time_to_epoch(current.modified_time) or 0
    if candidate_time != current_time:
        return candidate_time > current_time
    return candidate.id > current.id


def _resolve_folder_path(
    client: GoogleDriveClient,
    root_folder_id: str,
    folder_names: list[str],
    *,
    create_missing: bool = False,
) -> str:
    folder_id = root_folder_id
    for folder_name in folder_names:
        item = _children_by_name(client, folder_id).get(folder_name)
        if not item and create_missing:
            item = client.create_folder(folder_id, folder_name)
        if not item or not item.is_folder:
            raise RuntimeError(f"Google Drive folder not found: {'/'.join(folder_names)}")
        folder_id = item.id
    return folder_id


def _sync_marker(data_dir: Path) -> Path:
    return data_dir / ".google-drive-sync.json"


def _registry_sync_marker(data_dir: Path) -> Path:
    return data_dir / ".google-drive-registry-sync.json"


def _project_sync_marker(data_dir: Path, project_id: str) -> Path:
    return data_dir / PROJECT_SYNC_MARKER_FOLDER / f"{project_id}.json"


def _drive_file_cache_path(data_dir: Path) -> Path:
    return data_dir / DRIVE_FILE_CACHE_FILENAME


def _load_drive_file_cache(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = _drive_file_cache_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _write_drive_file_cache(data_dir: Path, cache: dict[str, dict[str, Any]]) -> None:
    path = _drive_file_cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _drive_cache_key(data_dir: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(data_dir.resolve()).as_posix()
    except ValueError:
        return str(target.resolve())


def _drive_item_signature(item: DriveItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "modifiedTime": item.modified_time,
        "size": item.size,
    }


def _drive_file_is_unchanged(
    item: DriveItem,
    target: Path,
    *,
    data_dir: Path,
    file_cache: dict[str, dict[str, Any]],
) -> bool:
    if not target.exists():
        return False
    return file_cache.get(_drive_cache_key(data_dir, target)) == _drive_item_signature(item)


def _remember_drive_file(
    item: DriveItem,
    target: Path,
    *,
    data_dir: Path,
    file_cache: dict[str, dict[str, Any]],
) -> None:
    file_cache[_drive_cache_key(data_dir, target)] = _drive_item_signature(item)


def _download_file_if_changed(
    client: GoogleDriveClient,
    item: DriveItem,
    target: Path,
    *,
    data_dir: Path | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> bool:
    if data_dir is not None and file_cache is not None:
        if _drive_file_is_unchanged(item, target, data_dir=data_dir, file_cache=file_cache):
            if skipped is not None:
                skipped.append(str(target))
            return False
        client.download_file(item.id, target)
        _remember_drive_file(item, target, data_dir=data_dir, file_cache=file_cache)
        return True
    client.download_file(item.id, target)
    return True


def sync_google_drive_registry_files(
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
    data_dir: Path = DATA_DIR,
    force: bool = False,
) -> dict[str, Any]:
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id:
        return {"status": "skipped", "reason": "MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set.", "scope": "registry"}

    ttl = int(str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_REGISTRY_SYNC_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))))
    marker = _registry_sync_marker(data_dir)
    if not force and ttl > 0 and marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            if time.time() - float(previous.get("synced_at", 0)) < ttl:
                return {"status": "cached", **previous}
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    client = client or GoogleDriveClient.from_env()
    direct_result = _sync_serverless_registry_files_by_id(client, data_dir=data_dir)
    if direct_result is not None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(direct_result, ensure_ascii=False, indent=2), encoding="utf-8")
        return direct_result
    root = _children_by_name(client, root_folder_id)
    downloaded: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []
    file_cache = _load_drive_file_cache(data_dir)

    admin = root.get(ADMIN_FOLDER)
    if admin and admin.is_folder:
        downloaded.extend(
            _download_named_files(
                client,
                admin.id,
                data_dir / ADMIN_FOLDER,
                {"users.json", "mcp_remote.json", "mcp_tokens.json"},
                data_dir=data_dir,
                file_cache=file_cache,
                skipped=skipped,
            )
        )
    else:
        missing.append(ADMIN_FOLDER)

    database = root.get(DATABASE_FOLDER)
    if database and database.is_folder:
        downloaded.extend(
            _download_database_file(
                client,
                database.id,
                data_dir / DATABASE_FOLDER,
                data_dir=data_dir,
                file_cache=file_cache,
                skipped=skipped,
            )
        )
    else:
        missing.append(DATABASE_FOLDER)

    ifc_models = root.get(IFC_MODELS_FOLDER)
    if ifc_models and ifc_models.is_folder:
        downloaded.extend(
            _download_ifc_metadata_files(
                client,
                ifc_models.id,
                data_dir / IFC_MODELS_FOLDER,
                warnings=warnings,
                data_dir=data_dir,
                file_cache=file_cache,
                skipped=skipped,
            )
        )

    projects = root.get(PROJECTS_FOLDER)
    if projects and projects.is_folder:
        _write_project_folders(data_dir, _project_folder_records(client, projects.id, warnings=warnings))
        downloaded.extend(
            _download_project_ifc_metadata_files(
                client,
                projects.id,
                data_dir,
                warnings=warnings,
                file_cache=file_cache,
                skipped=skipped,
            )
        )
    else:
        _write_project_folders(data_dir, [])

    _write_drive_file_cache(data_dir, file_cache)
    result = {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "skipped": skipped,
        "missing": missing,
        "warnings": warnings,
        "scope": "registry",
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _sync_serverless_registry_files_by_id(
    client: GoogleDriveClient,
    *,
    data_dir: Path,
) -> dict[str, Any] | None:
    """Restore the minimal Vercel registry without traversing shared Drive folders."""

    if not EPHEMERAL_STORAGE:
        return None
    query_database_file_id = str(env("MODULAR_ONTOLOGY_QUERY_DATABASE_FILE_ID", "") or "").strip()
    if not query_database_file_id:
        return None

    downloaded: list[str] = []
    warnings: list[str] = []
    database_target = data_dir / DATABASE_FOLDER / DATABASE_FILENAME
    client.download_gzip_file(query_database_file_id, database_target)
    downloaded.append(str(database_target))

    direct_files = (
        (
            "MODULAR_ONTOLOGY_USERS_FILE_ID",
            data_dir / ADMIN_FOLDER / "users.json",
        ),
        (
            "MODULAR_ONTOLOGY_MCP_TOKENS_FILE_ID",
            data_dir / ADMIN_FOLDER / "mcp_tokens.json",
        ),
    )
    for env_name, target in direct_files:
        file_id = str(env(env_name, "") or "").strip()
        if not file_id:
            warnings.append(f"{env_name} is not set")
            continue
        client.download_file(file_id, target)
        downloaded.append(str(target))

    return {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "skipped": [],
        "missing": [],
        "warnings": warnings,
        "scope": "registry",
        "mode": "direct-file-ids",
    }


def _project_folder_records(
    client: GoogleDriveClient,
    projects_folder_id: str,
    *,
    warnings: list[str] | None = None,
) -> list[dict[str, str]]:
    project_folders: list[dict[str, str]] = []
    for project in client.list_children(projects_folder_id):
        if not project.is_folder:
            continue
        project_id = _safe_drive_filename_or_none(project.name, warnings, f"{PROJECTS_FOLDER} project folder")
        if not project_id or project_id == COMMON_PROJECT_ID:
            continue
        project_folders.append(
            {
                "folderId": project.id,
                "projectId": project_id,
                "name": project.name,
                "modifiedTime": project.modified_time,
            }
        )
    return project_folders


def _download_project_ifc_metadata_files(
    client: GoogleDriveClient,
    projects_folder_id: str,
    data_dir: Path,
    *,
    warnings: list[str] | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> list[str]:
    downloaded: list[str] = []
    for project in client.list_children(projects_folder_id):
        if not project.is_folder:
            continue
        project_id = _safe_drive_filename_or_none(project.name, warnings, f"{PROJECTS_FOLDER} project folder")
        if not project_id:
            continue
        project_children = _children_by_name(client, project.id)
        metadata_folder = project_children.get("metadata")
        existing_metadata_names: set[str] = set()
        target_metadata_dir = data_dir / IFC_MODELS_FOLDER / project_id / "metadata"
        if metadata_folder and metadata_folder.is_folder:
            for item in client.list_children(metadata_folder.id):
                if item.is_folder or not item.name.endswith(".metadata.json"):
                    continue
                metadata_name = _safe_drive_filename_or_none(item.name, warnings, f"{PROJECTS_FOLDER}/{project.name}/metadata")
                if not metadata_name:
                    continue
                target = target_metadata_dir / metadata_name
                if _download_file_if_changed(
                    client,
                    item,
                    target,
                    data_dir=data_dir,
                    file_cache=file_cache,
                    skipped=skipped,
                ):
                    downloaded.append(str(target))
                existing_metadata_names.add(metadata_name)

        ifc_folder = project_children.get(PROJECT_IFC_FOLDER)
        if ifc_folder and ifc_folder.is_folder:
            active_metadata_names: set[str] = set()
            downloaded.extend(
                _register_ifc_files_from_drive_folder(
                    client,
                    ifc_folder.id,
                    data_dir / IFC_MODELS_FOLDER,
                    project_id,
                    project.name,
                    f"{PROJECTS_FOLDER}/{project.name}/{PROJECT_IFC_FOLDER}",
                    existing_metadata_names=existing_metadata_names,
                    active_metadata_names=active_metadata_names,
                    source_is_project=True,
                    warnings=warnings,
                )
            )
            _prune_project_metadata(target_metadata_dir, active_metadata_names)
    return downloaded


def _download_named_files(
    client: GoogleDriveClient,
    folder_id: str,
    target_dir: Path,
    names: set[str],
    *,
    data_dir: Path | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> list[str]:
    downloaded: list[str] = []
    for item in client.list_children(folder_id):
        if item.is_folder or item.name not in names:
            continue
        target = target_dir / _safe_drive_filename(item.name)
        if _download_file_if_changed(
            client,
            item,
            target,
            data_dir=data_dir,
            file_cache=file_cache,
            skipped=skipped,
        ):
            downloaded.append(str(target))
    return downloaded


def _download_database_file(
    client: GoogleDriveClient,
    folder_id: str,
    target_dir: Path,
    *,
    data_dir: Path | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
    prefer_compact: bool | None = None,
) -> list[str]:
    children = [item for item in client.list_children(folder_id) if not item.is_folder]
    prefer_compact = EPHEMERAL_STORAGE if prefer_compact is None else prefer_compact
    if prefer_compact:
        source = next((item for item in children if item.name == QUERY_DATABASE_FILENAME), None)
        if not source:
            raise RuntimeError(
                f"{QUERY_DATABASE_FILENAME} is missing from {DATABASE_FOLDER}; "
                "run a local database write-back before deploying the serverless MCP."
            )
    else:
        source = next((item for item in children if item.name == DATABASE_FILENAME), None)
        if not source:
            source = next((item for item in children if item.name == LEGACY_DATABASE_FILENAME), None)
    if not source:
        return []
    target = target_dir / DATABASE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    if data_dir is not None and file_cache is not None and _drive_file_is_unchanged(source, target, data_dir=data_dir, file_cache=file_cache):
        if skipped is not None:
            skipped.append(str(target))
        return []
    staging = target.with_name(f".{target.name}.download")
    if prefer_compact:
        client.download_gzip_file(source.id, staging)
    else:
        client.download_file(source.id, staging)
    with _DB_SYNC_LOCK:
        _remove_sqlite_sidecars(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)
        _remove_sqlite_sidecars(target)
    if data_dir is not None and file_cache is not None:
        _remember_drive_file(source, target, data_dir=data_dir, file_cache=file_cache)
    return [str(target)]


def _download_ifc_metadata_files(
    client: GoogleDriveClient,
    ifc_folder_id: str,
    target_dir: Path,
    *,
    warnings: list[str] | None = None,
    data_dir: Path | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> list[str]:
    downloaded: list[str] = []
    for project in client.list_children(ifc_folder_id):
        if not project.is_folder:
            continue
        project_id = _safe_drive_filename_or_none(project.name, warnings, f"{IFC_MODELS_FOLDER} project folder")
        if not project_id:
            continue
        project_children = _children_by_name(client, project.id)
        metadata_folder = project_children.get("metadata")
        existing_metadata_names: set[str] = set()
        if metadata_folder and metadata_folder.is_folder:
            for item in client.list_children(metadata_folder.id):
                if item.is_folder or not item.name.endswith(".metadata.json"):
                    continue
                metadata_name = _safe_drive_filename_or_none(item.name, warnings, f"{IFC_MODELS_FOLDER}/{project.name}/metadata")
                if not metadata_name:
                    continue
                target = target_dir / project_id / "metadata" / metadata_name
                if _is_project_metadata_file(target):
                    existing_metadata_names.add(metadata_name)
                    continue
                if _download_file_if_changed(
                    client,
                    item,
                    target,
                    data_dir=data_dir,
                    file_cache=file_cache,
                    skipped=skipped,
                ):
                    downloaded.append(str(target))
                existing_metadata_names.add(metadata_name)

        files_folder = project_children.get("files")
        if files_folder and files_folder.is_folder:
            downloaded.extend(
                _register_ifc_files_from_drive_folder(
                    client,
                    files_folder.id,
                    target_dir,
                    project_id,
                    project.name,
                    f"{IFC_MODELS_FOLDER}/{project.name}/files",
                    existing_metadata_names=existing_metadata_names,
                    warnings=warnings,
                )
            )
    return downloaded


def _download_project_assets(
    client: GoogleDriveClient,
    projects_folder_id: str,
    data_dir: Path,
    *,
    warnings: list[str] | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
    include_common_packs: bool = True,
) -> tuple[list[str], dict[str, list[str]]]:
    downloaded: list[str] = []
    project_pack_links: dict[str, list[str]] = {}
    project_folders: list[dict[str, str]] = []
    common_folder_found = False
    normal_projects: list[tuple[DriveItem, str]] = []
    for project in client.list_children(projects_folder_id):
        if not project.is_folder:
            continue
        project_id = _safe_drive_filename_or_none(project.name, warnings, f"{PROJECTS_FOLDER} project folder")
        if not project_id:
            continue
        if project_id == COMMON_PROJECT_ID:
            common_folder_found = True
            if not include_common_packs:
                continue
            common_downloads, common_active_paths = _download_common_zip_files(
                client,
                project.id,
                _children_by_name(client, project.id),
                data_dir / ONTOLOGY_PACKS_FOLDER / "indexed",
                warnings=warnings,
                data_dir=data_dir,
                file_cache=file_cache,
                skipped=skipped,
            )
            downloaded.extend(common_downloads)
            project_pack_links[COMMON_PROJECT_PACK_LINKS_KEY] = [_pack_id_from_zip(Path(path)) for path in common_active_paths]
            continue
        normal_projects.append((project, project_id))

    def _process_project(entry: tuple[DriveItem, str]):
        project, project_id = entry
        local_warnings: list[str] = []
        local_skipped: list[str] = []
        children = _children_by_name(client, project.id)
        result = _download_single_project_assets(
            client,
            project,
            project_id,
            data_dir,
            project_children=children,
            warnings=local_warnings,
            file_cache=file_cache,
            skipped=local_skipped,
        )
        return project, project_id, result, local_warnings, local_skipped

    # 프로젝트별 Drive 트리 워크는 I/O 바운드 — 병렬화로 순차 왕복 지연을 겹친다.
    # (파일 캐시 dict 갱신은 GIL 하 원자적 연산이고, 대상 경로는 프로젝트별로 분리됨)
    if normal_projects:
        with ThreadPoolExecutor(max_workers=min(6, len(normal_projects))) as pool:
            for project, project_id, result, local_warnings, local_skipped in pool.map(_process_project, normal_projects):
                project_downloads, pack_ids, packs_folder_found = result
                if warnings is not None:
                    warnings.extend(local_warnings)
                if skipped is not None:
                    skipped.extend(local_skipped)
                project_folders.append(
                    {
                        "folderId": project.id,
                        "projectId": project_id,
                        "name": project.name,
                        "modifiedTime": project.modified_time,
                    }
                )
                downloaded.extend(project_downloads)
                if packs_folder_found:
                    project_pack_links[project_id] = pack_ids
    _write_project_folders(data_dir, project_folders)
    active_project_ids = {item["projectId"] for item in project_folders}
    if common_folder_found:
        active_project_ids.add(COMMON_PROJECT_ID)
    _prune_removed_project_assets(data_dir, active_project_ids)
    return downloaded, project_pack_links


def _download_single_project_assets(
    client: GoogleDriveClient,
    project: DriveItem,
    project_id: str,
    data_dir: Path,
    *,
    project_children: dict[str, DriveItem] | None = None,
    warnings: list[str] | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> tuple[list[str], list[str], bool]:
    downloaded: list[str] = []
    project_children = project_children or _children_by_name(client, project.id)
    metadata_folder = project_children.get("metadata")
    existing_metadata_names: set[str] = set()
    target_metadata_dir = data_dir / IFC_MODELS_FOLDER / project_id / "metadata"
    if metadata_folder and metadata_folder.is_folder:
        for item in client.list_children(metadata_folder.id):
            if item.is_folder or not item.name.endswith(".metadata.json"):
                continue
            metadata_name = _safe_drive_filename_or_none(item.name, warnings, f"{PROJECTS_FOLDER}/{project.name}/metadata")
            if not metadata_name:
                continue
            target = target_metadata_dir / metadata_name
            if _download_file_if_changed(
                client,
                item,
                target,
                data_dir=data_dir,
                file_cache=file_cache,
                skipped=skipped,
            ):
                downloaded.append(str(target))
            existing_metadata_names.add(metadata_name)

    ifc_folder = project_children.get(PROJECT_IFC_FOLDER)
    if ifc_folder and ifc_folder.is_folder:
        active_metadata_names: set[str] = set()
        downloaded.extend(
            _register_ifc_files_from_drive_folder(
                client,
                ifc_folder.id,
                data_dir / IFC_MODELS_FOLDER,
                project_id,
                project.name,
                f"{PROJECTS_FOLDER}/{project.name}/{PROJECT_IFC_FOLDER}",
                existing_metadata_names=existing_metadata_names,
                active_metadata_names=active_metadata_names,
                source_is_project=True,
                warnings=warnings,
            )
        )
        _prune_project_metadata(target_metadata_dir, active_metadata_names)

    packs_folder = project_children.get(PROJECT_PACKS_FOLDER)
    if not packs_folder or not packs_folder.is_folder:
        return downloaded, [], False

    pack_downloads, pack_active_paths = _download_project_grouped_zip_files(
        client,
        packs_folder.id,
        data_dir / ONTOLOGY_PACKS_FOLDER / "indexed",
        project_id,
        warnings=warnings,
        data_dir=data_dir,
        file_cache=file_cache,
        skipped=skipped,
    )
    downloaded.extend(pack_downloads)
    return downloaded, [_pack_id_from_zip(Path(path)) for path in pack_active_paths], True


def _download_common_zip_files(
    client: GoogleDriveClient,
    common_folder_id: str,
    common_children: dict[str, DriveItem],
    target_dir: Path,
    *,
    warnings: list[str] | None = None,
    data_dir: Path | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    downloaded: list[str] = []
    active_paths: list[str] = []
    packs_folder = common_children.get(PROJECT_PACKS_FOLDER)
    if packs_folder and packs_folder.is_folder:
        pack_downloads, pack_active_paths = _download_project_zip_files(
            client,
            packs_folder.id,
            target_dir,
            COMMON_PROJECT_ID,
            warnings=warnings,
            data_dir=data_dir,
            file_cache=file_cache,
            skipped=skipped,
        )
        downloaded.extend(pack_downloads)
        active_paths.extend(pack_active_paths)

    # common_children에 이미 폴더 목록이 있으므로 재조회하지 않는다
    for category in common_children.values():
        if not category.is_folder or category.name == PROJECT_PACKS_FOLDER:
            continue
        category_id = _safe_drive_filename_or_none(category.name, warnings, f"{PROJECTS_FOLDER}/{COMMON_PROJECT_ID} category folder")
        if not category_id:
            continue
        category_children = _children_by_name(client, category.id)
        category_packs = category_children.get(PROJECT_PACKS_FOLDER)
        if not category_packs or not category_packs.is_folder:
            continue
        category_downloads, category_active_paths = _download_project_zip_files(
            client,
            category_packs.id,
            target_dir,
            f"{COMMON_PROJECT_ID}__{category_id}",
            warnings=warnings,
            data_dir=data_dir,
            file_cache=file_cache,
            skipped=skipped,
        )
        downloaded.extend(category_downloads)
        active_paths.extend(category_active_paths)
    return downloaded, active_paths


def _download_project_grouped_zip_files(
    client: GoogleDriveClient,
    packs_folder_id: str,
    target_dir: Path,
    project_id: str,
    *,
    warnings: list[str] | None = None,
    data_dir: Path | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    downloaded, active_paths = _download_project_zip_files(
        client,
        packs_folder_id,
        target_dir,
        project_id,
        warnings=warnings,
        data_dir=data_dir,
        file_cache=file_cache,
        skipped=skipped,
    )
    for category in client.list_children(packs_folder_id):
        if not category.is_folder:
            continue
        category_id = _safe_drive_filename_or_none(
            category.name,
            warnings,
            f"{PROJECTS_FOLDER}/{project_id}/{PROJECT_PACKS_FOLDER} category folder",
        )
        if not category_id:
            continue
        category_downloads, category_active_paths = _download_project_zip_files(
            client,
            category.id,
            target_dir,
            f"{project_id}__{category_id}",
            warnings=warnings,
            data_dir=data_dir,
            file_cache=file_cache,
            skipped=skipped,
        )
        downloaded.extend(category_downloads)
        active_paths.extend(category_active_paths)
    return downloaded, active_paths


def _prune_removed_project_assets(data_dir: Path, active_project_ids: set[str]) -> None:
    ifc_root = data_dir / IFC_MODELS_FOLDER
    if ifc_root.exists():
        for project_dir in ifc_root.iterdir():
            if not project_dir.is_dir() or project_dir.name in active_project_ids:
                continue
            metadata_dir = project_dir / "metadata"
            metadata_files = list(metadata_dir.glob("*.metadata.json")) if metadata_dir.exists() else list(project_dir.glob("*.metadata.json"))
            if metadata_files and all(_is_project_metadata_file(path) for path in metadata_files):
                shutil.rmtree(project_dir, ignore_errors=True)

    indexed_dir = data_dir / ONTOLOGY_PACKS_FOLDER / "indexed"
    if indexed_dir.exists():
        for pack_path in indexed_dir.glob("*.zip"):
            project_id, separator, _pack_name = pack_path.name.partition("__")
            if separator and project_id not in active_project_ids:
                pack_path.unlink(missing_ok=True)


def _register_ifc_files_from_drive_folder(
    client: GoogleDriveClient,
    files_folder_id: str,
    target_dir: Path,
    project_id: str,
    project_name: str,
    drive_folder_path: str,
    *,
    existing_metadata_names: set[str] | None = None,
    active_metadata_names: set[str] | None = None,
    source_is_project: bool = False,
    warnings: list[str] | None = None,
) -> list[str]:
    downloaded: list[str] = []
    existing_metadata_names = existing_metadata_names or set()
    registered_metadata_names: set[str] = set()
    file_items = [
        item
        for item in client.list_children(files_folder_id)
        if not item.is_folder and Path(item.name).suffix.lower() in {".ifc", ".ifczip", ".zip", ".xkt"}
    ]
    file_items.sort(key=lambda item: (Path(item.name).suffix.lower() == ".xkt", item.name.lower()))
    safe_names: dict[str, str] = {}
    for item in file_items:
        safe_name = _safe_drive_filename_or_none(item.name, warnings, drive_folder_path)
        if safe_name:
            safe_names[item.name] = safe_name
    file_names = set(safe_names.values())
    for item in file_items:
        safe_name = safe_names.get(item.name)
        if not safe_name:
            continue
        stem = Path(safe_name).stem
        metadata_name = f"{stem}.metadata.json"
        if metadata_name in registered_metadata_names:
            continue
        if active_metadata_names is not None:
            active_metadata_names.add(metadata_name)
        target = target_dir / project_id / "metadata" / metadata_name
        if metadata_name in existing_metadata_names and not source_is_project:
            continue
        if _is_project_metadata_file(target) and not source_is_project:
            existing_metadata_names.add(metadata_name)
            continue
        sibling_xkt = Path(safe_name).suffix.lower() == ".xkt" or f"{stem}.xkt" in file_names
        metadata = {
            "filename": safe_name,
            "sizeBytes": item.size,
            "projectId": project_id,
            "projectName": project_name,
            "uploadedAt": _drive_time_to_epoch(item.modified_time),
            "storage": "google-drive",
            "localPath": str(target_dir / project_id / "files" / safe_name),
            "viewerStatus": "ready" if sibling_xkt else "pending-xkt",
            "xktPath": str(target_dir / project_id / "files" / f"{stem}.xkt") if sibling_xkt else None,
            "xktError": None if sibling_xkt else f"XKT file is not available in Google Drive {drive_folder_path} folder.",
            "drive": {
                "file": {
                    "id": item.id,
                    "name": item.name,
                    "folder": drive_folder_path,
                    "modifiedTime": item.modified_time,
                }
            },
        }
        if source_is_project and not _project_metadata_needs_update(target, metadata):
            existing_metadata_names.add(metadata_name)
            registered_metadata_names.add(metadata_name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        existing_metadata_names.add(metadata_name)
        registered_metadata_names.add(metadata_name)
        downloaded.append(str(target))
    return downloaded


def _is_project_metadata_file(target: Path) -> bool:
    try:
        metadata = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    drive_folder = (
        metadata.get("drive", {})
        .get("file", {})
        .get("folder")
        if isinstance(metadata.get("drive"), dict)
        else None
    )
    return isinstance(drive_folder, str) and drive_folder.startswith(f"{PROJECTS_FOLDER}/")


def _project_metadata_needs_update(target: Path, metadata: dict[str, Any]) -> bool:
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not _is_project_metadata_file(target):
        return True
    existing_drive_file = existing.get("drive", {}).get("file", {}) if isinstance(existing.get("drive"), dict) else {}
    next_drive_file = metadata.get("drive", {}).get("file", {}) if isinstance(metadata.get("drive"), dict) else {}
    comparable_keys = ("filename", "sizeBytes", "viewerStatus", "xktPath", "xktError")
    for key in comparable_keys:
        if existing.get(key) != metadata.get(key):
            return True
    for key in ("id", "name", "folder", "modifiedTime"):
        if existing_drive_file.get(key) != next_drive_file.get(key):
            return True
    return False


def _prune_project_metadata(metadata_dir: Path, active_metadata_names: set[str]) -> None:
    if not metadata_dir.exists():
        return
    for target in metadata_dir.glob("*.metadata.json"):
        if target.name in active_metadata_names:
            continue
        if _is_project_metadata_file(target):
            target.unlink(missing_ok=True)


def _write_project_pack_links(data_dir: Path, project_pack_links: dict[str, list[str]]) -> None:
    marker = data_dir / PROJECTS_FOLDER / PROJECT_PACK_LINKS_FILENAME
    if not project_pack_links:
        marker.unlink(missing_ok=True)
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(project_pack_links, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_project_pack_links(data_dir: Path) -> dict[str, list[str]]:
    marker = data_dir / PROJECTS_FOLDER / PROJECT_PACK_LINKS_FILENAME
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    links: dict[str, list[str]] = {}
    for project_id, pack_ids in payload.items():
        if not isinstance(pack_ids, list):
            continue
        clean_project_id = str(project_id).strip()
        clean_pack_ids = [str(pack_id).strip() for pack_id in pack_ids if str(pack_id).strip()]
        if clean_project_id:
            links[clean_project_id] = list(dict.fromkeys(clean_pack_ids))
    return links


def _merge_project_pack_links(data_dir: Path, updates: dict[str, list[str]]) -> None:
    links = _read_project_pack_links(data_dir)
    for project_id, pack_ids in updates.items():
        clean_project_id = str(project_id).strip()
        if not clean_project_id:
            continue
        clean_pack_ids = [str(pack_id).strip() for pack_id in pack_ids if str(pack_id).strip()]
        links[clean_project_id] = list(dict.fromkeys(clean_pack_ids))
    marker = data_dir / PROJECTS_FOLDER / PROJECT_PACK_LINKS_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_project_folders(data_dir: Path, project_folders: list[dict[str, str]]) -> None:
    marker = data_dir / PROJECTS_FOLDER / PROJECT_FOLDERS_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(project_folders, ensure_ascii=False, indent=2), encoding="utf-8")


def _drive_time_to_epoch(value: str) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def restore_ifc_files_from_drive(
    project_folder: str,
    file_names: list[str],
    target_dir: Path,
    *,
    client: GoogleDriveClient | None = None,
    root_folder_id: str | None = None,
) -> list[str]:
    """Download IFC model files (e.g. .ifc/.xkt) back from Drive into the local data dir."""
    root_folder_id = root_folder_id or str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "")).strip()
    if not root_folder_id or not file_names:
        return []
    client = client or GoogleDriveClient.from_env()
    wanted = {_safe_drive_filename(name) for name in file_names}
    downloaded: list[str] = []
    for drive_folder_path in (
        [PROJECTS_FOLDER, project_folder, PROJECT_IFC_FOLDER],
        [IFC_MODELS_FOLDER, project_folder, "files"],
    ):
        try:
            folder_id = _resolve_folder_path(client, root_folder_id, drive_folder_path)
        except RuntimeError:
            continue
        children = _children_by_name(client, folder_id)
        for name in list(wanted):
            item = children.get(name)
            if not item or item.is_folder:
                continue
            target = target_dir / name
            client.download_file(item.id, target)
            downloaded.append(str(target))
            wanted.remove(name)
        if not wanted:
            break
    return downloaded


def _safe_drive_filename_or_none(name: str, warnings: list[str] | None, context: str) -> str | None:
    try:
        return _safe_drive_filename(name)
    except RuntimeError as exc:
        if warnings is not None:
            warnings.append(f"{context}: {exc}")
        return None


def _safe_drive_filename(name: str) -> str:
    safe_name = Path(name.replace("\\", "/")).name
    if not safe_name or safe_name in {".", ".."} or safe_name != name:
        raise RuntimeError(f"Unsafe Google Drive filename: {name!r}")
    return safe_name


def _guess_mime_type(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    return guessed or "application/octet-stream"


def _sqlite_sidecar_paths(path: Path) -> list[Path]:
    return [Path(f"{path}-wal"), Path(f"{path}-shm")]


def _remove_sqlite_sidecars(path: Path) -> None:
    for sidecar in _sqlite_sidecar_paths(path):
        sidecar.unlink(missing_ok=True)


def _checkpoint_sqlite(path: Path, *, truncate: bool = False) -> None:
    if not path.exists() or path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
        return
    try:
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            mode = "TRUNCATE" if truncate else "FULL"
            conn.execute(f"PRAGMA wal_checkpoint({mode});")
        finally:
            conn.close()
    except sqlite3.Error:
        return


def _download_zip_files(
    client: GoogleDriveClient,
    folder_id: str,
    target_dir: Path,
    *,
    warnings: list[str] | None = None,
    data_dir: Path | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> list[str]:
    downloaded: list[str] = []
    for item in client.list_children(folder_id):
        if item.is_folder or not item.name.lower().endswith(".zip"):
            continue
        safe_name = _safe_drive_filename_or_none(item.name, warnings, "legacy ontology pack folder")
        if not safe_name:
            continue
        target = target_dir / safe_name
        if _download_file_if_changed(
            client,
            item,
            target,
            data_dir=data_dir,
            file_cache=file_cache,
            skipped=skipped,
        ):
            downloaded.append(str(target))
    return downloaded


def _download_project_zip_files(
    client: GoogleDriveClient,
    folder_id: str,
    target_dir: Path,
    project_id: str,
    *,
    warnings: list[str] | None = None,
    data_dir: Path | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    downloaded: list[str] = []
    active_paths: list[str] = []
    active_names: set[str] = set()
    for item in client.list_children(folder_id):
        if item.is_folder or not item.name.lower().endswith(".zip"):
            continue
        safe_name = _safe_drive_filename_or_none(item.name, warnings, f"{PROJECTS_FOLDER}/{project_id}/{PROJECT_PACKS_FOLDER}")
        if not safe_name:
            continue
        target = target_dir / f"{project_id}__{safe_name}"
        active_names.add(target.name)
        if _download_file_if_changed(
            client,
            item,
            target,
            data_dir=data_dir,
            file_cache=file_cache,
            skipped=skipped,
        ):
            downloaded.append(str(target))
        if target.exists():
            active_paths.append(str(target))
    _prune_project_pack_cache(target_dir, project_id, active_names)
    return downloaded, active_paths


def _pack_id_from_zip(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            if "manifest.json" in zf.namelist():
                manifest = json.loads(zf.read("manifest.json").decode("utf-8-sig"))
                pack_id = str(manifest.get("pack_id") or "").strip()
                if pack_id:
                    return pack_id
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        pass
    return path.stem


def _prune_project_pack_cache(target_dir: Path, project_id: str, active_names: set[str]) -> None:
    if not target_dir.exists():
        return
    prefix = f"{project_id}__"
    for target in target_dir.glob(f"{prefix}*.zip"):
        if target.name not in active_names:
            target.unlink(missing_ok=True)


def _load_service_account_info() -> dict[str, Any] | None:
    raw_json = env("MODULAR_ONTOLOGY_GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw_json:
        return json.loads(str(raw_json))

    raw_b64 = env("MODULAR_ONTOLOGY_GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if raw_b64:
        try:
            return json.loads(base64.b64decode(str(raw_b64).strip()).decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Failed to decode service account JSON B64: {exc}") from exc

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    return None


def _gcloud_access_token() -> str:
    command = str(env("MODULAR_ONTOLOGY_GCLOUD_COMMAND", "gcloud"))
    resolved_command = shutil.which(command) or command
    try:
        token = subprocess.check_output(
            [resolved_command, "auth", "print-access-token"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
        ).strip()
    except Exception as exc:
        raise RuntimeError(f"Failed to get gcloud access token: {exc}") from exc
    if not token:
        raise RuntimeError("gcloud did not return an access token.")
    return token


def _service_account_token_provider(service_account: dict[str, Any]):
    """만료 시 자동 재발급하는 토큰 provider (요청마다 호출, 갱신은 락으로 직렬화)."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account as google_service_account
    except ImportError as exc:
        raise RuntimeError("Install google-auth and requests to use Google Drive service account sync.") from exc

    credentials = google_service_account.Credentials.from_service_account_info(
        service_account,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    refresh_lock = threading.Lock()

    def provider() -> str:
        if not credentials.valid:
            with refresh_lock:
                if not credentials.valid:
                    credentials.refresh(Request())
        if not credentials.token:
            raise RuntimeError("Google Drive service account did not return an access token.")
        return str(credentials.token)

    return provider


def _service_account_access_token(service_account: dict[str, Any]) -> str:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account as google_service_account
    except ImportError as exc:
        raise RuntimeError("Install google-auth and requests to use Google Drive service account sync.") from exc

    credentials = google_service_account.Credentials.from_service_account_info(
        service_account,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("Google Drive service account did not return an access token.")
    return credentials.token
