from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import pytest

from modular_ontology import google_drive_sync
from modular_ontology.google_drive_sync import (
    DriveItem,
    _download_database_file,
    sync_google_drive_registry_files,
    sync_google_drive_runtime_metadata,
    sync_google_drive_storage,
)
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
    registry = {
        "version": 1,
        "projects": [
            {
                "id": "project-a",
                "driveFolderId": "project-a",
                "packIds": ["pack-a"],
            }
        ],
        "commonPackIds": [],
        "packs": [
            {"id": "pack-a", "drive": {"fileId": "pack-a-file"}},
        ],
    }

    class FakeDriveClient:
        def list_children(self, folder_id):
            return {
                "root": [DriveItem("projects", "02_Projects", google_drive_sync.FOLDER_MIME)],
                "projects": [DriveItem("project-a", "project-a", google_drive_sync.FOLDER_MIME)],
                "project-a": [DriveItem("ifc", "ifc-models", google_drive_sync.FOLDER_MIME)],
                "ifc": [
                    DriveItem("ifc-file", "model.ifc", "application/octet-stream", size=100),
                    DriveItem("xkt-file", "model.xkt", "application/octet-stream", size=80),
                ],
            }.get(folder_id, [])

        def download_gzip_file(self, file_id, target):
            assert file_id == "query-file"
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(compressed, "rb") as input_stream, target.open("wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(registry) if file_id == "registry-file" else file_id,
                encoding="utf-8",
            )

    monkeypatch.setattr(google_drive_sync, "EPHEMERAL_STORAGE", True)
    monkeypatch.setenv("MODULAR_ONTOLOGY_QUERY_DATABASE_FILE_ID", "query-file")
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE_ID", "users-file")
    monkeypatch.setenv("MODULAR_ONTOLOGY_MCP_TOKENS_FILE_ID", "tokens-file")
    monkeypatch.setenv("MODULAR_ONTOLOGY_PACK_REGISTRY_FILE_ID", "registry-file")
    runtime = tmp_path / "runtime"
    stale_active = runtime / "03_IFC_Models" / "project-a" / "metadata" / "stale.metadata.json"
    stale_deleted = runtime / "03_IFC_Models" / "deleted-project" / "metadata" / "old.metadata.json"
    for target, project_id in ((stale_active, "project-a"), (stale_deleted, "deleted-project")):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "drive": {"file": {"folder": f"02_Projects/{project_id}/ifc-models"}},
                }
            ),
            encoding="utf-8",
        )

    result = sync_google_drive_registry_files(
        client=FakeDriveClient(),
        root_folder_id="root",
        data_dir=runtime,
        force=True,
    )

    assert result["status"] == "synced"
    assert result["mode"] == "direct-file-ids"
    assert validate_query_database(runtime / "01_Database" / "modular_ontology.sqlite3")["documents"] == 1
    assert (runtime / "00_Admin" / "users.json").read_text(encoding="utf-8") == "users-file"
    assert (runtime / "00_Admin" / "mcp_tokens.json").read_text(encoding="utf-8") == "tokens-file"
    assert json.loads((runtime / "01_Database" / "pack_registry.json").read_text(encoding="utf-8")) == registry
    folders = json.loads((runtime / "02_Projects" / ".drive-project-folders.json").read_text(encoding="utf-8"))
    assert [item["projectId"] for item in folders] == ["project-a"]
    assert json.loads((runtime / "02_Projects" / ".drive-project-pack-links.json").read_text(encoding="utf-8")) == {
        "project-a": ["pack-a"],
    }
    metadata = json.loads((runtime / "03_IFC_Models" / "project-a" / "metadata" / "model.metadata.json").read_text(encoding="utf-8"))
    assert metadata["viewerStatus"] == "ready"
    assert metadata["xktPath"].endswith("model.xkt")
    assert not stale_active.exists()
    assert not (runtime / "03_IFC_Models" / "deleted-project").exists()


def test_ephemeral_runtime_metadata_sync_never_downloads_query_database(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = {
        "version": 1,
        "projects": [
            {
                "id": "project-a",
                "driveFolderId": "project-a",
                "packIds": ["pack-a"],
            }
        ],
        "commonPackIds": [],
        "packs": [
            {"id": "pack-a", "drive": {"fileId": "pack-a-file"}},
        ],
    }

    class MetadataOnlyDrive:
        def __init__(self) -> None:
            self.download_calls: list[str] = []
            self.gzip_download_calls: list[str] = []

        def list_children(self, folder_id):
            return {
                "root": [DriveItem("projects", "02_Projects", google_drive_sync.FOLDER_MIME)],
                "projects": [DriveItem("project-a", "project-a", google_drive_sync.FOLDER_MIME)],
                "project-a": [DriveItem("ifc", "ifc-models", google_drive_sync.FOLDER_MIME)],
                "ifc": [
                    DriveItem("ifc-file", "model.ifc", "application/octet-stream", size=100),
                    DriveItem("xkt-file", "model.xkt", "application/octet-stream", size=80),
                ],
            }.get(folder_id, [])

        def download_gzip_file(self, file_id, target):
            self.gzip_download_calls.append(file_id)
            raise AssertionError("metadata-only sync must not download the query database")

        def download_file(self, file_id, target):
            self.download_calls.append(file_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(registry) if file_id == "registry-file" else file_id,
                encoding="utf-8",
            )

    monkeypatch.setattr(google_drive_sync, "EPHEMERAL_STORAGE", True)
    monkeypatch.delenv("MODULAR_ONTOLOGY_QUERY_DATABASE_FILE_ID", raising=False)
    monkeypatch.setenv("MODULAR_ONTOLOGY_USERS_FILE_ID", "users-file")
    monkeypatch.setenv("MODULAR_ONTOLOGY_MCP_TOKENS_FILE_ID", "tokens-file")
    monkeypatch.setenv("MODULAR_ONTOLOGY_PACK_REGISTRY_FILE_ID", "registry-file")
    runtime = tmp_path / "runtime"
    fake_drive = MetadataOnlyDrive()

    result = sync_google_drive_runtime_metadata(
        client=fake_drive,
        root_folder_id="root",
        data_dir=runtime,
    )

    assert result["status"] == "synced"
    assert result["mode"] == "direct-file-ids"
    assert result["databaseIncluded"] is False
    assert fake_drive.gzip_download_calls == []
    assert fake_drive.download_calls == ["registry-file", "users-file", "tokens-file"]
    assert not (runtime / "01_Database" / "modular_ontology.sqlite3").exists()
    assert json.loads((runtime / "01_Database" / "pack_registry.json").read_text(encoding="utf-8")) == registry
    assert (runtime / "00_Admin" / "users.json").read_text(encoding="utf-8") == "users-file"
    assert (runtime / "00_Admin" / "mcp_tokens.json").read_text(encoding="utf-8") == "tokens-file"
    assert json.loads((runtime / "02_Projects" / ".drive-project-pack-links.json").read_text(encoding="utf-8")) == {
        "project-a": ["pack-a"],
    }
    metadata = json.loads(
        (runtime / "03_IFC_Models" / "project-a" / "metadata" / "model.metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["viewerStatus"] == "ready"
    assert metadata["xktPath"].endswith("model.xkt")
    assert (runtime / ".google-drive-registry-metadata-sync.json").exists()
    assert not (runtime / ".google-drive-registry-sync.json").exists()


def test_metadata_profile_does_not_satisfy_database_but_database_satisfies_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[bool] = []

    def fake_direct_sync(client, *, data_dir, root_folder_id, include_database):
        calls.append(include_database)
        return {
            "status": "synced",
            "synced_at": google_drive_sync.time.time(),
            "scope": "registry",
            "databaseIncluded": include_database,
        }

    monkeypatch.setattr(
        google_drive_sync,
        "_sync_serverless_registry_files_by_id",
        fake_direct_sync,
    )
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_REGISTRY_SYNC_TTL_SECONDS", "1800")

    sync_google_drive_registry_files(
        client=object(),
        root_folder_id="root",
        data_dir=tmp_path,
        include_database=False,
    )
    sync_google_drive_registry_files(
        client=object(),
        root_folder_id="root",
        data_dir=tmp_path,
    )
    sync_google_drive_registry_files(
        client=object(),
        root_folder_id="root",
        data_dir=tmp_path,
        include_database=False,
    )

    assert calls == [False, True]
    metadata_marker = json.loads(
        (tmp_path / ".google-drive-registry-metadata-sync.json").read_text(encoding="utf-8")
    )
    assert metadata_marker["databaseIncluded"] is True


def test_registry_sync_profiles_share_one_process_lock(monkeypatch, tmp_path: Path) -> None:
    state_lock = threading.Lock()
    start = threading.Barrier(3)
    active = 0
    max_active = 0
    calls: list[bool] = []
    errors: list[BaseException] = []

    def fake_direct_sync(client, *, data_dir, root_folder_id, include_database):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(include_database)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return {
            "status": "synced",
            "synced_at": google_drive_sync.time.time(),
            "scope": "registry",
            "databaseIncluded": include_database,
        }

    def run(include_database: bool) -> None:
        try:
            start.wait()
            sync_google_drive_registry_files(
                client=object(),
                root_folder_id="root",
                data_dir=tmp_path,
                force=True,
                include_database=include_database,
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    monkeypatch.setattr(
        google_drive_sync,
        "_sync_serverless_registry_files_by_id",
        fake_direct_sync,
    )
    workers = [
        threading.Thread(target=run, args=(False,)),
        threading.Thread(target=run, args=(True,)),
    ]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert not errors
    assert all(not worker.is_alive() for worker in workers)
    assert sorted(calls) == [False, True]
    assert max_active == 1


def test_ephemeral_direct_sync_rejects_mixed_database_registry_generation_without_activation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = _source_database(tmp_path)
    query_db = tmp_path / "query.sqlite3"
    compressed = tmp_path / QUERY_DATABASE_FILENAME
    build_query_database(source, query_db)
    compress_query_database(query_db, compressed)
    mismatched_registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "project-folder", "packIds": ["pack-b"]},
        ],
        "commonPackIds": [],
        "packs": [{"id": "pack-b", "drive": {"fileId": "pack-b-file"}}],
    }

    class MixedGenerationDrive:
        def list_children(self, folder_id):
            return {
                "root": [DriveItem("projects", "02_Projects", google_drive_sync.FOLDER_MIME)],
                "projects": [DriveItem("project-folder", "project-a", google_drive_sync.FOLDER_MIME)],
            }.get(folder_id, [])

        def download_gzip_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(compressed, "rb") as input_stream, target.open("wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(mismatched_registry), encoding="utf-8")

    monkeypatch.setattr(google_drive_sync, "EPHEMERAL_STORAGE", True)
    monkeypatch.setenv("MODULAR_ONTOLOGY_QUERY_DATABASE_FILE_ID", "query-file")
    monkeypatch.setenv("MODULAR_ONTOLOGY_PACK_REGISTRY_FILE_ID", "registry-file")
    runtime = tmp_path / "runtime"
    live_database = runtime / "01_Database" / "modular_ontology.sqlite3"
    live_registry = runtime / "01_Database" / "pack_registry.json"
    live_database.parent.mkdir(parents=True)
    live_database.write_bytes(b"previous-database")
    live_registry.write_text('{"previous":true}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="generation mismatch"):
        sync_google_drive_registry_files(
            client=MixedGenerationDrive(),
            root_folder_id="root",
            data_dir=runtime,
            force=True,
        )

    assert live_database.read_bytes() == b"previous-database"
    assert json.loads(live_registry.read_text(encoding="utf-8")) == {"previous": True}


@pytest.mark.parametrize(
    "sync_fn",
    [sync_google_drive_storage, sync_google_drive_registry_files],
    ids=["full", "registry"],
)
def test_non_ephemeral_sync_rejects_mixed_database_registry_generation_without_activation(
    sync_fn,
    tmp_path: Path,
) -> None:
    source = _source_database(tmp_path)
    mismatched_registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "project-folder", "packIds": ["pack-b"]},
        ],
        "commonPackIds": [],
        "packs": [{"id": "pack-b", "drive": {"fileId": "pack-b-file"}}],
    }

    class MixedFullGenerationDrive:
        def list_children(self, folder_id):
            return {
                "root": [
                    DriveItem("database", "01_Database", google_drive_sync.FOLDER_MIME),
                    DriveItem("projects", "02_Projects", google_drive_sync.FOLDER_MIME),
                ],
                "database": [
                    DriveItem("registry", "pack_registry.json", "application/json"),
                    DriveItem("database-file", "modular_ontology.sqlite3", "application/vnd.sqlite3"),
                ],
                "projects": [DriveItem("project-folder", "project-a", google_drive_sync.FOLDER_MIME)],
            }.get(folder_id, [])

        def download_file(self, file_id, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            if file_id == "registry":
                target.write_text(json.dumps(mismatched_registry), encoding="utf-8")
            elif file_id == "database-file":
                shutil.copy2(source, target)
            else:
                raise AssertionError(file_id)

    runtime = tmp_path / "runtime-full"
    live_database = runtime / "01_Database" / "modular_ontology.sqlite3"
    live_registry = runtime / "01_Database" / "pack_registry.json"
    live_database.parent.mkdir(parents=True)
    live_database.write_bytes(b"previous-full-database")
    live_registry.write_text('{"previous":true}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="generation mismatch"):
        sync_fn(
            client=MixedFullGenerationDrive(),
            root_folder_id="root",
            data_dir=runtime,
            force=True,
        )

    assert live_database.read_bytes() == b"previous-full-database"
    assert json.loads(live_registry.read_text(encoding="utf-8")) == {"previous": True}


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


def test_database_publication_uploads_registry_last_as_commit_marker(monkeypatch, tmp_path: Path) -> None:
    source = _source_database(tmp_path)
    registry_path = tmp_path / "pack_registry.json"
    registry = {
        "version": 1,
        "projects": [{"id": "project-a", "packIds": ["pack-a"]}],
        "commonPackIds": [],
        "packs": [{"id": "pack-a", "drive": {"fileId": "pack-file"}}],
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    uploads: list[str] = []

    def fake_upload(source_path, folder_names, *, client=None, name=None, mime_type=None):
        uploads.append(str(name))
        return {"status": "written", "name": name}

    monkeypatch.setattr(google_drive_sync, "DB_PATH", source)
    monkeypatch.setattr(google_drive_sync, "build_pack_registry", lambda: registry_path)
    monkeypatch.setattr(google_drive_sync, "load_pack_registry", lambda: registry)
    monkeypatch.setattr(google_drive_sync, "write_back_google_drive_file", fake_upload)

    result = google_drive_sync.write_back_database_file(client=object())

    assert result["status"] == "synced"
    assert uploads == [
        "modular_ontology.sqlite3",
        QUERY_DATABASE_FILENAME,
        "pack_registry.json",
    ]
    assert result["registry"]["status"] == "written"


def test_database_publication_does_not_advance_registry_after_query_upload_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = _source_database(tmp_path)
    registry_path = tmp_path / "pack_registry.json"
    registry = {
        "version": 1,
        "projects": [{"id": "project-a", "packIds": ["pack-a"]}],
        "commonPackIds": [],
        "packs": [{"id": "pack-a", "drive": {"fileId": "pack-file"}}],
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    uploads: list[str] = []

    def fail_query_upload(source_path, folder_names, *, client=None, name=None, mime_type=None):
        uploads.append(str(name))
        if name == QUERY_DATABASE_FILENAME:
            return {"status": "skipped", "reason": "simulated query upload failure"}
        return {"status": "written", "name": name}

    monkeypatch.setattr(google_drive_sync, "DB_PATH", source)
    monkeypatch.setattr(google_drive_sync, "build_pack_registry", lambda: registry_path)
    monkeypatch.setattr(google_drive_sync, "load_pack_registry", lambda: registry)
    monkeypatch.setattr(google_drive_sync, "write_back_google_drive_file", fail_query_upload)

    result = google_drive_sync.write_back_database_file(client=object())

    assert result["status"] == "error"
    assert result["registry"]["status"] == "skipped"
    assert uploads == ["modular_ontology.sqlite3", QUERY_DATABASE_FILENAME]


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
