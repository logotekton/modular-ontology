from __future__ import annotations

from pathlib import Path

import pytest

from modular_ontology import app


def test_mcp_allowed_hosts_include_exact_vercel_runtime_hosts(monkeypatch) -> None:
    monkeypatch.setenv(
        "VERCEL_URL",
        "https://Modular-Ontology-Unique-Candidate-123.vercel.app:443/a/deployment/path",
    )
    monkeypatch.setenv("VERCEL_BRANCH_URL", "modular-ontology-branch-456.vercel.app/preview")
    monkeypatch.setenv(
        "VERCEL_PROJECT_PRODUCTION_URL",
        "https://modular-ontology-production-789.vercel.app:8443/health",
    )
    monkeypatch.setenv(
        "MODULAR_ONTOLOGY_MCP_ALLOWED_HOSTS",
        "https://mcp.custom.example:443/path,internal.example:9443,*.vercel.app",
    )

    allowed = app._build_mcp_allowed_hosts()

    assert "modular-ontology-unique-candidate-123.vercel.app" in allowed
    assert "modular-ontology-branch-456.vercel.app" in allowed
    assert "modular-ontology-production-789.vercel.app:8443" in allowed
    assert "mcp.custom.example" in allowed
    assert "internal.example:9443" in allowed
    assert "*.vercel.app" not in allowed
    assert len(allowed) == len(set(allowed))


def test_viewer_lazy_restore_requests_xkt_without_ifc(monkeypatch, tmp_path: Path) -> None:
    metadata_path = tmp_path / "03_IFC_Models" / "project-a" / "metadata" / "building.metadata.json"
    files_dir = metadata_path.parent.parent / "files"
    files_dir.mkdir(parents=True)
    assert not (files_dir / "building.ifc").exists()
    requested: list[tuple[str, list[str], Path]] = []

    monkeypatch.setattr(app, "google_drive_sync_enabled", lambda: True)
    monkeypatch.setattr(
        app,
        "restore_ifc_files_from_drive",
        lambda project_folder, filenames, target_dir: requested.append((project_folder, filenames, target_dir)),
    )

    app._ensure_ifc_local_files(metadata_path, {"filename": "building.ifc"})

    assert requested == [("project-a", ["building.xkt"], files_dir)]
    assert all(not name.endswith(".ifc") for _project, names, _target in requested for name in names)


def test_viewer_lazy_restore_preserves_xkt_as_model(monkeypatch, tmp_path: Path) -> None:
    metadata_path = tmp_path / "03_IFC_Models" / "project-b" / "metadata" / "viewer.metadata.json"
    files_dir = metadata_path.parent.parent / "files"
    requested: list[tuple[str, list[str], Path]] = []

    monkeypatch.setattr(app, "google_drive_sync_enabled", lambda: True)
    monkeypatch.setattr(
        app,
        "restore_ifc_files_from_drive",
        lambda project_folder, filenames, target_dir: requested.append((project_folder, filenames, target_dir)),
    )

    app._ensure_ifc_local_files(metadata_path, {"filename": "viewer.xkt"})

    assert requested == [("project-b", ["viewer.xkt"], files_dir)]


def test_viewer_lazy_restore_surfaces_drive_failures(monkeypatch, tmp_path: Path) -> None:
    metadata_path = tmp_path / "03_IFC_Models" / "project-c" / "metadata" / "building.metadata.json"
    metadata_path.parent.mkdir(parents=True)

    monkeypatch.setattr(app, "google_drive_sync_enabled", lambda: True)
    monkeypatch.setattr(
        app,
        "restore_ifc_files_from_drive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("network unavailable")),
    )

    with pytest.raises(RuntimeError, match="could not be restored"):
        app._ensure_ifc_local_files(metadata_path, {"filename": "building.ifc"})


def test_project_graph_bounds_default_cold_pack_fanout(monkeypatch) -> None:
    project = {
        "id": "project-a",
        "name": "Project A",
        "packIds": ["common-a", "common-b", "direct-a", "direct-b", "direct-c"],
    }
    captured: dict[str, object] = {}

    monkeypatch.setenv("MODULAR_ONTOLOGY_PROJECT_GRAPH_MAX_PACKS", "3")
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(app, "current_user", lambda _authorization: {"email": "admin@example.com"})
    monkeypatch.setattr(app, "ensure_project_access", lambda _project_id, _user: project)
    monkeypatch.setattr(app, "ensure_pack_access", lambda _pack_id, _user: None)
    monkeypatch.setattr(
        app,
        "list_packs",
        lambda: [
            {"id": "common-a", "commonScoped": True},
            {"id": "common-b", "commonScoped": True},
            {"id": "direct-a", "commonScoped": False},
            {"id": "direct-b", "commonScoped": False},
            {"id": "direct-c", "commonScoped": False},
        ],
    )

    def build(pack_ids, **_kwargs):
        captured["packIds"] = list(pack_ids)
        return {"diagnostics": {}}

    monkeypatch.setattr(app, "build_multi_pack_graph", build)

    result = app.project_graph("project-a", authorization=None)

    assert captured["packIds"] == ["direct-a", "direct-b", "direct-c"]
    assert result["diagnostics"] == {
        "graphCache": "built",
        "projectPackSelectionTruncated": True,
        "projectPacksAvailable": 5,
        "projectPacksLoaded": 3,
        "projectGraphPackLimit": 3,
    }


def test_project_graph_rejects_explicit_pack_fanout_over_limit(monkeypatch) -> None:
    project = {
        "id": "project-a",
        "name": "Project A",
        "packIds": ["pack-a", "pack-b", "pack-c", "pack-d"],
    }
    monkeypatch.setenv("MODULAR_ONTOLOGY_PROJECT_GRAPH_MAX_PACKS", "3")
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(app, "current_user", lambda _authorization: {"email": "admin@example.com"})
    monkeypatch.setattr(app, "ensure_project_access", lambda _project_id, _user: project)

    with pytest.raises(app.HTTPException) as exc_info:
        app.project_graph("project-a", pack_ids="pack-a,pack-b,pack-c,pack-d", authorization=None)

    assert exc_info.value.status_code == 400
    assert "at most 3 packs" in str(exc_info.value.detail)


def test_project_graph_accepts_large_folder_selection_without_dropping_packs(monkeypatch) -> None:
    pack_ids = [f"pack-{index:03d}" for index in range(104)]
    project = {"id": "project-large", "name": "Large Project", "packIds": pack_ids}
    captured: dict[str, object] = {}
    registry_calls = 0

    def ensure_registry():
        nonlocal registry_calls
        registry_calls += 1

    monkeypatch.delenv("MODULAR_ONTOLOGY_PROJECT_GRAPH_MAX_PACKS", raising=False)
    monkeypatch.setattr(app, "ensure_runtime_registry", ensure_registry)
    monkeypatch.setattr(app, "current_user", lambda _authorization: {"email": "admin@example.com"})
    monkeypatch.setattr(app, "ensure_project_access", lambda _project_id, _user: project)
    monkeypatch.setattr(
        app,
        "list_packs",
        lambda: [
            {
                "id": pack_id,
                "sizeBytes": 1,
                "counts": {"nodes": 1, "edges": 0, "documents": 0},
            }
            for pack_id in pack_ids
        ],
    )

    def get_graph(active_pack_ids, **_kwargs):
        captured["packIds"] = list(active_pack_ids)
        return {"diagnostics": {}, "activePackIds": list(active_pack_ids)}

    monkeypatch.setattr(app, "get_or_build_project_graph", get_graph)

    result = app.project_graph(
        "project-large",
        pack_ids=",".join(reversed(pack_ids)),
        max_nodes=20_000,
        max_edges=50_000,
        authorization=None,
    )

    assert registry_calls == 1
    assert captured["packIds"] == sorted(pack_ids)
    assert result["activePackIds"] == sorted(pack_ids)
    assert result["diagnostics"]["projectPackSelectionTruncated"] is False
    assert result["diagnostics"]["projectPacksLoaded"] == 104
    assert result["diagnostics"]["projectGraphPackLimit"] == 250


def test_project_graph_checks_project_access_before_shared_cache(monkeypatch) -> None:
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(app, "current_user", lambda _authorization: {"email": "viewer@example.com"})

    def deny_project(_project_id, _user):
        raise app.HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(app, "ensure_project_access", deny_project)
    monkeypatch.setattr(
        app,
        "get_or_build_project_graph",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unauthorized requests must never reach the graph cache")
        ),
    )

    with pytest.raises(app.HTTPException) as exc_info:
        app.project_graph("private-project", pack_ids="pack-a", authorization="Bearer viewer")

    assert exc_info.value.status_code == 403


def test_project_graph_preview_miss_fails_fast_with_retry_hint(monkeypatch) -> None:
    project = {
        "id": "project-large",
        "name": "Large Project",
        "packIds": [f"pack-{index:02d}" for index in range(17)],
    }
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(app, "current_user", lambda _authorization: {"email": "admin@example.com"})
    monkeypatch.setattr(app, "ensure_project_access", lambda _project_id, _user: project)
    monkeypatch.setattr(
        app,
        "list_packs",
        lambda: [{"id": pack_id, "counts": {}} for pack_id in project["packIds"]],
    )
    monkeypatch.setattr(
        app,
        "get_or_build_project_graph",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            app.GraphPreviewUnavailableError("preview refresh required")
        ),
    )

    with pytest.raises(app.HTTPException) as exc_info:
        app.project_graph(
            "project-large",
            pack_ids=",".join(project["packIds"]),
            authorization="Bearer admin",
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "5"}
    assert "preview refresh required" in str(exc_info.value.detail)
