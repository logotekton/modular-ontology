from __future__ import annotations

from pathlib import Path

from modular_ontology.config import is_ephemeral_runtime
from modular_ontology import store


def test_ephemeral_runtime_detects_vercel_and_lambda_environment() -> None:
    local_root = Path("C:/app")

    assert is_ephemeral_runtime(local_root, {"VERCEL": "1"}) is True
    assert is_ephemeral_runtime(local_root, {"AWS_LAMBDA_FUNCTION_NAME": "api"}) is True
    assert is_ephemeral_runtime(local_root, {"LAMBDA_TASK_ROOT": "/var/task"}) is True


def test_ephemeral_runtime_detects_vercel_task_root_without_system_env() -> None:
    assert is_ephemeral_runtime(Path("/var/task"), {}) is True
    assert is_ephemeral_runtime(Path("/var/task/app"), {}) is True


def test_ephemeral_runtime_keeps_local_roots_persistent() -> None:
    assert is_ephemeral_runtime(Path("C:/workspace/modular-ontology"), {}) is False
    assert is_ephemeral_runtime(Path("/srv/modular-ontology"), {}) is False


def test_ephemeral_sqlite_connection_uses_memory_journal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(store, "EPHEMERAL_STORAGE", True)

    conn = store.connect(tmp_path / "runtime.sqlite3")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    finally:
        conn.close()
