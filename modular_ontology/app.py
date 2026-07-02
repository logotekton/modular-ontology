from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import (
    add_company,
    approve_user,
    authenticate,
    clear_session,
    delete_company,
    delete_user,
    extract_bearer_token,
    get_company_project_access,
    get_user_by_token,
    invalidate_users_cache,
    is_internal_user,
    list_companies,
    list_users,
    public_user,
    register_user,
    rename_company,
    set_company_project_access,
    set_user_company,
    set_user_role,
)
from .config import DATA_DIR, IFC_MODELS_FOLDER, MCP_REMOTE_FILE, MCP_TOKENS_FILE, PROJECTS_FOLDER, ROOT, env
from .google_drive_sync import (
    COMMON_PROJECT_ID,
    COMMON_PROJECT_PACK_LINKS_KEY,
    PROJECT_FOLDERS_FILENAME,
    PROJECT_PACK_LINKS_FILENAME,
    ensure_project_drive_folders,
    google_drive_sync_enabled,
    google_drive_sync_status,
    restore_ifc_files_from_drive,
    sync_google_drive_registry_files,
    sync_google_drive_storage,
    sync_google_drive_mcp_tokens_file,
    sync_google_drive_users_file,
    write_back_database_file,
    write_back_ifc_file,
    write_back_ifc_metadata_file,
    write_back_mcp_tokens_file,
    write_back_pack_file,
    write_back_users_file,
)
from .mcp_server import TOOL_NAMES, configure_server as configure_mcp_server, mcp as remote_mcp
from .mcp_tokens import build_user_mcp_urls, ensure_mcp_token_for_user, regenerate_mcp_token_for_user
from .pack_index import PackFile, build_graph, build_multi_pack_graph, list_packs, list_projects, save_uploaded_pack, unique_pack_files
from .project_store import (
    attach_pack_to_project,
    create_project,
    delete_project as delete_stored_project,
    set_project_packs,
    suggest_project_name,
    sync_projects_from_drive_folders,
    update_project,
)
from .qa import answer_pack_question, validate_openai_api_key
from .store import connect, index_all_packs, index_pack, index_stats, init_db


DIST_DIR = ROOT / "dist"
IFC_UPLOAD_DIR = DATA_DIR / IFC_MODELS_FOLDER
PUBLIC_MCP_DOMAIN = str(env("MODULAR_ONTOLOGY_PUBLIC_MCP_DOMAIN", "modular-ontology.xyz"))
PUBLIC_MCP_BASE_URL = f"https://{PUBLIC_MCP_DOMAIN}"
PUBLIC_MCP_URL = f"{PUBLIC_MCP_BASE_URL}/mcp"
VERCEL_MCP_HOSTS = (
    f"{PUBLIC_MCP_DOMAIN},"
    "modular-ontology.vercel.app,"
    "modular-ontology-ythongs-projects.vercel.app,"
    "modular-ontology-ghddudxor12-8502-ythongs-projects.vercel.app"
)

configure_mcp_server(
    host="127.0.0.1",
    port=8011,
    path="/{mcp_token}",
    allowed_hosts=[
        host
        for host in (
            env("MODULAR_ONTOLOGY_MCP_ALLOWED_HOSTS")
            or f"{VERCEL_MCP_HOSTS},127.0.0.1:*,localhost:*,[::1]:*,testserver"
        ).split(",")
        if host.strip()
    ],
    allowed_origins=[
        origin
        for origin in (
            env("MODULAR_ONTOLOGY_MCP_ALLOWED_ORIGINS")
            or "https://chatgpt.com,https://chat.openai.com,http://127.0.0.1:*,http://localhost:*"
        ).split(",")
        if origin.strip()
    ],
)
remote_mcp_app = remote_mcp.streamable_http_app()

_GOOGLE_DRIVE_SYNC_LOCK = threading.Lock()
_GOOGLE_DRIVE_SCOPE_SYNC_LOCK = threading.Lock()
_GOOGLE_DRIVE_SCOPE_SYNC_CACHE: dict[str, dict[str, Any]] = {}


def _sync_ttl_seconds(scope: str, default: int = 60) -> int:
    raw = env(
        f"MODULAR_ONTOLOGY_GOOGLE_DRIVE_{scope.upper()}_SYNC_TTL_SECONDS",
        env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_SCOPED_SYNC_TTL_SECONDS", str(default)),
    )
    try:
        return max(0, int(str(raw)))
    except (TypeError, ValueError):
        return default


def _cached_scope_sync(scope: str, ttl: int) -> dict[str, Any] | None:
    if ttl <= 0:
        return None
    cached = _GOOGLE_DRIVE_SCOPE_SYNC_CACHE.get(scope)
    if not cached:
        return None
    synced_at = float(cached.get("synced_at", 0) or 0)
    if synced_at and time.time() - synced_at < ttl:
        result = dict(cached.get("result", {}))
        return {"enabled": True, **result, "status": "cached"}
    return None


def _remember_scope_sync(scope: str, result: dict[str, Any]) -> None:
    _GOOGLE_DRIVE_SCOPE_SYNC_CACHE[scope] = {
        "synced_at": time.time(),
        "result": dict(result),
    }


def _file_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def run_google_drive_sync(force: bool = False) -> dict[str, Any]:
    try:
        with _GOOGLE_DRIVE_SYNC_LOCK:
            result = sync_google_drive_storage(force=force)
        if result.get("status") == "synced":
            invalidate_users_cache()
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def run_google_drive_registry_sync(force: bool = False) -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    try:
        with _GOOGLE_DRIVE_SYNC_LOCK:
            result = sync_google_drive_registry_files(force=force)
        if result.get("status") == "synced":
            invalidate_users_cache()
        return {"enabled": True, **result}
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc)}


def run_google_drive_users_sync(force: bool = False) -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    try:
        ttl = _sync_ttl_seconds("users")
        with _GOOGLE_DRIVE_SCOPE_SYNC_LOCK:
            if not force:
                cached = _cached_scope_sync("users", ttl)
                if cached:
                    return cached
            result = sync_google_drive_users_file()
            _remember_scope_sync("users", result)
        if result.get("status") == "synced":
            invalidate_users_cache()
        return {"enabled": True, **result}
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc)}


def run_google_drive_mcp_tokens_sync(force: bool = False) -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    try:
        ttl = _sync_ttl_seconds("mcp_tokens")
        with _GOOGLE_DRIVE_SCOPE_SYNC_LOCK:
            if not force:
                cached = _cached_scope_sync("mcp_tokens", ttl)
                if cached:
                    return cached
            result = sync_google_drive_mcp_tokens_file()
            _remember_scope_sync("mcp_tokens", result)
        return {"enabled": True, **result}
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc)}


def require_google_drive_sync(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("enabled") and result.get("status") == "error":
        raise HTTPException(status_code=502, detail=f"Google Drive sync failed: {result.get('error')}")
    return result


def _env_flag(name: str, default: bool = False) -> bool:
    raw = env(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def google_drive_sync_on_startup() -> bool:
    default = not bool(os.environ.get("VERCEL"))
    return google_drive_sync_enabled() and _env_flag("MODULAR_ONTOLOGY_SYNC_ON_STARTUP", default)


def ensure_runtime_storage() -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    status = google_drive_sync_status()
    if status.get("status") in {"synced", "cached"}:
        ttl = int(str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_SYNC_TTL_SECONDS", "300")))
        try:
            synced_at = float(status.get("synced_at", 0) or 0)
        except (TypeError, ValueError):
            synced_at = 0
        if ttl > 0 and synced_at and time.time() - synced_at < ttl:
            return {"enabled": True, **status}
    result = run_google_drive_sync()
    if result.get("status") == "synced":
        try:
            stats = index_stats()
            if stats.get("packs", 0) == 0 and list_packs():
                reindex_result = index_all_packs()
                result["reindexed"] = reindex_result.get("stats", {})
            drive_projects = _apply_drive_project_folders()
            if any(drive_projects.get(key) for key in ("created", "updated", "renamed", "deleted", "conflicts")):
                result["driveProjects"] = drive_projects
            links = _apply_drive_project_pack_links()
            if links:
                result["projectPackLinks"] = links
        except Exception as exc:
            result["reindexError"] = str(exc)
    return {"enabled": True, **result}


def ensure_runtime_registry() -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    storage_status = google_drive_sync_status()
    stats = index_stats()
    if stats.get("packs", 0) > 0 and storage_status.get("status") in {"synced", "cached"}:
        ttl = int(str(env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_REGISTRY_SYNC_TTL_SECONDS", str(1800))))
        try:
            synced_at = float(storage_status.get("synced_at", 0) or 0)
        except (TypeError, ValueError):
            synced_at = 0
        if ttl > 0 and synced_at and time.time() - synced_at < ttl:
            return {"enabled": True, **storage_status}
    result = run_google_drive_registry_sync()
    if result.get("status") in {"synced", "cached"}:
        try:
            drive_projects = _apply_drive_project_folders()
            if any(drive_projects.get(key) for key in ("created", "updated", "renamed", "deleted", "conflicts")):
                result["driveProjects"] = drive_projects
            links = _apply_drive_project_pack_links()
            if links:
                result["projectPackLinks"] = links
        except Exception as exc:
            result["registryApplyError"] = str(exc)
    return result


def storage_runtime_status(status: dict[str, Any] | None = None) -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    status = status or google_drive_sync_status()
    return {
        "enabled": True,
        "status": status.get("status", "unknown"),
        "downloadedCount": len(status.get("downloaded", [])) if isinstance(status.get("downloaded"), list) else 0,
        "missing": status.get("missing", []),
        "error": status.get("error"),
    }


def run_google_drive_write_back(
    kind: Literal["users", "database", "pack", "mcp_tokens"],
    path: Path | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    try:
        if kind == "users":
            result = write_back_users_file()
        elif kind == "database":
            result = write_back_database_file()
        elif kind == "mcp_tokens":
            result = write_back_mcp_tokens_file()
        elif kind == "pack" and path:
            result = write_back_pack_file(path, project_id=project_id)
        else:
            result = {"status": "skipped", "reason": f"Unsupported write-back kind: {kind}"}
        return {"enabled": True, **result}
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc)}


def require_google_drive_write_back(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("enabled") and result.get("status") == "error":
        raise HTTPException(status_code=502, detail=f"Google Drive write-back failed: {result.get('error')}")
    return result


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if google_drive_sync_on_startup():
        run_google_drive_sync()
    async with remote_mcp.session_manager.run():
        yield


app = FastAPI(
    title="Modular Ontology API",
    description="Project ontology pack ingestion, graph exploration, and MCP-ready data access API.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if (DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="assets")

app.mount("/mcp", remote_mcp_app, name="remote-mcp")


class QueryRequest(BaseModel):
    pack_id: str
    question: str
    use_openai: bool = False
    openai_api_key: str | None = None
    openai_model: str | None = None


class OpenAIKeyValidationRequest(BaseModel):
    openai_api_key: str
    openai_model: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str | None = None
    company: str | None = None


class UserRoleRequest(BaseModel):
    role: Literal["admin", "member"] = "member"


class CompanyRequest(BaseModel):
    name: str


class RenameCompanyRequest(BaseModel):
    name: str
    new_name: str


class UserCompanyRequest(BaseModel):
    company: str


class CompanyProjectAccessRequest(BaseModel):
    project_ids: list[str] = []


class ProjectRequest(BaseModel):
    name: str
    company: str = ""
    manager: str = ""
    discipline: str = ""
    description: str = ""
    pack_ids: list[str] = []


class ProjectPackRequest(BaseModel):
    pack_ids: list[str] = []


class IfcModelLinkRequest(BaseModel):
    model_id: str
    project_id: str | None = None


def _safe_upload_filename(filename: str, allowed_suffixes: set[str]) -> str:
    safe_name = Path(filename.replace("\\", "/")).name.strip()
    if not safe_name:
        raise ValueError("File name is required.")
    if Path(safe_name).suffix.lower() not in allowed_suffixes:
        raise ValueError(f"Supported file types: {', '.join(sorted(allowed_suffixes))}")
    return safe_name


def _ifc_metadata_filename(filename: str) -> str:
    return f"{Path(filename).stem}.metadata.json"


def _read_ifc_metadata(metadata_path: Path) -> dict[str, Any] | None:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    project_folder = metadata_path.parent.parent.name if metadata_path.parent.name == "metadata" else metadata_path.parent.name
    filename = str(metadata.get("filename") or metadata_path.name.removesuffix(".metadata.json"))
    return {
        "id": f"{project_folder}/{metadata_path.name}",
        "filename": filename,
        "projectId": metadata.get("projectId"),
        "projectName": metadata.get("projectName"),
        "sizeBytes": metadata.get("sizeBytes"),
        "uploadedAt": metadata.get("uploadedAt"),
        "storage": metadata.get("storage", "local"),
        "localPath": metadata.get("localPath"),
        "viewerStatus": metadata.get("viewerStatus"),
        "xktPath": metadata.get("xktPath"),
        "xktError": metadata.get("xktError"),
        "objectKey": metadata.get("objectKey"),
        "xktObjectKey": metadata.get("xktObjectKey"),
        "metadataPath": str(metadata_path),
    }


def list_ifc_models() -> list[dict[str, Any]]:
    if not IFC_UPLOAD_DIR.exists():
        return []
    models_by_id: dict[str, dict[str, Any]] = {}
    metadata_paths = {*IFC_UPLOAD_DIR.glob("*/*.metadata.json"), *IFC_UPLOAD_DIR.glob("*/metadata/*.metadata.json")}
    for metadata_path in sorted(metadata_paths):
        metadata = _read_ifc_metadata(metadata_path)
        if metadata:
            existing = models_by_id.get(str(metadata["id"]))
            if not existing or "/metadata/" in str(metadata.get("metadataPath", "")):
                models_by_id[str(metadata["id"])] = metadata
    return list(models_by_id.values())


def _find_ifc_metadata_path(model_id: str) -> Path:
    safe_id = model_id.replace("\\", "/").strip("/")
    if not safe_id or "/" not in safe_id:
        raise FileNotFoundError(model_id)
    project_folder, metadata_name = safe_id.split("/", 1)
    base_dir = IFC_UPLOAD_DIR / Path(project_folder).name
    metadata_filename = Path(metadata_name).name
    for metadata_path in (base_dir / "metadata" / metadata_filename, base_dir / metadata_filename):
        if metadata_path.exists():
            return metadata_path
    raise FileNotFoundError(model_id)


def _model_file_path(metadata_path: Path, metadata: dict[str, Any]) -> Path:
    filename = _safe_upload_filename(str(metadata.get("filename") or "model.ifc"), {".ifc", ".ifczip", ".zip", ".xkt"})
    default_file_dir = metadata_path.parent.parent / "files" if metadata_path.parent.name == "metadata" else metadata_path.parent
    recorded = str(metadata.get("localPath") or "").strip()
    # Metadata restored from Drive can carry a path from another machine/instance.
    if recorded and Path(recorded).exists():
        return Path(recorded)
    return default_file_dir / filename


def _ifc_project_folder(metadata_path: Path) -> str:
    return metadata_path.parent.parent.name if metadata_path.parent.name == "metadata" else metadata_path.parent.name


def _ensure_ifc_local_files(metadata_path: Path, metadata: dict[str, Any]) -> None:
    """Lazily restore the model file (and its XKT sibling) from Drive if they are not on local disk."""
    if not google_drive_sync_enabled():
        return
    model_path = _model_file_path(metadata_path, metadata)
    wanted: list[str] = []
    if not model_path.exists():
        wanted.append(model_path.name)
    if model_path.suffix.lower() != ".xkt" and not _candidate_xkt_path(model_path, metadata):
        wanted.append(model_path.with_suffix(".xkt").name)
    if not wanted:
        return
    try:
        restore_ifc_files_from_drive(_ifc_project_folder(metadata_path), wanted, model_path.parent)
    except Exception:
        return


def _candidate_xkt_path(model_path: Path, metadata: dict[str, Any]) -> Path | None:
    recorded = str(metadata.get("xktPath") or "").strip()
    if recorded:
        path = Path(recorded)
        if path.exists():
            return path
    if model_path.suffix.lower() == ".xkt" and model_path.exists():
        return model_path
    sibling = model_path.with_suffix(".xkt")
    if sibling.exists():
        return sibling
    nested = model_path.parent / "model.xkt"
    if nested.exists():
        return nested
    return None


def _shell_arg(path: Path) -> str:
    if os.name != "nt":
        return shlex.quote(str(path))
    return subprocess.list2cmdline([str(path)])


def _node_command() -> str:
    command = str(env("MODULAR_ONTOLOGY_NODE_COMMAND") or "node").strip()
    command_path = Path(command)
    return _shell_arg(command_path) if command_path.exists() else command


def _npx_command() -> str:
    command = str(env("MODULAR_ONTOLOGY_NPX_COMMAND") or "npx").strip()
    command_path = Path(command)
    return _shell_arg(command_path) if command_path.exists() else command


def _xkt_converter_candidates() -> list[Path]:
    relative = Path("node_modules") / "@xeokit" / "xeokit-convert" / "convert2xkt.js"
    roots = [ROOT, Path.cwd(), Path(__file__).resolve().parents[1]]
    candidates: list[Path] = []
    for root in roots:
        candidate = (root / relative).resolve()
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _default_xkt_converter_command() -> str:
    if str(env("MODULAR_ONTOLOGY_DISABLE_DEFAULT_XKT_CONVERTER") or "").strip() == "1":
        return ""
    for converter_path in _xkt_converter_candidates():
        if converter_path.exists():
            return f"{_node_command()} {_shell_arg(converter_path)} -s {{ifc}} -f ifc -o {{xkt}}"
    return f"{_npx_command()} -y @xeokit/xeokit-convert@1.3.2 -s {{ifc}} -f ifc -o {{xkt}}"


def _xkt_converter_command() -> str:
    configured = str(env("MODULAR_ONTOLOGY_XKT_CONVERTER_CMD") or env("XKT_CONVERTER_CMD") or "").strip()
    return configured or _default_xkt_converter_command()


def _maybe_create_xkt(model_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    if model_path.suffix.lower() == ".xkt":
        metadata.update({"viewerStatus": "ready", "xktPath": str(model_path), "xktError": None})
        return metadata
    if model_path.suffix.lower() != ".ifc":
        metadata.update({"viewerStatus": "pending-xkt", "xktError": "Only IFC or XKT files can be opened in the 3D viewer."})
        return metadata
    existing = _candidate_xkt_path(model_path, metadata)
    if existing:
        metadata.update({"viewerStatus": "ready", "xktPath": str(existing), "xktError": None})
        return metadata
    command_template = _xkt_converter_command()
    if not command_template:
        metadata.update({"viewerStatus": "pending-xkt", "xktError": "XKT converter command is not configured."})
        return metadata
    target = model_path.with_suffix(".xkt")
    timeout_seconds = int(str(env("MODULAR_ONTOLOGY_XKT_CONVERTER_TIMEOUT_SECONDS", "900")))
    try:
        command = command_template.format(ifc=_shell_arg(model_path), xkt=_shell_arg(target))
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout_seconds)
    except (KeyError, IndexError, ValueError) as exc:
        metadata.update({"viewerStatus": "error", "xktError": f"Invalid XKT converter command template: {exc}"})
        return metadata
    except subprocess.TimeoutExpired:
        metadata.update({"viewerStatus": "error", "xktError": f"XKT converter timed out after {timeout_seconds} seconds."})
        return metadata
    except OSError as exc:
        metadata.update({"viewerStatus": "error", "xktError": f"XKT converter could not start: {exc}"})
        return metadata
    if result.returncode != 0 or not target.exists():
        metadata.update(
            {
                "viewerStatus": "error",
                "xktError": (result.stderr or result.stdout or "XKT converter did not create an output file.").strip(),
            }
        )
        return metadata
    metadata.update({"viewerStatus": "ready", "xktPath": str(target), "xktError": None})
    return metadata


def _read_ifc_metadata_by_id(model_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    metadata_path = _find_ifc_metadata_path(model_id)
    raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    public_metadata = _read_ifc_metadata(metadata_path)
    if not public_metadata:
        raise FileNotFoundError(model_id)
    return metadata_path, raw_metadata, public_metadata


def _ensure_ifc_model_access(model_id: str, authorization: str | None) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    metadata_path, raw_metadata, public_metadata = _read_ifc_metadata_by_id(model_id)
    user = current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication is required for model viewer access.")
    project_id = public_metadata.get("projectId")
    if project_id:
        ensure_project_access(str(project_id), user)
    elif not is_internal_user(user):
        raise HTTPException(status_code=403, detail="This model is not available for your company.")
    return metadata_path, raw_metadata, public_metadata


def assign_ifc_model_to_project(model_id: str, project_id: str | None) -> dict[str, Any]:
    metadata_path = _find_ifc_metadata_path(model_id)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    filename = _safe_upload_filename(str(metadata.get("filename") or "model.ifc"), {".ifc", ".ifczip", ".zip", ".xkt"})
    target_project_id = (project_id or "").strip() or "_unassigned"
    project = None
    if target_project_id != "_unassigned":
        project = next((item for item in list_projects() if item["id"] == target_project_id), None)
        if not project:
            raise KeyError(target_project_id)

    default_file_dir = metadata_path.parent.parent / "files" if metadata_path.parent.name == "metadata" else metadata_path.parent
    source_file = Path(str(metadata.get("localPath") or default_file_dir / filename))
    target_dir = IFC_UPLOAD_DIR / target_project_id
    target_file = target_dir / "files" / filename
    target_metadata_path = target_dir / "metadata" / _ifc_metadata_filename(filename)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_metadata_path.parent.mkdir(parents=True, exist_ok=True)

    if source_file.exists() and source_file.resolve() != target_file.resolve():
        os.replace(source_file, target_file)
    source_xkt = _candidate_xkt_path(source_file, metadata)
    if source_xkt and source_xkt.exists():
        target_xkt = target_file.with_suffix(".xkt")
        if source_xkt.resolve() != target_xkt.resolve():
            os.replace(source_xkt, target_xkt)
        metadata["xktPath"] = str(target_xkt)
    metadata_path.unlink(missing_ok=True)

    metadata.update(
        {
            "projectId": None if target_project_id == "_unassigned" else target_project_id,
            "projectName": project.get("name") if project else None,
            "localPath": str(target_file),
        }
    )
    target_metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return _read_ifc_metadata(target_metadata_path) or {}


async def _write_upload_file_with_limit(file: UploadFile, target: Path, *, max_bytes: int) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    total = 0
    try:
        with temp.open("wb") as stream:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=f"File exceeds {max_bytes} byte limit.")
                stream.write(chunk)
        os.replace(temp, target)
        return total
    finally:
        temp.unlink(missing_ok=True)


def require_admin(authorization: str | None):
    token = extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=403, detail="Only administrators can access this resource.")
    ensure_runtime_registry()
    user = get_user_by_token(token)
    if not user or user.role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can access this resource.")
    return user


def current_user(authorization: str | None):
    token = extract_bearer_token(authorization)
    if not token:
        return None
    ensure_runtime_registry()
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
    return user


def visible_projects_for_user(user) -> list[dict[str, Any]]:
    if not user:
        return []
    ensure_runtime_registry()
    projects = list_projects()
    if is_internal_user(user):
        return projects
    allowed_project_ids = set(get_company_project_access(user.company))
    return [project for project in projects if project["id"] in allowed_project_ids]


def visible_pack_ids_for_user(user) -> set[str]:
    ensure_runtime_registry()
    return {
        pack_id
        for project in visible_projects_for_user(user)
        for pack_id in project.get("packIds", [])
        if isinstance(pack_id, str)
    }


def ensure_pack_access(pack_id: str, user) -> None:
    if not user:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    ensure_runtime_registry()
    if is_internal_user(user):
        return
    if pack_id not in visible_pack_ids_for_user(user):
        raise HTTPException(status_code=403, detail="This pack is not available for your company.")


def ensure_project_access(project_id: str, user) -> dict[str, Any]:
    if not user:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    ensure_runtime_registry()
    project = next((item for item in list_projects() if item["id"] == project_id), None)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    if is_internal_user(user):
        return project
    if not any(item["id"] == project_id for item in visible_projects_for_user(user)):
        raise HTTPException(status_code=403, detail="This project is not available for your company.")
    return project


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "modular-ontology"}


@app.get("/", include_in_schema=False)
def web_index():
    index_html = DIST_DIR / "index.html"
    if index_html.exists():
        return FileResponse(index_html)
    return HTMLResponse(
        """
        <html>
          <head><title>Modular Ontology</title></head>
          <body>
            <h1>Modular Ontology API is running</h1>
            <p>Run <code>npm run build</code> to serve the React UI from this FastAPI server.</p>
          </body>
        </html>
        """
    )


@app.post("/api/auth/login")
def login(request: LoginRequest) -> dict[str, object]:
    users_sync = run_google_drive_users_sync()
    try:
        token, user = authenticate(request.email, request.password)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response: dict[str, object] = {"token": token, "user": public_user(user)}
    if users_sync.get("status") == "error":
        response["syncWarning"] = users_sync.get("error")
    return response


@app.post("/api/auth/register")
def register(request: RegisterRequest) -> dict[str, object]:
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    try:
        user = register_user(request.email, request.password, request.name, request.company)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"status": "pending", "user": public_user(user), "writeBack": write_back}


@app.get("/api/auth/me")
def me(authorization: str | None = Header(default=None)) -> dict[str, object]:
    require_google_drive_sync(run_google_drive_users_sync())
    token = extract_bearer_token(authorization)
    user = get_user_by_token(token)
    if not user:
        return {"authenticated": False, "user": None}
    return {"authenticated": True, "user": public_user(user)}


@app.post("/api/auth/logout")
def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
    clear_session(extract_bearer_token(authorization))
    return {"status": "logged-out"}


@app.get("/api/admin/users")
def admin_users(authorization: str | None = Header(default=None)) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync())
    return {"users": [public_user(user) for user in list_users()]}


@app.get("/api/admin/companies")
def admin_companies(authorization: str | None = Header(default=None)) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync())
    return {"companies": list_companies()}


@app.get("/api/admin/company-project-access")
def admin_company_project_access(authorization: str | None = Header(default=None)) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync())
    return {"access": get_company_project_access()}


@app.post("/api/admin/companies")
def admin_add_company(request: CompanyRequest, authorization: str | None = Header(default=None)) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    try:
        company = add_company(request.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"company": company, "companies": list_companies(), "writeBack": write_back}


@app.post("/api/admin/companies/rename")
def admin_rename_company(
    request: RenameCompanyRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    try:
        company = rename_company(request.name, request.new_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"company": company, "companies": list_companies(), "writeBack": write_back}


@app.delete("/api/admin/companies/{name}")
def admin_delete_company(
    name: str,
    delete_users: bool = False,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    actor = require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    try:
        company, deleted_users = delete_company(name, delete_users=delete_users, actor_email=actor.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"company": company, "deletedUsers": deleted_users, "companies": list_companies(), "writeBack": write_back}


@app.post("/api/admin/users/{email}/approve")
def admin_approve_user(
    email: str,
    request: UserRoleRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    try:
        user = approve_user(email, request.role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"User not found: {email}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"user": public_user(user), "writeBack": write_back}


@app.post("/api/admin/users/{email}/company")
def admin_set_user_company(
    email: str,
    request: UserCompanyRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    try:
        user = set_user_company(email, request.company)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"User not found: {email}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"user": public_user(user), "writeBack": write_back}


@app.delete("/api/admin/users/{email}")
def admin_delete_user(email: str, authorization: str | None = Header(default=None)) -> dict[str, object]:
    actor = require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    try:
        user = delete_user(email, actor_email=actor.email)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"User not found: {email}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"user": public_user(user), "writeBack": write_back}


@app.post("/api/admin/users/{email}/role")
def admin_set_user_role(
    email: str,
    request: UserRoleRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    try:
        user = set_user_role(email, request.role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"User not found: {email}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"user": public_user(user), "writeBack": write_back}


@app.post("/api/admin/companies/{name}/projects")
def admin_set_company_projects(
    name: str,
    request: CompanyProjectAccessRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_users_sync(force=True))
    valid_project_ids = {project["id"] for project in list_projects()}
    invalid_project_ids = [project_id for project_id in request.project_ids if project_id not in valid_project_ids]
    if invalid_project_ids:
        raise HTTPException(status_code=400, detail=f"Unknown project ids: {', '.join(invalid_project_ids)}")
    try:
        project_ids = set_company_project_access(name, request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"company": name, "projectIds": project_ids, "access": get_company_project_access(), "writeBack": write_back}


@app.get("/api/projects")
def projects(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    return visible_projects_for_user(current_user(authorization))


@app.get("/api/projects/suggestions")
def project_suggestions(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    ensure_runtime_registry()
    linked_pack_ids = {pack_id for project in list_projects() for pack_id in project.get("packIds", [])}
    suggestions = []
    for pack in list_packs():
        if pack["id"] in linked_pack_ids:
            continue
        suggestions.append(
            {
                "packId": pack["id"],
                "packTitle": pack["title"],
                "suggestedName": suggest_project_name(pack),
                "discipline": pack["source"],
            }
        )
    return {"suggestions": suggestions[:12]}


@app.post("/api/admin/projects")
def admin_create_project(request: ProjectRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_registry_sync(force=True))
    valid_pack_ids = {pack["id"] for pack in list_packs()}
    invalid_pack_ids = [pack_id for pack_id in request.pack_ids if pack_id not in valid_pack_ids]
    if invalid_pack_ids:
        raise HTTPException(status_code=400, detail=f"Unknown pack ids: {', '.join(invalid_pack_ids)}")
    try:
        project = create_project(
            name=request.name,
            company=request.company,
            manager=request.manager,
            discipline=request.discipline,
            description=request.description,
            pack_ids=request.pack_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    drive_folders: dict[str, Any] | None = None
    if google_drive_sync_enabled():
        try:
            drive_folders = ensure_project_drive_folders(project["id"])
        except Exception as exc:
            drive_folders = {"status": "error", "error": str(exc)}
    write_back = require_google_drive_write_back(run_google_drive_write_back("database"))
    return {"project": project, "projects": list_projects(), "driveFolders": drive_folders, "writeBack": write_back}


@app.put("/api/admin/projects/{project_id}")
def admin_update_project(
    project_id: str,
    request: ProjectRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_registry_sync(force=True))
    try:
        update_project(
            project_id,
            name=request.name,
            company=request.company,
            manager=request.manager,
            discipline=request.discipline,
            description=request.description,
        )
        set_project_packs(project_id, request.pack_ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("database"))
    return {
        "project": next(project for project in list_projects() if project["id"] == project_id),
        "projects": list_projects(),
        "writeBack": write_back,
    }


@app.delete("/api/admin/projects/{project_id}")
def admin_delete_project(project_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_registry_sync(force=True))
    try:
        delete_stored_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}") from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("database"))
    return {"projectId": project_id, "projects": list_projects(), "writeBack": write_back}


@app.post("/api/admin/projects/{project_id}/packs")
def admin_set_project_pack_links(
    project_id: str,
    request: ProjectPackRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_registry_sync(force=True))
    valid_pack_ids = {pack["id"] for pack in list_packs()}
    invalid_pack_ids = [pack_id for pack_id in request.pack_ids if pack_id not in valid_pack_ids]
    if invalid_pack_ids:
        raise HTTPException(status_code=400, detail=f"Unknown pack ids: {', '.join(invalid_pack_ids)}")
    try:
        pack_ids = set_project_packs(project_id, request.pack_ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}") from exc
    write_back = require_google_drive_write_back(run_google_drive_write_back("database"))
    return {"projectId": project_id, "packIds": pack_ids, "projects": list_projects(), "writeBack": write_back}


@app.get("/api/packs")
def packs(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    user = current_user(authorization)
    if not user:
        return []
    if is_internal_user(user):
        return list_packs()
    visible_pack_ids = visible_pack_ids_for_user(user)
    return [pack for pack in list_packs() if pack["id"] in visible_pack_ids]


@app.get("/api/index/status")
def index_status(sync: bool = False) -> dict[str, Any]:
    storage_status = ensure_runtime_storage() if sync else ensure_runtime_registry()
    stats = index_stats()
    stats["users"] = len(list_users())
    stats["projects"] = len(list_projects())
    stats["ifcModels"] = len(list_ifc_models())
    stats["storage"] = storage_runtime_status(storage_status)
    return stats


@app.get("/api/storage/google-drive/status")
def google_drive_storage_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    return {"enabled": True, **google_drive_sync_status()}


@app.post("/api/admin/storage/google-drive/sync")
def admin_sync_google_drive_storage(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    if not google_drive_sync_enabled():
        raise HTTPException(status_code=400, detail="MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set.")
    result = run_google_drive_sync(force=True)
    if result.get("status") == "synced":
        index_result = index_all_packs()
        result["reindexed"] = index_result.get("stats", {})
        result["driveProjects"] = _apply_drive_project_folders()
        result["projectPackLinks"] = _apply_drive_project_pack_links()
        result["projects"] = list_projects()
        result["writeBack"] = require_google_drive_write_back(run_google_drive_write_back("database"))
        if result["driveProjects"].get("accessRenamed") or result["driveProjects"].get("accessRemoved"):
            result["usersWriteBack"] = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"enabled": True, **result}


@app.post("/api/admin/storage/google-drive/write-back")
def admin_write_back_google_drive_storage(
    include_packs: bool = False,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    if not google_drive_sync_enabled():
        raise HTTPException(status_code=400, detail="MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set.")
    results: dict[str, Any] = {
        "users": require_google_drive_write_back(run_google_drive_write_back("users")),
        "database": require_google_drive_write_back(run_google_drive_write_back("database")),
    }
    if include_packs:
        project_ids_by_pack_id: dict[str, list[str]] = {}
        for project in list_projects():
            for pack_id in project.get("packIds", []):
                project_ids_by_pack_id.setdefault(str(pack_id), []).append(str(project["id"]))
        pack_results = []
        for pack in unique_pack_files():
            summary = None
            try:
                summary = next(item for item in list_packs() if item["id"] == pack.id or item["filename"] == pack.path.name)
            except StopIteration:
                summary = None
            pack_id = str(summary.get("id") if isinstance(summary, dict) else pack.id)
            project_ids = project_ids_by_pack_id.get(pack_id, [])
            if not project_ids:
                pack_results.append({"status": "skipped", "source": str(pack.path), "reason": "Pack is not linked to a project."})
                continue
            for project_id in project_ids:
                pack_results.append(
                    require_google_drive_write_back(run_google_drive_write_back("pack", pack.path, project_id=project_id))
                )
        results["packs"] = pack_results
    return {"enabled": True, "status": "written", "results": results}


def _apply_drive_project_pack_links() -> dict[str, list[str]]:
    marker = DATA_DIR / PROJECTS_FOLDER / PROJECT_PACK_LINKS_FILENAME
    if not marker.exists():
        return {}
    try:
        raw_links = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw_links, dict):
        return {}

    packs = list_packs()
    valid_pack_ids = {str(pack["id"]) for pack in packs}
    common_pack_ids = [
        str(pack_id).strip()
        for pack_id in raw_links.get(COMMON_PROJECT_PACK_LINKS_KEY, [])
        if str(pack_id).strip() in valid_pack_ids
    ]
    project_prefixes = {
        f"{str(project_id).strip()}__"
        for project_id in raw_links
        if str(project_id).strip() and str(project_id).strip() != COMMON_PROJECT_PACK_LINKS_KEY
    }
    drive_scoped_pack_ids = {
        str(pack_id).strip()
        for pack_ids in raw_links.values()
        if isinstance(pack_ids, list)
        for pack_id in pack_ids
        if str(pack_id).strip()
    }
    drive_scoped_pack_ids.update(
        str(pack["id"])
        for pack in packs
        if any(str(pack.get("filename") or "").startswith(prefix) for prefix in project_prefixes)
    )
    projects_by_id = {str(project["id"]): project for project in list_projects()}
    applied: dict[str, list[str]] = {}
    for project_id, pack_ids in raw_links.items():
        project_id = str(project_id).strip()
        if project_id == COMMON_PROJECT_PACK_LINKS_KEY:
            applied[COMMON_PROJECT_PACK_LINKS_KEY] = common_pack_ids
            continue
        if project_id not in projects_by_id or not isinstance(pack_ids, list):
            continue
        linked_pack_ids = [
            str(pack_id).strip()
            for pack_id in pack_ids
            if str(pack_id).strip() in valid_pack_ids
        ]
        existing_pack_ids = [
            str(pack_id).strip()
            for pack_id in projects_by_id[project_id].get("packIds", [])
            if str(pack_id).strip()
        ]
        preserved_pack_ids = [
            pack_id
            for pack_id in existing_pack_ids
            if pack_id in valid_pack_ids and pack_id not in drive_scoped_pack_ids
        ]
        authoritative_pack_ids = list(dict.fromkeys([*preserved_pack_ids, *common_pack_ids, *linked_pack_ids]))
        set_project_packs(project_id, authoritative_pack_ids)
        applied[project_id] = authoritative_pack_ids
    if common_pack_ids:
        for project_id, project in projects_by_id.items():
            if project_id in applied:
                continue
            existing_pack_ids = [
                str(pack_id).strip()
                for pack_id in project.get("packIds", [])
                if str(pack_id).strip() in valid_pack_ids
            ]
            authoritative_pack_ids = list(dict.fromkeys([*existing_pack_ids, *common_pack_ids]))
            set_project_packs(project_id, authoritative_pack_ids)
            applied[project_id] = authoritative_pack_ids
    return applied


def _apply_drive_project_folders() -> dict[str, Any]:
    marker = DATA_DIR / PROJECTS_FOLDER / PROJECT_FOLDERS_FILENAME
    if not marker.exists():
        return {"created": [], "updated": [], "renamed": [], "conflicts": []}
    try:
        raw_projects = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"created": [], "updated": [], "renamed": [], "conflicts": []}
    if not isinstance(raw_projects, list):
        return {"created": [], "updated": [], "renamed": [], "conflicts": []}
    drive_projects = [item for item in raw_projects if isinstance(item, dict)]
    result = sync_projects_from_drive_folders(drive_projects)
    rename_map = {
        str(item.get("from")): str(item.get("to"))
        for item in result.get("renamed", [])
        if item.get("from") and item.get("to")
    }
    if rename_map:
        result["accessRenamed"] = _rename_company_project_access(rename_map)
    deleted_ids = [str(project_id) for project_id in result.get("deleted", []) if str(project_id)]
    if deleted_ids:
        result["accessRemoved"] = _remove_company_project_access(deleted_ids)
    return result


def _rename_company_project_access(rename_map: dict[str, str]) -> dict[str, list[str]]:
    access = get_company_project_access()
    if not isinstance(access, dict):
        return {}
    changed: dict[str, list[str]] = {}
    for company, project_ids in access.items():
        if not isinstance(project_ids, list):
            continue
        next_ids = [rename_map.get(str(project_id), str(project_id)) for project_id in project_ids]
        deduped = list(dict.fromkeys(project_id for project_id in next_ids if project_id))
        if deduped != project_ids:
            set_company_project_access(company, deduped)
            changed[company] = deduped
    return changed


def _remove_company_project_access(project_ids: list[str]) -> dict[str, list[str]]:
    removed = set(project_ids)
    access = get_company_project_access()
    if not isinstance(access, dict):
        return {}
    changed: dict[str, list[str]] = {}
    for company, current_ids in access.items():
        if not isinstance(current_ids, list):
            continue
        next_ids = [str(project_id) for project_id in current_ids if str(project_id) not in removed]
        if next_ids != current_ids:
            set_company_project_access(company, next_ids)
            changed[company] = next_ids
    return changed


@app.post("/api/admin/reindex")
def reindex(full: bool = False, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    if not full:
        registry = require_google_drive_sync(run_google_drive_registry_sync(force=True))
        result = index_all_packs()
        drive_projects = _apply_drive_project_folders()
        links = _apply_drive_project_pack_links()
        result.update({
            "status": "registry-synced",
            "registry": registry,
            "driveProjects": drive_projects,
            "projectPackLinks": links,
            "projects": list_projects(),
        })
        if any(drive_projects.get(key) for key in ("created", "updated", "renamed", "deleted", "conflicts")):
            result["writeBack"] = require_google_drive_write_back(run_google_drive_write_back("database"))
            if drive_projects.get("accessRenamed") or drive_projects.get("accessRemoved"):
                result["usersWriteBack"] = require_google_drive_write_back(run_google_drive_write_back("users"))
        return result
    require_google_drive_sync(run_google_drive_sync(force=True))
    result = index_all_packs()
    result["driveProjects"] = _apply_drive_project_folders()
    result["projectPackLinks"] = _apply_drive_project_pack_links()
    result["projects"] = list_projects()
    result["writeBack"] = require_google_drive_write_back(run_google_drive_write_back("database"))
    if result["driveProjects"].get("accessRenamed") or result["driveProjects"].get("accessRemoved"):
        result["usersWriteBack"] = require_google_drive_write_back(run_google_drive_write_back("users"))
    return result


@app.get("/api/graph/{pack_id}")
def graph(
    pack_id: str,
    max_nodes: int = 900,
    max_edges: int = 1600,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ensure_runtime_registry()
    ensure_pack_access(pack_id, current_user(authorization))
    try:
        return build_graph(pack_id=pack_id, max_nodes=max_nodes, max_edges=max_edges)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Pack not found: {pack_id}") from None


@app.get("/api/projects/{project_id}/graph")
def project_graph(
    project_id: str,
    pack_ids: str | None = None,
    max_nodes: int = 900,
    max_edges: int = 1600,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ensure_runtime_registry()
    user = current_user(authorization)
    project = ensure_project_access(project_id, user)
    project_pack_ids = [pack_id for pack_id in project.get("packIds", []) if isinstance(pack_id, str)]
    requested_pack_ids = [
        pack_id.strip()
        for pack_id in (pack_ids or "").split(",")
        if pack_id.strip()
    ]
    active_pack_ids = requested_pack_ids or project_pack_ids
    invalid_pack_ids = [pack_id for pack_id in active_pack_ids if pack_id not in project_pack_ids]
    if invalid_pack_ids:
        raise HTTPException(status_code=400, detail=f"Packs are not linked to this project: {', '.join(invalid_pack_ids)}")
    for pack_id in active_pack_ids:
        ensure_pack_access(pack_id, user)
    try:
        return build_multi_pack_graph(
            active_pack_ids,
            title=project["name"],
            project=project,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Pack not found: {exc}") from None


@app.post("/api/packs/upload")
async def upload_pack(
    file: UploadFile = File(...),
    project_mode: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
    new_project_name: str | None = Form(default=None),
    new_project_company: str | None = Form(default=None),
    new_project_manager: str | None = Form(default=None),
    new_project_discipline: str | None = Form(default=None),
    new_project_description: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_sync(force=True))
    content = await file.read()
    try:
        summary = save_uploaded_pack(file.filename or "ontology-pack.zip", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pack_path = summary.pop("_path", None)
    if pack_path:
        conn = connect()
        try:
            init_db(conn)
            ingest_result = index_pack(conn, PackFile(path=Path(pack_path)))
        finally:
            conn.close()
        summary["ingest"] = {"status": "indexed", **ingest_result}
    else:
        summary["ingest"] = {"status": "saved"}
    target_project_id = (project_id or "").strip()
    if (project_mode or "").strip() == "new" or (new_project_name and new_project_name.strip()):
        project = create_project(
            name=(new_project_name or "").strip() or suggest_project_name(summary),
            company=new_project_company or "",
            manager=new_project_manager or "",
            discipline=new_project_discipline or summary.get("source", ""),
            description=new_project_description or "",
            pack_ids=[summary["id"]],
        )
        summary["project"] = project
    elif target_project_id:
        try:
            attach_pack_to_project(target_project_id, summary["id"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Project not found: {target_project_id}") from exc
        summary["project"] = next(project for project in list_projects() if project["id"] == target_project_id)
    write_back: dict[str, Any] = {}
    if pack_path:
        project_for_write = summary.get("project") if isinstance(summary.get("project"), dict) else None
        write_back["pack"] = require_google_drive_write_back(
            run_google_drive_write_back(
                "pack",
                Path(pack_path),
                project_id=str(project_for_write.get("id")) if project_for_write else None,
            )
        )
    write_back["database"] = require_google_drive_write_back(run_google_drive_write_back("database"))
    summary["writeBack"] = write_back
    return summary


@app.post("/api/ifc/upload")
async def upload_ifc_model(
    file: UploadFile = File(...),
    project_id: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    ensure_runtime_registry()
    target_project_id = (project_id or "").strip()
    project = ensure_project_access(target_project_id, current_user(authorization)) if target_project_id else None
    try:
        safe_name = _safe_upload_filename(file.filename or "model.ifc", {".ifc", ".ifczip", ".zip", ".xkt"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    max_bytes = int(str(env("MODULAR_ONTOLOGY_IFC_MAX_BYTES", str(250 * 1024 * 1024))))
    project_folder = target_project_id or "_unassigned"
    target_dir = IFC_UPLOAD_DIR / project_folder / "files"
    metadata_dir = IFC_UPLOAD_DIR / project_folder / "metadata"
    target_path = target_dir / safe_name
    size_bytes = await _write_upload_file_with_limit(file, target_path, max_bytes=max_bytes)
    metadata = {
        "filename": safe_name,
        "sizeBytes": size_bytes,
        "projectId": target_project_id or None,
        "projectName": project.get("name") if project else None,
        "uploadedAt": time.time(),
        "storage": "local",
        "localPath": str(target_path),
    }
    metadata = await asyncio.to_thread(_maybe_create_xkt, target_path, metadata)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = metadata_dir / _ifc_metadata_filename(safe_name)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    write_back: dict[str, Any] = {}
    if google_drive_sync_enabled():
        try:
            write_back["file"] = write_back_ifc_file(target_path, target_project_id or "_unassigned")
            xkt_path = _candidate_xkt_path(target_path, metadata)
            if xkt_path and xkt_path != target_path:
                write_back["xkt"] = write_back_ifc_file(xkt_path, target_project_id or "_unassigned")
            metadata["storage"] = "google-drive"
            metadata["drive"] = write_back
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            write_back["metadata"] = write_back_ifc_metadata_file(metadata_path, target_project_id or "_unassigned")
            metadata["drive"] = write_back
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            target_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            raise HTTPException(status_code=502, detail=f"Google Drive IFC upload failed: {exc}") from exc
    return {
        "status": "stored",
        "projectId": target_project_id or None,
        "projectName": project.get("name") if project else None,
        "filename": safe_name,
        "sizeBytes": size_bytes,
        "path": str(target_path),
        "metadata": metadata,
        "writeBack": write_back,
    }


@app.get("/api/ifc/models")
def ifc_models(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    user = current_user(authorization)
    if not user:
        return []
    ensure_runtime_registry()
    models = list_ifc_models()
    if is_internal_user(user):
        return models
    allowed_project_ids = set(get_company_project_access(user.company))
    return [model for model in models if model.get("projectId") in allowed_project_ids]


@app.get("/api/ifc/model-viewer/manifest")
def ifc_model_viewer_manifest(model_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    metadata_path, raw_metadata, public_metadata = _ensure_ifc_model_access(model_id, authorization)
    _ensure_ifc_local_files(metadata_path, raw_metadata)
    model_path = _model_file_path(metadata_path, raw_metadata)
    xkt_path = _candidate_xkt_path(model_path, raw_metadata)
    status = "ready" if xkt_path else str(raw_metadata.get("viewerStatus") or "pending-xkt")
    if not model_path.exists() and not xkt_path:
        status = "missing-file"
    encoded_model_id = quote(model_id, safe="")
    return {
        "modelId": public_metadata["id"],
        "filename": public_metadata["filename"],
        "projectId": public_metadata.get("projectId"),
        "projectName": public_metadata.get("projectName"),
        "status": status,
        "xktUrl": f"/api/ifc/model-viewer/asset?model_id={encoded_model_id}" if xkt_path else None,
        "error": raw_metadata.get("xktError"),
        "sizeBytes": public_metadata.get("sizeBytes"),
        "storage": public_metadata.get("storage"),
    }


@app.get("/api/ifc/model-viewer/asset")
def ifc_model_viewer_asset(model_id: str, authorization: str | None = Header(default=None)) -> FileResponse:
    metadata_path, raw_metadata, _public_metadata = _ensure_ifc_model_access(model_id, authorization)
    _ensure_ifc_local_files(metadata_path, raw_metadata)
    xkt_path = _candidate_xkt_path(_model_file_path(metadata_path, raw_metadata), raw_metadata)
    if not xkt_path or not xkt_path.exists():
        raise HTTPException(status_code=404, detail="XKT asset is not available for this model.")
    return FileResponse(xkt_path, media_type="application/octet-stream", filename=xkt_path.name)


@app.get("/api/ifc/model-viewer/object")
def ifc_model_viewer_object(
    model_id: str,
    object_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _metadata_path, raw_metadata, public_metadata = _ensure_ifc_model_access(model_id, authorization)
    return {
        "objectId": object_id,
        "label": object_id,
        "type": "Viewer Object",
        "source": "xeokit",
        "properties": {
            "model": public_metadata.get("filename"),
            "project": public_metadata.get("projectName"),
            "viewerStatus": raw_metadata.get("viewerStatus"),
            "storage": public_metadata.get("storage"),
        },
    }


@app.post("/api/admin/ifc/models/link")
def admin_link_ifc_model(
    request: IfcModelLinkRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    ensure_runtime_registry()
    if google_drive_sync_enabled():
        try:
            _metadata_path, _raw_metadata, public_metadata = _read_ifc_metadata_by_id(request.model_id)
            current_project_id = public_metadata.get("projectId")
            target_project_id = (request.project_id or "").strip() or None
            if public_metadata.get("storage") == "google-drive" and target_project_id != current_project_id:
                raise HTTPException(
                    status_code=400,
                    detail="Drive-managed IFC models must be moved between project folders in Google Drive, then synced.",
                )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"IFC model not found: {request.model_id}") from exc
    try:
        model = assign_ifc_model_to_project(request.model_id, request.project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"IFC model not found: {request.model_id}") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Project not found: {request.project_id}") from exc
    write_back: dict[str, Any] = {}
    if google_drive_sync_enabled():
        metadata_path = Path(str(model.get("metadataPath", "")))
        file_path = Path(str(model.get("localPath", "")))
        if file_path.exists():
            write_back["file"] = write_back_ifc_file(file_path, model.get("projectId") or "_unassigned")
        xkt_path = _candidate_xkt_path(file_path, model)
        if xkt_path and xkt_path != file_path and xkt_path.exists():
            write_back["xkt"] = write_back_ifc_file(xkt_path, model.get("projectId") or "_unassigned")
        if metadata_path.exists():
            write_back["metadata"] = write_back_ifc_metadata_file(metadata_path, model.get("projectId") or "_unassigned")
    return {"model": model, "models": list_ifc_models(), "writeBack": write_back}


@app.post("/api/query")
def query_ontology(request: QueryRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    ensure_runtime_registry()
    user = current_user(authorization)
    if request.use_openai and not user:
        raise HTTPException(status_code=401, detail="Login is required to use OpenAI AI Query.")
    if request.use_openai and not (request.openai_api_key or "").strip():
        raise HTTPException(status_code=400, detail="OpenAI API key is required.")
    ensure_pack_access(request.pack_id, user)
    try:
        result = answer_pack_question(
            request.pack_id,
            request.question,
            limit=6,
            use_openai=request.use_openai,
            openai_api_key=request.openai_api_key,
            openai_model=request.openai_model,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Pack not found: {request.pack_id}") from None
    return result


@app.post("/api/llm/openai/validate")
def validate_openai_key(request: OpenAIKeyValidationRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not current_user(authorization):
        raise HTTPException(status_code=401, detail="Login is required to validate an OpenAI API key.")
    return validate_openai_api_key(request.openai_api_key, request.openai_model)


def _public_mcp_remote(request: Request | None = None) -> tuple[str | None, str | None]:
    configured_public_url = (
        env("MODULAR_ONTOLOGY_PUBLIC_MCP_URL")
        or PUBLIC_MCP_URL
    )
    public_url = configured_public_url
    public_base_url: str | None = None
    if MCP_REMOTE_FILE.exists():
        try:
            payload = json.loads(MCP_REMOTE_FILE.read_text(encoding="utf-8-sig"))
            if not public_url:
                public_url = payload.get("publicUrl")
                public_base_url = payload.get("publicBaseUrl")
        except (OSError, json.JSONDecodeError):
            public_base_url = None

    if public_url and not public_base_url:
        parts = urlsplit(public_url)
        public_base_url = urlunsplit((parts.scheme, parts.netloc, "", "", "")) if parts.scheme and parts.netloc else None
    if request and not public_base_url:
        public_base_url = urlunsplit((request.url.scheme, request.url.netloc, "", "", ""))
        public_url = f"{public_base_url}/mcp"
    return public_url, public_base_url


def _mcp_status_payload(request: Request, authorization: str | None, *, regenerate_user_token: bool = False) -> dict[str, Any]:
    public_url, public_base_url = _public_mcp_remote(request)
    user_url: dict[str, Any] | None = None
    token_sync: dict[str, Any] | None = None
    token_write_back: dict[str, Any] | None = None
    user = None
    token = extract_bearer_token(authorization)
    if token:
        require_google_drive_sync(run_google_drive_users_sync())
        user = get_user_by_token(token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid or expired session.")
    if regenerate_user_token and not user:
        raise HTTPException(status_code=401, detail="Login is required to regenerate an MCP URL.")
    if user and user.status == "active":
        token_sync = run_google_drive_mcp_tokens_sync(force=regenerate_user_token or not MCP_TOKENS_FILE.exists())
        token_file_mtime = _file_mtime_ns(MCP_TOKENS_FILE)
        token_record = regenerate_mcp_token_for_user(user) if regenerate_user_token else ensure_mcp_token_for_user(user)
        token_file_changed = token_file_mtime != _file_mtime_ns(MCP_TOKENS_FILE)
        user_urls = build_user_mcp_urls(public_base_url, token_record["token"])
        user_url = {
            **user_urls,
            "token": token_record["token"],
            "userEmail": token_record["userEmail"],
            "userName": token_record["userName"],
            "company": token_record["company"],
            "role": token_record["role"],
        }
        if token_file_changed:
            token_write_back = run_google_drive_write_back("mcp_tokens")
    return {
        "status": "ready",
        "server": "python -m modular_ontology.mcp_server",
        "remote": {
            "transport": "streamable-http",
            "localUrl": "http://127.0.0.1:8011/mcp",
            "localUserUrlTemplate": "http://127.0.0.1:8011/mcp/{token}",
            "publicUrl": public_url,
            "publicBaseUrl": public_base_url,
            "publicUserUrlTemplate": f"{public_base_url.rstrip('/')}/mcp/{{token}}" if public_base_url else None,
            "userUrl": user_url,
            "tokenSync": token_sync,
            "tokenWriteBack": token_write_back,
            "command": ".\\scripts\\run_remote_mcp.ps1",
        },
        "tools": TOOL_NAMES,
    }


@app.get("/api/mcp/status")
def mcp_status(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _mcp_status_payload(request, authorization)


@app.post("/api/mcp/user-url/regenerate")
def regenerate_mcp_user_url(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _mcp_status_payload(request, authorization, regenerate_user_token=True)


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API route not found.")
    index_html = DIST_DIR / "index.html"
    if index_html.exists():
        return FileResponse(index_html)
    raise HTTPException(status_code=404, detail="React build not found. Run npm run build.")
