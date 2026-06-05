from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

os.environ.setdefault("MODDULAR_GRAPH_ADMIN_YTHONG_PASSWORD", "test-admin-password")
os.environ.setdefault("MODDULAR_GRAPH_ADMIN_MWHONG_PASSWORD", "test-admin-password-2")

import anyio
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from moddular_graph import mcp_server
from moddular_graph.app import app
from moddular_graph.pack_index import (
    build_graph,
    get_fasteners,
    get_module,
    list_modules,
    list_pack_documents,
    list_packs,
    list_projects,
    read_pack_document,
)
from moddular_graph.qa import answer_pack_question
from moddular_graph.store import index_all_packs, search_documents


client = TestClient(app)
TEST_ADMIN_PASSWORD = os.environ["MODDULAR_GRAPH_ADMIN_YTHONG_PASSWORD"]


def _sample_pack_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "opencrab-cloud-pack-v1",
                    "pack_id": "sample-upload-pack",
                    "title": "Sample Upload Pack",
                    "entrypoints": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl"},
                    "counts": {"nodes": 2, "edges": 1, "documents": 1},
                }
            ),
        )
        zf.writestr(
            "graph/nodes.jsonl",
            "\n".join(
                [
                    json.dumps({"id": "doc:sample", "labels": ["Document"], "properties": {"title": "Sample"}}),
                    json.dumps({"id": "chunk:sample", "labels": ["Chunk"], "properties": {"chunk_index": 1}}),
                ]
            ),
        )
        zf.writestr(
            "graph/edges.jsonl",
            json.dumps({"source": "doc:sample", "target": "chunk:sample", "relation": "contains_chunk"}),
        )
        zf.writestr("documents/sample.md", "Sample Beam evidence for upload validation.")
    return buffer.getvalue()


def test_discovers_existing_revit_and_advance_steel_packs() -> None:
    packs = list_packs()
    ids = {pack["id"] for pack in packs}

    assert "advance-steel-samcheok-bldg-b-bm25-evidence-pack" in ids
    assert "revit-yeoju-ar-ifc-workset-module-localcrab-pack" in ids
    assert all(pack["counts"]["documents"] > 0 for pack in packs)


def test_builds_graph_from_both_pack_shapes() -> None:
    packs = {pack["id"]: pack for pack in list_packs()}

    advance = build_graph("advance-steel-samcheok-bldg-b-bm25-evidence-pack", max_nodes=120, max_edges=240)
    revit = build_graph("revit-yeoju-ar-ifc-workset-module-localcrab-pack", max_nodes=120, max_edges=240)

    assert advance["stats"]["visibleNodes"] > 0
    assert advance["stats"]["visibleEdges"] > 0
    assert advance["stats"]["totalNodes"] == packs["advance-steel-samcheok-bldg-b-bm25-evidence-pack"]["counts"]["nodes"]
    assert revit["stats"]["visibleNodes"] > 0
    assert revit["stats"]["visibleEdges"] > 0
    assert revit["stats"]["totalNodes"] == packs["revit-yeoju-ar-ifc-workset-module-localcrab-pack"]["counts"]["nodes"]


def test_projects_replace_marketplace_with_project_pack_grouping() -> None:
    projects = list_projects()

    assert {project["id"] for project in projects} == {"samcheok-building-b", "yeoju-modular-dormitory"}
    assert all(project["packIds"] for project in projects)
    assert all(project["role"] == "Admin" for project in projects)


def test_admin_project_crud_and_pack_link_management(monkeypatch, tmp_path) -> None:
    from moddular_graph import project_store

    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")

    admin = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    token = admin.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/admin/projects",
        headers=headers,
        json={
            "name": "Client Plant A",
            "company": "Client Co",
            "manager": "Site Manager",
            "discipline": "Advance Steel",
            "description": "Client-visible project",
            "pack_ids": ["advance-steel-samcheok-bldg-b-bm25-evidence-pack"],
        },
    )
    project_id = created.json()["project"]["id"]
    linked = client.post(
        f"/api/admin/projects/{project_id}/packs",
        headers=headers,
        json={"pack_ids": ["revit-yeoju-ar-ifc-workset-module-localcrab-pack"]},
    )
    updated = client.put(
        f"/api/admin/projects/{project_id}",
        headers=headers,
        json={
            "name": "Client Plant A Rev",
            "company": "Client Co",
            "manager": "Site Manager",
            "discipline": "Revit IFC",
            "description": "Updated project",
            "pack_ids": ["revit-yeoju-ar-ifc-workset-module-localcrab-pack"],
        },
    )
    deleted = client.delete(f"/api/admin/projects/{project_id}", headers=headers)

    assert created.status_code == 200
    assert created.json()["project"]["company"] == "Client Co"
    assert linked.status_code == 200
    assert linked.json()["packIds"] == ["revit-yeoju-ar-ifc-workset-module-localcrab-pack"]
    assert updated.status_code == 200
    assert updated.json()["project"]["name"] == "Client Plant A Rev"
    assert deleted.status_code == 200


def test_query_api_returns_evidence() -> None:
    response = client.post(
        "/api/query",
        json={"pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack", "question": "Beam"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence"]
    assert payload["mode"] == "local-graph-rag"
    assert "Local Graph RAG result" in payload["answer"]
    assert "graphContext" in payload


def test_fastapi_serves_web_shell_or_build_hint() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Modular Ontology" in response.text or "Modular Graph" in response.text


def test_auth_sessions_distinguish_admin_and_member(monkeypatch, tmp_path) -> None:
    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))

    admin = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    signup = client.post(
        "/api/auth/register",
        json={"email": "new.member@example.com", "password": "member123!", "name": "New Member"},
    )
    pending_login = client.post(
        "/api/auth/login",
        json={"email": "new.member@example.com", "password": "member123!"},
    )
    bad = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": "wrong"},
    )

    assert admin.status_code == 200
    assert admin.json()["user"]["role"] == "admin"
    assert signup.status_code == 200
    assert signup.json()["user"]["status"] == "pending"
    assert pending_login.status_code == 401

    approved = client.post(
        "/api/admin/users/new.member@example.com/approve",
        headers={"Authorization": f"Bearer {admin.json()['token']}"},
        json={"role": "member"},
    )
    member = client.post(
        "/api/auth/login",
        json={"email": "new.member@example.com", "password": "member123!"},
    )

    assert approved.status_code == 200
    assert member.status_code == 200
    assert member.json()["user"]["role"] == "member"
    assert member.json()["user"]["status"] == "active"
    assert bad.status_code == 401

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {admin.json()['token']}"})
    assert me.json()["authenticated"] is True
    assert me.json()["user"]["role"] == "admin"


def test_signup_company_does_not_create_managed_company_card(monkeypatch, tmp_path) -> None:
    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))

    admin = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    token = admin.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    signup = client.post(
        "/api/auth/register",
        json={
            "email": "temu.user@example.com",
            "password": "member123!",
            "name": "Temu User",
            "company": "temu",
        },
    )
    companies = client.get("/api/admin/companies", headers=headers)
    move_to_unmanaged = client.post(
        "/api/admin/users/temu.user@example.com/company",
        headers={**headers, "Content-Type": "application/json"},
        json={"company": "temu"},
    )
    added = client.post("/api/admin/companies", headers=headers, json={"name": "temu"})
    move_to_managed = client.post(
        "/api/admin/users/temu.user@example.com/company",
        headers={**headers, "Content-Type": "application/json"},
        json={"company": "temu"},
    )

    assert signup.status_code == 200
    assert companies.status_code == 200
    assert "temu" not in companies.json()["companies"]
    assert move_to_unmanaged.status_code == 400
    assert added.status_code == 200
    assert move_to_managed.status_code == 200


def test_admin_can_delete_company_with_confirmed_users_and_delete_members(monkeypatch, tmp_path) -> None:
    from moddular_graph.auth import _hash_password

    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "companies": ["Delete Co"],
                "users": [
                    {
                        "id": "delete-member",
                        "name": "Delete Member",
                        "email": "delete.member@example.com",
                        "company": "Delete Co",
                        "role": "member",
                        "status": "active",
                        "password_hash": _hash_password("member123!"),
                    },
                    {
                        "id": "remove-member",
                        "name": "Remove Member",
                        "email": "remove.member@example.com",
                        "company": "Kumkang Kind",
                        "role": "member",
                        "status": "active",
                        "password_hash": _hash_password("member123!"),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))
    admin_login = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    token = admin_login.json()["token"]

    blocked = client.delete("/api/admin/companies/Delete Co", headers={"Authorization": f"Bearer {token}"})
    deleted_company = client.delete(
        "/api/admin/companies/Delete Co?delete_users=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    deleted_user = client.delete(
        "/api/admin/users/remove.member@example.com",
        headers={"Authorization": f"Bearer {token}"},
    )
    self_delete = client.delete(
        "/api/admin/users/ythong@kumkangkind.com",
        headers={"Authorization": f"Bearer {token}"},
    )
    users = client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"}).json()["users"]

    assert blocked.status_code == 400
    assert deleted_company.status_code == 200
    assert deleted_company.json()["deletedUsers"] == 1
    assert deleted_user.status_code == 200
    assert self_delete.status_code == 400
    assert {user["email"] for user in users}.isdisjoint(
        {"delete.member@example.com", "remove.member@example.com"}
    )


def test_external_company_only_sees_assigned_projects_and_graphs(monkeypatch, tmp_path) -> None:
    from moddular_graph.auth import _hash_password

    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "companies": ["Client Co"],
                "company_project_access": {"Client Co": ["yeoju-modular-dormitory"]},
                "users": [
                    {
                        "id": "client-member",
                        "name": "Client Member",
                        "email": "client.member@example.com",
                        "company": "Client Co",
                        "role": "member",
                        "status": "active",
                        "password_hash": _hash_password("member123!"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))

    login = client.post(
        "/api/auth/login",
        json={"email": "client.member@example.com", "password": "member123!"},
    )
    token = login.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    projects_response = client.get("/api/projects", headers=headers)
    packs_response = client.get("/api/packs", headers=headers)
    blocked_graph = client.get(
        "/api/graph/advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        headers=headers,
    )
    allowed_graph = client.get(
        "/api/graph/revit-yeoju-ar-ifc-workset-module-localcrab-pack?max_nodes=5&max_edges=5",
        headers=headers,
    )

    assert projects_response.status_code == 200
    assert [project["id"] for project in projects_response.json()] == ["yeoju-modular-dormitory"]
    assert [pack["id"] for pack in packs_response.json()] == ["revit-yeoju-ar-ifc-workset-module-localcrab-pack"]
    assert blocked_graph.status_code == 403
    assert allowed_graph.status_code == 200


def test_auth_can_load_users_from_json_config(monkeypatch, tmp_path) -> None:
    from moddular_graph.auth import _hash_password, authenticate, load_users

    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "id": "config-admin",
                        "name": "Configured Admin",
                        "email": "configured.admin@example.com",
                        "role": "admin",
                        "password_hash": _hash_password("configured-admin-pass"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))

    users = load_users()
    token, user = authenticate("configured.admin@example.com", "configured-admin-pass")

    assert users["configured.admin@example.com"].role == "admin"
    assert token
    assert user.id == "config-admin"


def test_pack_upload_is_session_admin_only_and_accepts_valid_zip(monkeypatch, tmp_path) -> None:
    import moddular_graph.pack_index as pack_index
    import moddular_graph.store as store
    from moddular_graph.auth import _hash_password

    monkeypatch.setattr(pack_index, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "uploaded-index.sqlite3")
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "id": "active-member",
                        "name": "Active Member",
                        "email": "active.member@example.com",
                        "role": "member",
                        "status": "active",
                        "password_hash": _hash_password("member123!"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))
    admin_login = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    member_login = client.post(
        "/api/auth/login",
        json={"email": "active.member@example.com", "password": "member123!"},
    )
    admin_token = admin_login.json()["token"]
    member_token = member_login.json()["token"]

    forbidden = client.post(
        "/api/packs/upload",
        headers={"Authorization": f"Bearer {member_token}"},
        files={"file": ("sample.zip", _sample_pack_bytes(), "application/zip")},
    )
    allowed = client.post(
        "/api/packs/upload",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("sample.zip", _sample_pack_bytes(), "application/zip")},
    )

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["id"] == "sample-upload-pack"
    assert allowed.json()["ingest"]["status"] == "indexed"
    assert allowed.json()["ingest"]["documents"] == 1
    assert allowed.json()["ingest"]["nodes"] == 2
    assert allowed.json()["ingest"]["edges"] == 1
    assert (tmp_path / "sample.zip").exists()


def test_mcp_tools_return_json_payloads() -> None:
    packs = json.loads(mcp_server.list_packs())
    projects = json.loads(mcp_server.list_projects())
    graph = json.loads(mcp_server.get_graph("revit-yeoju-ar-ifc-workset-module-localcrab-pack", 20, 40))
    evidence = json.loads(mcp_server.search_pack("advance-steel-samcheok-bldg-b-bm25-evidence-pack", "Beam", 2))
    answer = json.loads(mcp_server.ask_pack_question("advance-steel-samcheok-bldg-b-bm25-evidence-pack", "Beam", 2))
    modules = json.loads(mcp_server.list_modules("advance-steel-samcheok-bldg-b-bm25-evidence-pack"))
    fasteners = json.loads(mcp_server.get_fasteners("advance-steel-samcheok-bldg-b-bm25-evidence-pack"))

    assert packs
    assert projects
    assert graph["nodes"]
    assert isinstance(evidence, list)
    assert evidence
    assert answer["mode"] == "local-graph-rag"
    assert answer["evidence"]
    assert modules["module_count"] == 24
    assert fasteners["bolt_quantity"] == 1011


def test_mcp_company_scope_filters_projects_and_packs(monkeypatch, tmp_path) -> None:
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "companies": ["Client Co"],
                "company_project_access": {"Client Co": ["yeoju-modular-dormitory"]},
                "users": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))
    monkeypatch.setenv("MODULAR_GRAPH_MCP_COMPANY", "Client Co")

    projects = json.loads(mcp_server.list_projects())
    packs = json.loads(mcp_server.list_packs())
    allowed_graph = json.loads(mcp_server.get_graph("revit-yeoju-ar-ifc-workset-module-localcrab-pack", 5, 5))
    blocked_graph = json.loads(mcp_server.get_graph("advance-steel-samcheok-bldg-b-bm25-evidence-pack", 5, 5))

    assert [project["id"] for project in projects] == ["yeoju-modular-dormitory"]
    assert [pack["id"] for pack in packs] == ["revit-yeoju-ar-ifc-workset-module-localcrab-pack"]
    assert allowed_graph["stats"]["visibleNodes"] > 0
    assert blocked_graph["error"] == "forbidden"


def test_mcp_stdio_server_lists_and_calls_tools() -> None:
    async def run_client() -> None:
        root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "moddular_graph.mcp_server"],
            cwd=root,
            env={"MODDULAR_GRAPH_ROOT": str(root)},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                result = await session.call_tool(
                    "ask_pack_question",
                    {
                        "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
                        "question": "Beam",
                        "limit": 2,
                    },
                )
                payload = json.loads(result.content[0].text)

        assert {
            "list_projects",
            "list_packs",
            "get_graph",
            "search_pack",
            "ask_pack_question",
            "list_pack_documents",
            "read_pack_document",
            "list_modules",
            "get_fasteners",
        }.issubset(names)
        assert payload["mode"] == "local-graph-rag"
        assert payload["evidence"]

    anyio.run(run_client)


def test_direct_evidence_mcp_helpers_read_zip_without_index() -> None:
    pack_id = "advance-steel-samcheok-bldg-b-bm25-evidence-pack"

    docs = list_pack_documents(pack_id, prefix="documents/modules/", suffix=".md", limit=100)
    rf_truss = read_pack_document(pack_id, "documents/modules/RF-TRUSS.md", max_chars=5000)

    assert docs["count"] == 24
    assert "documents/modules/RF-TRUSS.md" in docs["documents"]
    assert rf_truss["path"] == "documents/modules/RF-TRUSS.md"
    assert "module_type: TRUSS" in rf_truss["content"]


def test_bim_specialized_helpers_parse_modules_and_fasteners() -> None:
    pack_id = "advance-steel-samcheok-bldg-b-bm25-evidence-pack"

    modules = list_modules(pack_id)
    rf_truss = get_module(pack_id, "RF-TRUSS")
    fasteners = get_fasteners(pack_id)

    assert modules["module_count"] == 24
    assert any(module["module_id"] == "RF-TRUSS" for module in modules["modules"])
    assert rf_truss["module_id"] == "RF-TRUSS"
    assert rf_truss["module_type"] == "TRUSS"
    assert rf_truss["assembly_count"] == 72
    assert rf_truss["single_part_count"] == 420
    assert rf_truss["total_weight_kg"] == 6092.78
    assert fasteners["bolt_quantity"] == 1011
    assert fasteners["anchor_quantity"] == 314
    assert fasteners["bolts_by_spec"]


def test_sqlite_index_persists_pack_documents_and_graph(tmp_path) -> None:
    db_path = tmp_path / "index.sqlite3"

    result = index_all_packs(db_path=db_path)
    evidence = search_documents("advance-steel-samcheok-bldg-b-bm25-evidence-pack", "Beam", limit=2, db_path=db_path)

    assert result["stats"]["packs"] >= 2
    assert result["stats"]["documents"] > 0
    assert result["stats"]["nodes"] > 0
    assert result["stats"]["edges"] > 0
    assert evidence


def test_local_graph_rag_answer_uses_evidence_and_graph_context(tmp_path) -> None:
    db_path = tmp_path / "qa-index.sqlite3"
    index_all_packs(db_path=db_path)

    answer = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "Beam",
        limit=4,
        db_path=db_path,
    )

    assert answer["mode"] == "local-graph-rag"
    assert "Local Graph RAG result" in answer["answer"]
    assert answer["evidence"]
    assert answer["graphContext"]["nodes"]


def test_openai_graph_rag_path_uses_injected_client(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        output_text = "Synthesized answer from provided ontology evidence."

    class FakeResponses:
        def __init__(self) -> None:
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return FakeResponse()

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    db_path = tmp_path / "openai-qa-index.sqlite3"
    index_all_packs(db_path=db_path)
    client_stub = FakeClient()
    monkeypatch.setenv("MODDULAR_GRAPH_OPENAI_MODEL", "test-model")

    answer = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "Beam",
        limit=2,
        db_path=db_path,
        use_openai=True,
        llm_client=client_stub,
    )

    assert answer["mode"] == "openai-graph-rag"
    assert answer["answer"] == "Synthesized answer from provided ontology evidence."
    assert client_stub.responses.calls[0]["model"] == "test-model"
    assert "evidence" in client_stub.responses.calls[0]["input"]


def test_ollama_graph_rag_path_uses_injected_client(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_ollama(**kwargs):
        calls.append(kwargs)
        return "Local Qwen answer from ontology evidence."

    db_path = tmp_path / "ollama-qa-index.sqlite3"
    index_all_packs(db_path=db_path)
    monkeypatch.setenv("MODDULAR_GRAPH_OLLAMA_MODEL", "qwen3:14b")

    answer = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "Beam",
        limit=2,
        db_path=db_path,
        use_ollama=True,
        llm_client=fake_ollama,
    )

    assert answer["mode"] == "ollama-qwen-graph-rag"
    assert answer["answer"] == "Local Qwen answer from ontology evidence."
    assert calls[0]["model"] == "qwen3:14b"
    assert "evidence" in calls[0]["prompt"]


def test_admin_reindex_api_requires_admin_session(monkeypatch, tmp_path) -> None:
    import moddular_graph.store as store
    from moddular_graph.auth import _hash_password

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "api-index.sqlite3")
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "id": "active-member",
                        "name": "Active Member",
                        "email": "active.member@example.com",
                        "role": "member",
                        "status": "active",
                        "password_hash": _hash_password("member123!"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODDULAR_GRAPH_USERS_FILE", str(users_file))
    admin_login = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    member_login = client.post(
        "/api/auth/login",
        json={"email": "active.member@example.com", "password": "member123!"},
    )

    forbidden = client.post(
        "/api/admin/reindex",
        headers={"Authorization": f"Bearer {member_login.json()['token']}"},
    )
    allowed = client.post(
        "/api/admin/reindex",
        headers={"Authorization": f"Bearer {admin_login.json()['token']}"},
    )

    assert forbidden.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["stats"]["documents"] > 0
