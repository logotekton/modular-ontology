from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shlex
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
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
from .config import (
    DATA_DIR,
    EPHEMERAL_STORAGE,
    IFC_MODELS_FOLDER,
    MCP_REMOTE_FILE,
    MCP_TOKENS_FILE,
    PROJECTS_FOLDER,
    ROOT,
    env,
    is_ephemeral_runtime,
)
from .google_drive_sync import (
    COMMON_PROJECT_PACK_LINKS_KEY,
    PROJECT_FOLDERS_FILENAME,
    PROJECT_PACK_LINKS_FILENAME,
    ensure_project_drive_folders,
    google_drive_sync_enabled,
    google_drive_sync_status,
    restore_ifc_files_from_drive,
    sync_google_drive_registry_files,
    sync_google_drive_runtime_metadata,
    sync_google_drive_project_storage,
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
from .mcp_server import configure_server as configure_mcp_server, mcp as remote_mcp
from .mcp_tokens import (
    build_user_mcp_urls,
    ensure_mcp_token_for_user,
    get_mcp_token_record,
    regenerate_mcp_token_for_user,
)
from .graph_preview import (
    GraphBuildInProgressError,
    GraphPreviewUnavailableError,
    get_or_build_project_graph,
    invalidate_graph_cache,
)
from .full_graph import (
    FULL_GRAPH_ALGORITHM,
    FULL_GRAPH_LAYOUT,
    FULL_GRAPH_VERSION,
    FullGraphChunkNotFoundError,
    FullGraphUnavailableError,
    full_graph_node_detail,
    issue_full_graph_capability,
    load_full_graph_entry,
    resolve_full_graph_chunk,
    resolve_full_graph_chunk_from_manifest,
    validate_full_graph_capability,
)
from .pack_index import (
    PackFile,
    build_graph,
    build_multi_pack_graph,
    list_packs as _list_packs,
    list_projects as _list_projects,
    save_uploaded_pack,
    unique_pack_files,
)
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
from .store import bm25_index_status, connect, index_all_packs, index_pack, index_stats, init_db


DIST_DIR = ROOT / "dist"
IFC_UPLOAD_DIR = DATA_DIR / IFC_MODELS_FOLDER
PUBLIC_MCP_DOMAIN = str(env("MODULAR_ONTOLOGY_PUBLIC_MCP_DOMAIN", "modular-ontology.xyz"))
PUBLIC_MCP_BASE_URL = f"https://{PUBLIC_MCP_DOMAIN}"
PUBLIC_MCP_URL = f"{PUBLIC_MCP_BASE_URL}/mcp"
_FIXED_MCP_ALLOWED_HOSTS = (
    PUBLIC_MCP_DOMAIN,
    "modular-ontology.vercel.app",
    "modular-ontology-ythongs-projects.vercel.app",
    "modular-ontology-ghddudxor12-8502-ythongs-projects.vercel.app",
)
_VERCEL_RUNTIME_HOST_ENV_NAMES = (
    "VERCEL_URL",
    "VERCEL_BRANCH_URL",
    "VERCEL_PROJECT_PRODUCTION_URL",
)
_DEFAULT_PROJECT_GRAPH_MAX_PACKS = 250


def list_packs() -> list[dict[str, Any]]:
    """List runtime metadata without hydrating the separate query database."""

    return _list_packs(include_query_database=False)


def list_projects() -> list[dict[str, Any]]:
    """List runtime projects without hydrating the separate query database."""

    return _list_projects(include_query_database=False)


def _project_graph_max_packs() -> int:
    raw = env("MODULAR_ONTOLOGY_PROJECT_GRAPH_MAX_PACKS", str(_DEFAULT_PROJECT_GRAPH_MAX_PACKS))
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        value = _DEFAULT_PROJECT_GRAPH_MAX_PACKS
    return max(1, min(value, 500))


def _default_project_graph_pack_ids(
    project_pack_ids: list[str],
    limit: int,
    *,
    pack_summaries: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Prefer project-specific packs and keep a bounded cold Drive fan-out."""

    summaries = {
        str(pack.get("id") or ""): pack
        for pack in (pack_summaries if pack_summaries is not None else list_packs())
    }
    project_specific = [
        pack_id
        for pack_id in project_pack_ids
        if not bool(summaries.get(pack_id, {}).get("commonScoped"))
    ]
    project_specific_ids = set(project_specific)
    common = [pack_id for pack_id in project_pack_ids if pack_id not in project_specific_ids]
    if len(project_specific) >= limit:
        return project_specific[-limit:]
    remaining = limit - len(project_specific)
    return [*project_specific, *common[-remaining:]]


def _normalize_mcp_allowed_host(value: Any) -> str | None:
    """Convert an allowlist value or URL to the exact Host form understood by MCP."""
    raw = str(value or "").strip()
    if not raw or any(ord(character) < 32 for character in raw):
        return None

    wildcard_port = raw.endswith(":*") and "://" not in raw
    if wildcard_port:
        raw = raw[:-2]
    if "*" in raw:
        # MCP supports only an exact host or an exact host with a wildcard port.
        return None

    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    except ValueError:
        return None
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        return None
    try:
        hostname = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return None
    if not hostname or any(character.isspace() or character in "%/?#@," for character in hostname):
        return None
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if ":" in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return None
    else:
        labels = hostname.split(".")
        if len(hostname) > 253 or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (character.isalnum() or character == "-") for character in label)
            for label in labels
        ):
            return None

    normalized = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (parsed.scheme.lower() == "https" and port == 443) or (
        parsed.scheme.lower() == "http" and port == 80
    )
    if port is not None and not default_port:
        normalized = f"{normalized}:{port}"
    if wildcard_port:
        normalized = f"{normalized}:*"
    return normalized


def _build_mcp_allowed_hosts() -> list[str]:
    candidates: list[Any] = [
        *_FIXED_MCP_ALLOWED_HOSTS,
        *(os.environ.get(name) for name in _VERCEL_RUNTIME_HOST_ENV_NAMES),
        *(str(env("MODULAR_ONTOLOGY_MCP_ALLOWED_HOSTS") or "").split(",")),
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "testserver",
    ]
    allowed_hosts: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_mcp_allowed_host(candidate)
        if normalized and normalized not in seen:
            allowed_hosts.append(normalized)
            seen.add(normalized)
    return allowed_hosts


configure_mcp_server(
    host="127.0.0.1",
    port=8011,
    path="/{mcp_token}",
    allowed_hosts=_build_mcp_allowed_hosts(),
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

_GOOGLE_DRIVE_SYNC_LOCK = threading.RLock()
# Registry metadata and the compact query database describe one published
# generation.  Keep scoped hydration on the same process lock as full/query
# syncs so a metadata-only activation cannot race a database activation.
_GOOGLE_DRIVE_SCOPE_SYNC_LOCK = _GOOGLE_DRIVE_SYNC_LOCK
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


def _invalidate_runtime_metadata_caches() -> None:
    invalidate_users_cache()
    invalidate_graph_cache()


def _run_scoped_google_drive_sync(
    scope: str,
    sync_fn: Callable[[], dict[str, Any]],
    *,
    force: bool = False,
    on_synced: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    try:
        ttl = _sync_ttl_seconds(scope)
        with _GOOGLE_DRIVE_SCOPE_SYNC_LOCK:
            if not force:
                cached = _cached_scope_sync(scope, ttl)
                if cached:
                    return cached
            result = sync_fn()
            _remember_scope_sync(scope, result)
            if result.get("status") == "synced" and on_synced:
                on_synced()
        return {"enabled": True, **result}
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc)}


def _file_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def run_google_drive_sync(force: bool = False, *, include_shared_packs: bool = True) -> dict[str, Any]:
    try:
        with _GOOGLE_DRIVE_SYNC_LOCK:
            result = sync_google_drive_storage(force=force, include_shared_packs=include_shared_packs)
            if result.get("status") == "synced":
                _invalidate_runtime_metadata_caches()
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def run_google_drive_project_sync(project_id: str, force: bool = False) -> dict[str, Any]:
    try:
        with _GOOGLE_DRIVE_SYNC_LOCK:
            result = sync_google_drive_project_storage(project_id, force=force)
        return result
    except Exception as exc:
        return {"status": "error", "scope": "project", "projectId": project_id, "error": str(exc)}


def run_google_drive_registry_sync(force: bool = False) -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    try:
        with _GOOGLE_DRIVE_SYNC_LOCK:
            result = sync_google_drive_registry_files(force=force)
            if result.get("status") in {"synced", "cached"} and result.get("databaseIncluded") is not False:
                # DB-inclusive hydration also downloads and activates every file
                # needed by current_user().  Record that fact while holding the
                # shared lock so /api/query does not immediately hydrate the
                # same registry metadata a second time.
                _remember_scope_sync("runtime_metadata", result)
            if result.get("status") == "synced":
                _invalidate_runtime_metadata_caches()
        return {"enabled": True, **result}
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc)}


def run_google_drive_runtime_metadata_sync(force: bool = False) -> dict[str, Any]:
    return _run_scoped_google_drive_sync(
        "runtime_metadata",
        lambda: sync_google_drive_runtime_metadata(force=force),
        force=force,
        on_synced=_invalidate_runtime_metadata_caches,
    )


def run_google_drive_users_sync(force: bool = False) -> dict[str, Any]:
    return _run_scoped_google_drive_sync(
        "users",
        sync_google_drive_users_file,
        force=force,
        on_synced=invalidate_users_cache,
    )


def run_google_drive_mcp_tokens_sync(force: bool = False) -> dict[str, Any]:
    return _run_scoped_google_drive_sync(
        "mcp_tokens",
        sync_google_drive_mcp_tokens_file,
        force=force,
    )


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
    default = not is_ephemeral_runtime()
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
    result = run_google_drive_runtime_metadata_sync()
    if result.get("status") == "synced":
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


def ensure_runtime_query_index() -> dict[str, Any]:
    """Hydrate the compact SQLite query index only for search/QA paths."""

    result = run_google_drive_registry_sync()
    if result.get("enabled") and result.get("status") == "error":
        raise RuntimeError(f"Google Drive query-index sync failed: {result.get('error')}")
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
    """Lazily restore only the XKT asset required by the browser viewer."""
    if not google_drive_sync_enabled():
        return
    model_path = _model_file_path(metadata_path, metadata)
    if _candidate_xkt_path(model_path, metadata):
        return
    if model_path.suffix.lower() == ".xkt":
        xkt_filename = model_path.name
    else:
        recorded = str(metadata.get("xktPath") or "").strip()
        recorded_name = Path(recorded.replace("\\", "/")).name if recorded else ""
        xkt_filename = (
            recorded_name
            if Path(recorded_name).suffix.lower() == ".xkt"
            else model_path.with_suffix(".xkt").name
        )
    try:
        restore_ifc_files_from_drive(
            _ifc_project_folder(metadata_path),
            [xkt_filename],
            model_path.parent,
        )
    except Exception as exc:
        raise RuntimeError("XKT asset could not be restored from Google Drive.") from exc


def _candidate_xkt_path(model_path: Path, metadata: dict[str, Any]) -> Path | None:
    recorded = str(metadata.get("xktPath") or "").strip()
    if recorded:
        path = Path(recorded)
        if path.exists():
            return path
        restored_path = model_path.parent / Path(recorded.replace("\\", "/")).name
        if restored_path.exists():
            return restored_path
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


@app.get("/api/bootstrap")
def bootstrap(authorization: str | None = Header(default=None)) -> dict[str, object]:
    """Return the authenticated workspace catalog in one cold-start request."""

    ensure_runtime_registry()
    token = extract_bearer_token(authorization)
    user = get_user_by_token(token)
    if not user:
        return {
            "authenticated": False,
            "user": None,
            "packs": [],
            "projects": [],
            "ifcModels": [],
        }

    all_projects = list_projects()
    if is_internal_user(user):
        visible_projects = all_projects
    else:
        allowed_project_ids = set(get_company_project_access(user.company))
        visible_projects = [
            project for project in all_projects if project.get("id") in allowed_project_ids
        ]
    visible_pack_ids = {
        pack_id
        for project in visible_projects
        for pack_id in project.get("packIds", [])
        if isinstance(pack_id, str)
    }
    all_packs = list_packs()
    visible_packs = (
        all_packs
        if is_internal_user(user)
        else [pack for pack in all_packs if pack.get("id") in visible_pack_ids]
    )
    all_models = list_ifc_models()
    visible_models = (
        all_models
        if is_internal_user(user)
        else [model for model in all_models if model.get("projectId") in {project["id"] for project in visible_projects}]
    )
    return {
        "authenticated": True,
        "user": public_user(user),
        "packs": visible_packs,
        "projects": visible_projects,
        "ifcModels": visible_models,
        "stats": _runtime_catalog_stats(visible_packs, visible_projects, visible_models),
    }


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


def _runtime_catalog_stats(
    pack_rows: list[dict[str, Any]] | None = None,
    project_rows: list[dict[str, Any]] | None = None,
    model_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pack_rows = pack_rows if pack_rows is not None else list_packs()
    project_rows = project_rows if project_rows is not None else list_projects()
    model_rows = model_rows if model_rows is not None else list_ifc_models()
    return {
        "packs": len(pack_rows),
        "documents": sum(int(pack.get("counts", {}).get("documents") or 0) for pack in pack_rows),
        "nodes": sum(int(pack.get("counts", {}).get("nodes") or 0) for pack in pack_rows),
        "edges": sum(int(pack.get("counts", {}).get("edges") or 0) for pack in pack_rows),
        "users": len(list_users()),
        "projects": len(project_rows),
        "ifcModels": len(model_rows),
    }


@app.get("/api/index/status")
def index_status(sync: bool = False) -> dict[str, Any]:
    if sync:
        storage_status = ensure_runtime_storage()
        stats = index_stats()
        stats["users"] = len(list_users())
        stats["projects"] = len(list_projects())
        stats["ifcModels"] = len(list_ifc_models())
        stats["bm25"] = bm25_index_status()
    else:
        storage_status = ensure_runtime_registry()
        stats = _runtime_catalog_stats()
        stats["statsSource"] = "registryCatalog"
        stats["queryIndex"] = {
            "state": "deferred",
            "validated": False,
        }
        stats["bm25"] = {"available": False, "deferred": True}
    stats["storage"] = storage_runtime_status(storage_status)
    return stats


@app.get("/api/storage/google-drive/status")
def google_drive_storage_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled", "persistentStorage": not EPHEMERAL_STORAGE}
    return {"enabled": True, "persistentStorage": not EPHEMERAL_STORAGE, **google_drive_sync_status()}


def _sync_changed_zip_paths(result: dict[str, Any]) -> list[Path]:
    changed: list[Path] = []
    for entry in result.get("downloaded") or []:
        path = Path(str(entry))
        if path.suffix.lower() == ".zip":
            changed.append(path)
    return changed


def _reindex_packs_by_path(paths: list[Path]) -> dict[str, Any]:
    """변경된 zip만 재색인 — 전체 delete/reinsert 대신 해당 팩만 갱신한다."""
    conn = connect()
    try:
        init_db(conn)
        known = {pack.path.resolve(): pack for pack in unique_pack_files()}
        indexed = []
        for path in paths:
            pack = known.get(path.resolve())
            if pack is not None:
                indexed.append(index_pack(conn, pack))
        result = {"status": "indexed", "packs": indexed, "stats": index_stats(conn)}
    finally:
        conn.close()
    if indexed:
        invalidate_graph_cache()
    return result


def _require_persistent_sync_storage() -> None:
    """Vercel의 /tmp은 콜드 스타트마다 증발 — 매번 제로부터 풀 다운로드가 시작돼
    함수 시간제한(504)으로 끊기는 루프가 된다. 명확한 안내로 빠르게 실패시킨다."""
    if EPHEMERAL_STORAGE:
        raise HTTPException(
            status_code=400,
            detail=(
                "이 배포 환경(Vercel)은 임시 스토리지라 Drive 동기화 상태가 유지되지 않습니다. "
                "동기화는 로컬 서버(uvicorn)에서 실행한 뒤 결과를 Drive write-back으로 공유하세요."
            ),
        )


@app.post("/api/admin/storage/google-drive/sync")
def admin_sync_google_drive_storage(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    _require_persistent_sync_storage()
    if not google_drive_sync_enabled():
        raise HTTPException(status_code=400, detail="MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set.")
    result = run_google_drive_sync(force=True, include_shared_packs=False)
    if result.get("status") == "synced":
        changed = _sync_changed_zip_paths(result)
        # 재색인/프로젝트 적용/write-back도 동기화 락으로 직렬화 — 동시 요청이
        # 같은 SQLite에 쓰며 'database is locked'로 500 나던 문제 방지.
        with _GOOGLE_DRIVE_SYNC_LOCK:
            if changed:
                index_result = index_all_packs()
                invalidate_graph_cache()
                result["reindexed"] = index_result.get("stats", {})
            else:
                result["reindexed"] = index_stats()
            result["driveProjects"] = _apply_drive_project_folders()
            result["projectPackLinks"] = _apply_drive_project_pack_links()
            result["projects"] = list_projects()
            if changed:
                result["writeBack"] = require_google_drive_write_back(run_google_drive_write_back("database"))
            else:
                # 변경 없는 동기화가 DB를 재업로드해 modifiedTime을 올리고
                # 다음 동기화의 캐시를 스스로 무효화하던 루프 차단.
                result["writeBack"] = {"status": "skipped", "reason": "no changed files"}
            if result["driveProjects"].get("accessRenamed") or result["driveProjects"].get("accessRemoved"):
                result["usersWriteBack"] = require_google_drive_write_back(run_google_drive_write_back("users"))
    return {"enabled": True, **result}


@app.post("/api/admin/storage/google-drive/projects/{project_id}/sync")
def admin_sync_google_drive_project(project_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    _require_persistent_sync_storage()
    if not google_drive_sync_enabled():
        raise HTTPException(status_code=400, detail="MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID is not set.")
    if not any(str(project["id"]) == project_id for project in list_projects()):
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
    result = run_google_drive_project_sync(project_id, force=True)
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail=f"Google Drive project folder not found: {project_id}")
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=f"Google Drive project sync failed: {result.get('error')}")
    if result.get("status") == "synced":
        changed = _sync_changed_zip_paths(result)
        with _GOOGLE_DRIVE_SYNC_LOCK:
            if changed:
                # 프로젝트 스코프 동기화는 변경 팩만 재색인 — 전체 재색인이
                # 스코핑을 무력화하고 요청 시간을 폭증시키던 문제 해결.
                index_result = _reindex_packs_by_path(changed)
                result["reindexed"] = index_result.get("stats", {})
                result["reindexedPacks"] = [entry.get("id") for entry in index_result.get("packs", [])]
            else:
                result["reindexed"] = index_stats()
            result["projectPackLinks"] = _apply_drive_project_pack_links()
            result["projects"] = list_projects()
            if changed:
                result["writeBack"] = require_google_drive_write_back(run_google_drive_write_back("database"))
            else:
                result["writeBack"] = {"status": "skipped", "reason": "no changed files"}
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
        invalidate_graph_cache()
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
    full_sync = require_google_drive_sync(run_google_drive_sync(force=True))
    if full_sync.get("status") != "synced" or full_sync.get("missing") or full_sync.get("warnings"):
        raise HTTPException(
            status_code=502,
            detail="Full Drive sync was incomplete; refusing to prune the local ontology index.",
        )
    result = index_all_packs(prune_missing=True)
    invalidate_graph_cache()
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
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
    requested_pack_ids = list(dict.fromkeys(requested_pack_ids))
    pack_limit = _project_graph_max_packs()
    if len(requested_pack_ids) > pack_limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A project graph can load at most {pack_limit} packs per request. "
                "Select fewer packs to keep lazy graph loading within the serverless time budget."
            ),
        )
    project_pack_id_set = set(project_pack_ids)
    invalid_pack_ids = [
        pack_id for pack_id in requested_pack_ids if pack_id not in project_pack_id_set
    ]
    if invalid_pack_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Packs are not linked to this project: {', '.join(invalid_pack_ids)}",
        )
    pack_summaries = list_packs()
    if requested_pack_ids:
        requested_pack_id_set = set(requested_pack_ids)
        # Graph selection is order-sensitive for fair budgeting and ambiguous
        # cross-pack endpoints. Canonicalize to the project's declared order so
        # UI click order and query-string order cannot change graph semantics.
        active_pack_ids = [
            pack_id for pack_id in project_pack_ids if pack_id in requested_pack_id_set
        ]
    else:
        active_pack_ids = _default_project_graph_pack_ids(
            project_pack_ids,
            pack_limit,
            pack_summaries=pack_summaries,
        )
    selection_truncated = not requested_pack_ids and len(project_pack_ids) > len(active_pack_ids)
    try:
        result = get_or_build_project_graph(
            active_pack_ids,
            pack_summaries=pack_summaries,
            title=project["name"],
            project=project,
            max_nodes=max_nodes,
            max_edges=max_edges,
            builder=build_multi_pack_graph,
        )
        diagnostics = result.setdefault("diagnostics", {})
        diagnostics.update(
            {
                "projectPackSelectionTruncated": selection_truncated,
                "projectPacksAvailable": len(project_pack_ids),
                "projectPacksLoaded": len(active_pack_ids),
                "projectGraphPackLimit": pack_limit,
            }
        )
        return result
    except GraphPreviewUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except GraphBuildInProgressError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "2"},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Pack not found: {exc}") from None


def _full_graph_project_selection(
    project: dict[str, Any],
    pack_ids: str | None,
    *,
    pack_summaries: list[dict[str, Any]],
) -> tuple[list[str], bool]:
    project_pack_ids = [
        pack_id
        for pack_id in project.get("packIds", [])
        if isinstance(pack_id, str)
    ]
    requested_pack_ids = list(
        dict.fromkeys(
            pack_id.strip()
            for pack_id in (pack_ids or "").split(",")
            if pack_id.strip()
        )
    )
    pack_limit = _project_graph_max_packs()
    if len(requested_pack_ids) > pack_limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A project graph can load at most {pack_limit} packs per request."
            ),
        )
    project_pack_id_set = set(project_pack_ids)
    invalid_pack_ids = [
        pack_id
        for pack_id in requested_pack_ids
        if pack_id not in project_pack_id_set
    ]
    if invalid_pack_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "Packs are not linked to this project: "
                f"{', '.join(invalid_pack_ids)}"
            ),
        )
    if requested_pack_ids:
        requested_pack_id_set = set(requested_pack_ids)
        active_pack_ids = [
            pack_id
            for pack_id in project_pack_ids
            if pack_id in requested_pack_id_set
        ]
    else:
        active_pack_ids = _default_project_graph_pack_ids(
            project_pack_ids,
            pack_limit,
            pack_summaries=pack_summaries,
        )
    selection_truncated = (
        not requested_pack_ids
        and len(project_pack_ids) > len(active_pack_ids)
    )
    if not active_pack_ids:
        raise HTTPException(
            status_code=400,
            detail="Select at least one ontology pack for the full graph.",
        )
    return active_pack_ids, selection_truncated


@app.get("/api/projects/{project_id}/graph/full/manifest")
def project_full_graph_manifest(
    project_id: str,
    pack_ids: str | None = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ensure_runtime_registry()
    user = current_user(authorization)
    project = ensure_project_access(project_id, user)
    pack_summaries = list_packs()
    active_pack_ids, selection_truncated = _full_graph_project_selection(
        project,
        pack_ids,
        pack_summaries=pack_summaries,
    )
    try:
        entry = load_full_graph_entry(
            active_pack_ids,
            pack_summaries=pack_summaries,
        )
    except FullGraphUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc

    selection_key = str(entry["selectionKey"])
    graph_capability = issue_full_graph_capability(project_id, selection_key)
    graph_capability_query = (
        f"?graph_access={quote(graph_capability, safe='')}"
        if graph_capability
        else ""
    )
    chunks = [
        {
            **descriptor,
            "url": (
                f"/api/projects/{quote(project_id, safe='')}/graph/full/chunks/"
                f"{quote(selection_key, safe='')}/"
                f"{quote(str(descriptor['file']), safe='')}"
                f"{graph_capability_query}"
            ),
        }
        for descriptor in entry.get("chunks") or []
        if isinstance(descriptor, dict)
    ]
    stats = dict(entry.get("stats") or {})
    diagnostics = {
        **dict(entry.get("diagnostics") or {}),
        "projectPackSelectionTruncated": selection_truncated,
        "projectPacksAvailable": len(project.get("packIds") or []),
        "projectPacksLoaded": len(active_pack_ids),
        "projectGraphPackLimit": _project_graph_max_packs(),
    }
    return {
        "version": FULL_GRAPH_VERSION,
        "algorithm": FULL_GRAPH_ALGORITHM,
        "layout": FULL_GRAPH_LAYOUT,
        "selectionKey": selection_key,
        "projectId": project_id,
        "projectName": str(project.get("name") or project_id),
        "packIds": active_pack_ids,
        "packCount": len(active_pack_ids),
        "totalNodes": int(stats.get("totalNodes") or 0),
        "totalEdges": int(stats.get("totalEdges") or 0),
        "stats": stats,
        "diagnostics": diagnostics,
        "chunks": chunks,
    }


@app.get(
    "/api/projects/{project_id}/graph/full/chunks/"
    "{selection_key}/{filename}"
)
def project_full_graph_chunk(
    project_id: str,
    selection_key: str,
    filename: str,
    graph_access: str | None = None,
    authorization: str | None = Header(default=None),
    if_none_match: str | None = Header(default=None),
) -> Response:
    capability_authorized = validate_full_graph_capability(
        graph_access,
        project_id,
        selection_key,
    )
    if graph_access is not None and not capability_authorized:
        raise HTTPException(
            status_code=401,
            detail="The full-graph access capability is invalid or expired.",
        )
    try:
        if capability_authorized:
            path, descriptor, entry = resolve_full_graph_chunk_from_manifest(
                selection_key,
                filename,
            )
        else:
            ensure_runtime_registry()
            user = current_user(authorization)
            project = ensure_project_access(project_id, user)
            pack_summaries = list_packs()
            path, descriptor, entry = resolve_full_graph_chunk(
                selection_key,
                filename,
                pack_summaries=pack_summaries,
            )
    except FullGraphUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FullGraphChunkNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Full-graph chunk not found: {filename}",
        ) from exc

    if not capability_authorized:
        project_pack_ids = [
            pack_id
            for pack_id in project.get("packIds") or []
            if isinstance(pack_id, str)
        ]
        entry_pack_ids = [
            pack_id
            for pack_id in entry.get("packIds") or []
            if isinstance(pack_id, str)
        ]
        entry_pack_id_set = set(entry_pack_ids)
        canonical_entry_ids = [
            pack_id
            for pack_id in project_pack_ids
            if pack_id in entry_pack_id_set
        ]
        if canonical_entry_ids != entry_pack_ids:
            raise HTTPException(
                status_code=403,
                detail="This full-graph chunk is not available for the project.",
            )

    digest = str(descriptor["sha256"])
    etag = f'"{digest}"'
    cache_headers = {
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Encoding": "gzip",
        "ETag": etag,
        "Vary": "Authorization",
        "X-Content-Type-Options": "nosniff",
    }
    if isinstance(if_none_match, str) and etag in {
        token.strip()
        for token in if_none_match.split(",")
    }:
        return Response(status_code=304, headers=cache_headers)
    return FileResponse(
        path,
        media_type="application/json",
        headers=cache_headers,
    )


@app.get("/api/projects/{project_id}/graph/full/node")
def project_full_graph_node_detail(
    project_id: str,
    node_id: str,
    original_id: str | None = None,
    occurrence: int = 0,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ensure_runtime_registry()
    user = current_user(authorization)
    project = ensure_project_access(project_id, user)
    project_pack_ids = [
        pack_id
        for pack_id in project.get("packIds") or []
        if isinstance(pack_id, str)
    ]
    matched_pack_id = next(
        (
            pack_id
            for pack_id in sorted(
                project_pack_ids,
                key=len,
                reverse=True,
            )
            if node_id.startswith(f"{pack_id}::")
        ),
        None,
    )
    if not matched_pack_id:
        raise HTTPException(
            status_code=404,
            detail="The graph node is not authored by a pack in this project.",
        )
    authored_id = (
        str(original_id)
        if original_id is not None
        else node_id[len(matched_pack_id) + 2 :]
    )
    authored_occurrence = max(0, int(occurrence))
    detail = full_graph_node_detail(
        matched_pack_id,
        authored_id,
        occurrence=authored_occurrence,
    )
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Graph node not found: {node_id}",
        )
    return detail


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
    invalidate_graph_cache()
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
    try:
        _ensure_ifc_local_files(metadata_path, raw_metadata)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
    try:
        _ensure_ifc_local_files(metadata_path, raw_metadata)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
    ensure_runtime_query_index()
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


def _persist_and_verify_mcp_token(token_record: dict[str, Any]) -> dict[str, Any]:
    write_back = run_google_drive_write_back("mcp_tokens")
    if not google_drive_sync_enabled():
        return {**write_back, "verified": True}

    verification_sync = run_google_drive_mcp_tokens_sync(force=True)
    persisted_record = get_mcp_token_record(str(token_record.get("token") or ""))
    sync_verified = verification_sync.get("status") in {"synced", "cached"}
    if not sync_verified or not persisted_record:
        if not sync_verified:
            MCP_TOKENS_FILE.unlink(missing_ok=True)
        reason = (
            write_back.get("error")
            or verification_sync.get("error")
            or "the issued token was not found after central-store verification"
        )
        raise HTTPException(status_code=502, detail=f"MCP URL persistence failed: {reason}")
    return {
        **write_back,
        "verified": True,
        "verificationSyncStatus": verification_sync.get("status"),
    }


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
        require_google_drive_sync(token_sync)
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
        if regenerate_user_token or token_file_changed:
            token_write_back = _persist_and_verify_mcp_token(token_record)
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
        "toolProfile": remote_mcp.tool_profile,
        "tools": remote_mcp.visible_tool_names(),
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
