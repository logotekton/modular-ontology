from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

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
from .mcp_server import TOOL_NAMES
from .pack_index import PackFile, build_graph, list_packs, list_projects, save_uploaded_pack
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


app = FastAPI(
    title="Modular Graph API",
    description="Project ontology pack ingestion, graph exploration, and MCP-ready data access API.",
    version="0.1.0",
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
    user = get_user_by_token(extract_bearer_token(authorization))
    if not user or user.role != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can access this resource.")
    return user


def current_user(authorization: str | None):
    token = extract_bearer_token(authorization)
    if not token:
        return None
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
    return user


def visible_projects_for_user(user) -> list[dict[str, Any]]:
    projects = list_projects()
    if not user or is_internal_user(user):
        return projects
    allowed_project_ids = set(get_company_project_access(user.company))
    return [project for project in projects if project["id"] in allowed_project_ids]


def visible_pack_ids_for_user(user) -> set[str]:
    return {
        pack_id
        for project in visible_projects_for_user(user)
        for pack_id in project.get("packIds", [])
        if isinstance(pack_id, str)
    }


def ensure_pack_access(pack_id: str, user) -> None:
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
    try:
        token, user = authenticate(request.email, request.password)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"token": token, "user": public_user(user)}


@app.post("/api/auth/register")
def register(request: RegisterRequest) -> dict[str, object]:
    try:
        user = register_user(request.email, request.password, request.name, request.company)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "pending", "user": public_user(user)}


@app.get("/api/auth/me")
def me(authorization: str | None = Header(default=None)) -> dict[str, object]:
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
    return {"users": [public_user(user) for user in list_users()]}


@app.get("/api/admin/companies")
def admin_companies(authorization: str | None = Header(default=None)) -> dict[str, object]:
    require_admin(authorization)
    return {"companies": list_companies()}


@app.get("/api/admin/company-project-access")
def admin_company_project_access(authorization: str | None = Header(default=None)) -> dict[str, object]:
    require_admin(authorization)
    return {"access": get_company_project_access()}


@app.post("/api/admin/companies")
def admin_add_company(request: CompanyRequest, authorization: str | None = Header(default=None)) -> dict[str, object]:
    require_admin(authorization)
    try:
        company = add_company(request.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"company": company, "companies": list_companies()}


@app.post("/api/admin/companies/rename")
def admin_rename_company(
    request: RenameCompanyRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    try:
        company = rename_company(request.name, request.new_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"company": company, "companies": list_companies()}


@app.delete("/api/admin/companies/{name}")
def admin_delete_company(
    name: str,
    delete_users: bool = False,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    actor = require_admin(authorization)
    try:
        company, deleted_users = delete_company(name, delete_users=delete_users, actor_email=actor.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"company": company, "deletedUsers": deleted_users, "companies": list_companies()}


@app.post("/api/admin/users/{email}/approve")
def admin_approve_user(
    email: str,
    request: UserRoleRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    try:
        user = approve_user(email, request.role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"User not found: {email}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": public_user(user)}


@app.post("/api/admin/users/{email}/company")
def admin_set_user_company(
    email: str,
    request: UserCompanyRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    try:
        user = set_user_company(email, request.company)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"User not found: {email}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": public_user(user)}


@app.delete("/api/admin/users/{email}")
def admin_delete_user(email: str, authorization: str | None = Header(default=None)) -> dict[str, object]:
    actor = require_admin(authorization)
    try:
        user = delete_user(email, actor_email=actor.email)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"User not found: {email}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": public_user(user)}


@app.post("/api/admin/users/{email}/role")
def admin_set_user_role(
    email: str,
    request: UserRoleRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    try:
        user = set_user_role(email, request.role)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"User not found: {email}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": public_user(user)}


@app.post("/api/admin/companies/{name}/projects")
def admin_set_company_projects(
    name: str,
    request: CompanyProjectAccessRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_admin(authorization)
    valid_project_ids = {project["id"] for project in list_projects()}
    invalid_project_ids = [project_id for project_id in request.project_ids if project_id not in valid_project_ids]
    if invalid_project_ids:
        raise HTTPException(status_code=400, detail=f"Unknown project ids: {', '.join(invalid_project_ids)}")
    try:
        project_ids = set_company_project_access(name, request.project_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"company": name, "projectIds": project_ids, "access": get_company_project_access()}


@app.get("/api/projects")
def projects(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    return visible_projects_for_user(current_user(authorization))


@app.get("/api/projects/suggestions")
def project_suggestions(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
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
    return {"project": project, "projects": list_projects()}


@app.put("/api/admin/projects/{project_id}")
def admin_update_project(
    project_id: str,
    request: ProjectRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
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
    return {"project": next(project for project in list_projects() if project["id"] == project_id), "projects": list_projects()}


@app.delete("/api/admin/projects/{project_id}")
def admin_delete_project(project_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    try:
        delete_stored_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}") from exc
    return {"projectId": project_id, "projects": list_projects()}


@app.post("/api/admin/projects/{project_id}/packs")
def admin_set_project_pack_links(
    project_id: str,
    request: ProjectPackRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(authorization)
    valid_pack_ids = {pack["id"] for pack in list_packs()}
    invalid_pack_ids = [pack_id for pack_id in request.pack_ids if pack_id not in valid_pack_ids]
    if invalid_pack_ids:
        raise HTTPException(status_code=400, detail=f"Unknown pack ids: {', '.join(invalid_pack_ids)}")
    try:
        pack_ids = set_project_packs(project_id, request.pack_ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}") from exc
    return {"projectId": project_id, "packIds": pack_ids, "projects": list_projects()}


@app.get("/api/packs")
def packs(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    user = current_user(authorization)
    if not user or is_internal_user(user):
        return list_packs()
    visible_pack_ids = visible_pack_ids_for_user(user)
    return [pack for pack in list_packs() if pack["id"] in visible_pack_ids]


@app.get("/api/index/status")
def index_status() -> dict[str, int]:
    stats = index_stats()
    stats["projects"] = len(list_projects())
    return stats


@app.post("/api/admin/reindex")
def reindex(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(authorization)
    return index_all_packs()


@app.get("/api/graph/{pack_id}")
def graph(
    pack_id: str,
    max_nodes: int = 900,
    max_edges: int = 1600,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
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
    return summary


@app.post("/api/query")
def query_ontology(request: QueryRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
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


@app.get("/api/mcp/status")
def mcp_status() -> dict[str, Any]:
    public_url = os.environ.get("MODULAR_GRAPH_PUBLIC_MCP_URL")
    if not public_url and MCP_REMOTE_FILE.exists():
        try:
            public_url = json.loads(MCP_REMOTE_FILE.read_text(encoding="utf-8-sig")).get("publicUrl")
        except (OSError, json.JSONDecodeError):
            public_url = None
    return {
        "status": "ready",
        "server": "python -m moddular_graph.mcp_server",
        "remote": {
            "transport": "streamable-http",
            "localUrl": "http://127.0.0.1:8011/mcp",
            "publicUrl": public_url,
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
