from __future__ import annotations

from modular_ontology import app


def test_bootstrap_returns_workspace_in_one_metadata_hydration(monkeypatch) -> None:
    user = object()
    sync_calls = 0
    pack_rows = [
        {
            "id": "pack-a",
            "counts": {"documents": 2, "nodes": 3, "edges": 4},
        }
    ]
    project_rows = [{"id": "project-a", "name": "Project A", "packIds": ["pack-a"]}]
    model_rows = [{"id": "model-a", "projectId": "project-a"}]

    def ensure_registry():
        nonlocal sync_calls
        sync_calls += 1

    monkeypatch.setattr(app, "ensure_runtime_registry", ensure_registry)
    monkeypatch.setattr(app, "extract_bearer_token", lambda _authorization: "token")
    monkeypatch.setattr(app, "get_user_by_token", lambda _token: user)
    monkeypatch.setattr(app, "public_user", lambda _user: {"email": "demo@example.com", "role": "admin"})
    monkeypatch.setattr(app, "is_internal_user", lambda _user: True)
    monkeypatch.setattr(app, "list_packs", lambda: pack_rows)
    monkeypatch.setattr(app, "list_projects", lambda: project_rows)
    monkeypatch.setattr(app, "list_ifc_models", lambda: model_rows)
    monkeypatch.setattr(app, "list_users", lambda: [user])

    payload = app.bootstrap("Bearer token")

    assert sync_calls == 1
    assert payload["authenticated"] is True
    assert payload["packs"] == pack_rows
    assert payload["projects"] == project_rows
    assert payload["ifcModels"] == model_rows
    assert payload["stats"] == {
        "packs": 1,
        "documents": 2,
        "nodes": 3,
        "edges": 4,
        "users": 1,
        "projects": 1,
        "ifcModels": 1,
    }


def test_public_index_status_never_opens_query_database(monkeypatch) -> None:
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: {"enabled": False, "status": "disabled"})
    monkeypatch.setattr(
        app,
        "list_packs",
        lambda: [{"id": "pack-a", "counts": {"documents": 2, "nodes": 3, "edges": 4}}],
    )
    monkeypatch.setattr(app, "list_projects", lambda: [{"id": "project-a"}])
    monkeypatch.setattr(app, "list_ifc_models", lambda: [])
    monkeypatch.setattr(app, "list_users", lambda: [])
    monkeypatch.setattr(
        app,
        "index_stats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("query DB must stay deferred")),
    )
    monkeypatch.setattr(
        app,
        "bm25_index_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("BM25 must stay deferred")),
    )

    result = app.index_status(sync=False)

    assert result["packs"] == 1
    assert result["nodes"] == 3
    assert result["edges"] == 4
    assert result["statsSource"] == "registryCatalog"
    assert result["queryIndex"] == {"state": "deferred", "validated": False}
    assert result["bm25"] == {"available": False, "deferred": True}


def test_query_index_hydration_remains_explicit(monkeypatch) -> None:
    calls = 0

    def sync_query_index():
        nonlocal calls
        calls += 1
        return {"enabled": True, "status": "synced", "databaseIncluded": True}

    monkeypatch.setattr(app, "run_google_drive_registry_sync", sync_query_index)

    result = app.ensure_runtime_query_index()

    assert calls == 1
    assert result["databaseIncluded"] is True


def test_query_sync_satisfies_metadata_cache_and_invalidates_runtime_caches(monkeypatch) -> None:
    query_calls = 0
    metadata_calls = 0
    user_cache_invalidations = 0
    graph_cache_invalidations = 0

    def sync_query_index(*, force=False):
        nonlocal query_calls
        query_calls += 1
        return {
            "status": "synced",
            "synced_at": app.time.time(),
            "databaseIncluded": True,
        }

    def sync_metadata(*, force=False):
        nonlocal metadata_calls
        metadata_calls += 1
        return {
            "status": "synced",
            "synced_at": app.time.time(),
            "databaseIncluded": False,
        }

    def invalidate_users():
        nonlocal user_cache_invalidations
        user_cache_invalidations += 1

    def invalidate_graph():
        nonlocal graph_cache_invalidations
        graph_cache_invalidations += 1

    monkeypatch.setattr(app, "google_drive_sync_enabled", lambda: True)
    monkeypatch.setattr(app, "sync_google_drive_registry_files", sync_query_index)
    monkeypatch.setattr(app, "sync_google_drive_runtime_metadata", sync_metadata)
    monkeypatch.setattr(
        app,
        "sync_google_drive_storage",
        lambda **_kwargs: {"status": "synced", "synced_at": app.time.time()},
    )
    monkeypatch.setattr(app, "invalidate_users_cache", invalidate_users)
    monkeypatch.setattr(app, "invalidate_graph_cache", invalidate_graph)
    monkeypatch.setattr(app, "_sync_ttl_seconds", lambda _scope: 60)
    authenticated_user = object()
    monkeypatch.setattr(app, "extract_bearer_token", lambda _authorization: "token")
    monkeypatch.setattr(app, "get_user_by_token", lambda _token: authenticated_user)
    app._GOOGLE_DRIVE_SCOPE_SYNC_CACHE.clear()
    try:
        query_result = app.run_google_drive_registry_sync()
        current_user = app.current_user("Bearer token")
        cached_metadata = app.run_google_drive_runtime_metadata_sync()

        assert query_result["status"] == "synced"
        assert current_user is authenticated_user
        assert cached_metadata["status"] == "cached"
        assert query_calls == 1
        assert metadata_calls == 0
        assert user_cache_invalidations == 1
        assert graph_cache_invalidations == 1

        forced_metadata = app.run_google_drive_runtime_metadata_sync(force=True)
        full_sync = app.run_google_drive_sync(force=True)

        assert forced_metadata["status"] == "synced"
        assert full_sync["status"] == "synced"
        assert metadata_calls == 1
        assert user_cache_invalidations == 3
        assert graph_cache_invalidations == 3
    finally:
        app._GOOGLE_DRIVE_SCOPE_SYNC_CACHE.clear()
