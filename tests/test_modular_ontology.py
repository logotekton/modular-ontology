from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import zipfile
from pathlib import Path

os.environ.setdefault("MODULAR_ONTOLOGY_ADMIN_YTHONG_PASSWORD", "test-admin-password")
os.environ.setdefault("MODULAR_ONTOLOGY_ADMIN_MWHONG_PASSWORD", "test-admin-password-2")

import anyio
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from modular_ontology import mcp_server
from modular_ontology.app import app
from modular_ontology.auth import User, _hash_password
from modular_ontology.mcp_tokens import build_user_mcp_urls, ensure_mcp_token_for_user, get_mcp_token_record, regenerate_mcp_token_for_user
from modular_ontology.pack_index import (
    build_graph,
    build_multi_pack_graph,
    get_fasteners,
    get_module,
    list_modules,
    list_pack_documents,
    list_packs,
    list_projects,
    read_pack_document,
)
from modular_ontology.qa import answer_pack_question
from modular_ontology.store import index_all_packs, search_documents
from modular_ontology.google_drive_sync import (
    DriveItem,
    GoogleDriveClient,
    ensure_project_drive_folders,
    google_drive_sync_status,
    restore_ifc_files_from_drive,
    sync_google_drive_registry_files,
    sync_google_drive_storage,
    write_back_ifc_file,
    write_back_pack_file,
)
from modular_ontology.drive_xkt_worker import convert_missing_drive_xkts


client = TestClient(app)
TEST_ADMIN_PASSWORD = os.environ["MODULAR_ONTOLOGY_ADMIN_YTHONG_PASSWORD"]


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


def test_builds_project_graph_from_multiple_packs() -> None:
    pack_ids = [
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "revit-yeoju-ar-ifc-workset-module-localcrab-pack",
    ]

    graph = build_multi_pack_graph(pack_ids, title="Combined Project", max_nodes=120, max_edges=240)

    assert graph["pack"]["title"] == "Combined Project"
    assert graph["activePackIds"] == pack_ids
    assert {pack["id"] for pack in graph["packs"]} == set(pack_ids)
    assert {node["packId"] for node in graph["nodes"]} == set(pack_ids)
    assert all("::" in node["id"] for node in graph["nodes"])
    assert graph["stats"]["visibleNodes"] > 0
    assert graph["stats"]["totalNodes"] >= graph["stats"]["visibleNodes"]


def test_projects_replace_marketplace_with_project_pack_grouping() -> None:
    projects = list_projects()
    project_by_id = {project["id"]: project for project in projects}

    assert set(project_by_id) >= {"samcheok-building-b", "yeoju-modular-dormitory"}
    assert project_by_id["samcheok-building-b"]["packIds"]
    assert project_by_id["yeoju-modular-dormitory"]["packIds"]
    assert all(project["role"] == "Admin" for project in projects)


def test_index_status_includes_landing_kpi_counts() -> None:
    response = client.get("/api/index/status")
    payload = response.json()

    assert response.status_code == 200
    assert payload["users"] >= 0
    assert payload["projects"] >= 0
    assert payload["ifcModels"] >= 0
    assert payload["packs"] >= 0


def test_project_graph_api_returns_project_scoped_graph(monkeypatch) -> None:
    from modular_ontology import app as app_module

    admin_user = User(
        id="admin-test",
        name="Admin",
        email="admin@example.com",
        company="Kumkang Kind",
        role="admin",
        password_hash="unused",
        status="active",
    )
    monkeypatch.setattr(app_module, "current_user", lambda authorization: admin_user if authorization else None)
    headers = {"Authorization": "Bearer admin-test-token"}
    anonymous = client.get("/api/projects/samcheok-building-b/graph?max_nodes=20&max_edges=40")
    response = client.get("/api/projects/samcheok-building-b/graph?max_nodes=20&max_edges=40", headers=headers)
    invalid_pack = client.get(
        "/api/projects/samcheok-building-b/graph?pack_ids=revit-yeoju-ar-ifc-workset-module-localcrab-pack",
        headers=headers,
    )

    assert anonymous.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["id"] == "samcheok-building-b"
    assert payload["activePackIds"] == ["advance-steel-samcheok-bldg-b-bm25-evidence-pack"]
    assert payload["nodes"]
    assert invalid_pack.status_code == 400


def test_ifc_upload_stores_project_file_for_admin(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module

    admin_user = User(
        id="admin-test",
        name="Admin",
        email="admin@example.com",
        company="Kumkang Kind",
        role="admin",
        password_hash="unused",
        status="active",
    )
    monkeypatch.setattr(app_module, "IFC_UPLOAD_DIR", tmp_path / "ifc")
    monkeypatch.setenv("MODULAR_ONTOLOGY_DISABLE_DEFAULT_XKT_CONVERTER", "1")
    monkeypatch.delenv("MODULAR_ONTOLOGY_XKT_CONVERTER_CMD", raising=False)
    monkeypatch.delenv("XKT_CONVERTER_CMD", raising=False)
    monkeypatch.setattr(app_module, "require_admin", lambda authorization: admin_user)
    monkeypatch.setattr(app_module, "current_user", lambda authorization: admin_user)
    headers = {"Authorization": "Bearer admin-test-token"}

    response = client.post(
        "/api/ifc/upload",
        headers=headers,
        data={"project_id": "samcheok-building-b"},
        files={"file": ("sample.ifc", b"ISO-10303-21;", "application/octet-stream")},
    )
    invalid = client.post(
        "/api/ifc/upload",
        headers=headers,
        data={"project_id": "samcheok-building-b"},
        files={"file": ("sample.txt", b"not-ifc", "text/plain")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "stored"
    assert payload["projectId"] == "samcheok-building-b"
    assert payload["filename"] == "sample.ifc"
    assert (tmp_path / "ifc" / "samcheok-building-b" / "files" / "sample.ifc").read_bytes() == b"ISO-10303-21;"
    metadata = json.loads((tmp_path / "ifc" / "samcheok-building-b" / "metadata" / "sample.metadata.json").read_text(encoding="utf-8"))
    assert metadata["projectId"] == "samcheok-building-b"
    assert metadata["storage"] == "local"
    assert invalid.status_code == 400

    listed = client.get("/api/ifc/models", headers=headers)
    assert listed.status_code == 200
    model = listed.json()[0]
    assert model["id"] == "samcheok-building-b/sample.metadata.json"
    assert model["filename"] == "sample.ifc"
    assert model["viewerStatus"] == "pending-xkt"

    manifest = client.get(
        "/api/ifc/model-viewer/manifest",
        headers=headers,
        params={"model_id": model["id"]},
    )
    assert manifest.status_code == 200
    assert manifest.json()["status"] == "pending-xkt"

    linked = client.post(
        "/api/admin/ifc/models/link",
        headers=headers,
        json={"model_id": model["id"], "project_id": "yeoju-modular-dormitory"},
    )
    assert linked.status_code == 200
    linked_model = linked.json()["model"]
    assert linked_model["projectId"] == "yeoju-modular-dormitory"
    assert linked_model["projectName"] == "Yeoju Modular Dormitory"
    assert (tmp_path / "ifc" / "yeoju-modular-dormitory" / "files" / "sample.ifc").read_bytes() == b"ISO-10303-21;"
    assert (tmp_path / "ifc" / "yeoju-modular-dormitory" / "metadata" / "sample.metadata.json").exists()


def test_xkt_upload_is_ready_for_model_viewer(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module

    admin_user = User(
        id="admin-test",
        name="Admin",
        email="admin@example.com",
        company="Kumkang Kind",
        role="admin",
        password_hash="unused",
        status="active",
    )
    monkeypatch.setattr(app_module, "IFC_UPLOAD_DIR", tmp_path / "ifc")
    monkeypatch.setenv("MODULAR_ONTOLOGY_DISABLE_DEFAULT_XKT_CONVERTER", "1")
    monkeypatch.delenv("MODULAR_ONTOLOGY_XKT_CONVERTER_CMD", raising=False)
    monkeypatch.delenv("XKT_CONVERTER_CMD", raising=False)
    monkeypatch.setattr(app_module, "require_admin", lambda authorization: admin_user)
    monkeypatch.setattr(app_module, "current_user", lambda authorization: admin_user)
    headers = {"Authorization": "Bearer admin-test-token"}

    response = client.post(
        "/api/ifc/upload",
        headers=headers,
        data={"project_id": "samcheok-building-b"},
        files={"file": ("sample.xkt", b"xkt-bytes", "application/octet-stream")},
    )
    assert response.status_code == 200
    model = client.get("/api/ifc/models", headers=headers).json()[0]
    assert model["viewerStatus"] == "ready"

    manifest = client.get(
        "/api/ifc/model-viewer/manifest",
        headers=headers,
        params={"model_id": model["id"]},
    )
    assert manifest.status_code == 200
    assert manifest.json()["status"] == "ready"
    assert manifest.json()["xktUrl"]

    asset = client.get(
        "/api/ifc/model-viewer/asset",
        headers=headers,
        params={"model_id": model["id"]},
    )
    assert asset.status_code == 200
    assert asset.content == b"xkt-bytes"


def test_ifc_upload_uses_configured_xkt_converter(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module

    admin_user = User(
        id="admin-test",
        name="Admin",
        email="admin@example.com",
        company="Kumkang Kind",
        role="admin",
        password_hash="unused",
        status="active",
    )
    converter = tmp_path / "fake_converter.py"
    converter.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[sys.argv.index('--out') + 1]).write_bytes(b'converted-xkt')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "IFC_UPLOAD_DIR", tmp_path / "ifc")
    monkeypatch.setenv("MODULAR_ONTOLOGY_XKT_CONVERTER_CMD", f"{sys.executable} {converter} --in {{ifc}} --out {{xkt}}")
    monkeypatch.setattr(app_module, "require_admin", lambda authorization: admin_user)
    monkeypatch.setattr(app_module, "current_user", lambda authorization: admin_user)
    headers = {"Authorization": "Bearer admin-test-token"}

    response = client.post(
        "/api/ifc/upload",
        headers=headers,
        data={"project_id": "samcheok-building-b"},
        files={"file": ("sample.ifc", b"ISO-10303-21;", "application/octet-stream")},
    )

    assert response.status_code == 200
    model = client.get("/api/ifc/models", headers=headers).json()[0]
    assert model["viewerStatus"] == "ready"
    asset = client.get(
        "/api/ifc/model-viewer/asset",
        headers=headers,
        params={"model_id": model["id"]},
    )
    assert asset.status_code == 200
    assert asset.content == b"converted-xkt"


def test_drive_managed_ifc_model_cannot_be_reassigned_in_app(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module

    admin_user = User(
        id="admin-test",
        name="Admin",
        email="admin@example.com",
        company="Kumkang Kind",
        role="admin",
        password_hash="unused",
        status="active",
    )
    model_dir = tmp_path / "ifc" / "samcheok-building-b"
    metadata_dir = model_dir / "metadata"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "sample.metadata.json").write_text(
        json.dumps(
            {
                "filename": "sample.ifc",
                "projectId": "samcheok-building-b",
                "projectName": "Samcheok Building B",
                "storage": "google-drive",
                "localPath": str(model_dir / "files" / "sample.ifc"),
                "drive": {"file": {"folder": "02_Projects/samcheok-building-b/ifc-models"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "IFC_UPLOAD_DIR", tmp_path / "ifc")
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(app_module, "run_google_drive_sync", lambda *args, **kwargs: {"status": "synced"})
    monkeypatch.setattr(app_module, "require_admin", lambda authorization: admin_user)
    monkeypatch.setattr(app_module, "current_user", lambda authorization: admin_user)

    response = client.post(
        "/api/admin/ifc/models/link",
        headers={"Authorization": "Bearer admin-test-token"},
        json={"model_id": "samcheok-building-b/sample.metadata.json", "project_id": "yeoju-modular-dormitory"},
    )

    assert response.status_code == 400
    assert "Google Drive" in response.json()["detail"]


def test_unassigned_xkt_asset_requires_user(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module

    monkeypatch.setattr(app_module, "IFC_UPLOAD_DIR", tmp_path / "ifc")
    monkeypatch.setattr(app_module, "current_user", lambda authorization: None)
    model_dir = tmp_path / "ifc" / "_unassigned"
    model_dir.mkdir(parents=True)
    (model_dir / "sample.xkt").write_bytes(b"xkt-bytes")
    (model_dir / "sample.metadata.json").write_text(
        json.dumps(
            {
                "id": "_unassigned/sample.metadata.json",
                "filename": "sample.xkt",
                "sizeBytes": 9,
                "uploadedAt": 1,
                "storage": "local",
                "localPath": str(model_dir / "sample.xkt"),
                "viewerStatus": "ready",
                "xktPath": str(model_dir / "sample.xkt"),
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/api/ifc/model-viewer/asset", params={"model_id": "_unassigned/sample.metadata.json"})

    assert response.status_code == 401


def test_assigned_xkt_model_viewer_requires_user(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module

    monkeypatch.setattr(app_module, "IFC_UPLOAD_DIR", tmp_path / "ifc")
    monkeypatch.setattr(app_module, "current_user", lambda authorization: None)
    model_dir = tmp_path / "ifc" / "samcheok-building-b"
    model_dir.mkdir(parents=True)
    (model_dir / "sample.xkt").write_bytes(b"xkt-bytes")
    (model_dir / "sample.metadata.json").write_text(
        json.dumps(
            {
                "id": "samcheok-building-b/sample.metadata.json",
                "filename": "sample.xkt",
                "sizeBytes": 9,
                "uploadedAt": 1,
                "storage": "local",
                "localPath": str(model_dir / "sample.xkt"),
                "projectId": "samcheok-building-b",
                "projectName": "Samcheok Building B",
                "viewerStatus": "ready",
                "xktPath": str(model_dir / "sample.xkt"),
            }
        ),
        encoding="utf-8",
    )

    manifest = client.get(
        "/api/ifc/model-viewer/manifest",
        params={"model_id": "samcheok-building-b/sample.metadata.json"},
    )
    asset = client.get(
        "/api/ifc/model-viewer/asset",
        params={"model_id": "samcheok-building-b/sample.metadata.json"},
    )

    assert manifest.status_code == 401
    assert asset.status_code == 401


def test_ifc_drive_write_back_creates_project_folders(monkeypatch, tmp_path) -> None:
    class FakeDriveClient:
        def __init__(self) -> None:
            self.children = {"root": []}
            self.created = []
            self.uploads = []

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def create_folder(self, parent_id, name):
            folder_id = f"{parent_id}/{name}"
            item = DriveItem(folder_id, name, "application/vnd.google-apps.folder")
            self.children.setdefault(parent_id, []).append(item)
            self.children.setdefault(folder_id, [])
            self.created.append((parent_id, name))
            return item

        def upload_file_by_name(self, folder_id, source, name=None, mime_type=None):
            self.uploads.append({"folder_id": folder_id, "source": Path(source), "name": name, "mime_type": mime_type})
            return {"status": "created", "id": "drive-file", "name": name or Path(source).name}

    ifc_path = tmp_path / "sample.ifc"
    ifc_path.write_bytes(b"ISO-10303-21;")
    fake_client = FakeDriveClient()
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")

    result = write_back_ifc_file(ifc_path, "samcheok-building-b", client=fake_client)

    assert result["status"] == "written"
    assert result["drivePath"] == "02_Projects/samcheok-building-b/ifc-models/sample.ifc"
    assert fake_client.created == [
        ("root", "02_Projects"),
        ("root/02_Projects", "samcheok-building-b"),
        ("root/02_Projects/samcheok-building-b", "ifc-models"),
    ]
    assert fake_client.uploads[0]["folder_id"] == "root/02_Projects/samcheok-building-b/ifc-models"


def test_ensure_project_drive_folders_creates_default_upload_folders(monkeypatch) -> None:
    class FakeDriveClient:
        def __init__(self) -> None:
            self.children = {"root": []}
            self.created = []
            self.uploads = []

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def create_folder(self, parent_id, name):
            folder_id = f"{parent_id}/{name}"
            item = DriveItem(folder_id, name, "application/vnd.google-apps.folder")
            self.children.setdefault(parent_id, []).append(item)
            self.children.setdefault(folder_id, [])
            self.created.append((parent_id, name))
            return item

        def upload_file_by_name(self, folder_id, source, name=None, mime_type=None):
            file_name = name or Path(source).name
            item = DriveItem(f"{folder_id}/{file_name}", file_name, mime_type or "text/markdown")
            self.children.setdefault(folder_id, []).append(item)
            self.uploads.append({"folder_id": folder_id, "name": file_name, "mime_type": mime_type})
            return {"status": "created", "id": item.id, "name": file_name}

    fake_client = FakeDriveClient()
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")

    result = ensure_project_drive_folders("client-plant-a", client=fake_client)

    assert result["status"] == "ensured"
    assert result["paths"] == [
        "02_Projects/client-plant-a",
        "02_Projects/client-plant-a/ifc-models",
        "02_Projects/client-plant-a/ontology-packs",
    ]
    assert fake_client.created == [
        ("root", "02_Projects"),
        ("root/02_Projects", "client-plant-a"),
        ("root/02_Projects/client-plant-a", "ifc-models"),
        ("root/02_Projects/client-plant-a", "ontology-packs"),
    ]
    assert result["files"] == ["02_Projects/client-plant-a/README.md"]
    assert fake_client.uploads == [
        {
            "folder_id": "root/02_Projects/client-plant-a",
            "name": "README.md",
            "mime_type": "text/markdown; charset=utf-8",
        }
    ]


def test_pack_drive_write_back_needs_project_scope(monkeypatch, tmp_path) -> None:
    pack_path = tmp_path / "sample-pack.zip"
    pack_path.write_bytes(b"zip")
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")

    result = write_back_pack_file(pack_path)

    assert result["status"] == "skipped"
    assert "project_id" in result["reason"]


def test_pack_drive_write_back_uses_project_ontology_pack_folder(monkeypatch, tmp_path) -> None:
    class FakeDriveClient:
        def __init__(self) -> None:
            self.children = {"root": []}
            self.created = []
            self.uploads = []

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def create_folder(self, parent_id, name):
            folder_id = f"{parent_id}/{name}"
            item = DriveItem(folder_id, name, "application/vnd.google-apps.folder")
            self.children.setdefault(parent_id, []).append(item)
            self.children.setdefault(folder_id, [])
            self.created.append((parent_id, name))
            return item

        def upload_file_by_name(self, folder_id, source, name=None, mime_type=None):
            self.uploads.append({"folder_id": folder_id, "source": Path(source), "name": name, "mime_type": mime_type})
            return {"status": "created", "id": "drive-pack", "name": name or Path(source).name}

    pack_path = tmp_path / "sample-pack.zip"
    pack_path.write_bytes(b"zip")
    fake_client = FakeDriveClient()
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")

    result = write_back_pack_file(pack_path, project_id="samcheok-building-b", client=fake_client)

    assert result["status"] == "written"
    assert result["drivePath"] == "02_Projects/samcheok-building-b/ontology-packs/sample-pack.zip"
    assert fake_client.created == [
        ("root", "02_Projects"),
        ("root/02_Projects", "samcheok-building-b"),
        ("root/02_Projects/samcheok-building-b", "ontology-packs"),
    ]
    assert fake_client.uploads[0]["folder_id"] == "root/02_Projects/samcheok-building-b/ontology-packs"


def test_resumable_upload_retries_same_chunk_when_drive_omits_range(monkeypatch, tmp_path) -> None:
    from modular_ontology import google_drive_sync as drive_module

    class FakeResponse:
        def __init__(self, payload: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self._payload = payload
            self.headers = headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return self._payload

    def header_value(request, name: str) -> str:
        for key, value in request.header_items():
            if key.lower() == name.lower():
                return value
        return ""

    source = tmp_path / "large.ifc"
    source.write_bytes(b"a" * (6 * 1024 * 1024))
    put_requests = []

    def fake_urlopen(request, timeout=0):
        if request.get_method() == "POST" and "uploadType=resumable" in request.full_url:
            return FakeResponse(headers={"Location": "https://upload.example/session"})
        if request.get_method() == "PUT":
            put_requests.append(request)
            if len(put_requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 308, "Resume Incomplete", {}, None)
            return FakeResponse(json.dumps({"id": "drive-file", "name": "large.ifc"}).encode("utf-8"))
        raise AssertionError(f"Unexpected request: {request.get_method()} {request.full_url}")

    monkeypatch.setattr(drive_module.urllib.request, "urlopen", fake_urlopen)
    result = GoogleDriveClient(access_token="token").create_file("folder", source, "large.ifc", "application/octet-stream")

    expected_range = f"bytes 0-{source.stat().st_size - 1}/{source.stat().st_size}"
    assert result["id"] == "drive-file"
    assert [header_value(request, "Content-Range") for request in put_requests] == [expected_range, expected_range]


def test_admin_project_crud_and_pack_link_management(monkeypatch, tmp_path) -> None:
    from modular_ontology import project_store

    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
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


def test_admin_create_project_survives_drive_folder_failure(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    from modular_ontology import project_store

    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")

    admin = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(app_module, "run_google_drive_registry_sync", lambda *args, **kwargs: {"enabled": True, "status": "synced"})
    monkeypatch.setattr(app_module, "run_google_drive_write_back", lambda *args, **kwargs: {"enabled": True, "status": "written"})

    def fail_drive_folders(_project_id):
        raise RuntimeError("Drive temporarily unavailable")

    monkeypatch.setattr(app_module, "ensure_project_drive_folders", fail_drive_folders)

    created = client.post(
        "/api/admin/projects",
        headers={"Authorization": f"Bearer {admin.json()['token']}"},
        json={
            "name": "Folder Failure Project",
            "company": "Client Co",
            "manager": "Site Manager",
            "discipline": "Advance Steel",
            "description": "Project must survive Drive folder failure",
            "pack_ids": [],
        },
    )

    assert created.status_code == 200
    assert created.json()["project"]["id"] == "folder-failure-project"
    assert created.json()["driveFolders"]["status"] == "error"
    assert any(project["id"] == "folder-failure-project" for project in created.json()["projects"])


def test_admin_create_project_uses_registry_sync_not_full_storage_sync(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    from modular_ontology import project_store

    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(app_module, "run_google_drive_registry_sync", lambda *args, **kwargs: {"enabled": True, "status": "synced"})

    def fail_full_storage_sync(*_args, **_kwargs):
        raise AssertionError("Project creation should not run full Google Drive storage sync")

    monkeypatch.setattr(app_module, "run_google_drive_sync", fail_full_storage_sync)
    monkeypatch.setattr(app_module, "run_google_drive_write_back", lambda *args, **kwargs: {"enabled": True, "status": "written"})
    monkeypatch.setattr(app_module, "ensure_project_drive_folders", lambda project_id: {"status": "ensured", "projectId": project_id})

    admin = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    created = client.post(
        "/api/admin/projects",
        headers={"Authorization": f"Bearer {admin.json()['token']}"},
        json={
            "name": "Registry Only Project",
            "company": "Client Co",
            "manager": "Site Manager",
            "discipline": "BIM",
            "description": "Project creation should avoid full storage sync",
            "pack_ids": [],
        },
    )

    assert created.status_code == 200
    assert created.json()["project"]["id"] == "registry-only-project"
    assert created.json()["driveFolders"]["status"] == "ensured"


def test_drive_project_pack_links_are_authoritative(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    from modular_ontology import project_store

    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")
    monkeypatch.setattr(
        app_module,
        "list_packs",
        lambda: [
            {"id": "pack-a", "filename": "project-a__pack-a.zip"},
            {"id": "pack-b", "filename": "project-a__pack-b.zip"},
        ],
    )
    monkeypatch.setattr(
        app_module,
        "list_projects",
        lambda: project_store.list_projects([{"id": "pack-a"}, {"id": "pack-b"}]),
    )
    project_store.create_project(name="Project A", pack_ids=["pack-a", "pack-b"])
    marker = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"project-a": ["pack-b"]}), encoding="utf-8")

    applied = app_module._apply_drive_project_pack_links()
    projects = project_store.list_projects([{"id": "pack-a"}, {"id": "pack-b"}])

    assert applied == {"project-a": ["pack-b"]}
    assert projects[0]["packIds"] == ["pack-b"]


def test_drive_project_pack_links_preserve_manual_legacy_links(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    from modular_ontology import project_store

    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")
    monkeypatch.setattr(
        app_module,
        "list_packs",
        lambda: [{"id": "legacy-pack", "filename": "legacy-pack.zip"}],
    )
    monkeypatch.setattr(
        app_module,
        "list_projects",
        lambda: project_store.list_projects([{"id": "legacy-pack"}]),
    )
    project_store.create_project(name="Project A", pack_ids=["legacy-pack"])
    marker = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"project-a": []}), encoding="utf-8")

    applied = app_module._apply_drive_project_pack_links()
    projects = project_store.list_projects([{"id": "legacy-pack"}])

    assert applied == {"project-a": ["legacy-pack"]}
    assert projects[0]["packIds"] == ["legacy-pack"]


def test_drive_common_pack_links_apply_to_all_projects(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    from modular_ontology import project_store

    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")
    monkeypatch.setattr(
        app_module,
        "list_packs",
        lambda: [
            {"id": "project-pack", "filename": "project-a__project-pack.zip"},
            {"id": "common-spec-pack", "filename": "_Common__standard-spec.zip"},
        ],
    )
    monkeypatch.setattr(
        app_module,
        "list_projects",
        lambda: project_store.list_projects([{"id": "project-pack"}, {"id": "common-spec-pack"}]),
    )
    project_store.create_project(name="Project A", pack_ids=["project-pack"])
    project_store.create_project(name="Project B", pack_ids=[])
    marker = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"__common__": ["common-spec-pack"], "project-a": ["project-pack"]}),
        encoding="utf-8",
    )

    applied = app_module._apply_drive_project_pack_links()
    projects = {project["id"]: project for project in project_store.list_projects([{"id": "project-pack"}, {"id": "common-spec-pack"}])}

    assert applied == {
        "__common__": ["common-spec-pack"],
        "project-a": ["common-spec-pack", "project-pack"],
        "project-b": ["common-spec-pack"],
    }
    assert projects["project-a"]["packIds"] == ["common-spec-pack", "project-pack"]
    assert projects["project-b"]["packIds"] == ["common-spec-pack"]


def test_drive_project_folder_rename_updates_project_and_company_access(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    from modular_ontology import project_store
    from modular_ontology.auth import get_company_project_access, invalidate_users_cache, set_company_project_access

    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    invalidate_users_cache()
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")

    project_store.create_project(name="Old Project", pack_ids=["sample-pack"])
    project_store.sync_projects_from_drive_folders(
        [{"folderId": "drive-folder-1", "projectId": "old-project", "name": "Old Project"}]
    )
    set_company_project_access("Client Co", ["old-project"])

    marker = tmp_path / "02_Projects" / ".drive-project-folders.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            [
                {
                    "folderId": "drive-folder-1",
                    "projectId": "renamed-project",
                    "name": "Renamed Project",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = app_module._apply_drive_project_folders()
    projects = project_store.list_projects([{"id": "sample-pack"}])

    assert result["renamed"] == [{"from": "old-project", "to": "renamed-project", "folderId": "drive-folder-1"}]
    assert projects[0]["id"] == "renamed-project"
    assert projects[0]["name"] == "Renamed Project"
    assert projects[0]["driveFolderId"] == "drive-folder-1"
    assert projects[0]["packIds"] == ["sample-pack"]
    assert get_company_project_access("Client Co") == ["renamed-project"]


def test_drive_project_folder_delete_removes_project_and_company_access(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    from modular_ontology import project_store
    from modular_ontology.auth import get_company_project_access, invalidate_users_cache, set_company_project_access

    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    invalidate_users_cache()
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")

    project_store.sync_projects_from_drive_folders(
        [
            {"folderId": "drive-folder-1", "projectId": "active-project", "name": "Active Project"},
            {"folderId": "drive-folder-2", "projectId": "deleted-project", "name": "Deleted Project"},
        ]
    )
    project_store.create_project(name="Legacy Seed Project", pack_ids=["legacy-pack"])
    set_company_project_access("Client Co", ["active-project", "deleted-project"])
    marker = tmp_path / "02_Projects" / ".drive-project-folders.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps([{"folderId": "drive-folder-1", "projectId": "active-project", "name": "Active Project"}]),
        encoding="utf-8",
    )

    result = app_module._apply_drive_project_folders()
    projects = project_store.list_projects([])

    assert set(result["deleted"]) == {"deleted-project", "legacy-seed-project"}
    assert [project["id"] for project in projects] == ["active-project"]
    assert get_company_project_access("Client Co") == ["active-project"]


def test_admin_fast_reindex_applies_drive_project_folder_deletions(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    from modular_ontology import project_store

    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(project_store, "DB_PATH", tmp_path / "projects.sqlite3")
    monkeypatch.setattr(app_module, "require_admin", lambda authorization: None)
    monkeypatch.setattr(app_module, "run_google_drive_registry_sync", lambda *args, **kwargs: {"enabled": True, "status": "synced"})
    monkeypatch.setattr(app_module, "run_google_drive_write_back", lambda *args, **kwargs: {"enabled": True, "status": "written"})

    project_store.sync_projects_from_drive_folders(
        [
            {"folderId": "drive-folder-1", "projectId": "active-project", "name": "Active Project"},
            {"folderId": "drive-folder-2", "projectId": "deleted-project", "name": "Deleted Project"},
        ]
    )
    marker = tmp_path / "02_Projects" / ".drive-project-folders.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps([{"folderId": "drive-folder-1", "projectId": "active-project", "name": "Active Project"}]),
        encoding="utf-8",
    )

    response = app_module.reindex(authorization="Bearer test-token")

    assert response["driveProjects"]["deleted"] == ["deleted-project"]
    assert response["writeBack"]["status"] == "written"
    assert [project["id"] for project in project_store.list_projects([])] == ["active-project"]


def test_query_api_returns_evidence(monkeypatch) -> None:
    from modular_ontology import app as app_module

    admin_user = User(
        id="admin-test",
        name="Admin",
        email="admin@example.com",
        company="Kumkang Kind",
        role="admin",
        password_hash="unused",
        status="active",
    )
    monkeypatch.setattr(app_module, "current_user", lambda authorization: admin_user if authorization else None)
    anonymous = client.post(
        "/api/query",
        json={"pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack", "question": "Beam"},
    )
    response = client.post(
        "/api/query",
        json={"pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack", "question": "Beam"},
        headers={"Authorization": "Bearer admin-test-token"},
    )

    assert anonymous.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence"]
    assert payload["mode"] == "local-graph-rag"
    assert "Local Graph RAG result" in payload["answer"]
    assert "graphContext" in payload


def test_fastapi_serves_web_shell_or_build_hint() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Modular Ontology" in response.text or "Modular Ontology" in response.text


def test_auth_sessions_distinguish_admin_and_member(monkeypatch, tmp_path) -> None:
    users_file = tmp_path / "users.json"
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))

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
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))

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
    from modular_ontology.auth import _hash_password

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
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
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


def test_deleted_default_user_can_re_register_and_show_for_approval(monkeypatch, tmp_path) -> None:
    from modular_ontology.auth import _hash_password, invalidate_users_cache

    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "companies": ["Kumkang Kind"],
                "deleted_users": ["mwhong@kumkangkind.com"],
                "users": [
                    {
                        "id": "rejoin-mwhong",
                        "name": "홍민우",
                        "email": "mwhong@kumkangkind.com",
                        "company": "Kumkang Kind",
                        "role": "member",
                        "status": "pending",
                        "password_hash": _hash_password("member123!"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    invalidate_users_cache()
    admin_login = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    token = admin_login.json()["token"]

    users = client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"}).json()["users"]
    rejoin_user = next(user for user in users if user["email"] == "mwhong@kumkangkind.com")
    deleted = client.delete(
        "/api/admin/users/mwhong@kumkangkind.com",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert rejoin_user["name"] == "홍민우"
    assert rejoin_user["status"] == "pending"
    assert deleted.status_code == 200


def test_deleted_user_can_register_again_and_clears_tombstone(monkeypatch, tmp_path) -> None:
    import json as _json

    from modular_ontology import auth
    from modular_ontology.auth import (
        _load_deleted_users,
        delete_user,
        invalidate_users_cache,
        load_users,
        register_user,
    )

    users_file = tmp_path / "users.json"
    users_file.write_text(_json.dumps({"companies": ["Kumkang Kind"], "users": []}), encoding="utf-8")
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    invalidate_users_cache()

    # Delete a default admin -> creates a deletion tombstone.
    delete_user("mwhong@kumkangkind.com", actor_email="ythong@kumkangkind.com")
    assert "mwhong@kumkangkind.com" in _load_deleted_users(users_file.resolve())
    assert "mwhong@kumkangkind.com" not in load_users()

    # Re-registration should succeed and clear the tombstone so the pending
    # signup is visible to admins for approval.
    user = register_user("mwhong@kumkangkind.com", "member123!", name="홍민우")
    assert user.status == "pending"
    assert "mwhong@kumkangkind.com" not in _load_deleted_users(users_file.resolve())

    reloaded = load_users().get("mwhong@kumkangkind.com")
    assert reloaded is not None
    assert reloaded.status == "pending"
    assert reloaded.name == "홍민우"


def test_external_company_only_sees_assigned_projects_and_graphs(monkeypatch, tmp_path) -> None:
    from modular_ontology.auth import _hash_password

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
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))

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
    from modular_ontology.auth import _hash_password, authenticate, load_users

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
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))

    users = load_users()
    token, user = authenticate("configured.admin@example.com", "configured-admin-pass")

    assert users["configured.admin@example.com"].role == "admin"
    assert token
    assert user.id == "config-admin"


def test_auth_token_survives_empty_memory_session(monkeypatch, tmp_path) -> None:
    import modular_ontology.auth as auth
    from modular_ontology.auth import _hash_password, authenticate, get_user_by_token, invalidate_users_cache

    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "id": "stateless-admin",
                        "name": "Stateless Admin",
                        "email": "stateless.admin@example.com",
                        "role": "admin",
                        "password_hash": _hash_password("stateless-admin-pass"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    monkeypatch.setenv("MODULAR_ONTOLOGY_SESSION_SECRET", "test-session-secret")
    invalidate_users_cache()

    token, user = authenticate("stateless.admin@example.com", "stateless-admin-pass")
    auth.SESSIONS.clear()

    assert token.startswith("v1.")
    assert user.id == "stateless-admin"
    assert get_user_by_token(token).id == "stateless-admin"  # type: ignore[union-attr]


def test_auth_accepts_utf8_bom_user_files(monkeypatch, tmp_path) -> None:
    from modular_ontology.auth import _hash_password, authenticate, invalidate_users_cache

    users_file = tmp_path / "users-with-bom.json"
    payload = json.dumps(
        {
            "users": [
                {
                    "id": "bom-admin",
                    "name": "BOM Admin",
                    "email": "bom.admin@example.com",
                    "role": "admin",
                    "password_hash": _hash_password("bom-admin-pass"),
                }
            ]
        }
    )
    users_file.write_text(f"\ufeff{payload}", encoding="utf-8")
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    invalidate_users_cache()

    token, user = authenticate("bom.admin@example.com", "bom-admin-pass")

    assert token
    assert user.id == "bom-admin"


def test_pack_upload_is_session_admin_only_and_accepts_valid_zip(monkeypatch, tmp_path) -> None:
    from modular_ontology import app as app_module
    import modular_ontology.pack_index as pack_index
    import modular_ontology.store as store
    from modular_ontology.auth import _hash_password

    monkeypatch.setattr(pack_index, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "uploaded-index.sqlite3")
    sync_forces: list[bool] = []

    def fake_run_google_drive_sync(force=False):
        sync_forces.append(force)
        return {"status": "skipped"}

    monkeypatch.setattr(app_module, "run_google_drive_sync", fake_run_google_drive_sync)
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
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
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
    assert True in sync_forces


def test_public_read_endpoints_do_not_sync_without_session(monkeypatch) -> None:
    from modular_ontology import app as app_module

    sync_calls: list[bool] = []

    def fake_ensure_runtime_storage():
        sync_calls.append(True)
        return {"enabled": True, "status": "synced", "downloaded": [], "missing": []}

    monkeypatch.setattr(app_module, "ensure_runtime_storage", fake_ensure_runtime_storage)
    monkeypatch.setattr(app_module, "index_stats", lambda: {"packs": 0, "documents": 0, "nodes": 0, "edges": 0})
    monkeypatch.setattr(app_module, "list_users", lambda: [])
    monkeypatch.setattr(app_module, "list_projects", lambda: [])
    monkeypatch.setattr(app_module, "list_ifc_models", lambda: [])
    monkeypatch.setattr(app_module, "google_drive_sync_status", lambda: {"status": "not-synced", "downloaded": [], "missing": []})

    for path in ("/api/packs", "/api/projects", "/api/ifc/models", "/api/index/status"):
        response = client.get(path)
        assert response.status_code == 200

    assert sync_calls == []


def test_index_status_sync_query_runs_runtime_sync(monkeypatch) -> None:
    from modular_ontology import app as app_module

    sync_calls: list[bool] = []

    def fake_ensure_runtime_storage():
        sync_calls.append(True)
        return {"enabled": True, "status": "synced", "downloaded": [], "missing": []}

    monkeypatch.setattr(app_module, "ensure_runtime_storage", fake_ensure_runtime_storage)
    monkeypatch.setattr(app_module, "index_stats", lambda: {"packs": 0, "documents": 0, "nodes": 0, "edges": 0})
    monkeypatch.setattr(app_module, "list_users", lambda: [])
    monkeypatch.setattr(app_module, "list_projects", lambda: [])
    monkeypatch.setattr(app_module, "list_ifc_models", lambda: [])

    response = client.get("/api/index/status?sync=true")

    assert response.status_code == 200
    assert sync_calls == [True]


def test_google_drive_sync_on_startup_defaults_off_on_vercel(monkeypatch) -> None:
    from modular_ontology import app as app_module

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "drive-root")
    monkeypatch.delenv("MODULAR_ONTOLOGY_SYNC_ON_STARTUP", raising=False)

    assert app_module.google_drive_sync_on_startup() is False

    monkeypatch.setenv("MODULAR_ONTOLOGY_SYNC_ON_STARTUP", "1")

    assert app_module.google_drive_sync_on_startup() is True


def test_mcp_tools_return_json_payloads() -> None:
    packs = json.loads(mcp_server.list_packs())
    mo_packs = json.loads(mcp_server.mo_pack_list())
    flat_mo_packs = json.loads(mcp_server.mo_pack_list(flat=True))
    project_packs = json.loads(mcp_server.mo_project_pack_list("samcheok-building-b"))
    projects = json.loads(mcp_server.list_projects())
    manifest = json.loads(mcp_server.mo_tool_manifest())
    ontology_manifest = json.loads(mcp_server.mo_ontology_manifest("advance-steel-samcheok-bldg-b-bm25-evidence-pack", 200, 400))
    schema = json.loads(mcp_server.mo_pack_schema("advance-steel-samcheok-bldg-b-bm25-evidence-pack", 200, 400))
    node_types = json.loads(mcp_server.mo_node_type_list("advance-steel-samcheok-bldg-b-bm25-evidence-pack", 400))
    relation_types = json.loads(mcp_server.mo_relation_type_list("advance-steel-samcheok-bldg-b-bm25-evidence-pack", 400))
    pack_overview = json.loads(mcp_server.mo_pack_overview("advance-steel-samcheok-bldg-b-bm25-evidence-pack", 120, 240))
    project_overview = json.loads(mcp_server.mo_project_overview("samcheok-building-b", 80, 160))
    project_search = json.loads(mcp_server.mo_project_search("samcheok-building-b", "Beam", 2))
    evidence_trace = json.loads(mcp_server.mo_evidence_trace("advance-steel-samcheok-bldg-b-bm25-evidence-pack", "Beam", 2))
    graph = json.loads(mcp_server.get_graph("revit-yeoju-ar-ifc-workset-module-localcrab-pack", 20, 40))
    evidence = json.loads(mcp_server.search_pack("advance-steel-samcheok-bldg-b-bm25-evidence-pack", "Beam", 2))
    answer = json.loads(mcp_server.ask_pack_question("advance-steel-samcheok-bldg-b-bm25-evidence-pack", "Beam", 2))
    mo_answer = json.loads(mcp_server.mo_question_answer("advance-steel-samcheok-bldg-b-bm25-evidence-pack", "Beam", 2))
    modules = json.loads(mcp_server.list_modules("advance-steel-samcheok-bldg-b-bm25-evidence-pack"))
    fasteners = json.loads(mcp_server.get_fasteners("advance-steel-samcheok-bldg-b-bm25-evidence-pack"))

    assert packs
    assert flat_mo_packs == packs
    assert mo_packs["mode"] == "project-first"
    assert mo_packs["projects"]
    assert project_packs["projects"][0]["project"]["id"] == "samcheok-building-b"
    assert project_packs["projects"][0]["packCount"] == 1
    assert projects
    assert manifest["naming"]["pattern"] == "mo_<domain>_<action>"
    assert manifest["workflow"]["primaryUnit"] == "project"
    assert "mo_ontology_manifest" in manifest["canonicalTools"]
    assert "mo_question_answer" in manifest["canonicalTools"]
    assert "mo_node_type_list" in manifest["canonicalTools"]
    assert "mo_relation_type_list" in manifest["canonicalTools"]
    assert "mo_project_overview" in manifest["canonicalTools"]
    assert "mo_evidence_trace" in manifest["canonicalTools"]
    assert "mo_distinct_property_values" in manifest["canonicalTools"]
    assert "mo_filtered_search_nodes" in manifest["canonicalTools"]
    assert "mo_aggregate_nodes" in manifest["canonicalTools"]
    assert "mo_list_project_modules" in manifest["canonicalTools"]
    assert manifest["legacyAliases"]["ask_pack_question"] == "mo_question_answer"
    assert ontology_manifest["schemas"][0]["queryableFields"]["nodeFields"]
    assert schema["nodeTypes"]
    assert schema["relationTypes"]
    assert schema["queryableFields"]["searchFields"]["nodes"]
    assert node_types["nodeTypes"]
    assert node_types["queryableNodeFields"]
    assert relation_types["relationTypes"]
    assert relation_types["queryableEdgeFields"]
    assert pack_overview["pack"]["id"] == "advance-steel-samcheok-bldg-b-bm25-evidence-pack"
    assert "mo_ontology_manifest" in pack_overview["recommendedTools"]
    assert project_overview["projects"][0]["project"]["id"] == "samcheok-building-b"
    assert project_overview["projects"][0]["packs"]
    assert project_search["matchCount"] > 0
    assert evidence_trace["evidence"]
    assert evidence_trace["nodes"]["nodes"]
    assert graph["nodes"]
    assert isinstance(evidence, list)
    assert evidence
    assert answer["mode"] == "local-graph-rag"
    assert mo_answer["mode"] == "local-graph-rag"
    assert answer["evidence"]
    assert modules["module_count"] == 24
    assert fasteners["bolt_quantity"] == 1011


def test_mcp_query_tools_filter_project_and_aggregate_nodes() -> None:
    pack_id = "advance-steel-samcheok-bldg-b-bm25-evidence-pack"

    distinct_roles = json.loads(
        mcp_server.mo_distinct_property_values(
            pack_id,
            "assembly_role",
            node_type="Assembly",
            limit=20,
        )
    )
    filtered = json.loads(
        mcp_server.mo_filtered_search_nodes(
            pack_id,
            node_type="Assembly",
            where={"assembly_role": {"eq": "Column"}},
            fields=["id", "assembly_mark", "assembly_role", "total_weight_kg", "formula"],
            order_by=[{"field": "assembly_mark", "direction": "asc", "natural": True}],
            limit=5,
        )
    )
    aggregate = json.loads(
        mcp_server.mo_aggregate_nodes(
            pack_id,
            node_type="Assembly",
            where={"assembly_role": {"eq": "Column"}},
            group_by=["assembly_role"],
            metrics=[
                {"field": "total_weight_kg", "agg": "sum", "as": "weight_kg"},
                {"agg": "count", "as": "row_count"},
            ],
        )
    )
    modules = json.loads(mcp_server.mo_list_project_modules(project_id="samcheok-building-b", limit=50))
    schema_profile = json.loads(mcp_server.mo_schema_profile(pack_id, sample_size=50))

    assert "Column" in distinct_roles["values"]
    assert filtered["count"] > 0
    assert filtered["rows"]
    assert "formula" not in filtered["rows"][0]
    assert set(filtered["rows"][0]).issubset({"id", "assembly_mark", "assembly_role", "total_weight_kg"})
    assert aggregate["rows"] == [{"assembly_role": "Column", "weight_kg": 4697.07888, "row_count": 25}]
    assert modules["module_count"] >= 17
    assert any(module["module_id"] == "1-02-A" for module in modules["modules"])
    assert schema_profile["node_types"]
    assert schema_profile["node_types"][0]["properties"]["assemblies"]["sample_values"][0]["type"] == "list"


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
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    monkeypatch.setenv("MODULAR_ONTOLOGY_MCP_COMPANY", "Client Co")

    projects = json.loads(mcp_server.list_projects())
    packs = json.loads(mcp_server.list_packs())
    allowed_graph = json.loads(mcp_server.get_graph("revit-yeoju-ar-ifc-workset-module-localcrab-pack", 5, 5))
    blocked_graph = json.loads(mcp_server.get_graph("advance-steel-samcheok-bldg-b-bm25-evidence-pack", 5, 5))

    assert [project["id"] for project in projects] == ["yeoju-modular-dormitory"]
    assert [pack["id"] for pack in packs] == ["revit-yeoju-ar-ifc-workset-module-localcrab-pack"]
    assert allowed_graph["stats"]["visibleNodes"] > 0
    assert blocked_graph["error"] == "forbidden"


def test_mcp_user_token_store_reuses_active_token(tmp_path) -> None:
    token_file = tmp_path / "00_Admin" / "mcp_tokens.json"
    user = User(
        id="client-member",
        name="Client Member",
        email="Client.Member@example.com",
        company="Client Co",
        role="member",
        password_hash=_hash_password("member-pass"),
        status="active",
    )

    first = ensure_mcp_token_for_user(user, path=token_file)
    second = ensure_mcp_token_for_user(user, path=token_file)
    urls = build_user_mcp_urls("https://example.trycloudflare.com", first["token"])

    assert first["token"].startswith("mom_")
    assert second["token"] == first["token"]
    assert get_mcp_token_record(first["token"], path=token_file)["company"] == "Client Co"
    assert urls["publicUrl"] == f"https://example.trycloudflare.com/mcp/{first['token']}"


def test_mcp_user_token_regeneration_revokes_previous_token(tmp_path) -> None:
    token_file = tmp_path / "00_Admin" / "mcp_tokens.json"
    user = User(
        id="client-member",
        name="Client Member",
        email="Client.Member@example.com",
        company="Client Co",
        role="member",
        password_hash=_hash_password("member-pass"),
        status="active",
    )

    first = ensure_mcp_token_for_user(user, path=token_file)
    regenerated = regenerate_mcp_token_for_user(user, path=token_file)
    reused = ensure_mcp_token_for_user(user, path=token_file)
    payload = json.loads(token_file.read_text(encoding="utf-8"))
    records = [item for item in payload["tokens"] if item["userEmail"] == "client.member@example.com"]

    assert regenerated["token"].startswith("mom_")
    assert regenerated["token"] != first["token"]
    assert reused["token"] == regenerated["token"]
    assert get_mcp_token_record(first["token"], path=token_file) is None
    assert get_mcp_token_record(regenerated["token"], path=token_file)["status"] == "active"
    assert [record["status"] for record in records] == ["revoked", "active"]


def test_mcp_status_returns_user_url_when_token_drive_write_back_fails(monkeypatch) -> None:
    from modular_ontology import app as app_module

    user = User(
        id="client-member",
        name="Client Member",
        email="client.member@example.com",
        company="Client Co",
        role="member",
        password_hash=_hash_password("member-pass"),
        status="active",
    )

    monkeypatch.setattr(app_module, "current_user", lambda authorization: user)
    monkeypatch.setattr(
        app_module,
        "_public_mcp_remote",
        lambda request: ("https://modular-ontology.xyz/mcp", "https://modular-ontology.xyz"),
    )
    monkeypatch.setattr(
        app_module,
        "run_google_drive_mcp_tokens_sync",
        lambda: {"enabled": True, "status": "error", "error": "drive download failed"},
    )
    monkeypatch.setattr(
        app_module,
        "ensure_mcp_token_for_user",
        lambda active_user: {
            "token": "mom_test-token",
            "userEmail": active_user.email,
            "userName": active_user.name,
            "company": active_user.company,
            "role": active_user.role,
        },
    )
    monkeypatch.setattr(
        app_module,
        "run_google_drive_write_back",
        lambda kind: {"enabled": True, "status": "error", "error": "drive upload failed"},
    )

    response = client.get("/api/mcp/status", headers={"Authorization": "Bearer test-token"})
    payload = response.json()

    assert response.status_code == 200
    assert payload["remote"]["userUrl"]["publicUrl"] == "https://modular-ontology.xyz/mcp/mom_test-token"
    assert payload["remote"]["tokenSync"]["status"] == "error"
    assert payload["remote"]["tokenWriteBack"]["status"] == "error"


def test_mcp_user_url_regenerate_requires_login() -> None:
    response = client.post("/api/mcp/user-url/regenerate")

    assert response.status_code == 401


def test_mcp_user_token_scope_filters_projects_and_packs(monkeypatch, tmp_path) -> None:
    users_file = tmp_path / "users.json"
    token_file = tmp_path / "mcp_tokens.json"
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
    user = User(
        id="client-member",
        name="Client Member",
        email="client.member@example.com",
        company="Client Co",
        role="member",
        password_hash=_hash_password("member-pass"),
        status="active",
    )
    token = ensure_mcp_token_for_user(user, path=token_file)["token"]
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    monkeypatch.setattr(mcp_server, "_request_mcp_token", lambda: token)
    monkeypatch.setattr(mcp_server, "get_mcp_token_record", lambda value: get_mcp_token_record(value, path=token_file))

    projects = json.loads(mcp_server.list_projects())
    packs = json.loads(mcp_server.list_packs())
    blocked_graph = json.loads(mcp_server.get_graph("advance-steel-samcheok-bldg-b-bm25-evidence-pack", 5, 5))

    assert [project["id"] for project in projects] == ["yeoju-modular-dormitory"]
    assert [pack["id"] for pack in packs] == ["revit-yeoju-ar-ifc-workset-module-localcrab-pack"]
    assert blocked_graph["error"] == "forbidden"


def test_mcp_stdio_server_lists_and_calls_tools() -> None:
    async def run_client() -> None:
        root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "modular_ontology.mcp_server"],
            cwd=root,
            env={"MODULAR_ONTOLOGY_ROOT": str(root)},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                result = await session.call_tool(
                    "mo_question_answer",
                    {
                        "pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
                        "question": "Beam",
                        "limit": 2,
                    },
                )
                payload = json.loads(result.content[0].text)

        assert {
            "mo_tool_manifest",
            "mo_ontology_manifest",
            "mo_project_list",
            "mo_project_overview",
            "mo_project_pack_list",
            "mo_project_search",
            "mo_pack_list",
            "mo_pack_overview",
            "mo_pack_schema",
            "mo_graph_get",
            "mo_node_type_list",
            "mo_distinct_property_values",
            "mo_filtered_search_nodes",
            "mo_aggregate_nodes",
            "mo_list_project_modules",
            "mo_query_quantity_evidence",
            "mo_join_by_property",
            "mo_relation_type_list",
            "mo_evidence_search",
            "mo_evidence_trace",
            "mo_question_answer",
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


def test_local_graph_rag_answers_module_weight_when_document_search_misses(tmp_path) -> None:
    db_path = tmp_path / "module-weight-index.sqlite3"
    index_all_packs(db_path=db_path)

    answer = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "1-05-ST weight",
        limit=4,
        db_path=db_path,
    )

    assert "5,800.246 kg" in answer["answer"]
    assert answer["evidence"][0]["path"] == "documents/modules/1-05-ST.md"
    assert answer["graphContext"]["facts"][0]["total_weight_kg"] == 5800.246


def test_korean_question_retrieves_grounded_context(tmp_path) -> None:
    db_path = tmp_path / "korean-index.sqlite3"
    index_all_packs(db_path=db_path)

    # Natural-language Korean must surface evidence (token/bigram search), not the
    # old whole-phrase substring behavior that returned nothing.
    weight = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "가장 무거운 모듈은 무엇이고 중량은 얼마야?",
        limit=6,
        db_path=db_path,
    )
    assert weight["evidence"]
    assert any(fact.get("kind") == "module_weight_list" for fact in weight["graphContext"]["facts"])

    beam = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "보 부재의 단면 정보를 알려줘",
        limit=6,
        db_path=db_path,
    )
    assert beam["evidence"] or beam["graphContext"]["nodes"]


def test_vague_question_falls_back_to_pack_context_for_every_pack(tmp_path) -> None:
    db_path = tmp_path / "fallback-index.sqlite3"
    index_all_packs(db_path=db_path)

    for pack_id in (
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "revit-yeoju-ar-ifc-workset-module-localcrab-pack",
    ):
        answer = answer_pack_question(pack_id, "이 프로젝트 개요를 설명해줘", limit=6, db_path=db_path)
        # Every pack must hand the LLM something grounded to reason over.
        assert answer["evidence"] or answer["graphContext"]["nodes"]


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
    monkeypatch.setenv("MODULAR_ONTOLOGY_OPENAI_MODEL", "test-model")

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


def test_openai_graph_rag_uses_request_api_key(monkeypatch, tmp_path) -> None:
    import modular_ontology.qa as qa

    class FakeResponse:
        output_text = "Request-key OpenAI answer."

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    captured_keys = []

    def fake_make_openai_client(api_key=None):
        captured_keys.append(api_key)
        return FakeClient()

    db_path = tmp_path / "openai-request-key-index.sqlite3"
    index_all_packs(db_path=db_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(qa, "_make_openai_client", fake_make_openai_client)

    answer = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "Beam",
        limit=2,
        db_path=db_path,
        use_openai=True,
        openai_api_key="sk-user-secret-for-test",
        openai_model="gpt-4.1-mini",
    )

    assert answer["mode"] == "openai-graph-rag"
    assert answer["answer"] == "Request-key OpenAI answer."
    assert captured_keys == ["sk-user-secret-for-test"]


def test_openai_graph_rag_requires_request_api_key_even_when_server_key_exists(monkeypatch, tmp_path) -> None:
    import modular_ontology.qa as qa

    def fail_if_called(api_key=None):
        raise AssertionError("server OpenAI key fallback should not be used")

    db_path = tmp_path / "openai-no-server-fallback.sqlite3"
    index_all_packs(db_path=db_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-server-secret-for-test")
    monkeypatch.setattr(qa, "_make_openai_client", fail_if_called)

    answer = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "Beam",
        limit=2,
        db_path=db_path,
        use_openai=True,
        openai_model="gpt-4.1-mini",
    )

    assert answer["mode"] == "local-graph-rag"
    assert answer["llmError"] == "OpenAI API key is required for web AI Query. Provide a user API key."


def test_openai_graph_rag_redacts_request_api_key_from_errors(monkeypatch, tmp_path) -> None:
    import modular_ontology.qa as qa

    def fake_make_openai_client(api_key=None):
        raise RuntimeError(f"bad api key {api_key}")

    db_path = tmp_path / "openai-redacted-key-index.sqlite3"
    index_all_packs(db_path=db_path)
    monkeypatch.setattr(qa, "_make_openai_client", fake_make_openai_client)

    answer = answer_pack_question(
        "advance-steel-samcheok-bldg-b-bm25-evidence-pack",
        "Beam",
        limit=2,
        db_path=db_path,
        use_openai=True,
        openai_api_key="sk-user-secret-for-test",
        openai_model="gpt-4.1-mini",
    )

    assert answer["mode"] == "local-graph-rag"
    assert answer["llmError"]
    assert "sk-user-secret-for-test" not in answer["llmError"]
    assert "[redacted" in answer["llmError"]


def test_safe_llm_error_redacts_openai_masked_key() -> None:
    import modular_ontology.qa as qa

    error = "Incorrect API key provided: sk-user-********************************test. Check your API key."

    safe = qa._safe_llm_error(error)

    assert "sk-user" not in safe
    assert "test." not in safe
    assert "[redacted-api-key]" in safe


def test_validate_openai_api_key_uses_request_key(monkeypatch) -> None:
    import modular_ontology.qa as qa

    class FakeResponses:
        def __init__(self) -> None:
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return object()

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    fake_client = FakeClient()
    captured_keys = []

    def fake_make_openai_client(api_key=None):
        captured_keys.append(api_key)
        return fake_client

    monkeypatch.setattr(qa, "_make_openai_client", fake_make_openai_client)

    result = qa.validate_openai_api_key("sk-user-secret-for-test", "gpt-4.1-mini")

    assert result == {"valid": True, "model": "gpt-4.1-mini"}
    assert captured_keys == ["sk-user-secret-for-test"]
    assert fake_client.responses.calls[0]["model"] == "gpt-4.1-mini"
    assert fake_client.responses.calls[0]["max_output_tokens"] == 16


def test_validate_openai_api_key_redacts_errors(monkeypatch) -> None:
    import modular_ontology.qa as qa

    def fake_make_openai_client(api_key=None):
        raise RuntimeError(f"Incorrect API key provided: {api_key}.")

    monkeypatch.setattr(qa, "_make_openai_client", fake_make_openai_client)

    result = qa.validate_openai_api_key("sk-user-secret-for-test", "gpt-4.1-mini")

    assert result["valid"] is False
    assert result["model"] == "gpt-4.1-mini"
    assert "sk-user-secret-for-test" not in result["error"]
    assert "[redacted-api-key]" in result["error"]


def test_openai_validate_api_requires_login() -> None:
    response = client.post(
        "/api/llm/openai/validate",
        json={"openai_api_key": "sk-user-secret-for-test", "openai_model": "gpt-4.1-mini"},
    )

    assert response.status_code == 401


def test_query_openai_mode_requires_login_and_request_key(monkeypatch) -> None:
    from modular_ontology import app as app_module

    active_user = User(
        id="member-test",
        name="Member",
        email="member@example.com",
        company="Kumkang Kind",
        role="member",
        password_hash="unused",
        status="active",
    )
    no_auth = client.post(
        "/api/query",
        json={"pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack", "question": "Beam", "use_openai": True},
    )
    monkeypatch.setattr(app_module, "current_user", lambda authorization: active_user)
    missing_key = client.post(
        "/api/query",
        headers={"Authorization": "Bearer member-test-token"},
        json={"pack_id": "advance-steel-samcheok-bldg-b-bm25-evidence-pack", "question": "Beam", "use_openai": True},
    )

    assert no_auth.status_code == 401
    assert missing_key.status_code == 400


def test_admin_reindex_api_requires_admin_session(monkeypatch, tmp_path) -> None:
    import modular_ontology.store as store
    from modular_ontology.auth import _hash_password

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
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
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


def test_google_drive_sync_downloads_runtime_storage(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("admin", "00_Admin", "application/vnd.google-apps.folder"),
                DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                DriveItem("packs", "04_Ontology_Packs", "application/vnd.google-apps.folder"),
            ],
            "admin": [
                DriveItem("users", "users.json", "application/json"),
                DriveItem("mcp", "mcp_remote.json", "application/json"),
                DriveItem("mcp_tokens", "mcp_tokens.json", "application/json"),
            ],
            "database": [DriveItem("db", "modular_ontology.sqlite3", "application/octet-stream")],
            "packs": [DriveItem("indexed", "indexed", "application/vnd.google-apps.folder")],
            "indexed": [
                DriveItem("pack", "sample-pack.zip", "application/x-zip-compressed"),
                DriveItem("readme", "README.md", "text/markdown"),
            ],
        }

        payloads = {
            "users": b'{"users":[]}',
            "mcp": b'{"publicUrl":""}',
            "mcp_tokens": b'{"tokens":[]}',
            "db": b"sqlite-bytes",
            "pack": b"zip-bytes",
        }

        def list_children(self, folder_id):
            return self.children[folder_id]

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.payloads[file_id])

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert result["status"] == "synced"
    assert (tmp_path / "00_Admin" / "users.json").read_text(encoding="utf-8") == '{"users":[]}'
    assert (tmp_path / "00_Admin" / "mcp_remote.json").exists()
    assert (tmp_path / "00_Admin" / "mcp_tokens.json").exists()
    assert (tmp_path / "01_Database" / "modular_ontology.sqlite3").read_bytes() == b"sqlite-bytes"
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "sample-pack.zip").read_bytes() == b"zip-bytes"
    assert not (tmp_path / "04_Ontology_Packs" / "indexed" / "README.md").exists()


def test_google_drive_sync_reads_legacy_pack_folder_into_new_layout(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("packs", "02_Ontology_Packs", "application/vnd.google-apps.folder"),
            ],
            "packs": [DriveItem("indexed", "indexed", "application/vnd.google-apps.folder")],
            "indexed": [DriveItem("pack", "legacy-pack.zip", "application/x-zip-compressed")],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"legacy-zip-bytes")

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert result["status"] == "synced"
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "legacy-pack.zip").read_bytes() == b"legacy-zip-bytes"


def test_google_drive_sync_merges_new_and_legacy_pack_folders(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("new-packs", "04_Ontology_Packs", "application/vnd.google-apps.folder"),
                DriveItem("legacy-packs", "02_Ontology_Packs", "application/vnd.google-apps.folder"),
            ],
            "new-packs": [DriveItem("new-indexed", "indexed", "application/vnd.google-apps.folder")],
            "legacy-packs": [DriveItem("legacy-indexed", "indexed", "application/vnd.google-apps.folder")],
            "new-indexed": [DriveItem("new-pack", "new-pack.zip", "application/x-zip-compressed")],
            "legacy-indexed": [DriveItem("legacy-pack", "legacy-pack.zip", "application/x-zip-compressed")],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file_id.encode("utf-8"))

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert result["status"] == "synced"
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "new-pack.zip").read_bytes() == b"new-pack"
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "legacy-pack.zip").read_bytes() == b"legacy-pack"


def test_google_drive_sync_registers_manual_ifc_files_as_metadata(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("ifc-root", "03_IFC_Models", "application/vnd.google-apps.folder"),
            ],
            "ifc-root": [DriveItem("project", "samcheok-building-b", "application/vnd.google-apps.folder")],
            "project": [DriveItem("files", "files", "application/vnd.google-apps.folder")],
            "files": [
                DriveItem("ifc", "sample.ifc", "application/octet-stream", "2026-06-11T00:00:00Z", 1200),
                DriveItem("xkt", "sample.xkt", "application/octet-stream", "2026-06-11T00:01:00Z", 800),
            ],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            raise AssertionError("manual IFC registration should not download raw model files")

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    metadata_path = tmp_path / "03_IFC_Models" / "samcheok-building-b" / "metadata" / "sample.metadata.json"
    assert result["status"] == "synced"
    assert str(metadata_path) in result["downloaded"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["filename"] == "sample.ifc"
    assert metadata["projectId"] == "samcheok-building-b"
    assert metadata["storage"] == "google-drive"
    assert metadata["viewerStatus"] == "ready"
    assert metadata["sizeBytes"] == 1200


def test_google_drive_sync_reads_project_scoped_models_and_packs(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder"),
            ],
            "projects-root": [
                DriveItem("project", "renamed-building", "application/vnd.google-apps.folder", "2026-06-11T02:00:00Z"),
            ],
            "project": [
                DriveItem("ifc-folder", "ifc-models", "application/vnd.google-apps.folder"),
                DriveItem("packs-folder", "ontology-packs", "application/vnd.google-apps.folder"),
            ],
            "ifc-folder": [
                DriveItem("ifc", "renamed-model.ifc", "application/octet-stream", "2026-06-11T00:00:00Z", 1200),
                DriveItem("xkt", "renamed-model.xkt", "application/octet-stream", "2026-06-11T00:01:00Z", 800),
            ],
            "packs-folder": [
                DriveItem("pack", "renamed-project-pack.zip", "application/x-zip-compressed"),
            ],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"pack_id": "sample-project-pack"}))

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    metadata_path = tmp_path / "03_IFC_Models" / "renamed-building" / "metadata" / "renamed-model.metadata.json"
    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / "renamed-building__renamed-project-pack.zip"
    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    folders_path = tmp_path / "02_Projects" / ".drive-project-folders.json"

    assert result["status"] == "synced"
    assert "04_Ontology_Packs" not in result["missing"]
    assert str(metadata_path) in result["downloaded"]
    assert pack_path.exists()
    assert json.loads(links_path.read_text(encoding="utf-8")) == {
        "renamed-building": ["sample-project-pack"],
    }
    assert json.loads(folders_path.read_text(encoding="utf-8")) == [
        {
            "folderId": "project",
            "projectId": "renamed-building",
            "name": "renamed-building",
            "modifiedTime": "2026-06-11T02:00:00Z",
        }
    ]

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["filename"] == "renamed-model.ifc"
    assert metadata["projectId"] == "renamed-building"
    assert metadata["viewerStatus"] == "ready"
    assert metadata["drive"]["file"]["folder"] == "02_Projects/renamed-building/ifc-models"


def test_google_drive_registry_sync_registers_project_scoped_ifc_metadata(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder"),
            ],
            "projects-root": [
                DriveItem("project", "yeoju-modular-dormitory", "application/vnd.google-apps.folder"),
            ],
            "project": [
                DriveItem("ifc-folder", "ifc-models", "application/vnd.google-apps.folder"),
                DriveItem("packs-folder", "ontology-packs", "application/vnd.google-apps.folder"),
            ],
            "ifc-folder": [
                DriveItem("ifc", "yeoju.ifc", "application/octet-stream", "2026-06-11T00:00:00Z", 1200),
                DriveItem("xkt", "yeoju.xkt", "application/octet-stream", "2026-06-11T00:01:00Z", 800),
            ],
            "packs-folder": [
                DriveItem("pack", "project-pack.zip", "application/x-zip-compressed"),
            ],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            raise AssertionError("registry sync should not download project IFC files or pack zips")

    result = sync_google_drive_registry_files(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    metadata_path = tmp_path / "03_IFC_Models" / "yeoju-modular-dormitory" / "metadata" / "yeoju.metadata.json"
    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / "project-pack.zip"
    folders_path = tmp_path / "02_Projects" / ".drive-project-folders.json"

    assert result["status"] == "synced"
    assert str(metadata_path) in result["downloaded"]
    assert not pack_path.exists()
    assert json.loads(folders_path.read_text(encoding="utf-8")) == [
        {
            "folderId": "project",
            "projectId": "yeoju-modular-dormitory",
            "name": "yeoju-modular-dormitory",
            "modifiedTime": "",
        }
    ]

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["filename"] == "yeoju.ifc"
    assert metadata["projectId"] == "yeoju-modular-dormitory"
    assert metadata["storage"] == "google-drive"
    assert metadata["viewerStatus"] == "ready"
    assert metadata["xktPath"].endswith("03_IFC_Models\\yeoju-modular-dormitory\\files\\yeoju.xkt") or metadata["xktPath"].endswith("03_IFC_Models/yeoju-modular-dormitory/files/yeoju.xkt")
    assert metadata["drive"]["file"]["folder"] == "02_Projects/yeoju-modular-dormitory/ifc-models"


def test_drive_xkt_worker_converts_missing_project_xkt(monkeypatch, tmp_path) -> None:
    from modular_ontology import drive_xkt_worker

    uploaded: dict[str, bytes] = {}

    class FakeDriveClient:
        children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
            "projects-root": [DriveItem("project", "samcheok-building-b", "application/vnd.google-apps.folder")],
            "project": [DriveItem("ifc-folder", "ifc-models", "application/vnd.google-apps.folder")],
            "ifc-folder": [DriveItem("ifc", "sample.ifc", "application/octet-stream", "2026-06-11T00:00:00Z", 1200)],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"ISO-10303-21;")

        def upload_file_by_name(self, folder_id, source, name=None, mime_type=None):
            uploaded[f"{folder_id}/{name or source.name}"] = source.read_bytes()
            return {"status": "created", "id": "xkt-file", "name": name or source.name}

    def fake_run(command, shell, capture_output, text, timeout):
        xkt_path = command.rsplit("-o ", 1)[1].strip().strip('"')
        Path(xkt_path).write_bytes(b"xkt")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(drive_xkt_worker, "_xkt_converter_command", lambda: "fake-convert -s {ifc} -o {xkt}")
    monkeypatch.setattr(drive_xkt_worker.subprocess, "run", fake_run)

    result = convert_missing_drive_xkts(client=FakeDriveClient(), root_folder_id="root", max_files=5)

    assert result["status"] == "converted"
    assert result["converted"] == [
        {
            "projectId": "samcheok-building-b",
            "ifc": "sample.ifc",
            "xkt": "sample.xkt",
            "upload": "created",
            "driveFileId": "xkt-file",
        }
    ]
    assert uploaded["ifc-folder/sample.xkt"] == b"xkt"


def test_drive_xkt_worker_uses_npx_fallback_when_local_converter_is_missing(monkeypatch) -> None:
    from modular_ontology import drive_xkt_worker

    monkeypatch.delenv("MODULAR_ONTOLOGY_DISABLE_DEFAULT_XKT_CONVERTER", raising=False)
    monkeypatch.setattr(drive_xkt_worker, "_xkt_converter_candidates", lambda: [])

    command = drive_xkt_worker._default_xkt_converter_command()

    assert command.startswith("npx -y @xeokit/xeokit-convert@1.3.2 ")
    assert "-s {ifc}" in command
    assert "-o {xkt}" in command


def test_google_drive_sync_namespaces_same_pack_filename_per_project(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
            "projects-root": [
                DriveItem("project-a", "project-a", "application/vnd.google-apps.folder"),
                DriveItem("project-b", "project-b", "application/vnd.google-apps.folder"),
            ],
            "project-a": [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")],
            "project-b": [DriveItem("packs-b", "ontology-packs", "application/vnd.google-apps.folder")],
            "packs-a": [DriveItem("pack-a", "pack.zip", "application/x-zip-compressed")],
            "packs-b": [DriveItem("pack-b", "pack.zip", "application/x-zip-compressed")],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            pack_id = "project-a-pack" if file_id == "pack-a" else "project-b-pack"
            with zipfile.ZipFile(target, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"pack_id": pack_id}))

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    assert result["status"] == "synced"
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__pack.zip").exists()
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "project-b__pack.zip").exists()
    assert json.loads(links_path.read_text(encoding="utf-8")) == {
        "project-a": ["project-a-pack"],
        "project-b": ["project-b-pack"],
    }


def test_google_drive_sync_reads_project_pack_folders_as_groups(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
            "projects-root": [DriveItem("project-a", "project-a", "application/vnd.google-apps.folder")],
            "project-a": [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")],
            "packs-a": [
                DriveItem("direct-pack", "direct-pack.zip", "application/x-zip-compressed"),
                DriveItem("revit-jsonl-folder", "revit-jsonl", "application/vnd.google-apps.folder"),
            ],
            "revit-jsonl-folder": [
                DriveItem("objects-pack", "objects.zip", "application/x-zip-compressed"),
                DriveItem("relationships-pack", "relationships.zip", "application/x-zip-compressed"),
            ],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            pack_id = {
                "direct-pack": "project-direct-pack",
                "objects-pack": "project-revit-objects-pack",
                "relationships-pack": "project-revit-relationships-pack",
            }[file_id]
            with zipfile.ZipFile(target, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"pack_id": pack_id}))

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    indexed_dir = tmp_path / "04_Ontology_Packs" / "indexed"

    assert result["status"] == "synced"
    assert (indexed_dir / "project-a__direct-pack.zip").exists()
    assert (indexed_dir / "project-a__revit-jsonl__objects.zip").exists()
    assert (indexed_dir / "project-a__revit-jsonl__relationships.zip").exists()
    assert json.loads(links_path.read_text(encoding="utf-8")) == {
        "project-a": [
            "project-direct-pack",
            "project-revit-objects-pack",
            "project-revit-relationships-pack",
        ],
    }


def test_google_drive_sync_reads_common_project_packs_without_creating_project(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
            "projects-root": [
                DriveItem("common", "_Common", "application/vnd.google-apps.folder"),
                DriveItem("project-a", "project-a", "application/vnd.google-apps.folder"),
            ],
            "common": [
                DriveItem("spec-category", "시방서", "application/vnd.google-apps.folder"),
                DriveItem("guide-category", "설계지침", "application/vnd.google-apps.folder"),
            ],
            "spec-category": [DriveItem("spec-packs", "ontology-packs", "application/vnd.google-apps.folder")],
            "guide-category": [DriveItem("guide-packs", "ontology-packs", "application/vnd.google-apps.folder")],
            "project-a": [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")],
            "spec-packs": [DriveItem("spec-pack", "standard-spec.zip", "application/x-zip-compressed")],
            "guide-packs": [DriveItem("guide-pack", "design-guide.zip", "application/x-zip-compressed")],
            "packs-a": [DriveItem("pack-a", "project-pack.zip", "application/x-zip-compressed")],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            pack_id = {
                "spec-pack": "common-spec-pack",
                "guide-pack": "common-design-guide-pack",
                "pack-a": "project-a-pack",
            }[file_id]
            with zipfile.ZipFile(target, "w") as zf:
                zf.writestr("manifest.json", json.dumps({"pack_id": pack_id}))

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    folders_path = tmp_path / "02_Projects" / ".drive-project-folders.json"

    assert result["status"] == "synced"
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "_Common__시방서__standard-spec.zip").exists()
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "_Common__설계지침__design-guide.zip").exists()
    assert (tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__project-pack.zip").exists()
    assert json.loads(links_path.read_text(encoding="utf-8")) == {
        "__common__": ["common-spec-pack", "common-design-guide-pack"],
        "project-a": ["project-a-pack"],
    }
    assert json.loads(folders_path.read_text(encoding="utf-8")) == [
        {
            "folderId": "project-a",
            "projectId": "project-a",
            "name": "project-a",
            "modifiedTime": "",
        }
    ]


def test_project_scoped_pack_title_uses_drive_filename(tmp_path, monkeypatch) -> None:
    from modular_ontology import pack_index

    pack_dir = tmp_path / "04_Ontology_Packs" / "indexed"
    pack_dir.mkdir(parents=True)
    pack_path = pack_dir / "yeoju-project__여주_건축_온톨로지팩.zip"
    with zipfile.ZipFile(pack_path, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "pack_id": "revit-yeoju-ar-ifc-workset-module-localcrab-pack",
                    "title": "Revit IFC Workset Module LocalCrab Pack",
                    "entrypoints": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl"},
                    "counts": {"nodes": 0, "edges": 0, "documents": 0},
                }
            ),
        )
        zf.writestr("graph/nodes.jsonl", "")
        zf.writestr("graph/edges.jsonl", "")

    pack = pack_index.summarize_pack(pack_index.PackFile(pack_path))

    assert pack["id"] == "revit-yeoju-ar-ifc-workset-module-localcrab-pack"
    assert pack["title"] == "여주 건축 온톨로지팩"
    assert pack["displayName"] == "여주 건축 온톨로지팩"
    assert pack["displayFilename"] == "여주_건축_온톨로지팩.zip"
    assert pack["projectScoped"] is True
    assert pack["source"] == "Revit IFC"


def test_common_project_pack_summary_exposes_category(tmp_path) -> None:
    from modular_ontology import pack_index

    pack_dir = tmp_path / "04_Ontology_Packs" / "indexed"
    pack_dir.mkdir(parents=True)
    pack_path = pack_dir / "_Common__시방서__standard-spec.zip"
    with zipfile.ZipFile(pack_path, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "pack_id": "common-standard-spec-pack",
                    "title": "Standard Spec Pack",
                    "entrypoints": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl"},
                    "counts": {"nodes": 0, "edges": 0, "documents": 0},
                }
            ),
        )
        zf.writestr("graph/nodes.jsonl", "")
        zf.writestr("graph/edges.jsonl", "")

    pack = pack_index.summarize_pack(pack_index.PackFile(pack_path))

    assert pack["displayFilename"] == "standard-spec.zip"
    assert pack["commonCategory"] == "시방서"
    assert pack["commonScoped"] is True


def test_project_folder_pack_summary_exposes_category(tmp_path) -> None:
    from modular_ontology import pack_index

    pack_dir = tmp_path / "04_Ontology_Packs" / "indexed"
    pack_dir.mkdir(parents=True)
    pack_path = pack_dir / "project-a__revit-jsonl__objects.zip"
    with zipfile.ZipFile(pack_path, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "pack_id": "project-revit-objects-pack",
                    "title": "Revit Objects Pack",
                    "entrypoints": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl"},
                    "counts": {"nodes": 0, "edges": 0, "documents": 0},
                }
            ),
        )
        zf.writestr("graph/nodes.jsonl", "")
        zf.writestr("graph/edges.jsonl", "")

    pack = pack_index.summarize_pack(pack_index.PackFile(pack_path))

    assert pack["displayFilename"] == "objects.zip"
    assert pack["driveScope"] == "project-a"
    assert pack["driveCategory"] == "revit-jsonl"
    assert pack["projectCategory"] == "revit-jsonl"
    assert pack["commonCategory"] is None
    assert pack["projectScoped"] is True


def test_google_drive_sync_project_metadata_wins_over_legacy_ifc_folder(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder"),
                DriveItem("legacy-ifc-root", "03_IFC_Models", "application/vnd.google-apps.folder"),
            ],
            "projects-root": [DriveItem("project", "samcheok-building-b", "application/vnd.google-apps.folder")],
            "project": [DriveItem("ifc-folder", "ifc-models", "application/vnd.google-apps.folder")],
            "ifc-folder": [DriveItem("project-ifc", "sample.ifc", "application/octet-stream", "2026-06-11T00:00:00Z", 1200)],
            "legacy-ifc-root": [DriveItem("legacy-project", "samcheok-building-b", "application/vnd.google-apps.folder")],
            "legacy-project": [DriveItem("legacy-files", "files", "application/vnd.google-apps.folder")],
            "legacy-files": [DriveItem("legacy-ifc", "sample.ifc", "application/octet-stream", "2026-06-10T00:00:00Z", 20)],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            raise AssertionError("manual IFC registration should not download raw model files")

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    metadata_path = tmp_path / "03_IFC_Models" / "samcheok-building-b" / "metadata" / "sample.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert result["status"] == "synced"
    assert metadata["sizeBytes"] == 1200
    assert metadata["drive"]["file"]["id"] == "project-ifc"
    assert metadata["drive"]["file"]["folder"] == "02_Projects/samcheok-building-b/ifc-models"


def test_restore_ifc_files_falls_back_to_legacy_per_missing_file(tmp_path, monkeypatch) -> None:
    class FakeDriveClient:
        children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder"), DriveItem("legacy-root", "03_IFC_Models", "application/vnd.google-apps.folder")],
            "projects-root": [DriveItem("project", "samcheok-building-b", "application/vnd.google-apps.folder")],
            "project": [DriveItem("ifc-folder", "ifc-models", "application/vnd.google-apps.folder")],
            "ifc-folder": [],
            "legacy-root": [DriveItem("legacy-project", "samcheok-building-b", "application/vnd.google-apps.folder")],
            "legacy-project": [DriveItem("legacy-files", "files", "application/vnd.google-apps.folder")],
            "legacy-files": [DriveItem("ifc", "sample.ifc", "application/octet-stream")],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"ifc-bytes")

    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")

    downloaded = restore_ifc_files_from_drive(
        "samcheok-building-b",
        ["sample.ifc"],
        tmp_path,
        client=FakeDriveClient(),
    )

    assert downloaded == [str(tmp_path / "sample.ifc")]
    assert (tmp_path / "sample.ifc").read_bytes() == b"ifc-bytes"


def test_google_drive_sync_updates_project_metadata_when_xkt_is_added(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
            "projects-root": [DriveItem("project", "samcheok-building-b", "application/vnd.google-apps.folder")],
            "project": [DriveItem("ifc-folder", "ifc-models", "application/vnd.google-apps.folder")],
            "ifc-folder": [DriveItem("ifc", "sample.ifc", "application/octet-stream", "2026-06-11T00:00:00Z", 1200)],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            raise AssertionError("manual IFC registration should not download raw model files")

    fake_client = FakeDriveClient()
    sync_google_drive_storage(client=fake_client, root_folder_id="root", data_dir=tmp_path, force=True)
    metadata_path = tmp_path / "03_IFC_Models" / "samcheok-building-b" / "metadata" / "sample.metadata.json"
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["viewerStatus"] == "pending-xkt"

    fake_client.children["ifc-folder"] = [
        DriveItem("ifc", "sample.ifc", "application/octet-stream", "2026-06-11T00:00:00Z", 1200),
        DriveItem("xkt", "sample.xkt", "application/octet-stream", "2026-06-11T00:01:00Z", 800),
    ]
    sync_google_drive_storage(client=fake_client, root_folder_id="root", data_dir=tmp_path, force=True)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert metadata["filename"] == "sample.ifc"
    assert metadata["viewerStatus"] == "ready"
    assert metadata["xktPath"].endswith("sample.xkt")


def test_google_drive_sync_prunes_removed_project_ifc_metadata(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
            "projects-root": [DriveItem("project", "samcheok-building-b", "application/vnd.google-apps.folder")],
            "project": [DriveItem("ifc-folder", "ifc-models", "application/vnd.google-apps.folder")],
            "ifc-folder": [DriveItem("ifc", "sample.ifc", "application/octet-stream", "2026-06-11T00:00:00Z", 1200)],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            raise AssertionError("manual IFC registration should not download raw model files")

    fake_client = FakeDriveClient()
    sync_google_drive_storage(client=fake_client, root_folder_id="root", data_dir=tmp_path, force=True)
    metadata_path = tmp_path / "03_IFC_Models" / "samcheok-building-b" / "metadata" / "sample.metadata.json"
    assert metadata_path.exists()

    fake_client.children["ifc-folder"] = []
    sync_google_drive_storage(client=fake_client, root_folder_id="root", data_dir=tmp_path, force=True)

    assert not metadata_path.exists()


def test_google_drive_sync_prunes_deleted_project_assets(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
            "projects-root": [DriveItem("active-project", "active-project", "application/vnd.google-apps.folder")],
            "active-project": [DriveItem("ifc-folder", "ifc-models", "application/vnd.google-apps.folder")],
            "ifc-folder": [],
        }

        def list_children(self, folder_id):
            return self.children.get(folder_id, [])

        def download_file(self, file_id, target):
            raise AssertionError("No files should be downloaded")

    deleted_metadata_dir = tmp_path / "03_IFC_Models" / "deleted-project" / "metadata"
    deleted_metadata_dir.mkdir(parents=True)
    (deleted_metadata_dir / "old.metadata.json").write_text(
        json.dumps(
            {
                "filename": "old.ifc",
                "projectId": "deleted-project",
                "storage": "google-drive",
                "drive": {"file": {"folder": "02_Projects/deleted-project/ifc-models"}},
            }
        ),
        encoding="utf-8",
    )
    indexed = tmp_path / "04_Ontology_Packs" / "indexed"
    indexed.mkdir(parents=True)
    deleted_pack = indexed / "deleted-project__old-pack.zip"
    active_pack = indexed / "active-project__active-pack.zip"
    deleted_pack.write_bytes(b"deleted")
    active_pack.write_bytes(b"active")

    sync_google_drive_storage(client=FakeDriveClient(), root_folder_id="root", data_dir=tmp_path, force=True)

    assert not (tmp_path / "03_IFC_Models" / "deleted-project").exists()
    assert not deleted_pack.exists()
    assert active_pack.exists()


def test_google_drive_sync_downloads_legacy_database_filename(tmp_path) -> None:
    legacy_db_name = "mod" + "dular_" + "graph.sqlite3"

    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("admin", "00_Admin", "application/vnd.google-apps.folder"),
                DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                DriveItem("packs", "04_Ontology_Packs", "application/vnd.google-apps.folder"),
            ],
            "admin": [],
            "database": [DriveItem("db", legacy_db_name, "application/octet-stream")],
            "packs": [],
        }

        def list_children(self, folder_id):
            return self.children[folder_id]

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"legacy-sqlite-bytes")

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert result["status"] == "synced"
    assert (tmp_path / "01_Database" / "modular_ontology.sqlite3").read_bytes() == b"legacy-sqlite-bytes"


def test_new_env_helper_reads_legacy_prefix(monkeypatch) -> None:
    from modular_ontology.config import env

    legacy_name = "MOD" + "DULAR_GRAPH_GOOGLE_DRIVE_FOLDER_ID"
    monkeypatch.delenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", raising=False)
    monkeypatch.setenv(legacy_name, "legacy-root")

    assert env("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID") == "legacy-root"


def test_login_syncs_google_drive_users_before_auth(monkeypatch, tmp_path) -> None:
    from modular_ontology.auth import _hash_password, invalidate_users_cache, load_users
    from modular_ontology import app as app_module

    users_file = tmp_path / "00_Admin" / "users.json"
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")
    invalidate_users_cache()
    assert "drive.admin@example.com" not in load_users()

    def fake_sync_google_drive_users_file():
        users_file.parent.mkdir(parents=True, exist_ok=True)
        users_file.write_text(
            json.dumps(
                {
                    "users": [
                        {
                            "id": "drive-admin",
                            "name": "Drive Admin",
                            "email": "drive.admin@example.com",
                            "company": "Kumkang Kind",
                            "role": "admin",
                            "password_hash": _hash_password("drive-admin-pass"),
                            "status": "active",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return {"status": "synced", "downloaded": [str(users_file)], "missing": []}

    monkeypatch.setattr(app_module, "sync_google_drive_users_file", fake_sync_google_drive_users_file)

    response = client.post(
        "/api/auth/login",
        json={"email": "drive.admin@example.com", "password": "drive-admin-pass"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["role"] == "admin"


def test_admin_users_syncs_google_drive_users_before_listing(monkeypatch, tmp_path) -> None:
    from modular_ontology.auth import invalidate_users_cache
    from modular_ontology import app as app_module

    users_file = tmp_path / "00_Admin" / "users.json"
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE", str(users_file))
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")
    invalidate_users_cache()
    sync_calls = 0

    def fake_sync_google_drive_users_file():
        nonlocal sync_calls
        sync_calls += 1
        pending_users = []
        if sync_calls >= 2:
            pending_users.append(
                {
                    "id": "drive-pending",
                    "name": "Drive Pending",
                    "email": "drive.pending@example.com",
                    "company": "External Co",
                    "role": "member",
                    "password_hash": "unused",
                    "status": "pending",
                }
            )
        users_file.parent.mkdir(parents=True, exist_ok=True)
        users_file.write_text(json.dumps({"users": pending_users}), encoding="utf-8")
        return {"status": "synced", "downloaded": [str(users_file)], "missing": []}

    monkeypatch.setattr(app_module, "sync_google_drive_users_file", fake_sync_google_drive_users_file)

    admin = client.post(
        "/api/auth/login",
        json={"email": "ythong@kumkangkind.com", "password": TEST_ADMIN_PASSWORD},
    )
    response = client.get("/api/admin/users", headers={"Authorization": f"Bearer {admin.json()['token']}"})

    assert response.status_code == 200
    assert sync_calls >= 2
    assert any(user["email"] == "drive.pending@example.com" for user in response.json()["users"])


def test_google_drive_status_reads_marker_without_syncing(tmp_path) -> None:
    marker = tmp_path / ".google-drive-sync.json"
    marker.write_text(json.dumps({"status": "synced", "downloaded": ["db"], "missing": []}), encoding="utf-8")

    status = google_drive_sync_status(data_dir=tmp_path)

    assert status["status"] == "synced"
    assert status["downloaded"] == ["db"]


def test_google_drive_sync_tolerates_duplicate_folder_names(tmp_path) -> None:
    class FakeDriveClient:
        def list_children(self, folder_id):
            return [
                DriveItem("admin-a", "00_Admin", "application/vnd.google-apps.folder", "2026-06-10T00:00:00Z"),
                DriveItem("admin-b", "00_Admin", "application/vnd.google-apps.folder", "2026-06-11T00:00:00Z"),
            ]

        def download_file(self, file_id, target):
            raise AssertionError("empty duplicate admin folders should not download anything")

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert result["status"] == "synced"


def test_google_drive_status_requires_admin(monkeypatch) -> None:
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")

    response = client.get("/api/storage/google-drive/status")

    assert response.status_code == 403


def test_google_drive_sync_skips_unsafe_pack_filename(tmp_path) -> None:
    class FakeDriveClient:
        children = {
            "root": [
                DriveItem("admin", "00_Admin", "application/vnd.google-apps.folder"),
                DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                DriveItem("packs", "04_Ontology_Packs", "application/vnd.google-apps.folder"),
            ],
            "admin": [],
            "database": [],
            "packs": [DriveItem("indexed", "indexed", "application/vnd.google-apps.folder")],
            "indexed": [DriveItem("pack", "../evil.zip", "application/x-zip-compressed")],
        }

        def list_children(self, folder_id):
            return self.children[folder_id]

        def download_file(self, file_id, target):
            raise AssertionError("unsafe filenames must be rejected before download")

    result = sync_google_drive_storage(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert result["status"] == "synced"
    assert result["warnings"]
    assert "Unsafe Google Drive filename" in result["warnings"][0]
