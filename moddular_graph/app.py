from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
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
from .config import MCP_REMOTE_FILE, ROOT
from .google_drive_sync import (
    google_drive_sync_enabled,
    google_drive_sync_status,
    sync_google_drive_storage,
    sync_google_drive_users_file,
    write_back_database_file,
    write_back_mcp_tokens_file,
    write_back_pack_file,
    write_back_users_file,
)
from .mcp_server import TOOL_NAMES
from .mcp_tokens import build_user_mcp_urls, ensure_mcp_token_for_user
from .pack_index import PackFile, build_graph, discover_pack_files, list_packs, list_projects, save_uploaded_pack
from .project_store import (
    attach_pack_to_project,
    create_project,
    delete_project as delete_stored_project,
    set_project_packs,
    suggest_project_name,
    update_project,
)
from .qa import answer_pack_question, ollama_status
from .store import connect, index_all_packs, index_pack, index_stats, init_db


DIST_DIR = ROOT / "dist"


def run_google_drive_sync(force: bool = False) -> dict[str, Any]:
    try:
        result = sync_google_drive_storage(force=force)
        if result.get("status") == "synced":
            invalidate_users_cache()
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def run_google_drive_users_sync() -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    try:
        result = sync_google_drive_users_file()
        if result.get("status") == "synced":
            invalidate_users_cache()
        return {"enabled": True, **result}
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc)}


def require_google_drive_sync(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("enabled") and result.get("status") == "error":
        raise HTTPException(status_code=502, detail=f"Google Drive sync failed: {result.get('error')}")
    return result


def ensure_runtime_storage() -> dict[str, Any]:
    if not google_drive_sync_enabled():
        return {"enabled": False, "status": "disabled"}
    status = google_drive_sync_status()
    if status.get("status") in {"synced", "cached"}:
        return {"enabled": True, **status}
    result = run_google_drive_sync()
    if result.get("status") == "synced":
        try:
            stats = index_stats()
            if stats.get("packs", 0) == 0 and list_packs():
                reindex_result = index_all_packs()
                result["reindexed"] = reindex_result.get("stats", {})
        except Exception as exc:
            result["reindexError"] = str(exc)
    return {"enabled": True, **result}


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
    kind: Literal["users", "database", "pack", "mcp_tokens"], path: Path | None = None
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
            result = write_back_pack_file(path)
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
    if google_drive_sync_enabled():
        run_google_drive_sync()
    yield


app = FastAPI(
    title="Modular Graph API",
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


class QueryRequest(BaseModel):
    pack_id: str
    question: str
    use_openai: bool = False
    use_ollama: bool = False


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


def require_admin(authorization: str | None):
    ensure_runtime_storage()
    user = get_user_by_token(extract_bearer_token(authorization))
    if not user or user.role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can access this resource.")
    return user


def current_user(authorization: str | None):
    ensure_runtime_storage()
    token = extract_bearer_token(authorization)
    if not token:
        return None
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
    return user


def visible_projects_for_user(user) -> list[dict[str, Any]]:
    ensure_runtime_storage()
    projects = list_projects()
    if not user or is_internal_user(user):
        return projects
    allowed_project_ids = set(get_company_project_access(user.company))
    return [project for project in projects if project["id"] in allowed_project_ids]


def visible_pack_ids_for_user(user) -> set[str]:
    ensure_runtime_storage()
    return {
        pack_id
        for project in visible_projects_for_user(user)
        for pack_id in project.get("packIds", [])
        if isinstance(pack_id, str)
    }


def ensure_pack_access(pack_id: str, user) -> None:
    ensure_runtime_storage()
    if not user or is_internal_user(user):
        return
    if pack_id not in visible_pack_ids_for_user(user):
        raise HTTPException(status_code=403, detail="This pack is not available for your company.")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "modular-graph"}


@app.get("/", include_in_schema=False)
def web_index():
    index_html = DIST_DIR / "index.html"
    if index_html.exists():
        return FileResponse(index_html)
    return HTMLResponse(
        """
        <html>
          <head><title>Modular Graph</title></head>
          <body>
            <h1>Modular Graph API is running</h1>
            <p>Run <code>npm run build</code> to serve the React UI from this FastAPI server.</p>
          </body>
        </html>
        """
    )


@app.post("/api/auth/login")
def login(request: LoginRequest) -> dict[str, object]:
    require_google_drive_sync(run_google_drive_users_sync())
    try:
        token, user = authenticate(request.email, request.password)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"token": token, "user": public_user(user)}


@app.post("/api/auth/register")
def register(request: RegisterRequest) -> dict[str, object]:
    require_google_drive_sync(run_google_drive_users_sync())
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
    require_google_drive_sync(run_google_drive_users_sync())
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
    require_google_drive_sync(run_google_drive_users_sync())
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
    require_google_drive_sync(run_google_drive_users_sync())
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
    require_google_drive_sync(run_google_drive_users_sync())
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
    require_google_drive_sync(run_google_drive_users_sync())
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
    require_google_drive_sync(run_google_drive_users_sync())
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
    require_google_drive_sync(run_google_drive_users_sync())
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
    require_google_drive_sync(run_google_drive_users_sync())
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
    ensure_runtime_storage()
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
    require_google_drive_sync(run_google_drive_sync(force=True))
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
    write_back = require_google_drive_write_back(run_google_drive_write_back("database"))
    return {"project": project, "projects": list_projects(), "writeBack": write_back}


@app.put("/api/admin/projects/{project_id}")
def admin_update_project(
    project_id: str,
    request: ProjectRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_sync(force=True))
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
    require_google_drive_sync(run_google_drive_sync(force=True))
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
    require_google_drive_sync(run_google_drive_sync(force=True))
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
    ensure_runtime_storage()
    user = current_user(authorization)
    if not user or is_internal_user(user):
        return list_packs()
    visible_pack_ids = visible_pack_ids_for_user(user)
    return [pack for pack in list_packs() if pack["id"] in visible_pack_ids]


@app.get("/api/index/status")
def index_status() -> dict[str, Any]:
    storage_status = ensure_runtime_storage()
    stats = index_stats()
    stats["projects"] = len(list_projects())
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
        raise HTTPException(status_code=400, detail="MODDULAR_GRAPH_GOOGLE_DRIVE_FOLDER_ID is not set.")
    return {"enabled": True, **run_google_drive_sync(force=True)}


@app.post("/api/admin/storage/google-drive/write-back")
def admin_write_back_google_drive_storage(
    include_packs: bool = False,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    if not google_drive_sync_enabled():
        raise HTTPException(status_code=400, detail="MODDULAR_GRAPH_GOOGLE_DRIVE_FOLDER_ID is not set.")
    results: dict[str, Any] = {
        "users": require_google_drive_write_back(run_google_drive_write_back("users")),
        "database": require_google_drive_write_back(run_google_drive_write_back("database")),
    }
    if include_packs:
        results["packs"] = [
            require_google_drive_write_back(run_google_drive_write_back("pack", pack.path))
            for pack in discover_pack_files()
        ]
    return {"enabled": True, "status": "written", "results": results}


@app.post("/api/admin/reindex")
def reindex(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    require_google_drive_sync(run_google_drive_sync(force=True))
    result = index_all_packs()
    result["writeBack"] = require_google_drive_write_back(run_google_drive_write_back("database"))
    return result


@app.get("/api/graph/{pack_id}")
def graph(
    pack_id: str,
    max_nodes: int = 900,
    max_edges: int = 1600,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ensure_runtime_storage()
    ensure_pack_access(pack_id, current_user(authorization))
    try:
        return build_graph(pack_id=pack_id, max_nodes=max_nodes, max_edges=max_edges)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Pack not found: {pack_id}") from None


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
        write_back["pack"] = require_google_drive_write_back(run_google_drive_write_back("pack", Path(pack_path)))
    write_back["database"] = require_google_drive_write_back(run_google_drive_write_back("database"))
    summary["writeBack"] = write_back
    return summary


@app.post("/api/query")
def query_ontology(request: QueryRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    ensure_runtime_storage()
    ensure_pack_access(request.pack_id, current_user(authorization))
    try:
        result = answer_pack_question(
            request.pack_id,
            request.question,
            limit=6,
            use_openai=request.use_openai,
            use_ollama=request.use_ollama,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Pack not found: {request.pack_id}") from None
    return result


@app.get("/api/llm/ollama/status")
def ollama_llm_status() -> dict[str, Any]:
    return ollama_status()


def _public_mcp_remote() -> tuple[str | None, str | None]:
    public_url = os.environ.get("MODULAR_GRAPH_PUBLIC_MCP_URL") or os.environ.get("MODDULAR_GRAPH_PUBLIC_MCP_URL")
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
    return public_url, public_base_url


@app.get("/api/mcp/status")
def mcp_status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    public_url, public_base_url = _public_mcp_remote()
    user_url: dict[str, Any] | None = None
    user = current_user(authorization)
    if user and user.status == "active":
        token_record = ensure_mcp_token_for_user(user)
        user_urls = build_user_mcp_urls(public_base_url, token_record["token"])
        user_url = {
            **user_urls,
            "token": token_record["token"],
            "userEmail": token_record["userEmail"],
            "userName": token_record["userName"],
            "company": token_record["company"],
            "role": token_record["role"],
        }
        run_google_drive_write_back("mcp_tokens")
    return {
        "status": "ready",
        "server": "python -m moddular_graph.mcp_server",
        "remote": {
            "transport": "streamable-http",
            "localUrl": "http://127.0.0.1:8011/mcp",
            "localUserUrlTemplate": "http://127.0.0.1:8011/mcp/{token}",
            "publicUrl": public_url,
            "publicBaseUrl": public_base_url,
            "publicUserUrlTemplate": f"{public_base_url.rstrip('/')}/mcp/{{token}}" if public_base_url else None,
            "userUrl": user_url,
            "command": ".\\scripts\\run_remote_mcp.ps1",
        },
        "tools": TOOL_NAMES,
    }


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API route not found.")
    index_html = DIST_DIR / "index.html"
    if index_html.exists():
        return FileResponse(index_html)
    raise HTTPException(status_code=404, detail="React build not found. Run npm run build.")
