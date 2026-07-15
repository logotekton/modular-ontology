from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from modular_ontology import google_drive_sync
from modular_ontology.google_drive_sync import DriveItem, _download_database_file, sync_google_drive_registry_files
from modular_ontology.pack_index import PackFile
from modular_ontology.project_store import create_project
from modular_ontology.query_snapshot import (
    QUERY_DATABASE_FILENAME,
    build_query_database,
    compress_query_database,
    validate_query_database,
)
from modular_ontology.store import connect, index_pack, init_db, search_documents_bm25_multi


def _source_database(tmp_path: Path) -> Path:
    pack_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack_path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "pack_id": "pack-a",
                    "title": "Pack A",
                    "counts": {"documents": 1, "nodes": 1, "edges": 0},
                }
            ),
        )
        archive.writestr("documents/guide.md", "Beam Beam primary evidence")
        archive.writestr(
            "graph/nodes.jsonl",
            json.dumps({"id": "node:a", "labels": ["Element"], "properties": {"name": "Beam"}}),
        )
        archive.writestr("graph/edges.jsonl", "")

    db_path = tmp_path / "source.sqlite3"
    conn = connect(db_path)
    try:
        init_db(conn)
        index_pack(conn, PackFile(pack_path))
    finally:
        conn.close()
    create_project(name="Project A", pack_ids=["pack-a"], db_path=db_path)
    return db_path


def test_query_database_omits_graph_payload_and_preserves_bm25(tmp_path: Path) -> None:
    source = _source_database(tmp_path)
    target = tmp_path / "query.sqlite3"

    stats = build_query_database(source, target)

    assert stats["packs"] == 1
    assert stats["documents"] == 1
    assert stats["bm25Documents"] == 1
    assert stats["projects"] == 1
    assert stats["projectPacks"] == 1
    assert stats["bm25Source"] == "copied"
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    finally:
        conn.close()

    results = search_documents_bm25_multi(["pack-a"], "Beam", limit_per_pack=1, db_path=target)
    assert results is not None
    assert results["pack-a"][0]["path"] == "documents/guide.md"


def test_query_database_compresses_and_validates_after_streaming_round_trip(tmp_path: Path) -> None:
    source = _source_database(tmp_path)
    query_db = tmp_path / "query.sqlite3"
    compressed = tmp_path / QUERY_DATABASE_FILENAME
    restored = tmp_path / "restored.sqlite3"
    build_query_database(source, query_db)

    sizes = compress_query_database(query_db, compressed)
    with gzip.open(compressed, "rb") as input_stream, restored.open("wb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream)

    assert sizes["rawBytes"] == query_db.stat().st_size
    assert sizes["compressedBytes"] == compressed.stat().st_size
    assert validate_query_database(restored)["documents"] == 1


def test_ephemeral_database_sync_prefers_compact_snapshot(monkeypatch, tmp_path: Path) -> None:
    source = _source_database(tmp_path)
    query_db = tmp_path / "query.sqlite3"
    compressed = tmp_path / QUERY_DATABASE_FILENAME
    build_query_database(source, query_db)
    compress_query_database(query_db, compressed)

    class FakeDriveClient:
        def list_children(self, folder_id):
            return [
                DriveItem("full", "modular_ontology.sqlite3", "application/vnd.sqlite3", size=999_999_999),
                DriveItem("compact", QUERY_DATABASE_FILENAME, "application/gzip", size=compressed.stat().st_size),
            ]

        def download_file(self, file_id, target):
            raise AssertionError("serverless sync must not download the full database")

        def download_gzip_file(self, file_id, target):
            assert file_id == "compact"
            with gzip.open(compressed, "rb") as input_stream, target.open("wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)

    monkeypatch.setattr(google_drive_sync, "EPHEMERAL_STORAGE", True)
    target_dir = tmp_path / "runtime" / "01_Database"

    downloaded = _download_database_file(FakeDriveClient(), "database-folder", target_dir)

    target = target_dir / "modular_ontology.sqlite3"
    assert downloaded == [str(target)]
    assert validate_query_database(target)["documents"] == 1


def test_ephemeral_database_sync_never_falls_back_to_oversized_database(monkeypatch, tmp_path: Path) -> None:
    class FakeDriveClient:
        def list_children(self, folder_id):
            return [DriveItem("full", "modular_ontology.sqlite3", "application/vnd.sqlite3", size=999_999_999)]

    monkeypatch.setattr(google_drive_sync, "EPHEMERAL_STORAGE", True)

    with pytest.raises(RuntimeError, match=QUERY_DATABASE_FILENAME):
        _download_database_file(FakeDriveClient(), "database-folder", tmp_path / "runtime")


def test_ephemeral_registry_sync_uses_direct_file_ids(monkeypatch, tmp_path: Path) -> None:
    source = _source_database(tmp_path)
    query_db = tmp_path / "query.sqlite3"
    compressed = tmp_path / QUERY_DATABASE_FILENAME
    build_query_database(source, query_db)
    compress_query_database(query_db, compressed)

    class FakeDriveClient:
        def list_children(self, folder_id):
            raise AssertionError("direct serverless sync must not traverse Drive folders")

        def download_gzip_file(self, file_id, target):
            assert file_id == "query-file"
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(compressed, "rb") as input_stream, target.open("wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file_id, encoding="utf-8")

    monkeypatch.setattr(google_drive_sync, "EPHEMERAL_STORAGE", True)
    monkeypatch.setenv("MODULAR_ONTOLOGY_QUERY_DATABASE_FILE_ID", "query-file")
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE_ID", "users-file")
    monkeypatch.setenv("MODULAR_ONTOLOGY_MCP_TOKENS_FILE_ID", "tokens-file")
    runtime = tmp_path / "runtime"

    result = sync_google_drive_registry_files(
        client=FakeDriveClient(),
        root_folder_id="unused-root",
        data_dir=runtime,
        force=True,
    )

    assert result["status"] == "synced"
    assert result["mode"] == "direct-file-ids"
    assert validate_query_database(runtime / "01_Database" / "modular_ontology.sqlite3")["documents"] == 1
    assert (runtime / "00_Admin" / "users.json").read_text(encoding="utf-8") == "users-file"
    assert (runtime / "00_Admin" / "mcp_tokens.json").read_text(encoding="utf-8") == "tokens-file"


def test_query_database_write_back_uploads_only_compact_snapshot(monkeypatch, tmp_path: Path) -> None:
    source = _source_database(tmp_path)
    uploads: list[dict[str, object]] = []

    def fake_write_back(source_path, folder_names, *, client=None, name=None, mime_type=None):
        uploads.append(
            {
                "name": name,
                "folderNames": folder_names,
                "mimeType": mime_type,
                "bytes": source_path.stat().st_size,
            }
        )
        return {"status": "written", "name": name}

    monkeypatch.setattr(google_drive_sync, "DB_PATH", source)
    monkeypatch.setattr(google_drive_sync, "write_back_google_drive_file", fake_write_back)

    result = google_drive_sync.write_back_query_database_file(client=object())

    assert result["status"] == "synced"
    assert uploads == [
        {
            "name": QUERY_DATABASE_FILENAME,
            "folderNames": ["01_Database"],
            "mimeType": "application/gzip",
            "bytes": result["queryDatabaseStats"]["compressedBytes"],
        }
    ]


def test_query_database_write_back_propagates_skipped_status(monkeypatch, tmp_path: Path) -> None:
    source = _source_database(tmp_path)
    monkeypatch.setattr(google_drive_sync, "DB_PATH", source)
    monkeypatch.setattr(
        google_drive_sync,
        "write_back_google_drive_file",
        lambda *args, **kwargs: {"status": "skipped", "reason": "missing root"},
    )

    result = google_drive_sync.write_back_query_database_file(client=object())

    assert result["status"] == "skipped"


def test_drive_list_children_primes_with_fresh_connection(monkeypatch) -> None:
    client = google_drive_sync.GoogleDriveClient(access_token="test-token")
    calls: list[str] = []

    def payload(path, params):
        calls.append(path)
        return {
            "files": [
                {
                    "id": "pack",
                    "name": "pack.zip",
                    "mimeType": "application/zip",
                    "modifiedTime": "",
                    "size": "10",
                }
            ]
        }

    monkeypatch.setattr(client, "_request_json", lambda path, params: payload(f"pooled:{path}", params))
    monkeypatch.setattr(client, "_request_json_fresh", lambda path, params: payload(f"fresh:{path}", params))

    first = client.list_children("folder")
    second = client.list_children("folder")

    assert calls == ["fresh:files", "pooled:files"]
    assert [item.name for item in first] == ["pack.zip"]
    assert [item.name for item in second] == ["pack.zip"]


def test_drive_client_caches_service_account_token_per_client() -> None:
    tokens = iter(["first-token", "second-token"])
    client = google_drive_sync.GoogleDriveClient(token_provider=lambda: next(tokens))

    first = client._headers()
    second = client._headers()

    assert first == {"Authorization": "Bearer first-token"}
    assert second == first
