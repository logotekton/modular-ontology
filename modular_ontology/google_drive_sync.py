from __future__ import annotations

import base64
import gzip
import json
import mimetypes
import os
import shutil
import sqlite3
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
from datetime import datetime, timezone

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
    PACKS_DIR,
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
PACK_REGISTRY_FILENAME = "pack_registry.json"
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
_PACK_CACHE_LOCK = threading.Lock()

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
    projects = root.get(PROJECTS_FOLDER)
    project_root_found = bool(projects and projects.is_folder)
    project_inventory_client = (
        _capture_project_asset_inventory(client, projects.id)
        if projects and projects.is_folder
        else client
    )
    project_folders = (
        _project_folder_records(project_inventory_client, projects.id, warnings=warnings)
        if projects and projects.is_folder
        else []
    )
    registry_payload: dict[str, Any] = {}
    project_assets_reconciled = False
    if database and database.is_folder:
        database_items = [item for item in client.list_children(database.id) if not item.is_folder]
        registry_item = next(
            (item for item in database_items if item.name == PACK_REGISTRY_FILENAME),
            None,
        )
        database_item = _select_database_drive_item(database_items, prefer_compact=EPHEMERAL_STORAGE)
        if database_item and not registry_item:
            raise RuntimeError(
                f"{PACK_REGISTRY_FILENAME} is required before activating a Drive database generation."
            )
        if registry_item:
            with tempfile.TemporaryDirectory(
                prefix=".modular-ontology-full-sync-",
                dir=data_dir.parent,
            ) as staging_dir:
                staging_data = Path(staging_dir)
                staged_registry = staging_data / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
                registry_downloaded = _stage_drive_item(
                    client,
                    registry_item,
                    data_dir / DATABASE_FOLDER / PACK_REGISTRY_FILENAME,
                    staged_registry,
                    data_dir=data_dir,
                    file_cache=file_cache,
                    skipped=skipped,
                )
                staged_database: Path | None = None
                database_downloaded = False
                if database_item:
                    staged_database = staging_data / DATABASE_FOLDER / DATABASE_FILENAME
                    database_downloaded = _stage_drive_item(
                        client,
                        database_item,
                        data_dir / DATABASE_FOLDER / DATABASE_FILENAME,
                        staged_database,
                        data_dir=data_dir,
                        file_cache=file_cache,
                        skipped=skipped,
                        decompress_gzip=database_item.name == QUERY_DATABASE_FILENAME,
                    )

                registry_payload = _load_registry_path(staged_registry)
                validate_pack_registry(registry_payload, require_drive_mappings=True)
                if staged_database is not None:
                    validate_database_against_pack_registry(staged_database, registry_payload)
                else:
                    live_database = data_dir / DATABASE_FOLDER / DATABASE_FILENAME
                    if live_database.exists():
                        validate_database_against_pack_registry(live_database, registry_payload)
                _validate_registry_project_folder_inventory(
                    staging_data,
                    project_folders,
                    registry=registry_payload,
                )
                expected_pack_ids_by_file_id: dict[str, str] = {}
                if projects and projects.is_folder:
                    expected_pack_ids_by_file_id = _validate_drive_project_pack_inventory(
                        project_inventory_client,
                        projects.id,
                        project_folders,
                        registry_payload,
                    )
                live_database = data_dir / DATABASE_FOLDER / DATABASE_FILENAME
                generation_activated = staged_database is not None or live_database.exists()
                if projects and projects.is_folder:
                    staged_project_assets = staging_data / "project-assets"
                    _seed_staged_project_assets(data_dir, staged_project_assets)
                    staged_file_cache = dict(file_cache)
                    staged_skipped: list[str] = []
                    project_downloads, project_pack_links = _download_project_assets(
                        project_inventory_client,
                        projects.id,
                        staged_project_assets,
                        warnings=warnings,
                        file_cache=staged_file_cache,
                        skipped=staged_skipped,
                        include_common_packs=True,
                        prune=True,
                        persist_inventory=True,
                        expected_pack_ids_by_file_id=expected_pack_ids_by_file_id,
                    )
                    _write_project_pack_links(staged_project_assets, project_pack_links)
                    _activate_staged_database_registry(
                        data_dir=data_dir,
                        staged_registry=staged_registry,
                        staged_database=staged_database,
                        staged_project_assets=staged_project_assets,
                        activate_registry=generation_activated,
                    )
                    project_assets_reconciled = True
                    file_cache.clear()
                    file_cache.update(staged_file_cache)
                    downloaded.extend(
                        _live_paths_from_staging(
                            project_downloads,
                            staged_data_dir=staged_project_assets,
                            data_dir=data_dir,
                        )
                    )
                    skipped.extend(
                        _live_paths_from_staging(
                            staged_skipped,
                            staged_data_dir=staged_project_assets,
                            data_dir=data_dir,
                        )
                    )
                elif generation_activated:
                    _activate_staged_database_registry(
                        data_dir=data_dir,
                        staged_registry=staged_registry,
                        staged_database=staged_database,
                    )
                if generation_activated:
                    registry_target = data_dir / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
                    _remember_drive_file(
                        registry_item,
                        registry_target,
                        data_dir=data_dir,
                        file_cache=file_cache,
                    )
                    if registry_downloaded:
                        downloaded.append(str(registry_target))
                if generation_activated and database_item and staged_database is not None:
                    database_target = data_dir / DATABASE_FOLDER / DATABASE_FILENAME
                    _remember_drive_file(
                        database_item,
                        database_target,
                        data_dir=data_dir,
                        file_cache=file_cache,
                    )
                    if database_downloaded:
                        downloaded.append(str(database_target))
        else:
            missing.append(f"{DATABASE_FOLDER}/{PACK_REGISTRY_FILENAME}")
    else:
        missing.append(DATABASE_FOLDER)

    if projects and projects.is_folder and not project_assets_reconciled:
        folder_marker = data_dir / PROJECTS_FOLDER / PROJECT_FOLDERS_FILENAME
        links_marker = data_dir / PROJECTS_FOLDER / PROJECT_PACK_LINKS_FILENAME
        persist_inventory = bool(registry_payload) or not (folder_marker.exists() or links_marker.exists())
        project_downloads, project_pack_links = _download_project_assets(
            project_inventory_client,
            projects.id,
            data_dir,
            warnings=warnings,
            file_cache=file_cache,
            skipped=skipped,
            # _Common은 프로젝트 모델의 일부(모든 프로젝트에 공통 팩 배포)라 항상 동기화.
            # include_shared_packs는 레거시 전역 팩 폴더(02/04_Ontology_Packs)에만 적용.
            include_common_packs=True,
            prune=bool(registry_payload),
            persist_inventory=persist_inventory,
        )
        downloaded.extend(project_downloads)
        if persist_inventory:
            _write_project_pack_links(data_dir, project_pack_links)
    else:
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
        "inventoryValidated": bool(registry_payload),
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
        registry_source = build_pack_registry()
        validate_pack_registry(load_pack_registry(), require_drive_mappings=True)
        successful_statuses = {"created", "updated", "written", "synced"}
        full_result = write_back_google_drive_file(
            DB_PATH,
            [DATABASE_FOLDER],
            client=client,
            name=DATABASE_FILENAME,
            mime_type="application/vnd.sqlite3",
        )
        if str(full_result.get("status")) not in successful_statuses:
            return {
                "status": "error",
                "database": full_result,
                "queryDatabase": {"status": "skipped", "reason": "full database upload failed"},
                "queryDatabaseStats": {},
                "registry": {"status": "skipped", "reason": "database generation is incomplete"},
            }
        query_result = _write_back_query_database_file(client=client)
        compact_result = query_result["queryDatabase"]
        if str(compact_result.get("status")) not in successful_statuses:
            return {
                "status": "error",
                "database": full_result,
                "queryDatabase": compact_result,
                "queryDatabaseStats": query_result["queryDatabaseStats"],
                "registry": {"status": "skipped", "reason": "database generation is incomplete"},
            }
        # The registry is the publication commit marker.  Upload it last only
        # after both database artifacts are known to be durable in Drive.
        registry_result = write_back_google_drive_file(
            registry_source,
            [DATABASE_FOLDER],
            client=client,
            name=PACK_REGISTRY_FILENAME,
            mime_type="application/json; charset=utf-8",
        )
        status = (
            "synced"
            if str(registry_result.get("status")) in successful_statuses
            else "error"
        )
        return {
            "status": status,
            "database": full_result,
            "queryDatabase": compact_result,
            "queryDatabaseStats": query_result["queryDatabaseStats"],
            "registry": registry_result,
        }


def load_pack_registry(*, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    path = data_dir / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def validate_pack_registry(
    payload: dict[str, Any],
    *,
    require_drive_mappings: bool = False,
) -> dict[str, int]:
    """Reject partial or ambiguous registries before they become runtime authority."""

    if payload.get("version") != 1:
        raise RuntimeError("Pack registry version must be 1.")
    raw_packs = payload.get("packs")
    raw_projects = payload.get("projects")
    raw_common = payload.get("commonPackIds")
    if not isinstance(raw_packs, list) or not raw_packs:
        raise RuntimeError("Pack registry must contain at least one pack.")
    if not isinstance(raw_projects, list):
        raise RuntimeError("Pack registry projects must be a list.")
    if not isinstance(raw_common, list):
        raise RuntimeError("Pack registry commonPackIds must be a list.")

    pack_ids: list[str] = []
    drive_file_ids: list[str] = []
    for pack in raw_packs:
        if not isinstance(pack, dict):
            raise RuntimeError("Pack registry contains a non-object pack entry.")
        pack_id = str(pack.get("id") or "").strip()
        if not pack_id:
            raise RuntimeError("Pack registry contains a pack without an id.")
        pack_ids.append(pack_id)
        drive = pack.get("drive")
        drive_file_id = str(drive.get("fileId") or "").strip() if isinstance(drive, dict) else ""
        if require_drive_mappings and not drive_file_id:
            raise RuntimeError(f"Pack registry is missing a Drive fileId for {pack_id}.")
        if drive_file_id:
            drive_file_ids.append(drive_file_id)
    if len(pack_ids) != len(set(pack_ids)):
        raise RuntimeError("Pack registry contains duplicate pack ids.")
    if len(drive_file_ids) != len(set(drive_file_ids)):
        raise RuntimeError("Pack registry maps multiple packs to the same Drive fileId.")

    valid_pack_ids = set(pack_ids)
    common_pack_ids = [str(pack_id).strip() for pack_id in raw_common if str(pack_id).strip()]
    unknown_common = sorted(set(common_pack_ids) - valid_pack_ids)
    if unknown_common:
        raise RuntimeError(f"Pack registry commonPackIds reference unknown packs: {', '.join(unknown_common[:5])}")

    project_ids: list[str] = []
    drive_folder_ids: list[str] = []
    for project in raw_projects:
        if not isinstance(project, dict):
            raise RuntimeError("Pack registry contains a non-object project entry.")
        project_id = str(project.get("id") or "").strip()
        if not project_id:
            raise RuntimeError("Pack registry contains a project without an id.")
        project_ids.append(project_id)
        drive_folder_id = str(project.get("driveFolderId") or "").strip()
        if drive_folder_id:
            drive_folder_ids.append(drive_folder_id)
        project_pack_ids = project.get("packIds")
        if not isinstance(project_pack_ids, list):
            raise RuntimeError(f"Pack registry project {project_id} has invalid packIds.")
        unknown_project_packs = sorted(
            {
                str(pack_id).strip()
                for pack_id in project_pack_ids
                if str(pack_id).strip() and str(pack_id).strip() not in valid_pack_ids
            }
        )
        if unknown_project_packs:
            raise RuntimeError(
                f"Pack registry project {project_id} references unknown packs: "
                f"{', '.join(unknown_project_packs[:5])}"
            )
    if len(project_ids) != len(set(project_ids)):
        raise RuntimeError("Pack registry contains duplicate project ids.")
    if len(drive_folder_ids) != len(set(drive_folder_ids)):
        raise RuntimeError("Pack registry contains duplicate project Drive folder ids.")

    return {
        "packs": len(pack_ids),
        "projects": len(project_ids),
        "commonPacks": len(set(common_pack_ids)),
        "driveMappings": len(drive_file_ids),
    }


def _registry_source_database_path(data_dir: Path) -> Path:
    candidate = data_dir / DATABASE_FOLDER / DATABASE_FILENAME
    if candidate.exists() or data_dir.resolve() != DATA_DIR.resolve():
        return candidate
    return DB_PATH


def _load_registry_path(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Pack registry is missing or invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Pack registry must be a JSON object: {path}")
    return payload


def validate_database_against_pack_registry(
    database_path: Path,
    registry: dict[str, Any],
) -> dict[str, int]:
    """Fail closed when a database and registry describe different generations."""

    registry_stats = validate_pack_registry(registry)
    expected_pack_ids = {
        str(pack.get("id") or "").strip()
        for pack in registry["packs"]
        if isinstance(pack, dict) and str(pack.get("id") or "").strip()
    }
    common_pack_ids = {
        str(pack_id).strip()
        for pack_id in registry["commonPackIds"]
        if str(pack_id).strip()
    }
    expected_projects: dict[str, set[str]] = {}
    for project in registry["projects"]:
        project_id = str(project.get("id") or "").strip()
        project_pack_ids = {
            str(pack_id).strip()
            for pack_id in project.get("packIds", [])
            if str(pack_id).strip()
        }
        # Registries may store common packs explicitly or only in commonPackIds.
        # A set union makes both representations equivalent without double-counting.
        expected_projects[project_id] = project_pack_ids | common_pack_ids

    try:
        conn = sqlite3.connect(database_path)
        try:
            integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if integrity.casefold() != "ok":
                raise RuntimeError(f"database integrity check failed: {integrity}")
            actual_pack_ids = {str(row[0]) for row in conn.execute("SELECT id FROM packs")}
            actual_project_ids = {str(row[0]) for row in conn.execute("SELECT id FROM projects")}
            actual_projects = {project_id: set() for project_id in actual_project_ids}
            for project_id, pack_id in conn.execute("SELECT project_id, pack_id FROM project_packs"):
                actual_projects.setdefault(str(project_id), set()).add(str(pack_id))
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"Database/registry generation validation failed: {database_path}") from exc

    mismatches: list[str] = []
    if actual_pack_ids != expected_pack_ids:
        mismatches.append(
            f"packs expected={len(expected_pack_ids)} actual={len(actual_pack_ids)}"
        )
    expected_project_ids = set(expected_projects)
    if actual_project_ids != expected_project_ids:
        mismatches.append(
            f"projects expected={len(expected_project_ids)} actual={len(actual_project_ids)}"
        )
    for project_id in sorted(expected_project_ids & actual_project_ids):
        if actual_projects.get(project_id, set()) != expected_projects[project_id]:
            mismatches.append(
                f"project {project_id!r} packs expected={len(expected_projects[project_id])} "
                f"actual={len(actual_projects.get(project_id, set()))}"
            )
    if mismatches:
        raise RuntimeError(
            "Database/registry generation mismatch; refusing to activate mixed data: "
            + "; ".join(mismatches[:8])
        )
    return {
        **registry_stats,
        "projectPacks": sum(len(pack_ids) for pack_ids in actual_projects.values()),
    }


def _registry_pack_folder_path(pack: dict[str, Any], project_id: str | None) -> str:
    drive = pack.get("drive") if isinstance(pack.get("drive"), dict) else {}
    explicit = str(drive.get("folderPath") or "").strip().replace("\\", "/").strip("/")
    if explicit:
        return explicit
    if project_id is None:
        category = str(pack.get("commonCategory") or pack.get("driveCategory") or "").strip()
        parts = [PROJECTS_FOLDER, COMMON_PROJECT_ID]
        if category:
            parts.append(category)
        parts.append(PROJECT_PACKS_FOLDER)
        return "/".join(parts)
    category = str(pack.get("projectCategory") or pack.get("driveCategory") or "").strip()
    parts = [PROJECTS_FOLDER, project_id, PROJECT_PACKS_FOLDER]
    if category:
        parts.append(category)
    return "/".join(parts)


def build_pack_registry(*, data_dir: Path = DATA_DIR) -> Path:
    from .pack_index import PackFile, _db_pack_summaries, summarize_pack
    from .project_store import list_projects as list_stored_projects

    source_db = _registry_source_database_path(data_dir)
    search_dirs = [
        data_dir / ONTOLOGY_PACKS_FOLDER / "indexed",
        data_dir / LEGACY_ONTOLOGY_PACKS_FOLDER / "indexed",
        data_dir / "packs",
    ]
    if data_dir.resolve() == DATA_DIR.resolve():
        search_dirs.append(PACKS_DIR)
    physical_by_id: dict[str, PackFile] = {}
    seen_paths: set[Path] = set()
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*.zip"):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            pack_id = _required_pack_id_from_zip(resolved)
            candidate = PackFile(resolved)
            existing = physical_by_id.get(pack_id)
            if existing is not None:
                raise RuntimeError(
                    "Duplicate physical ZIP pack_id; refusing ambiguous registry authority: "
                    f"{pack_id} ({existing.path}, {resolved})"
                )
            physical_by_id[pack_id] = candidate

    physical_summaries: list[dict[str, Any]] = []
    for pack_id, pack in sorted(physical_by_id.items()):
        summary = summarize_pack(pack)
        if str(summary.get("id") or "").strip() != pack_id:
            raise RuntimeError(f"Physical pack summary id mismatch: {pack.path}")
        physical_summaries.append(summary)

    database_summaries = _db_pack_summaries(source_db)
    database_ids = {
        str(summary.get("id") or "").strip()
        for summary in database_summaries
        if str(summary.get("id") or "").strip()
    }
    physical_ids = set(physical_by_id)
    if physical_ids and database_ids and physical_ids != database_ids:
        raise RuntimeError(
            "Physical ZIP/database pack inventory mismatch; refusing to build a registry "
            f"(physical={len(physical_ids)} database={len(database_ids)})."
        )
    if physical_summaries:
        database_by_id = {
            str(summary.get("id") or "").strip(): summary
            for summary in database_summaries
            if str(summary.get("id") or "").strip()
        }
        packs = [
            {**database_by_id.get(str(summary["id"]), {}), **summary}
            for summary in physical_summaries
        ]
    else:
        packs = database_summaries
    if not packs:
        raise RuntimeError("No physical ZIP or database pack inventory is available.")

    projects = list_stored_projects(packs, db_path=source_db)
    file_cache = _load_drive_file_cache(data_dir)
    pack_files_by_id = {pack_id: pack.path for pack_id, pack in physical_by_id.items()}

    registry_packs: list[dict[str, Any]] = []
    for summary in packs:
        pack_id = str(summary.get("id") or "").strip()
        if not pack_id:
            continue
        registry_summary = dict(summary)
        pack_path = pack_files_by_id.get(pack_id)
        signature = (
            file_cache.get(_drive_cache_key(data_dir, pack_path))
            if pack_path is not None
            else None
        )
        if isinstance(signature, dict) and signature.get("id"):
            registry_summary["drive"] = {
                "fileId": str(signature["id"]),
                "name": str(signature.get("name") or registry_summary.get("displayFilename") or ""),
                "modifiedTime": str(signature.get("modifiedTime") or ""),
                "sizeBytes": signature.get("size"),
                "cachePath": _drive_cache_key(data_dir, pack_path),
            }
        registry_packs.append(registry_summary)

    db_stat = source_db.stat() if source_db.exists() else None
    project_pack_links = _read_project_pack_links(data_dir)
    valid_pack_ids = {str(pack.get("id")) for pack in registry_packs if pack.get("id")}
    common_pack_ids = project_pack_links.get(COMMON_PROJECT_PACK_LINKS_KEY, [])
    valid_common_pack_ids = [
        pack_id
        for pack_id in common_pack_ids
        if pack_id in valid_pack_ids
    ]
    effective_projects = []
    for project in projects:
        project_pack_ids = project.get("packIds")
        effective_projects.append(
            {
                **project,
                "packIds": list(
                    dict.fromkeys(
                        [
                            *valid_common_pack_ids,
                            *(
                                str(pack_id)
                                for pack_id in project_pack_ids
                                if str(pack_id) in valid_pack_ids
                            ),
                        ]
                    )
                )
                if isinstance(project_pack_ids, list)
                else list(valid_common_pack_ids),
            }
        )

    common_id_set = set(valid_common_pack_ids)
    owners_by_pack_id: dict[str, set[str]] = {}
    for project in effective_projects:
        project_id = str(project.get("id") or "").strip()
        for pack_id in {
            str(value).strip()
            for value in project.get("packIds", [])
            if str(value).strip()
        } - common_id_set:
            owners_by_pack_id.setdefault(pack_id, set()).add(project_id)
    for pack in registry_packs:
        drive = pack.get("drive")
        if not isinstance(drive, dict):
            continue
        pack_id = str(pack.get("id") or "").strip()
        owner: str | None
        if pack_id in common_id_set:
            owner = None
        else:
            owners = owners_by_pack_id.get(pack_id, set())
            owner = next(iter(owners)) if len(owners) == 1 else None
            if not owners:
                # Global packs under 04_Ontology_Packs are not project assets.
                continue
            if len(owners) > 1:
                raise RuntimeError(
                    f"Project pack {pack_id} has multiple owners; move it to _Common before publishing."
                )
        drive["folderPath"] = _registry_pack_folder_path(pack, owner)

    registry = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceDb": {
            "sizeBytes": db_stat.st_size if db_stat else 0,
            "modifiedTime": datetime.fromtimestamp(db_stat.st_mtime, timezone.utc).isoformat() if db_stat else "",
        },
        "projects": effective_projects,
        "commonPackIds": valid_common_pack_ids,
        "packs": registry_packs,
    }
    validate_pack_registry(registry)
    path = data_dir / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def write_back_pack_registry_file(
    *,
    data_dir: Path = DATA_DIR,
    client: GoogleDriveClient | None = None,
) -> dict[str, Any]:
    source = build_pack_registry(data_dir=data_dir)
    validate_pack_registry(load_pack_registry(data_dir=data_dir), require_drive_mappings=True)
    return write_back_google_drive_file(
        source,
        [DATABASE_FOLDER],
        client=client,
        name=PACK_REGISTRY_FILENAME,
        mime_type="application/json; charset=utf-8",
    )


def fetch_pack_file_from_drive(
    pack_id: str,
    *,
    client: GoogleDriveClient | None = None,
    data_dir: Path = DATA_DIR,
    packs_dir: Path = PACKS_DIR,
) -> Path:
    registry = load_pack_registry(data_dir=data_dir)
    registry_packs = registry.get("packs")
    if not isinstance(registry_packs, list):
        raise FileNotFoundError(f"Pack registry is unavailable: {pack_id}")
    entry = next(
        (
            item
            for item in registry_packs
            if isinstance(item, dict) and str(item.get("id") or "").strip() == pack_id
        ),
        None,
    )
    if not isinstance(entry, dict):
        raise FileNotFoundError(pack_id)
    drive = entry.get("drive")
    if not isinstance(drive, dict) or not drive.get("fileId"):
        raise FileNotFoundError(f"Pack has no Drive file mapping: {pack_id}")

    filename = _safe_drive_filename(
        str(entry.get("filename") or drive.get("name") or f"{pack_id}.zip")
    )
    if not filename.lower().endswith(".zip"):
        filename = f"{filename}.zip"
    target = packs_dir / filename
    size_value = drive.get("sizeBytes")
    try:
        incoming_size = max(0, int(size_value or entry.get("sizeBytes") or 0))
    except (TypeError, ValueError):
        incoming_size = 0
    drive_item = DriveItem(
        id=str(drive["fileId"]),
        name=str(drive.get("name") or filename),
        mime_type="application/zip",
        modified_time=str(drive.get("modifiedTime") or ""),
        size=incoming_size or None,
    )

    with _PACK_CACHE_LOCK:
        file_cache = _load_drive_file_cache(data_dir)
        if (
            target.exists()
            and _lazy_pack_matches(target, pack_id)
            and _drive_file_is_unchanged(drive_item, target, data_dir=data_dir, file_cache=file_cache)
        ):
            target.touch()
            return target
        target.unlink(missing_ok=True)
        _ensure_pack_cache_budget(packs_dir, incoming_size, keep=target)

        client = client or GoogleDriveClient.from_env()
        client.download_file(str(drive["fileId"]), target)
        try:
            actual_pack_id = _required_pack_id_from_zip(target)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            target.unlink(missing_ok=True)
            raise ValueError(f"Downloaded pack is not a valid manifest ZIP: {pack_id}") from exc
        if actual_pack_id != pack_id:
            target.unlink(missing_ok=True)
            raise ValueError(f"Downloaded pack id mismatch: expected {pack_id}, got {actual_pack_id}")

        _remember_drive_file(
            drive_item,
            target,
            data_dir=data_dir,
            file_cache=file_cache,
        )
        _write_drive_file_cache(data_dir, file_cache)
        return target


def _ensure_pack_cache_budget(packs_dir: Path, incoming_size: int, *, keep: Path) -> None:
    configured = env("MODULAR_ONTOLOGY_PACK_CACHE_MAX_BYTES")
    if configured is None and not EPHEMERAL_STORAGE:
        return
    try:
        limit = max(0, int(str(configured or 300 * 1024 * 1024)))
    except ValueError as exc:
        raise ValueError(f"Invalid MODULAR_ONTOLOGY_PACK_CACHE_MAX_BYTES: {configured}") from exc
    if limit <= 0:
        return
    if incoming_size > limit:
        raise OSError(f"Pack size {incoming_size} exceeds runtime pack cache limit {limit}.")

    packs_dir.mkdir(parents=True, exist_ok=True)
    candidates = [path for path in packs_dir.glob("*.zip") if path != keep]
    total = sum(path.stat().st_size for path in candidates if path.exists())
    if total + incoming_size <= limit:
        return
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime):
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            continue
        if total + incoming_size <= limit:
            return
    if total + incoming_size > limit:
        raise OSError(f"Runtime pack cache cannot free enough space for {keep.name}.")


def write_back_pack_file(
    pack_path: Path,
    *,
    project_id: str | None = None,
    client: GoogleDriveClient | None = None,
    data_dir: Path = DATA_DIR,
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
    drive_folder_path = [PROJECTS_FOLDER, safe_project_id, PROJECT_PACKS_FOLDER]
    try:
        from .pack_index import PackFile, summarize_pack

        summary = summarize_pack(PackFile(pack_path))
    except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError):
        summary = {}
    category = str(
        (summary.get("commonCategory") or "")
        if safe_project_id == COMMON_PROJECT_ID
        else summary.get("projectCategory") or summary.get("driveCategory") or ""
    ).strip()
    if category:
        safe_category = _safe_drive_filename(category)
        name = _safe_drive_filename(str(summary.get("displayFilename") or name.rsplit("__", 1)[-1]))
        drive_folder_path = (
            [PROJECTS_FOLDER, safe_project_id, safe_category, PROJECT_PACKS_FOLDER]
            if safe_project_id == COMMON_PROJECT_ID
            else [PROJECTS_FOLDER, safe_project_id, PROJECT_PACKS_FOLDER, safe_category]
        )
    result = write_back_google_drive_file(
        pack_path,
        drive_folder_path,
        client=client,
        name=name,
        mime_type="application/zip",
        create_folders=True,
    )
    upload = result.get("upload")
    upload = upload if isinstance(upload, dict) else {}
    upload_result = upload.get("result")
    upload_result = upload_result if isinstance(upload_result, dict) else {}
    file_id = str(upload.get("id") or upload_result.get("id") or "").strip()
    if result.get("status") == "written" and not file_id:
        raise RuntimeError("Google Drive pack upload did not return a file id.")
    if file_id:
        file_cache = _load_drive_file_cache(data_dir)
        _remember_drive_file(
            DriveItem(
                id=file_id,
                name=str(upload.get("name") or upload_result.get("name") or name),
                mime_type="application/zip",
                modified_time=str(upload.get("modifiedTime") or upload_result.get("modifiedTime") or ""),
                size=pack_path.stat().st_size,
            ),
            pack_path,
            data_dir=data_dir,
            file_cache=file_cache,
        )
        _write_drive_file_cache(data_dir, file_cache)
    return result


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
    include_database: bool | None = None,
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
    direct_result = _sync_serverless_registry_files_by_id(
        client,
        data_dir=data_dir,
        root_folder_id=root_folder_id,
        include_database=include_database,
    )
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

    projects = root.get(PROJECTS_FOLDER)
    project_inventory_client = (
        _capture_project_asset_inventory(client, projects.id)
        if projects and projects.is_folder
        else client
    )
    project_folders = (
        _project_folder_records(project_inventory_client, projects.id, warnings=warnings)
        if projects and projects.is_folder
        else []
    )
    registry_payload: dict[str, Any] = {}
    database = root.get(DATABASE_FOLDER)
    if database and database.is_folder:
        database_items = [item for item in client.list_children(database.id) if not item.is_folder]
        registry_item = next(
            (item for item in database_items if item.name == PACK_REGISTRY_FILENAME),
            None,
        )
        database_item = _select_database_drive_item(database_items, prefer_compact=EPHEMERAL_STORAGE)
        if database_item and not registry_item:
            raise RuntimeError(
                f"{PACK_REGISTRY_FILENAME} is required before activating a Drive database generation."
            )
        if registry_item:
            with tempfile.TemporaryDirectory(
                prefix=".modular-ontology-registry-sync-",
                dir=data_dir.parent,
            ) as staging_dir:
                staging_data = Path(staging_dir)
                staged_registry = staging_data / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
                registry_downloaded = _stage_drive_item(
                    client,
                    registry_item,
                    data_dir / DATABASE_FOLDER / PACK_REGISTRY_FILENAME,
                    staged_registry,
                    data_dir=data_dir,
                    file_cache=file_cache,
                    skipped=skipped,
                )
                staged_database: Path | None = None
                database_downloaded = False
                if include_database is not False and database_item:
                    staged_database = staging_data / DATABASE_FOLDER / DATABASE_FILENAME
                    database_downloaded = _stage_drive_item(
                        client,
                        database_item,
                        data_dir / DATABASE_FOLDER / DATABASE_FILENAME,
                        staged_database,
                        data_dir=data_dir,
                        file_cache=file_cache,
                        skipped=skipped,
                        decompress_gzip=database_item.name == QUERY_DATABASE_FILENAME,
                    )
                elif include_database is False:
                    skipped.append(str(data_dir / DATABASE_FOLDER / DATABASE_FILENAME))

                registry_payload = _load_registry_path(staged_registry)
                validate_pack_registry(registry_payload, require_drive_mappings=True)
                database_to_validate = staged_database or (data_dir / DATABASE_FOLDER / DATABASE_FILENAME)
                if database_to_validate.exists():
                    validate_database_against_pack_registry(database_to_validate, registry_payload)
                _validate_registry_project_folder_inventory(
                    staging_data,
                    project_folders,
                    registry=registry_payload,
                )
                _activate_staged_database_registry(
                    data_dir=data_dir,
                    staged_registry=staged_registry,
                    staged_database=staged_database,
                )
                registry_target = data_dir / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
                _remember_drive_file(
                    registry_item,
                    registry_target,
                    data_dir=data_dir,
                    file_cache=file_cache,
                )
                if registry_downloaded:
                    downloaded.append(str(registry_target))
                if staged_database is not None and database_item:
                    database_target = data_dir / DATABASE_FOLDER / DATABASE_FILENAME
                    _remember_drive_file(
                        database_item,
                        database_target,
                        data_dir=data_dir,
                        file_cache=file_cache,
                    )
                    if database_downloaded:
                        downloaded.append(str(database_target))
        else:
            missing.append(f"{DATABASE_FOLDER}/{PACK_REGISTRY_FILENAME}")
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

    if projects and projects.is_folder:
        folder_marker = data_dir / PROJECTS_FOLDER / PROJECT_FOLDERS_FILENAME
        links_marker = data_dir / PROJECTS_FOLDER / PROJECT_PACK_LINKS_FILENAME
        persist_inventory = bool(registry_payload) or not (folder_marker.exists() or links_marker.exists())
        if persist_inventory:
            _write_project_folders(data_dir, project_folders)
        if registry_payload:
            _hydrate_project_pack_links_from_registry(
                data_dir,
                project_folders,
                registry=registry_payload,
            )
        downloaded.extend(
            _download_project_ifc_metadata_files(
                project_inventory_client,
                projects.id,
                data_dir,
                warnings=warnings,
                file_cache=file_cache,
                skipped=skipped,
                prune=bool(registry_payload),
            )
        )
    else:
        missing.append(PROJECTS_FOLDER)

    if registry_payload:
        _prune_removed_project_ifc_metadata_cache(
            data_dir,
            {
                str(folder.get("projectId") or "")
                for folder in (project_folders if projects and projects.is_folder else [])
            },
        )

    _write_drive_file_cache(data_dir, file_cache)
    result = {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "skipped": skipped,
        "missing": missing,
        "warnings": warnings,
        "scope": "registry",
        "databaseIncluded": include_database is not False,
        "inventoryValidated": bool(registry_payload),
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _sync_serverless_registry_files_by_id(
    client: GoogleDriveClient,
    *,
    data_dir: Path,
    root_folder_id: str,
    include_database: bool | None = None,
) -> dict[str, Any] | None:
    """Restore the compact DB plus live project and IFC/XKT metadata on Vercel.

    The compact query database deliberately omits graph payloads and filesystem
    metadata.  Project folders and IFC/XKT metadata therefore remain authoritative
    in Drive and must be restored on every cold runtime.  Only ontology ZIP payloads
    are deferred until a graph is opened.
    """

    if not EPHEMERAL_STORAGE:
        return None
    query_database_file_id = str(env("MODULAR_ONTOLOGY_QUERY_DATABASE_FILE_ID", "") or "").strip()
    if not query_database_file_id:
        return None

    downloaded: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    root = _children_by_name(client, root_folder_id)
    with tempfile.TemporaryDirectory(
        prefix=".modular-ontology-direct-sync-",
        dir=data_dir.parent,
    ) as staging_dir:
        staging_data = Path(staging_dir)
        staged_database = staging_data / DATABASE_FOLDER / DATABASE_FILENAME
        staged_registry = staging_data / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
        if include_database is not False:
            client.download_gzip_file(query_database_file_id, staged_database)

        registry_file_id = str(env("MODULAR_ONTOLOGY_PACK_REGISTRY_FILE_ID", "") or "").strip()
        if registry_file_id:
            client.download_file(registry_file_id, staged_registry)
        else:
            warnings.append("MODULAR_ONTOLOGY_PACK_REGISTRY_FILE_ID is not set")
            database_folder = root.get(DATABASE_FOLDER)
            if database_folder and database_folder.is_folder:
                _download_named_files(
                    client,
                    database_folder.id,
                    staged_registry.parent,
                    {PACK_REGISTRY_FILENAME},
                )

        registry_payload = _load_registry_path(staged_registry)
        validate_pack_registry(registry_payload, require_drive_mappings=True)
        database_to_validate = (
            staged_database
            if include_database is not False
            else data_dir / DATABASE_FOLDER / DATABASE_FILENAME
        )
        if database_to_validate.exists():
            validate_database_against_pack_registry(database_to_validate, registry_payload)
        elif include_database is not False:
            raise RuntimeError("Direct query database download did not produce a database file.")

        projects = root.get(PROJECTS_FOLDER)
        project_inventory_client = (
            _capture_project_metadata_inventory(client, projects.id)
            if projects and projects.is_folder
            else client
        )
        project_folders = (
            _project_folder_records(project_inventory_client, projects.id, warnings=warnings)
            if projects and projects.is_folder
            else []
        )
        _validate_registry_project_folder_inventory(
            staging_data,
            project_folders,
            registry=registry_payload,
        )

        _activate_staged_database_registry(
            data_dir=data_dir,
            staged_registry=staged_registry,
            staged_database=staged_database if include_database is not False else None,
        )
        registry_target = data_dir / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
        downloaded.append(str(registry_target))
        if include_database is not False:
            database_target = data_dir / DATABASE_FOLDER / DATABASE_FILENAME
            downloaded.append(str(database_target))

    for env_name, target in (
        ("MODULAR_ONTOLOGY_USERS_FILE_ID", data_dir / ADMIN_FOLDER / "users.json"),
        ("MODULAR_ONTOLOGY_MCP_TOKENS_FILE_ID", data_dir / ADMIN_FOLDER / "mcp_tokens.json"),
    ):
        file_id = str(env(env_name, "") or "").strip()
        if not file_id:
            warnings.append(f"{env_name} is not set")
            continue
        client.download_file(file_id, target)
        downloaded.append(str(target))

    ifc_models = root.get(IFC_MODELS_FOLDER)
    if ifc_models and ifc_models.is_folder:
        downloaded.extend(
            _download_ifc_metadata_files(
                client,
                ifc_models.id,
                data_dir / IFC_MODELS_FOLDER,
                warnings=warnings,
            )
        )

    if projects and projects.is_folder:
        _write_project_folders(data_dir, project_folders)
        _hydrate_project_pack_links_from_registry(
            data_dir,
            project_folders,
            registry=registry_payload,
        )
        downloaded.extend(
            _download_project_ifc_metadata_files(
                project_inventory_client,
                projects.id,
                data_dir,
                warnings=warnings,
            )
        )
    else:
        missing.append(PROJECTS_FOLDER)
    _prune_removed_project_ifc_metadata_cache(
        data_dir,
        {str(folder.get("projectId") or "") for folder in project_folders},
    )

    return {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "skipped": [],
        "missing": missing,
        "warnings": warnings,
        "scope": "registry",
        "mode": "direct-file-ids",
        "databaseIncluded": include_database is not False,
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


class _FrozenDriveInventoryClient:
    """Delegate downloads while serving one immutable Drive folder snapshot."""

    def __init__(
        self,
        client: GoogleDriveClient,
        listings: dict[str, tuple[DriveItem, ...]],
    ) -> None:
        self._client = client
        self._listings = listings

    def list_children(self, folder_id: str) -> list[DriveItem]:
        if folder_id not in self._listings:
            raise RuntimeError(
                "Drive inventory snapshot is incomplete; refusing a live re-list after validation: "
                f"{folder_id}"
            )
        return list(self._listings[folder_id])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _capture_project_asset_inventory(
    client: GoogleDriveClient,
    projects_folder_id: str,
) -> _FrozenDriveInventoryClient:
    """Capture every listing consumed by project pack/IFC reconciliation once."""

    listings: dict[str, tuple[DriveItem, ...]] = {}

    def capture(folder_id: str) -> tuple[DriveItem, ...]:
        if folder_id not in listings:
            listings[folder_id] = tuple(client.list_children(folder_id))
        return listings[folder_id]

    for project in capture(projects_folder_id):
        if not project.is_folder:
            continue
        project_children = capture(project.id)
        children_by_name = {item.name: item for item in project_children}
        for folder_name in ("metadata", PROJECT_IFC_FOLDER):
            folder = children_by_name.get(folder_name)
            if folder and folder.is_folder:
                capture(folder.id)

        packs_folders = [
            item
            for item in project_children
            if item.is_folder and item.name == PROJECT_PACKS_FOLDER
        ]
        for packs_folder in packs_folders:
            pack_items = capture(packs_folder.id)
            for category in pack_items:
                if category.is_folder:
                    capture(category.id)

        try:
            project_id = _safe_drive_filename(project.name)
        except ValueError:
            project_id = ""
        if project_id != COMMON_PROJECT_ID:
            continue
        # _Common categories are one level above their ontology-packs folder.
        for category in project_children:
            if not category.is_folder or category.name == PROJECT_PACKS_FOLDER:
                continue
            category_children = capture(category.id)
            category_packs = next(
                (
                    item
                    for item in category_children
                    if item.is_folder and item.name == PROJECT_PACKS_FOLDER
                ),
                None,
            )
            if category_packs:
                capture(category_packs.id)

    return _FrozenDriveInventoryClient(client, listings)


def _capture_project_metadata_inventory(
    client: GoogleDriveClient,
    projects_folder_id: str,
) -> _FrozenDriveInventoryClient:
    """Freeze only project/IFC metadata needed by a serverless cold start.

    Ontology ZIP signatures are verified by the authoritative full-sync path and
    again when a lazy graph pack is opened. Recursively listing every pack and
    category on each Vercel cold start adds minutes without improving query-DB
    generation validation.
    """

    listings: dict[str, tuple[DriveItem, ...]] = {}

    def capture(folder_id: str) -> tuple[DriveItem, ...]:
        if folder_id not in listings:
            listings[folder_id] = tuple(client.list_children(folder_id))
        return listings[folder_id]

    for project in capture(projects_folder_id):
        if not project.is_folder:
            continue
        children = capture(project.id)
        children_by_name = {item.name: item for item in children}
        for folder_name in ("metadata", PROJECT_IFC_FOLDER):
            folder = children_by_name.get(folder_name)
            if folder and folder.is_folder:
                capture(folder.id)

    return _FrozenDriveInventoryClient(client, listings)


def _hydrate_project_pack_links_from_registry(
    data_dir: Path,
    project_folders: list[dict[str, str]],
    *,
    registry: dict[str, Any] | None = None,
) -> None:
    registry = registry or load_pack_registry(data_dir=data_dir)
    registry_projects = registry.get("projects")
    if not isinstance(registry_projects, list):
        return
    matches = _match_registry_projects_to_drive_folders(registry, project_folders)
    links: dict[str, list[str]] = {}
    common_pack_ids = registry.get("commonPackIds")
    if isinstance(common_pack_ids, list):
        clean_common_pack_ids = [
            str(pack_id).strip() for pack_id in common_pack_ids if str(pack_id).strip()
        ]
        if clean_common_pack_ids:
            links[COMMON_PROJECT_PACK_LINKS_KEY] = clean_common_pack_ids

    for folder in project_folders:
        project_id = str(folder.get("projectId") or "").strip()
        if not project_id:
            continue
        registry_project = matches[project_id]
        pack_ids = registry_project.get("packIds")
        links[project_id] = list(dict.fromkeys(
            [str(pack_id).strip() for pack_id in pack_ids if str(pack_id).strip()]
            if isinstance(pack_ids, list)
            else []
        ))
    _write_project_pack_links(data_dir, links)


def _validate_registry_project_folder_inventory(
    data_dir: Path,
    project_folders: list[dict[str, str]],
    *,
    registry: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Require an exact, bidirectional registry/Drive project inventory match."""

    registry = registry or load_pack_registry(data_dir=data_dir)
    registry_projects = registry.get("projects")
    if not isinstance(registry_projects, list):
        return {}
    return _match_registry_projects_to_drive_folders(registry, project_folders)


def _match_registry_projects_to_drive_folders(
    registry: dict[str, Any],
    project_folders: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    registry_projects = [
        project for project in registry.get("projects", []) if isinstance(project, dict)
    ]
    by_folder_id = {
        str(project.get("driveFolderId") or "").strip(): project
        for project in registry_projects
        if str(project.get("driveFolderId") or "").strip()
    }
    by_project_id = {
        str(project.get("id") or "").strip(): project
        for project in registry_projects
        if str(project.get("id") or "").strip()
    }
    matches: dict[str, dict[str, Any]] = {}
    matched_registry_ids: set[str] = set()
    for folder in project_folders:
        project_id = str(folder.get("projectId") or "").strip()
        folder_id = str(folder.get("folderId") or "").strip()
        registry_project = by_folder_id.get(folder_id) or by_project_id.get(project_id)
        if not isinstance(registry_project, dict):
            raise RuntimeError(
                "Drive project inventory contains an unexpected folder; refusing to write empty links "
                f"for {project_id or folder_id!r}."
            )
        registry_id = str(registry_project.get("id") or "").strip()
        if registry_id in matched_registry_ids:
            raise RuntimeError(f"Multiple Drive folders match registry project {registry_id!r}.")
        if project_id in matches:
            raise RuntimeError(f"Duplicate Drive project folder name: {project_id!r}.")
        matches[project_id] = registry_project
        matched_registry_ids.add(registry_id)

    missing_registry_ids = sorted(set(by_project_id) - matched_registry_ids)
    if missing_registry_ids:
        raise RuntimeError(
            "Drive project listing is incomplete; refusing to replace the registry inventory "
            f"({len(missing_registry_ids)} project(s) missing)."
        )
    return matches


def _validate_drive_project_pack_inventory(
    client: GoogleDriveClient,
    projects_folder_id: str,
    project_folders: list[dict[str, str]],
    registry: dict[str, Any],
) -> dict[str, str]:
    """Validate exact pack identity, grouping, and signatures from one snapshot."""

    matches = _match_registry_projects_to_drive_folders(registry, project_folders)
    packs_by_id = {
        str(pack.get("id") or "").strip(): pack
        for pack in registry.get("packs", [])
        if isinstance(pack, dict) and str(pack.get("id") or "").strip()
    }
    common_pack_ids = {
        str(pack_id).strip()
        for pack_id in registry.get("commonPackIds", [])
        if str(pack_id).strip()
    }
    problems: list[str] = []
    expected_by_file_id: dict[str, dict[str, Any]] = {}

    def expected_location(pack: dict[str, Any], project_id: str | None) -> str:
        return _registry_pack_folder_path(pack, project_id)

    def register_expected(pack_id: str, project_id: str | None) -> None:
        pack = packs_by_id.get(pack_id)
        drive = pack.get("drive") if isinstance(pack, dict) and isinstance(pack.get("drive"), dict) else {}
        file_id = str(drive.get("fileId") or "").strip()
        if not isinstance(pack, dict) or not file_id:
            problems.append(f"{pack_id}: registry mapping incomplete")
            return
        record = {
            "packId": pack_id,
            "fileId": file_id,
            "folderPath": expected_location(pack, project_id),
            "modifiedTime": str(drive.get("modifiedTime") or ""),
            "size": drive.get("sizeBytes"),
        }
        if file_id in expected_by_file_id:
            problems.append(f"{pack_id}: duplicate Drive fileId {file_id}")
            return
        expected_by_file_id[file_id] = record

    for pack_id in sorted(common_pack_ids):
        register_expected(pack_id, None)
    for registry_project in matches.values():
        registry_project_id = str(registry_project.get("id") or "").strip()
        for pack_id in {
            str(value).strip()
            for value in registry_project.get("packIds", [])
            if str(value).strip()
        } - common_pack_ids:
            register_expected(pack_id, registry_project_id)

    actual_records: list[dict[str, Any]] = []

    def add_zip_records(folder_id: str, folder_path: str) -> None:
        for item in client.list_children(folder_id):
            if not item.is_folder and item.name.lower().endswith(".zip"):
                actual_records.append(
                    {
                        "item": item,
                        "fileId": item.id,
                        "folderPath": folder_path,
                    }
                )

    def unique_named_folders(
        items: list[DriveItem],
        name: str,
        context: str,
    ) -> list[DriveItem]:
        matches_for_name = [item for item in items if item.is_folder and item.name == name]
        if len(matches_for_name) > 1:
            problems.append(f"{context}: duplicate {name} folders")
        return matches_for_name

    project_items = [item for item in client.list_children(projects_folder_id) if item.is_folder]
    project_items_by_id = {item.id: item for item in project_items}
    for folder in project_folders:
        actual_project_id = str(folder.get("projectId") or "").strip()
        folder_id = str(folder.get("folderId") or "").strip()
        registry_project = matches[actual_project_id]
        registry_project_id = str(registry_project.get("id") or "").strip()
        project_item = project_items_by_id.get(folder_id)
        children = client.list_children(project_item.id) if project_item else []
        packs_folders = unique_named_folders(children, PROJECT_PACKS_FOLDER, registry_project_id)
        if not packs_folders:
            continue
        packs_folder = packs_folders[0]
        direct_path = f"{PROJECTS_FOLDER}/{registry_project_id}/{PROJECT_PACKS_FOLDER}"
        pack_items = client.list_children(packs_folder.id)
        add_zip_records(packs_folder.id, direct_path)
        category_names: set[str] = set()
        for category in [item for item in pack_items if item.is_folder]:
            category_name = _safe_drive_filename(category.name)
            if category_name in category_names:
                problems.append(f"{registry_project_id}: duplicate category {category_name}")
            category_names.add(category_name)
            add_zip_records(category.id, f"{direct_path}/{category_name}")

    common_items = [
        item
        for item in project_items
        if _safe_drive_filename(item.name) == COMMON_PROJECT_ID
    ]
    if len(common_items) > 1:
        problems.append(f"{COMMON_PROJECT_ID}: duplicate project folders")
    if common_items:
        common = common_items[0]
        common_children = client.list_children(common.id)
        direct_folders = unique_named_folders(common_children, PROJECT_PACKS_FOLDER, COMMON_PROJECT_ID)
        if direct_folders:
            add_zip_records(
                direct_folders[0].id,
                f"{PROJECTS_FOLDER}/{COMMON_PROJECT_ID}/{PROJECT_PACKS_FOLDER}",
            )
        category_names: set[str] = set()
        for category in [
            item
            for item in common_children
            if item.is_folder and item.name != PROJECT_PACKS_FOLDER
        ]:
            category_children = client.list_children(category.id)
            category_packs = unique_named_folders(
                category_children,
                PROJECT_PACKS_FOLDER,
                f"{COMMON_PROJECT_ID}/{category.name}",
            )
            if not category_packs:
                continue
            category_name = _safe_drive_filename(category.name)
            if category_name in category_names:
                problems.append(f"{COMMON_PROJECT_ID}: duplicate category {category_name}")
            category_names.add(category_name)
            add_zip_records(
                category_packs[0].id,
                f"{PROJECTS_FOLDER}/{COMMON_PROJECT_ID}/{category_name}/{PROJECT_PACKS_FOLDER}",
            )

    actual_by_file_id: dict[str, dict[str, Any]] = {}
    for record in actual_records:
        file_id = str(record["fileId"])
        if file_id in actual_by_file_id:
            problems.append(f"duplicate pack fileId across folders: {file_id}")
            continue
        actual_by_file_id[file_id] = record

    if set(actual_by_file_id) != set(expected_by_file_id):
        problems.append(
            f"file ids expected={len(expected_by_file_id)} actual={len(actual_by_file_id)}"
        )
    for file_id in sorted(set(actual_by_file_id) & set(expected_by_file_id)):
        actual = actual_by_file_id[file_id]
        expected = expected_by_file_id[file_id]
        item = actual["item"]
        if actual["folderPath"] != expected["folderPath"]:
            problems.append(
                f"{expected['packId']}: folder expected={expected['folderPath']} "
                f"actual={actual['folderPath']}"
            )
        if expected["modifiedTime"] and item.modified_time != expected["modifiedTime"]:
            problems.append(f"{expected['packId']}: modifiedTime mismatch")
        if expected["size"] not in (None, ""):
            try:
                expected_size = int(expected["size"])
            except (TypeError, ValueError):
                problems.append(f"{expected['packId']}: invalid registry size")
            else:
                if item.size != expected_size:
                    problems.append(f"{expected['packId']}: size mismatch")

    if problems:
        raise RuntimeError(
            "Drive project pack inventory mismatch; refusing destructive cache reconciliation: "
            + "; ".join(problems[:12])
        )
    return {
        file_id: str(record["packId"])
        for file_id, record in expected_by_file_id.items()
    }


def _download_project_ifc_metadata_files(
    client: GoogleDriveClient,
    projects_folder_id: str,
    data_dir: Path,
    *,
    warnings: list[str] | None = None,
    file_cache: dict[str, dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
    prune: bool = True,
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

        active_metadata_names: set[str] = set(existing_metadata_names)
        ifc_folder = project_children.get(PROJECT_IFC_FOLDER)
        if ifc_folder and ifc_folder.is_folder:
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
        if prune:
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


def _select_database_drive_item(
    items: list[DriveItem],
    *,
    prefer_compact: bool,
) -> DriveItem | None:
    if prefer_compact:
        source = next((item for item in items if item.name == QUERY_DATABASE_FILENAME), None)
        if not source and any(
            item.name in {DATABASE_FILENAME, LEGACY_DATABASE_FILENAME} for item in items
        ):
            raise RuntimeError(
                f"{QUERY_DATABASE_FILENAME} is missing from {DATABASE_FOLDER}; "
                "run a local database write-back before deploying the serverless MCP."
            )
        return source
    return next(
        (
            item
            for preferred_name in (DATABASE_FILENAME, LEGACY_DATABASE_FILENAME)
            for item in items
            if item.name == preferred_name
        ),
        None,
    )


def _stage_drive_item(
    client: GoogleDriveClient,
    item: DriveItem,
    live_target: Path,
    staged_target: Path,
    *,
    data_dir: Path,
    file_cache: dict[str, dict[str, Any]],
    skipped: list[str] | None = None,
    decompress_gzip: bool = False,
) -> bool:
    """Materialize a Drive item in staging without mutating its live target."""

    staged_target.parent.mkdir(parents=True, exist_ok=True)
    if _drive_file_is_unchanged(
        item,
        live_target,
        data_dir=data_dir,
        file_cache=file_cache,
    ):
        shutil.copy2(live_target, staged_target)
        if skipped is not None:
            skipped.append(str(live_target))
        return False
    if decompress_gzip:
        client.download_gzip_file(item.id, staged_target)
    else:
        client.download_file(item.id, staged_target)
    return True


def _seed_staged_project_assets(data_dir: Path, staged_data_dir: Path) -> None:
    """Copy only Drive-managed project state so unchanged files remain skippable."""

    live_indexed = data_dir / ONTOLOGY_PACKS_FOLDER / "indexed"
    staged_indexed = staged_data_dir / ONTOLOGY_PACKS_FOLDER / "indexed"
    if live_indexed.exists():
        for source in live_indexed.glob("*__*.zip"):
            target = staged_indexed / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    live_ifc = data_dir / IFC_MODELS_FOLDER
    staged_ifc = staged_data_dir / IFC_MODELS_FOLDER
    if live_ifc.exists():
        for source in live_ifc.rglob("*.metadata.json"):
            if not _is_project_metadata_file(source):
                continue
            target = staged_ifc / source.relative_to(live_ifc)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    for marker_name in (PROJECT_FOLDERS_FILENAME, PROJECT_PACK_LINKS_FILENAME):
        source = data_dir / PROJECTS_FOLDER / marker_name
        if not source.exists():
            continue
        target = staged_data_dir / PROJECTS_FOLDER / marker_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _live_paths_from_staging(
    paths: list[str],
    *,
    staged_data_dir: Path,
    data_dir: Path,
) -> list[str]:
    translated: list[str] = []
    for value in paths:
        path = Path(value)
        try:
            relative = path.resolve().relative_to(staged_data_dir.resolve())
        except ValueError:
            translated.append(str(path))
        else:
            translated.append(str(data_dir / relative))
    return translated


def _activate_staged_database_registry(
    *,
    data_dir: Path,
    staged_registry: Path,
    staged_database: Path | None,
    staged_project_assets: Path | None = None,
    activate_registry: bool = True,
) -> None:
    """Activate one validated generation and roll back pair plus project assets."""

    registry_target = data_dir / DATABASE_FOLDER / PACK_REGISTRY_FILENAME
    database_target = data_dir / DATABASE_FOLDER / DATABASE_FILENAME
    files: list[tuple[Path, Path]] = []
    impacted_targets: set[Path] = set()
    if staged_project_assets is not None:
        staged_indexed = staged_project_assets / ONTOLOGY_PACKS_FOLDER / "indexed"
        live_indexed = data_dir / ONTOLOGY_PACKS_FOLDER / "indexed"
        for target in live_indexed.glob("*__*.zip") if live_indexed.exists() else []:
            impacted_targets.add(target)
        if staged_indexed.exists():
            for staged in staged_indexed.glob("*__*.zip"):
                target = live_indexed / staged.name
                files.append((staged, target))
                impacted_targets.add(target)

        staged_ifc = staged_project_assets / IFC_MODELS_FOLDER
        live_ifc = data_dir / IFC_MODELS_FOLDER
        if live_ifc.exists():
            for target in live_ifc.rglob("*.metadata.json"):
                if _is_project_metadata_file(target):
                    impacted_targets.add(target)
        if staged_ifc.exists():
            for staged in staged_ifc.rglob("*.metadata.json"):
                target = live_ifc / staged.relative_to(staged_ifc)
                files.append((staged, target))
                impacted_targets.add(target)

        for marker_name in (PROJECT_FOLDERS_FILENAME, PROJECT_PACK_LINKS_FILENAME):
            staged = staged_project_assets / PROJECTS_FOLDER / marker_name
            target = data_dir / PROJECTS_FOLDER / marker_name
            impacted_targets.add(target)
            if staged.exists():
                files.append((staged, target))
    if staged_database is not None:
        files.append((staged_database, database_target))
        impacted_targets.add(database_target)
    if activate_registry:
        files.append((staged_registry, registry_target))
        impacted_targets.add(registry_target)
    impacted_targets.update(target for _staged, target in files)
    backup_dir = staged_registry.parents[1] / ".activation-backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backups: dict[Path, Path] = {}
    activated: list[Path] = []
    with _DB_SYNC_LOCK:
        _remove_sqlite_sidecars(database_target)
        try:
            for target in sorted(impacted_targets, key=lambda path: path.as_posix()):
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    try:
                        relative = target.resolve().relative_to(data_dir.resolve())
                    except ValueError:
                        relative = Path(target.name)
                    backup = backup_dir / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.unlink(missing_ok=True)
                    os.replace(target, backup)
                    backups[target] = backup
            for staged, target in files:
                os.replace(staged, target)
                activated.append(target)
            _remove_sqlite_sidecars(database_target)
        except Exception:
            for target in reversed(activated):
                target.unlink(missing_ok=True)
            for target, backup in backups.items():
                if backup.exists():
                    os.replace(backup, target)
            _remove_sqlite_sidecars(database_target)
            raise
        else:
            for backup in backups.values():
                backup.unlink(missing_ok=True)
            if staged_project_assets is not None:
                ifc_root = data_dir / IFC_MODELS_FOLDER
                if ifc_root.exists():
                    for project_dir in ifc_root.iterdir():
                        if project_dir.is_dir() and not any(
                            path.is_file() for path in project_dir.rglob("*")
                        ):
                            shutil.rmtree(project_dir, ignore_errors=True)


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
    prune: bool = True,
    persist_inventory: bool = True,
    expected_pack_ids_by_file_id: dict[str, str] | None = None,
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
                prune=prune,
                expected_pack_ids_by_file_id=expected_pack_ids_by_file_id,
            )
            downloaded.extend(common_downloads)
            project_pack_links[COMMON_PROJECT_PACK_LINKS_KEY] = [
                (
                    _required_pack_id_from_zip(Path(path))
                    if expected_pack_ids_by_file_id is not None
                    else _pack_id_from_zip(Path(path))
                )
                for path in common_active_paths
            ]
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
            prune=prune,
            expected_pack_ids_by_file_id=expected_pack_ids_by_file_id,
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
    if persist_inventory:
        _write_project_folders(data_dir, project_folders)
    active_project_ids = {item["projectId"] for item in project_folders}
    if common_folder_found:
        active_project_ids.add(COMMON_PROJECT_ID)
    if prune:
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
    prune: bool = True,
    expected_pack_ids_by_file_id: dict[str, str] | None = None,
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
        if prune:
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
        prune=prune,
        expected_pack_ids_by_file_id=expected_pack_ids_by_file_id,
    )
    downloaded.extend(pack_downloads)
    return downloaded, [
        (
            _required_pack_id_from_zip(Path(path))
            if expected_pack_ids_by_file_id is not None
            else _pack_id_from_zip(Path(path))
        )
        for path in pack_active_paths
    ], True


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
    prune: bool = True,
    expected_pack_ids_by_file_id: dict[str, str] | None = None,
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
            prune=False,
            expected_pack_ids_by_file_id=expected_pack_ids_by_file_id,
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
            prune=False,
            expected_pack_ids_by_file_id=expected_pack_ids_by_file_id,
        )
        downloaded.extend(category_downloads)
        active_paths.extend(category_active_paths)
    if prune:
        _prune_project_pack_cache(
            target_dir,
            COMMON_PROJECT_ID,
            {Path(path).name for path in active_paths},
        )
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
    prune: bool = True,
    expected_pack_ids_by_file_id: dict[str, str] | None = None,
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
        prune=False,
        expected_pack_ids_by_file_id=expected_pack_ids_by_file_id,
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
            prune=False,
            expected_pack_ids_by_file_id=expected_pack_ids_by_file_id,
        )
        downloaded.extend(category_downloads)
        active_paths.extend(category_active_paths)
    if prune:
        _prune_project_pack_cache(
            target_dir,
            project_id,
            {Path(path).name for path in active_paths},
        )
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


def _prune_removed_project_ifc_metadata_cache(
    data_dir: Path,
    active_project_ids: set[str],
) -> None:
    """Remove only Drive-generated project metadata after inventory validation."""

    active_project_ids = {project_id for project_id in active_project_ids if project_id}
    ifc_root = data_dir / IFC_MODELS_FOLDER
    if not ifc_root.exists():
        return
    for project_dir in ifc_root.iterdir():
        if not project_dir.is_dir() or project_dir.name in active_project_ids:
            continue
        metadata_dir = project_dir / "metadata"
        metadata_files = (
            list(metadata_dir.glob("*.metadata.json"))
            if metadata_dir.exists()
            else list(project_dir.glob("*.metadata.json"))
        )
        if metadata_files and all(_is_project_metadata_file(path) for path in metadata_files):
            shutil.rmtree(project_dir, ignore_errors=True)


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
    prune: bool = True,
    expected_pack_ids_by_file_id: dict[str, str] | None = None,
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
        expected_pack_id = (
            expected_pack_ids_by_file_id.get(item.id)
            if expected_pack_ids_by_file_id is not None
            else None
        )
        if expected_pack_ids_by_file_id is not None and not expected_pack_id:
            raise RuntimeError(
                f"Validated Drive inventory has no pack identity for file {item.id}."
            )
        # A matching Drive signature is not enough: local cache bytes may be
        # truncated or may carry another pack's manifest.  Force a redownload
        # before allowing the file to participate in project links.
        cached_pack_id: str | None = None
        if expected_pack_id and target.exists():
            try:
                cached_pack_id = _required_pack_id_from_zip(target)
            except (OSError, ValueError, zipfile.BadZipFile):
                cached_pack_id = None
            if cached_pack_id != expected_pack_id:
                target.unlink(missing_ok=True)
                cached_pack_id = None
                if data_dir is not None and file_cache is not None:
                    file_cache.pop(_drive_cache_key(data_dir, target), None)
        file_changed = _download_file_if_changed(
            client,
            item,
            target,
            data_dir=data_dir,
            file_cache=file_cache,
            skipped=skipped,
        )
        if file_changed:
            downloaded.append(str(target))
        if target.exists():
            if expected_pack_id and (file_changed or cached_pack_id is None):
                try:
                    actual_pack_id = _required_pack_id_from_zip(target)
                except (OSError, ValueError, zipfile.BadZipFile) as exc:
                    target.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"Downloaded project pack failed ZIP/manifest validation: {expected_pack_id}"
                    ) from exc
                if actual_pack_id != expected_pack_id:
                    target.unlink(missing_ok=True)
                    raise RuntimeError(
                        "Downloaded project pack id mismatch: "
                        f"expected {expected_pack_id}, got {actual_pack_id}"
                    )
            active_paths.append(str(target))
    if prune:
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


def _required_pack_id_from_zip(path: Path) -> str:
    """Read the explicit identity required for authoritative/lazy pack use."""

    try:
        with zipfile.ZipFile(path) as zf:
            corrupt_member = zf.testzip()
            if corrupt_member is not None:
                raise ValueError(f"CRC check failed for {corrupt_member}")
            if "manifest.json" not in zf.namelist():
                raise ValueError("manifest.json is missing")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8-sig"))
    except (OSError, KeyError, UnicodeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid ontology pack ZIP: {path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"Ontology pack manifest must be an object: {path}")
    pack_id = str(manifest.get("pack_id") or "").strip()
    if not pack_id:
        raise ValueError(f"Ontology pack manifest must contain an explicit pack_id: {path}")
    return pack_id


def _lazy_pack_matches(path: Path, expected_pack_id: str) -> bool:
    try:
        return _required_pack_id_from_zip(path) == expected_pack_id
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


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
