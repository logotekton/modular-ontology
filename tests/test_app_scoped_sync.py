from __future__ import annotations

import os

os.environ.setdefault("MODULAR_ONTOLOGY_ADMIN_YTHONG_PASSWORD", "test-admin-password")
os.environ.setdefault("MODULAR_ONTOLOGY_ADMIN_MWHONG_PASSWORD", "test-admin-password-2")

from modular_ontology import app


def setup_function() -> None:
    app._GOOGLE_DRIVE_SCOPE_SYNC_CACHE.clear()


def test_scoped_sync_returns_disabled_without_calling_sync(monkeypatch) -> None:
    calls = 0

    def sync_fn() -> dict:
        nonlocal calls
        calls += 1
        return {"status": "synced"}

    monkeypatch.setattr(app, "google_drive_sync_enabled", lambda: False)

    assert app._run_scoped_google_drive_sync("users", sync_fn) == {
        "enabled": False,
        "status": "disabled",
    }
    assert calls == 0


def test_scoped_sync_caches_success_and_runs_callback_once(monkeypatch) -> None:
    calls = 0
    callbacks = 0

    def sync_fn() -> dict:
        nonlocal calls
        calls += 1
        return {"status": "synced", "downloaded": ["users.json"]}

    def on_synced() -> None:
        nonlocal callbacks
        callbacks += 1

    monkeypatch.setattr(app, "google_drive_sync_enabled", lambda: True)
    monkeypatch.setattr(app, "_sync_ttl_seconds", lambda scope: 60)

    first = app._run_scoped_google_drive_sync("users", sync_fn, on_synced=on_synced)
    second = app._run_scoped_google_drive_sync("users", sync_fn, on_synced=on_synced)

    assert first == {"enabled": True, "status": "synced", "downloaded": ["users.json"]}
    assert second == {"enabled": True, "status": "cached", "downloaded": ["users.json"]}
    assert calls == 1
    assert callbacks == 1


def test_scoped_sync_force_bypasses_cache(monkeypatch) -> None:
    calls = 0

    def sync_fn() -> dict:
        nonlocal calls
        calls += 1
        return {"status": "synced", "attempt": calls}

    monkeypatch.setattr(app, "google_drive_sync_enabled", lambda: True)
    monkeypatch.setattr(app, "_sync_ttl_seconds", lambda scope: 60)

    app._run_scoped_google_drive_sync("mcp_tokens", sync_fn)
    forced = app._run_scoped_google_drive_sync("mcp_tokens", sync_fn, force=True)

    assert forced == {"enabled": True, "status": "synced", "attempt": 2}
    assert calls == 2


def test_scoped_sync_returns_error_envelope(monkeypatch) -> None:
    monkeypatch.setattr(app, "google_drive_sync_enabled", lambda: True)
    monkeypatch.setattr(app, "_sync_ttl_seconds", lambda scope: 60)

    def sync_fn() -> dict:
        raise RuntimeError("drive unavailable")

    assert app._run_scoped_google_drive_sync("users", sync_fn) == {
        "enabled": True,
        "status": "error",
        "error": "drive unavailable",
    }
