from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from modular_ontology.google_drive_sync import (
    DriveItem,
    build_pack_registry,
    fetch_pack_file_from_drive,
    sync_google_drive_project_storage,
    sync_google_drive_registry_files,
    sync_google_drive_storage,
)
from modular_ontology.pack_index import PackFile


def _pack_bytes(pack_id: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"pack_id": pack_id}))
        zf.writestr("documents/readme.md", f"# {pack_id}")
    return stream.getvalue()


class FakeProjectPackDrive:
    def __init__(self, pack_payload: bytes, *, modified_time: str = "2026-07-02T00:00:00Z") -> None:
        self.pack_payload = pack_payload
        self.download_calls: list[str] = []
        self.children = {
            "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
            "projects-root": [DriveItem("project", "changed-project", "application/vnd.google-apps.folder")],
            "project": [DriveItem("packs-folder", "ontology-packs", "application/vnd.google-apps.folder")],
            "packs-folder": [
                DriveItem("pack", "changed-pack.zip", "application/x-zip-compressed", modified_time, len(pack_payload)),
            ],
        }

    def list_children(self, folder_id: str) -> list[DriveItem]:
        return self.children.get(folder_id, [])

    def download_file(self, file_id: str, target: Path) -> None:
        self.download_calls.append(file_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.pack_payload)

    def update_pack(self, pack_payload: bytes, *, modified_time: str) -> None:
        self.pack_payload = pack_payload
        self.children["packs-folder"] = [
            DriveItem("pack", "changed-pack.zip", "application/x-zip-compressed", modified_time, len(pack_payload)),
        ]


def test_google_drive_sync_skips_unchanged_project_pack_zip(tmp_path) -> None:
    fake_drive = FakeProjectPackDrive(_pack_bytes("changed-pack"))

    first = sync_google_drive_storage(client=fake_drive, root_folder_id="root", data_dir=tmp_path, force=True)
    second = sync_google_drive_storage(client=fake_drive, root_folder_id="root", data_dir=tmp_path, force=True)

    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / "changed-project__changed-pack.zip"
    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"

    assert str(pack_path) in first["downloaded"]
    assert second["downloaded"] == []
    assert str(pack_path) in second["skipped"]
    assert fake_drive.download_calls == ["pack"]
    assert json.loads(links_path.read_text(encoding="utf-8")) == {"changed-project": ["changed-pack"]}


def test_google_drive_sync_downloads_changed_project_pack_zip(tmp_path) -> None:
    fake_drive = FakeProjectPackDrive(_pack_bytes("changed-pack"))

    sync_google_drive_storage(client=fake_drive, root_folder_id="root", data_dir=tmp_path, force=True)
    fake_drive.update_pack(_pack_bytes("changed-pack-v2"), modified_time="2026-07-02T00:01:00Z")
    second = sync_google_drive_storage(client=fake_drive, root_folder_id="root", data_dir=tmp_path, force=True)

    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / "changed-project__changed-pack.zip"
    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"

    assert str(pack_path) in second["downloaded"]
    assert str(pack_path) not in second["skipped"]
    assert fake_drive.download_calls == ["pack", "pack"]
    assert json.loads(links_path.read_text(encoding="utf-8")) == {"changed-project": ["changed-pack-v2"]}


def test_google_drive_sync_skips_global_packs_but_always_syncs_common(tmp_path) -> None:
    payloads = {
        "project-pack": _pack_bytes("project-pack"),
        "common-pack": _pack_bytes("common-pack"),
        "global-pack": _pack_bytes("global-pack"),
    }

    class FakeSharedPackDrive:
        def __init__(self) -> None:
            self.download_calls: list[str] = []
            self.children = {
                "root": [
                    DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder"),
                    DriveItem("global-packs", "04_Ontology_Packs", "application/vnd.google-apps.folder"),
                ],
                "projects-root": [
                    DriveItem("project", "project-a", "application/vnd.google-apps.folder"),
                    DriveItem("common", "_Common", "application/vnd.google-apps.folder"),
                ],
                "project": [DriveItem("project-packs", "ontology-packs", "application/vnd.google-apps.folder")],
                "common": [DriveItem("common-packs", "ontology-packs", "application/vnd.google-apps.folder")],
                "global-packs": [DriveItem("global-indexed", "indexed", "application/vnd.google-apps.folder")],
                "project-packs": [
                    DriveItem("project-pack", "project-pack.zip", "application/x-zip-compressed", "2026-07-02T00:00:00Z", len(payloads["project-pack"])),
                ],
                "common-packs": [
                    DriveItem("common-pack", "common-pack.zip", "application/x-zip-compressed", "2026-07-02T00:00:00Z", len(payloads["common-pack"])),
                ],
                "global-indexed": [
                    DriveItem("global-pack", "global-pack.zip", "application/x-zip-compressed", "2026-07-02T00:00:00Z", len(payloads["global-pack"])),
                ],
            }

        def list_children(self, folder_id: str) -> list[DriveItem]:
            return self.children.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            self.download_calls.append(file_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payloads[file_id])

    fake_drive = FakeSharedPackDrive()

    result = sync_google_drive_storage(
        client=fake_drive,
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
        include_shared_packs=False,
    )

    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"

    assert result["sharedPacksIncluded"] is False
    # 전역(레거시) 팩 폴더만 스킵 — _Common은 모든 프로젝트 공통 팩이라 항상 동기화
    assert sorted(fake_drive.download_calls) == ["common-pack", "project-pack"]
    assert json.loads(links_path.read_text(encoding="utf-8")) == {
        "__common__": ["common-pack"],
        "project-a": ["project-pack"],
    }


def test_google_drive_project_sync_merges_pack_links_without_touching_other_projects(tmp_path) -> None:
    payloads = {
        "pack-a": _pack_bytes("project-a-pack-v2"),
    }
    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    links_path.parent.mkdir(parents=True, exist_ok=True)
    links_path.write_text(
        json.dumps(
            {
                "__common__": ["common-pack"],
                "project-a": ["project-a-pack-v1"],
                "project-b": ["project-b-pack"],
            }
        ),
        encoding="utf-8",
    )
    indexed_dir = tmp_path / "04_Ontology_Packs" / "indexed"
    indexed_dir.mkdir(parents=True, exist_ok=True)
    (indexed_dir / "project-b__pack.zip").write_bytes(_pack_bytes("project-b-pack"))

    class FakeProjectScopedDrive:
        def __init__(self) -> None:
            self.download_calls: list[str] = []
            self.children = {
                "root": [DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder")],
                "projects-root": [
                    DriveItem("project-a", "project-a", "application/vnd.google-apps.folder"),
                    DriveItem("project-b", "project-b", "application/vnd.google-apps.folder"),
                    DriveItem("common", "_Common", "application/vnd.google-apps.folder"),
                ],
                "project-a": [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")],
                "project-b": [DriveItem("packs-b", "ontology-packs", "application/vnd.google-apps.folder")],
                "common": [DriveItem("packs-common", "ontology-packs", "application/vnd.google-apps.folder")],
                "packs-a": [
                    DriveItem("pack-a", "pack.zip", "application/x-zip-compressed", "2026-07-02T00:00:00Z", len(payloads["pack-a"])),
                ],
                "packs-b": [
                    DriveItem("pack-b", "pack.zip", "application/x-zip-compressed"),
                ],
                "packs-common": [
                    DriveItem("pack-common", "pack.zip", "application/x-zip-compressed"),
                ],
            }

        def list_children(self, folder_id: str) -> list[DriveItem]:
            return self.children.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            self.download_calls.append(file_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payloads[file_id])

    fake_drive = FakeProjectScopedDrive()

    result = sync_google_drive_project_storage(
        "project-a",
        client=fake_drive,
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert result["status"] == "synced"
    assert result["scope"] == "project"
    assert result["packIds"] == ["project-a-pack-v2"]
    assert fake_drive.download_calls == ["pack-a"]
    assert (indexed_dir / "project-a__pack.zip").exists()
    assert (indexed_dir / "project-b__pack.zip").exists()
    assert json.loads(links_path.read_text(encoding="utf-8")) == {
        "__common__": ["common-pack"],
        "project-a": ["project-a-pack-v2"],
        "project-b": ["project-b-pack"],
    }


def test_registry_sync_skips_database_and_hydrates_renamed_project_links(tmp_path) -> None:
    registry = {
        "version": 1,
        "projects": [
            {
                "id": "old-project",
                "name": "Old Project",
                "driveFolderId": "project-folder",
                "packIds": ["project-pack", "common-pack"],
            }
        ],
        "commonPackIds": ["common-pack"],
        "packs": [{"id": "project-pack"}, {"id": "common-pack"}],
    }

    class FakeRegistryDrive:
        def __init__(self) -> None:
            self.download_calls: list[str] = []
            self.children = {
                "root": [
                    DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                    DriveItem("projects", "02_Projects", "application/vnd.google-apps.folder"),
                ],
                "database": [
                    DriveItem("registry", "pack_registry.json", "application/json", size=1024),
                    DriveItem("database-file", "modular_ontology.sqlite3", "application/vnd.sqlite3", size=900_000_000),
                ],
                "projects": [
                    DriveItem("project-folder", "Renamed Project", "application/vnd.google-apps.folder"),
                ],
                "project-folder": [],
            }

        def list_children(self, folder_id: str) -> list[DriveItem]:
            return self.children.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            self.download_calls.append(file_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(registry), encoding="utf-8")

    fake_drive = FakeRegistryDrive()
    result = sync_google_drive_registry_files(
        client=fake_drive,
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
        include_database=False,
    )

    assert result["status"] == "synced"
    assert result["databaseIncluded"] is False
    assert fake_drive.download_calls == ["registry"]
    assert not (tmp_path / "01_Database" / "modular_ontology.sqlite3").exists()
    assert json.loads((tmp_path / "02_Projects" / ".drive-project-pack-links.json").read_text(encoding="utf-8")) == {
        "__common__": ["common-pack"],
        "Renamed Project": ["project-pack", "common-pack"],
    }


def test_pack_index_reads_registry_without_sqlite(monkeypatch, tmp_path) -> None:
    from modular_ontology import pack_index

    registry_path = tmp_path / "01_Database" / "pack_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "packs": [{"id": "pack-a", "title": "Pack A", "counts": {}}],
                "projects": [
                    {
                        "id": "old-project",
                        "name": "Old Project",
                        "driveFolderId": "folder-a",
                        "packIds": ["pack-a"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    project_dir = tmp_path / "02_Projects"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".drive-project-folders.json").write_text(
        json.dumps([{"folderId": "folder-a", "projectId": "new-project", "name": "New Project"}]),
        encoding="utf-8",
    )
    (project_dir / ".drive-project-pack-links.json").write_text(
        json.dumps({"new-project": ["pack-a"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(pack_index, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pack_index, "EPHEMERAL_STORAGE", True)
    monkeypatch.setattr(pack_index, "unique_pack_files", lambda: [])
    monkeypatch.setattr(pack_index, "_db_pack_summaries", lambda: [])
    sync_calls: list[bool] = []
    monkeypatch.setattr(pack_index, "_sync_registry_from_drive", lambda: sync_calls.append(True))

    assert [pack["id"] for pack in pack_index.list_packs()] == ["pack-a"]
    assert sync_calls == [True]
    assert pack_index.list_projects() == [
        {
            "id": "new-project",
            "name": "New Project",
            "company": "",
            "manager": "",
            "discipline": "",
            "description": "",
            "role": "Admin",
            "driveFolderId": "folder-a",
            "packIds": ["pack-a"],
        }
    ]


def test_build_pack_registry_includes_drive_file_mapping(monkeypatch, tmp_path) -> None:
    from modular_ontology import pack_index, project_store

    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__pack.zip"
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_bytes(_pack_bytes("pack-a"))
    (tmp_path / ".google-drive-file-cache.json").write_text(
        json.dumps(
            {
                "04_Ontology_Packs/indexed/project-a__pack.zip": {
                    "id": "drive-pack-a",
                    "name": "pack.zip",
                    "modifiedTime": "2026-07-14T00:00:00Z",
                    "size": pack_path.stat().st_size,
                }
            }
        ),
        encoding="utf-8",
    )
    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    links_path.parent.mkdir(parents=True, exist_ok=True)
    links_path.write_text(json.dumps({"__common__": ["common-pack"]}), encoding="utf-8")
    summary = {"id": "pack-a", "filename": pack_path.name, "title": "Pack A", "counts": {}}
    monkeypatch.setattr(pack_index, "list_packs", lambda: [summary])
    monkeypatch.setattr(pack_index, "unique_pack_files", lambda: [PackFile(pack_path)])
    monkeypatch.setattr(pack_index, "summarize_pack", lambda _pack: summary)
    monkeypatch.setattr(
        project_store,
        "list_projects",
        lambda _packs: [{"id": "project-a", "name": "Project A", "driveFolderId": "folder-a", "packIds": ["pack-a"]}],
    )

    registry_path = build_pack_registry(data_dir=tmp_path)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    assert registry["commonPackIds"] == ["common-pack"]
    assert registry["projects"][0]["packIds"] == ["pack-a"]
    assert registry["packs"][0]["drive"]["fileId"] == "drive-pack-a"


def test_fetch_pack_file_from_drive_downloads_only_requested_pack(tmp_path) -> None:
    payload = _pack_bytes("pack-a")
    registry_path = tmp_path / "01_Database" / "pack_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "packs": [
                    {
                        "id": "pack-a",
                        "filename": "project-a__pack.zip",
                        "sizeBytes": len(payload),
                        "drive": {
                            "fileId": "drive-pack-a",
                            "name": "pack.zip",
                            "modifiedTime": "2026-07-14T00:00:00Z",
                            "sizeBytes": len(payload),
                        },
                    },
                    {
                        "id": "pack-b",
                        "filename": "project-b__pack.zip",
                        "drive": {"fileId": "drive-pack-b", "name": "pack.zip"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakePackDrive:
        def __init__(self) -> None:
            self.download_calls: list[str] = []

        def download_file(self, file_id: str, target: Path) -> None:
            self.download_calls.append(file_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    fake_drive = FakePackDrive()
    target = fetch_pack_file_from_drive(
        "pack-a",
        client=fake_drive,
        data_dir=tmp_path,
        packs_dir=tmp_path / "packs",
    )

    assert target.name == "project-a__pack.zip"
    assert fake_drive.download_calls == ["drive-pack-a"]
    assert zipfile.ZipFile(target).read("manifest.json")

    assert fetch_pack_file_from_drive(
        "pack-a",
        client=fake_drive,
        data_dir=tmp_path,
        packs_dir=tmp_path / "packs",
    ) == target
    assert fake_drive.download_calls == ["drive-pack-a"]

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["packs"][0]["drive"]["modifiedTime"] = "2026-07-14T00:01:00Z"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    assert fetch_pack_file_from_drive(
        "pack-a",
        client=fake_drive,
        data_dir=tmp_path,
        packs_dir=tmp_path / "packs",
    ) == target
    assert fake_drive.download_calls == ["drive-pack-a", "drive-pack-a"]


def test_index_all_packs_prunes_packs_removed_by_drive_sync(monkeypatch, tmp_path) -> None:
    from modular_ontology import store

    db_path = tmp_path / "index.sqlite3"
    conn = store.connect(db_path)
    try:
        store.init_db(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO packs (id, filename, title, source, validation_status, summary_json)
                VALUES ('deleted-pack', 'deleted.zip', 'Deleted', 'test', 'READY', '{}')
                """
            )
            conn.execute(
                "INSERT INTO documents (pack_id, path, title, body) VALUES ('deleted-pack', 'documents/a.md', 'A', 'A')"
            )
    finally:
        conn.close()
    monkeypatch.setattr(store, "unique_pack_files", lambda: [])

    result = store.index_all_packs(db_path=db_path)

    assert result["removedPacks"] == ["deleted-pack"]
    assert result["stats"]["packs"] == 0
    assert result["stats"]["documents"] == 0


def test_database_write_back_also_publishes_pack_registry(monkeypatch) -> None:
    from modular_ontology import app

    calls: list[str] = []
    monkeypatch.setenv("MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID", "root")
    monkeypatch.setattr(
        app,
        "write_back_database_file",
        lambda: calls.append("database") or {"status": "written", "drivePath": "01_Database/modular_ontology.sqlite3"},
    )
    monkeypatch.setattr(
        app,
        "write_back_pack_registry_file",
        lambda: calls.append("registry") or {"status": "written", "drivePath": "01_Database/pack_registry.json"},
    )

    result = app.run_google_drive_write_back("database")

    assert calls == ["database", "registry"]
    assert result["status"] == "written"
    assert result["registry"]["drivePath"] == "01_Database/pack_registry.json"
