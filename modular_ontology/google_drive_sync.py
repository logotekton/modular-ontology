from __future__ import annotations

import base64
import json
import mimetypes
import os
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR, DB_PATH, MCP_TOKENS_FILE, env

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_TTL_SECONDS = 300
DATABASE_FILENAME = "modular_ontology.sqlite3"
LEGACY_DATABASE_FILENAME = "mod" + "dular_" + "graph.sqlite3"


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
    def __init__(self, api_key: str | None = None, access_token: str | None = None) -> None:
        self.api_key = api_key
        self._access_token = access_token

    @classmethod
    def from_env(cls) -> "GoogleDriveClient":
        access_token = env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_ACCESS_TOKEN") or os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN")
        if access_token:
            return cls(access_token=str(access_token))

        if env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_USE_GCLOUD_AUTH") == "1":
            return cls(access_token=_gcloud_access_token())

        service_account = _load_service_account_info()
        if service_account:
            return cls(access_token=_service_account_access_token(service_account))

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
            payload = self._request_json("files", params)
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
        with urllib.request.urlopen(request, timeout=60) as response:
            with temp.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
        os.replace(temp, target)

    def update_file(self, file_id: str, source: Path, mime_type: str | None = None) -> dict[str, Any]:
        params = {"uploadType": "media", "supportsAllDrives": "true"}
        url = self._upload_url(f"files/{file_id}", params)
        data = source.read_bytes()
        request = urllib.request.Request(
            url,
            data=data,
            headers={**self._headers(), "Content-Type": mime_type or _guess_mime_type(source)},
            method="PATCH",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def create_file(self, parent_id: str, source: Path, name: str | None = None, mime_type: str | None = None) -> dict[str, Any]:
        boundary = f"modular-{uuid.uuid4().hex}"
        file_name = name or source.name
        content_type = mime_type or _guess_mime_type(source)
        metadata = {"name": file_name, "parents": [parent_id]}
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
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))

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
        with urllib.request.urlopen(request, timeout=30) as response:
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
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _url(self, path: str, params: dict[str, str]) -> str:
        all_params = dict(params)
        if self.api_key:
            all_params["key"] = self.api_key
        return f"{DRIVE_API}/{path}?{urllib.parse.urlencode(all_params)}"

    def _upload_url(self, path: str, params: dict[str, str]) -> str:
        return f"{DRIVE_UPLOAD_API}/{path}?{urllib.parse.urlencode(params)}"

    def _headers(self) -> dict[str, str]:
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
    missing: list[str] = []

    admin = root.get("00_Admin")
    if admin and admin.is_folder:
        downloaded.extend(
            _download_named_files(
                client,
                admin.id,
                data_dir / "00_Admin",
                {"users.json", "mcp_remote.json", "mcp_tokens.json"},
            )
        )
    else:
        missing.append("00_Admin")

    database = root.get("01_Database")
    if database and database.is_folder:
        downloaded.extend(_download_database_file(client, database.id, data_dir / "01_Database"))
    else:
        missing.append("01_Database")

    packs = root.get("02_Ontology_Packs")
    if packs and packs.is_folder:
        pack_folders = _children_by_name(client, packs.id)
        indexed = pack_folders.get("indexed")
        if indexed and indexed.is_folder:
            downloaded.extend(_download_zip_files(client, indexed.id, data_dir / "02_Ontology_Packs" / "indexed"))
        else:
            missing.append("02_Ontology_Packs/indexed")
    else:
        missing.append("02_Ontology_Packs")

    result = {"status": "synced", "synced_at": time.time(), "downloaded": downloaded, "missing": missing}
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
    admin = root.get("00_Admin")
    if not admin or not admin.is_folder:
        return {"status": "synced", "synced_at": time.time(), "downloaded": [], "missing": ["00_Admin"], "scope": "users"}

    downloaded = _download_named_files(client, admin.id, data_dir / "00_Admin", {"users.json"})
    return {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "missing": [] if downloaded else ["00_Admin/users.json"],
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
    admin = root.get("00_Admin")
    if not admin or not admin.is_folder:
        return {
            "status": "synced",
            "synced_at": time.time(),
            "downloaded": [],
            "missing": ["00_Admin"],
            "scope": "mcp_tokens",
        }

    downloaded = _download_named_files(client, admin.id, data_dir / "00_Admin", {"mcp_tokens.json"})
    return {
        "status": "synced",
        "synced_at": time.time(),
        "downloaded": downloaded,
        "missing": [] if downloaded else ["00_Admin/mcp_tokens.json"],
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
        data_dir / "00_Admin" / "users.json",
        ["00_Admin"],
        client=client,
        name="users.json",
        mime_type="application/json; charset=utf-8",
    )


def write_back_mcp_tokens_file(*, data_dir: Path = DATA_DIR, client: GoogleDriveClient | None = None) -> dict[str, Any]:
    return write_back_google_drive_file(
        Path(env("MODULAR_ONTOLOGY_MCP_TOKENS_FILE", MCP_TOKENS_FILE)),
        ["00_Admin"],
        client=client,
        name="mcp_tokens.json",
        mime_type="application/json; charset=utf-8",
    )


def write_back_database_file(*, client: GoogleDriveClient | None = None) -> dict[str, Any]:
    _checkpoint_sqlite(DB_PATH)
    return write_back_google_drive_file(
        DB_PATH,
        ["01_Database"],
        client=client,
        name=DATABASE_FILENAME,
        mime_type="application/vnd.sqlite3",
    )


def write_back_pack_file(pack_path: Path, *, client: GoogleDriveClient | None = None) -> dict[str, Any]:
    return write_back_google_drive_file(
        pack_path,
        ["02_Ontology_Packs", "indexed"],
        client=client,
        name=pack_path.name,
        mime_type="application/zip",
    )


def write_back_ifc_file(
    ifc_path: Path,
    project_id: str,
    *,
    client: GoogleDriveClient | None = None,
) -> dict[str, Any]:
    return write_back_google_drive_file(
        ifc_path,
        ["03_IFC_Models", project_id, "files"],
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
    return write_back_google_drive_file(
        metadata_path,
        ["03_IFC_Models", project_id, "metadata"],
        client=client,
        name=metadata_path.name,
        mime_type="application/json; charset=utf-8",
        create_folders=True,
    )


def _children_by_name(client: GoogleDriveClient, folder_id: str) -> dict[str, DriveItem]:
    children: dict[str, DriveItem] = {}
    duplicates: set[str] = set()
    for item in client.list_children(folder_id):
        if item.name in children:
            duplicates.add(item.name)
        children[item.name] = item
    if duplicates:
        raise RuntimeError(f"Duplicate Google Drive item names in folder {folder_id}: {', '.join(sorted(duplicates))}")
    return children


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


def _download_named_files(client: GoogleDriveClient, folder_id: str, target_dir: Path, names: set[str]) -> list[str]:
    downloaded: list[str] = []
    for item in client.list_children(folder_id):
        if item.is_folder or item.name not in names:
            continue
        target = target_dir / _safe_drive_filename(item.name)
        client.download_file(item.id, target)
        downloaded.append(str(target))
    return downloaded


def _download_database_file(client: GoogleDriveClient, folder_id: str, target_dir: Path) -> list[str]:
    children = [item for item in client.list_children(folder_id) if not item.is_folder]
    source = next((item for item in children if item.name == DATABASE_FILENAME), None)
    if not source:
        source = next((item for item in children if item.name == LEGACY_DATABASE_FILENAME), None)
    if not source:
        return []
    target = target_dir / DATABASE_FILENAME
    client.download_file(source.id, target)
    return [str(target)]


def _download_zip_files(client: GoogleDriveClient, folder_id: str, target_dir: Path) -> list[str]:
    downloaded: list[str] = []
    for item in client.list_children(folder_id):
        if item.is_folder or not item.name.lower().endswith(".zip"):
            continue
        target = target_dir / _safe_drive_filename(item.name)
        client.download_file(item.id, target)
        downloaded.append(str(target))
    return downloaded


def _safe_drive_filename(name: str) -> str:
    safe_name = Path(name.replace("\\", "/")).name
    if not safe_name or safe_name in {".", ".."} or safe_name != name:
        raise RuntimeError(f"Unsafe Google Drive filename: {name!r}")
    return safe_name


def _guess_mime_type(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    return guessed or "application/octet-stream"


def _checkpoint_sqlite(path: Path) -> None:
    if not path.exists() or path.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
        return
    try:
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL);")
        finally:
            conn.close()
    except sqlite3.Error:
        return


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
    try:
        token = subprocess.check_output(
            [command, "auth", "print-access-token"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
        ).strip()
    except Exception as exc:
        raise RuntimeError(f"Failed to get gcloud access token: {exc}") from exc
    if not token:
        raise RuntimeError("gcloud did not return an access token.")
    return token


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
