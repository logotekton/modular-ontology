from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from modular_ontology.google_drive_sync import (
    DriveItem,
    build_pack_registry,
    fetch_pack_file_from_drive,
    sync_google_drive_project_storage,
    sync_google_drive_registry_files,
    sync_google_drive_storage,
    validate_pack_registry,
    validate_database_against_pack_registry,
    write_back_pack_file,
)
from modular_ontology.pack_index import PackFile


def _pack_bytes(pack_id: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"pack_id": pack_id}))
        zf.writestr("documents/readme.md", f"# {pack_id}")
    return stream.getvalue()


def _query_database_bytes(path: Path, *, pack_id: str, project_id: str) -> bytes:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE packs (id TEXT PRIMARY KEY);
            CREATE TABLE projects (id TEXT PRIMARY KEY);
            CREATE TABLE project_packs (project_id TEXT, pack_id TEXT);
            """
        )
        conn.execute("INSERT INTO packs VALUES (?)", (pack_id,))
        conn.execute("INSERT INTO projects VALUES (?)", (project_id,))
        conn.execute("INSERT INTO project_packs VALUES (?, ?)", (project_id, pack_id))
        conn.commit()
    finally:
        conn.close()
    return path.read_bytes()


def _manifest_zip_bytes(manifest=...) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zf:
        if manifest is not ...:
            zf.writestr("manifest.json", json.dumps(manifest))
    return stream.getvalue()


def _pack_id_from_bytes(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return str(json.loads(zf.read("manifest.json"))["pack_id"])


class FakeProjectPackDrive:
    def __init__(self, pack_payload: bytes, *, modified_time: str = "2026-07-02T00:00:00Z") -> None:
        self.pack_payload = pack_payload
        self.pack_id = _pack_id_from_bytes(pack_payload)
        self.download_calls: list[str] = []
        self.children = {
            "root": [
                DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                DriveItem("projects-root", "02_Projects", "application/vnd.google-apps.folder"),
            ],
            "database": [DriveItem("registry", "pack_registry.json", "application/json", modified_time)],
            "projects-root": [DriveItem("project", "changed-project", "application/vnd.google-apps.folder")],
            "project": [DriveItem("packs-folder", "ontology-packs", "application/vnd.google-apps.folder")],
            "packs-folder": [
                DriveItem("pack", "changed-pack.zip", "application/x-zip-compressed", modified_time, len(pack_payload)),
            ],
        }

    def list_children(self, folder_id: str) -> list[DriveItem]:
        return self.children.get(folder_id, [])

    def download_file(self, file_id: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if file_id == "registry":
            target.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "projects": [
                            {
                                "id": "changed-project",
                                "driveFolderId": "project",
                                "packIds": [self.pack_id],
                            }
                        ],
                        "commonPackIds": [],
                        "packs": [{"id": self.pack_id, "drive": {"fileId": "pack"}}],
                    }
                ),
                encoding="utf-8",
            )
            return
        self.download_calls.append(file_id)
        target.write_bytes(self.pack_payload)

    def update_pack(self, pack_payload: bytes, *, modified_time: str) -> None:
        self.pack_payload = pack_payload
        self.pack_id = _pack_id_from_bytes(pack_payload)
        self.children["database"] = [
            DriveItem("registry", "pack_registry.json", "application/json", modified_time)
        ]
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
        "packs": [
            {"id": "project-pack", "drive": {"fileId": "project-pack-file"}},
            {"id": "common-pack", "drive": {"fileId": "common-pack-file"}},
        ],
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


def test_pack_registry_validation_rejects_missing_drive_mapping_and_unknown_reference() -> None:
    registry = {
        "version": 1,
        "projects": [{"id": "project-a", "packIds": ["pack-a"]}],
        "commonPackIds": [],
        "packs": [{"id": "pack-a"}],
    }

    with pytest.raises(RuntimeError, match="missing a Drive fileId"):
        validate_pack_registry(registry, require_drive_mappings=True)

    registry["commonPackIds"] = ["missing-pack"]
    with pytest.raises(RuntimeError, match="reference unknown packs"):
        validate_pack_registry(registry)


def test_registry_sync_refuses_partial_project_folder_inventory(tmp_path) -> None:
    registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "folder-a", "packIds": ["pack-a"]},
            {"id": "project-b", "driveFolderId": "folder-b", "packIds": ["pack-a"]},
        ],
        "commonPackIds": [],
        "packs": [{"id": "pack-a", "drive": {"fileId": "drive-pack-a"}}],
    }

    class PartialRegistryDrive:
        def list_children(self, folder_id: str) -> list[DriveItem]:
            return {
                "root": [
                    DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                    DriveItem("projects", "02_Projects", "application/vnd.google-apps.folder"),
                ],
                "database": [DriveItem("registry", "pack_registry.json", "application/json")],
                "projects": [DriveItem("folder-a", "Project A", "application/vnd.google-apps.folder")],
                "folder-a": [],
            }.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(registry), encoding="utf-8")

    live_registry = tmp_path / "01_Database" / "pack_registry.json"
    live_registry.parent.mkdir(parents=True)
    live_registry.write_text('{"previous":true}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="listing is incomplete"):
        sync_google_drive_registry_files(
            client=PartialRegistryDrive(),
            root_folder_id="root",
            data_dir=tmp_path,
            force=True,
            include_database=False,
        )

    assert not (tmp_path / "02_Projects" / ".drive-project-folders.json").exists()
    assert json.loads(live_registry.read_text(encoding="utf-8")) == {"previous": True}


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
        lambda _packs, db_path=None: [{"id": "project-a", "name": "Project A", "driveFolderId": "folder-a", "packIds": ["pack-a"]}],
    )

    registry_path = build_pack_registry(data_dir=tmp_path)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    assert registry["commonPackIds"] == []
    assert registry["projects"][0]["packIds"] == ["pack-a"]
    assert registry["packs"][0]["drive"]["fileId"] == "drive-pack-a"
    assert registry["packs"][0]["drive"]["folderPath"] == "02_Projects/project-a/ontology-packs"


def test_build_pack_registry_merges_valid_common_packs_into_projects(monkeypatch, tmp_path) -> None:
    from modular_ontology import pack_index, project_store

    links_path = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    links_path.parent.mkdir(parents=True, exist_ok=True)
    links_path.write_text(
        json.dumps({"__common__": ["common-pack", "missing-pack"]}),
        encoding="utf-8",
    )
    summaries = [
        {"id": "project-pack", "filename": "project-pack.zip", "counts": {}},
        {"id": "common-pack", "filename": "common-pack.zip", "counts": {}},
    ]
    indexed = tmp_path / "04_Ontology_Packs" / "indexed"
    indexed.mkdir(parents=True)
    (indexed / "project-pack.zip").write_bytes(_pack_bytes("project-pack"))
    (indexed / "common-pack.zip").write_bytes(_pack_bytes("common-pack"))
    monkeypatch.setattr(
        pack_index,
        "summarize_pack",
        lambda pack: next(summary for summary in summaries if summary["id"] == pack.path.stem),
    )
    monkeypatch.setattr(
        project_store,
        "list_projects",
        lambda _packs, db_path=None: [{"id": "project-a", "name": "Project A", "packIds": ["project-pack"]}],
    )

    registry_path = build_pack_registry(data_dir=tmp_path)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    assert registry["commonPackIds"] == ["common-pack"]
    assert registry["projects"][0]["packIds"] == ["common-pack", "project-pack"]


def test_fetch_pack_file_from_drive_downloads_only_requested_file_id(tmp_path) -> None:
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


def test_fetch_pack_file_rejects_manifest_mismatch(tmp_path) -> None:
    wrong_payload = _pack_bytes("wrong-pack")
    registry_path = tmp_path / "01_Database" / "pack_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "packs": [
                    {
                        "id": "pack-a",
                        "filename": "project-a__pack.zip",
                        "drive": {"fileId": "drive-pack-a", "sizeBytes": len(wrong_payload)},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakePackDrive:
        def download_file(self, file_id: str, target: Path) -> None:
            assert file_id == "drive-pack-a"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(wrong_payload)

    target = tmp_path / "packs" / "project-a__pack.zip"
    with pytest.raises(ValueError, match="Downloaded pack id mismatch"):
        fetch_pack_file_from_drive(
            "pack-a",
            client=FakePackDrive(),
            data_dir=tmp_path,
            packs_dir=target.parent,
        )
    assert not target.exists()


def test_fetch_pack_file_redownloads_crc_corrupt_matching_cache_entry(tmp_path) -> None:
    payload = _pack_bytes("pack-a")
    corrupt_payload = payload.replace(b"# pack-a", b"! pack-a", 1)
    modified = "2026-07-14T00:00:00Z"
    registry_path = tmp_path / "01_Database" / "pack_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "packs": [
                    {
                        "id": "pack-a",
                        "filename": "project-a__pack.zip",
                        "drive": {
                            "fileId": "drive-pack-a",
                            "name": "pack.zip",
                            "modifiedTime": modified,
                            "sizeBytes": len(payload),
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "packs" / "project-a__pack.zip"
    target.parent.mkdir(parents=True)
    target.write_bytes(corrupt_payload)
    (tmp_path / ".google-drive-file-cache.json").write_text(
        json.dumps(
            {
                "packs/project-a__pack.zip": {
                    "id": "drive-pack-a",
                    "name": "pack.zip",
                    "modifiedTime": modified,
                    "size": len(payload),
                }
            }
        ),
        encoding="utf-8",
    )

    class RepairDrive:
        calls = 0

        def download_file(self, file_id: str, download_target: Path) -> None:
            self.calls += 1
            download_target.parent.mkdir(parents=True, exist_ok=True)
            download_target.write_bytes(payload)

    drive = RepairDrive()
    assert fetch_pack_file_from_drive(
        "pack-a",
        client=drive,
        data_dir=tmp_path,
        packs_dir=target.parent,
    ) == target
    assert drive.calls == 1
    assert target.read_bytes() == payload


def test_fetch_pack_file_evicts_oldest_runtime_cache_entry(monkeypatch, tmp_path) -> None:
    payload = _pack_bytes("pack-a")
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    oldest = packs_dir / "oldest.zip"
    newest = packs_dir / "newest.zip"
    oldest.write_bytes(b"o" * 20)
    newest.write_bytes(b"n" * 20)
    os.utime(oldest, (1, 1))
    os.utime(newest, (2, 2))
    monkeypatch.setenv("MODULAR_ONTOLOGY_PACK_CACHE_MAX_BYTES", str(len(payload) + 20))

    registry_path = tmp_path / "01_Database" / "pack_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "packs": [
                    {
                        "id": "pack-a",
                        "filename": "pack-a.zip",
                        "drive": {"fileId": "drive-pack-a", "sizeBytes": len(payload)},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakePackDrive:
        def download_file(self, file_id: str, target: Path) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    fetch_pack_file_from_drive(
        "pack-a",
        client=FakePackDrive(),
        data_dir=tmp_path,
        packs_dir=packs_dir,
    )

    assert not oldest.exists()
    assert newest.exists()
    assert (packs_dir / "pack-a.zip").exists()


def test_database_registry_validation_counts_common_pack_once(tmp_path) -> None:
    database = tmp_path / "query.sqlite3"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(
            """
            CREATE TABLE packs (id TEXT PRIMARY KEY);
            CREATE TABLE projects (id TEXT PRIMARY KEY);
            CREATE TABLE project_packs (project_id TEXT, pack_id TEXT);
            INSERT INTO packs VALUES ('common-pack'), ('project-pack');
            INSERT INTO projects VALUES ('project-a');
            INSERT INTO project_packs VALUES
              ('project-a', 'common-pack'),
              ('project-a', 'project-pack');
            """
        )
        conn.commit()
    finally:
        conn.close()
    registry = {
        "version": 1,
        "projects": [
            {
                "id": "project-a",
                "packIds": ["common-pack", "project-pack"],
            }
        ],
        "commonPackIds": ["common-pack"],
        "packs": [{"id": "common-pack"}, {"id": "project-pack"}],
    }

    assert validate_database_against_pack_registry(database, registry)["projectPacks"] == 2


def test_registry_sync_rejects_unexpected_drive_project_without_erasing_links(tmp_path) -> None:
    registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "folder-a", "packIds": ["pack-a"]},
        ],
        "commonPackIds": [],
        "packs": [{"id": "pack-a", "drive": {"fileId": "pack-file-a"}}],
    }
    links = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    links.parent.mkdir(parents=True)
    links.write_text(json.dumps({"project-a": ["pack-a"]}), encoding="utf-8")

    class ExtraProjectDrive:
        def list_children(self, folder_id: str) -> list[DriveItem]:
            return {
                "root": [
                    DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                    DriveItem("projects", "02_Projects", "application/vnd.google-apps.folder"),
                ],
                "database": [DriveItem("registry", "pack_registry.json", "application/json")],
                "projects": [
                    DriveItem("folder-a", "project-a", "application/vnd.google-apps.folder"),
                    DriveItem("folder-b", "unexpected-project", "application/vnd.google-apps.folder"),
                ],
            }.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            assert file_id == "registry"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected folder"):
        sync_google_drive_registry_files(
            client=ExtraProjectDrive(),
            root_folder_id="root",
            data_dir=tmp_path,
            force=True,
            include_database=False,
        )

    assert json.loads(links.read_text(encoding="utf-8")) == {"project-a": ["pack-a"]}


def test_full_sync_validates_pack_inventory_before_pruning_cache(tmp_path) -> None:
    registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "folder-a", "packIds": ["pack-a"]},
        ],
        "commonPackIds": [],
        "packs": [{"id": "pack-a", "drive": {"fileId": "expected-pack-file"}}],
    }
    stale_pack = tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__stale.zip"
    stale_pack.parent.mkdir(parents=True)
    stale_pack.write_bytes(_pack_bytes("stale-pack"))
    stale_metadata = tmp_path / "03_IFC_Models" / "project-a" / "metadata" / "stale.metadata.json"
    stale_metadata.parent.mkdir(parents=True)
    stale_metadata.write_text(
        json.dumps(
            {
                "drive": {"file": {"folder": "02_Projects/project-a/ifc-models"}},
            }
        ),
        encoding="utf-8",
    )

    class IncompletePackInventoryDrive:
        def __init__(self) -> None:
            self.downloads: list[str] = []

        def list_children(self, folder_id: str) -> list[DriveItem]:
            return {
                "root": [
                    DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                    DriveItem("projects", "02_Projects", "application/vnd.google-apps.folder"),
                ],
                "database": [DriveItem("registry", "pack_registry.json", "application/json")],
                "projects": [DriveItem("folder-a", "project-a", "application/vnd.google-apps.folder")],
                "folder-a": [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")],
                "packs-a": [DriveItem("wrong-pack-file", "pack-a.zip", "application/zip")],
            }.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            self.downloads.append(file_id)
            if file_id != "registry":
                raise AssertionError("pack download must not start before inventory validation")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(registry), encoding="utf-8")

    drive = IncompletePackInventoryDrive()
    with pytest.raises(RuntimeError, match="pack inventory mismatch"):
        sync_google_drive_storage(
            client=drive,
            root_folder_id="root",
            data_dir=tmp_path,
            force=True,
        )

    assert drive.downloads == ["registry"]
    assert stale_pack.exists()
    assert stale_metadata.exists()


def test_full_sync_reuses_validated_snapshot_and_never_relists_before_prune(tmp_path) -> None:
    payload = _pack_bytes("pack-a")
    modified = "2026-07-15T00:00:00Z"
    registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "folder-a", "packIds": ["pack-a"]},
        ],
        "commonPackIds": [],
        "packs": [
            {
                "id": "pack-a",
                "drive": {
                    "fileId": "pack-file-a",
                    "modifiedTime": modified,
                    "sizeBytes": len(payload),
                },
            }
        ],
    }
    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__pack-a.zip"
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(payload)
    (tmp_path / ".google-drive-file-cache.json").write_text(
        json.dumps(
            {
                "04_Ontology_Packs/indexed/project-a__pack-a.zip": {
                    "id": "pack-file-a",
                    "name": "pack-a.zip",
                    "modifiedTime": modified,
                    "size": len(payload),
                }
            }
        ),
        encoding="utf-8",
    )

    class ChangingListingDrive:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}
            self.pack_downloads = 0

        def list_children(self, folder_id: str) -> list[DriveItem]:
            self.calls[folder_id] = self.calls.get(folder_id, 0) + 1
            if folder_id in {"projects", "folder-a", "packs-a"} and self.calls[folder_id] > 1:
                return []
            return {
                "root": [
                    DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                    DriveItem("projects", "02_Projects", "application/vnd.google-apps.folder"),
                ],
                "database": [DriveItem("registry", "pack_registry.json", "application/json")],
                "projects": [DriveItem("folder-a", "project-a", "application/vnd.google-apps.folder")],
                "folder-a": [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")],
                "packs-a": [
                    DriveItem("pack-file-a", "pack-a.zip", "application/zip", modified, len(payload))
                ],
            }.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if file_id == "registry":
                target.write_text(json.dumps(registry), encoding="utf-8")
                return
            self.pack_downloads += 1
            target.write_bytes(payload)

    drive = ChangingListingDrive()
    sync_google_drive_storage(
        client=drive,
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert drive.calls["projects"] == 1
    assert drive.calls["folder-a"] == 1
    assert drive.calls["packs-a"] == 1
    assert drive.pack_downloads == 0
    assert pack_path.read_bytes() == payload


def test_full_sync_pack_failure_preserves_database_registry_assets_and_links(tmp_path) -> None:
    valid_payload = _pack_bytes("pack-a")
    invalid_payload = valid_payload.replace(b"# pack-a", b"! pack-a", 1)
    modified = "2026-07-15T00:00:00Z"
    registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "folder-a", "packIds": ["pack-a"]},
        ],
        "commonPackIds": [],
        "packs": [
            {
                "id": "pack-a",
                "drive": {
                    "fileId": "pack-file-a",
                    "modifiedTime": modified,
                    "sizeBytes": len(invalid_payload),
                },
            }
        ],
    }
    database_payload = _query_database_bytes(
        tmp_path / "incoming.sqlite3",
        pack_id="pack-a",
        project_id="project-a",
    )
    live_database = tmp_path / "01_Database" / "modular_ontology.sqlite3"
    live_registry = tmp_path / "01_Database" / "pack_registry.json"
    live_database.parent.mkdir(parents=True)
    live_database.write_bytes(b"old-database")
    live_registry.write_bytes(b"old-registry")
    live_pack = tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__pack-a.zip"
    live_pack.parent.mkdir(parents=True)
    live_pack.write_bytes(valid_payload)
    live_links = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    live_links.parent.mkdir(parents=True)
    live_links.write_text(json.dumps({"project-a": ["old-link"]}), encoding="utf-8")

    class BrokenPackDrive:
        children = {
            "root": [
                DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                DriveItem("projects", "02_Projects", "application/vnd.google-apps.folder"),
            ],
            "database": [
                DriveItem("database-file", "modular_ontology.sqlite3", "application/vnd.sqlite3"),
                DriveItem("registry", "pack_registry.json", "application/json"),
            ],
            "projects": [DriveItem("folder-a", "project-a", "application/vnd.google-apps.folder")],
            "folder-a": [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")],
            "packs-a": [
                DriveItem("pack-file-a", "pack-a.zip", "application/zip", modified, len(invalid_payload))
            ],
        }

        def list_children(self, folder_id: str) -> list[DriveItem]:
            return self.children.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if file_id == "registry":
                target.write_text(json.dumps(registry), encoding="utf-8")
            elif file_id == "database-file":
                target.write_bytes(database_payload)
            elif file_id == "pack-file-a":
                target.write_bytes(invalid_payload)

    with pytest.raises(RuntimeError, match="ZIP/manifest validation"):
        sync_google_drive_storage(
            client=BrokenPackDrive(),
            root_folder_id="root",
            data_dir=tmp_path,
            force=True,
        )

    assert live_database.read_bytes() == b"old-database"
    assert live_registry.read_bytes() == b"old-registry"
    assert live_pack.read_bytes() == valid_payload
    assert json.loads(live_links.read_text(encoding="utf-8")) == {"project-a": ["old-link"]}


@pytest.mark.parametrize("mismatch", ["folder", "signature", "duplicate-packs", "duplicate-category"])
def test_full_sync_rejects_wrong_pack_grouping_signature_and_duplicate_folders(
    tmp_path,
    mismatch: str,
) -> None:
    payload = _pack_bytes("pack-a")
    modified = "2026-07-15T00:00:00Z"
    expected_folder = (
        "02_Projects/project-a/ontology-packs/expected"
        if mismatch == "folder"
        else "02_Projects/project-a/ontology-packs"
    )
    registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "folder-a", "packIds": ["pack-a"]},
        ],
        "commonPackIds": [],
        "packs": [
            {
                "id": "pack-a",
                "drive": {
                    "fileId": "pack-file-a",
                    "folderPath": expected_folder,
                    "modifiedTime": modified,
                    "sizeBytes": len(payload),
                },
            }
        ],
    }
    project_children = [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")]
    pack_children: list[DriveItem] = [
        DriveItem(
            "pack-file-a",
            "pack-a.zip",
            "application/zip",
            "wrong-time" if mismatch == "signature" else modified,
            len(payload) + 1 if mismatch == "signature" else len(payload),
        )
    ]
    children: dict[str, list[DriveItem]] = {
        "root": [
            DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
            DriveItem("projects", "02_Projects", "application/vnd.google-apps.folder"),
        ],
        "database": [DriveItem("registry", "pack_registry.json", "application/json")],
        "projects": [DriveItem("folder-a", "project-a", "application/vnd.google-apps.folder")],
        "folder-a": project_children,
        "packs-a": pack_children,
    }
    if mismatch == "folder":
        children["packs-a"] = [DriveItem("wrong-category", "wrong", "application/vnd.google-apps.folder")]
        children["wrong-category"] = pack_children
    elif mismatch == "duplicate-packs":
        project_children.append(DriveItem("packs-b", "ontology-packs", "application/vnd.google-apps.folder"))
        children["packs-b"] = []
    elif mismatch == "duplicate-category":
        children["packs-a"] = [
            DriveItem("category-a", "same", "application/vnd.google-apps.folder"),
            DriveItem("category-b", "same", "application/vnd.google-apps.folder"),
        ]
        children["category-a"] = pack_children
        children["category-b"] = []
        registry["packs"][0]["drive"]["folderPath"] = "02_Projects/project-a/ontology-packs/same"

    class InvalidInventoryDrive:
        def list_children(self, folder_id: str) -> list[DriveItem]:
            return children.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            assert file_id == "registry"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(registry), encoding="utf-8")

    with pytest.raises(RuntimeError, match="pack inventory mismatch"):
        sync_google_drive_storage(
            client=InvalidInventoryDrive(),
            root_folder_id="root",
            data_dir=tmp_path,
            force=True,
        )


def test_full_sync_redownloads_crc_corrupt_cached_pack_before_updating_links(tmp_path) -> None:
    valid_payload = _pack_bytes("pack-a")
    corrupt_payload = valid_payload.replace(b"# pack-a", b"! pack-a", 1)
    modified = "2026-07-15T00:00:00Z"
    registry = {
        "version": 1,
        "projects": [
            {"id": "project-a", "driveFolderId": "folder-a", "packIds": ["pack-a"]},
        ],
        "commonPackIds": [],
        "packs": [
            {
                "id": "pack-a",
                "drive": {
                    "fileId": "pack-file-a",
                    "modifiedTime": modified,
                    "sizeBytes": len(valid_payload),
                },
            }
        ],
    }
    live_pack = tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__pack-a.zip"
    live_pack.parent.mkdir(parents=True)
    live_pack.write_bytes(corrupt_payload)
    (tmp_path / ".google-drive-file-cache.json").write_text(
        json.dumps(
            {
                "04_Ontology_Packs/indexed/project-a__pack-a.zip": {
                    "id": "pack-file-a",
                    "name": "pack-a.zip",
                    "modifiedTime": modified,
                    "size": len(valid_payload),
                }
            }
        ),
        encoding="utf-8",
    )

    class RepairDrive:
        pack_downloads = 0

        def list_children(self, folder_id: str) -> list[DriveItem]:
            return {
                "root": [
                    DriveItem("database", "01_Database", "application/vnd.google-apps.folder"),
                    DriveItem("projects", "02_Projects", "application/vnd.google-apps.folder"),
                ],
                "database": [DriveItem("registry", "pack_registry.json", "application/json")],
                "projects": [DriveItem("folder-a", "project-a", "application/vnd.google-apps.folder")],
                "folder-a": [DriveItem("packs-a", "ontology-packs", "application/vnd.google-apps.folder")],
                "packs-a": [
                    DriveItem("pack-file-a", "pack-a.zip", "application/zip", modified, len(valid_payload))
                ],
            }.get(folder_id, [])

        def download_file(self, file_id: str, target: Path) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if file_id == "registry":
                target.write_text(json.dumps(registry), encoding="utf-8")
            else:
                self.pack_downloads += 1
                target.write_bytes(valid_payload)

    drive = RepairDrive()
    sync_google_drive_storage(
        client=drive,
        root_folder_id="root",
        data_dir=tmp_path,
        force=True,
    )

    assert drive.pack_downloads == 1
    assert live_pack.read_bytes() == valid_payload
    links = json.loads(
        (tmp_path / "02_Projects" / ".drive-project-pack-links.json").read_text(encoding="utf-8")
    )
    assert links == {"project-a": ["pack-a"]}


def test_pack_write_back_records_drive_mapping_for_registry_preflight(monkeypatch, tmp_path) -> None:
    from modular_ontology import google_drive_sync

    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__pack-a.zip"
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(_pack_bytes("pack-a"))
    monkeypatch.setattr(
        google_drive_sync,
        "write_back_google_drive_file",
        lambda *args, **kwargs: {
            "status": "written",
            "upload": {
                "id": "drive-pack-a",
                "name": "pack-a.zip",
                "modifiedTime": "2026-07-15T00:00:00Z",
            },
        },
    )

    write_back_pack_file(
        pack_path,
        project_id="project-a",
        data_dir=tmp_path,
    )

    cache = json.loads((tmp_path / ".google-drive-file-cache.json").read_text(encoding="utf-8"))
    assert cache["04_Ontology_Packs/indexed/project-a__pack-a.zip"]["id"] == "drive-pack-a"


@pytest.mark.parametrize(
    ("project_id", "filename", "expected_path"),
    [
        (
            "project-a",
            "project-a__category-a__pack-a.zip",
            ["02_Projects", "project-a", "ontology-packs", "category-a"],
        ),
        (
            "_Common",
            "_Common__category-a__pack-a.zip",
            ["02_Projects", "_Common", "category-a", "ontology-packs"],
        ),
    ],
)
def test_pack_write_back_preserves_drive_category_grouping(
    monkeypatch,
    tmp_path,
    project_id: str,
    filename: str,
    expected_path: list[str],
) -> None:
    from modular_ontology import google_drive_sync

    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / filename
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(_pack_bytes("pack-a"))
    captured: dict[str, object] = {}

    def fake_write(source, drive_folder_path, **kwargs):
        captured["path"] = drive_folder_path
        captured["name"] = kwargs.get("name")
        return {
            "status": "written",
            "upload": {"id": "drive-pack-a", "name": "pack-a.zip"},
        }

    monkeypatch.setattr(google_drive_sync, "write_back_google_drive_file", fake_write)

    write_back_pack_file(
        pack_path,
        project_id=project_id,
        data_dir=tmp_path,
    )

    assert captured == {"path": expected_path, "name": "pack-a.zip"}


@pytest.mark.parametrize(
    "payload",
    [
        b"not-a-zip",
        _manifest_zip_bytes(),
        _manifest_zip_bytes([]),
        _manifest_zip_bytes({}),
        _manifest_zip_bytes({"pack_id": ""}),
    ],
)
def test_lazy_pack_fetch_rejects_invalid_zip_or_missing_manifest(tmp_path, payload: bytes) -> None:
    registry_path = tmp_path / "01_Database" / "pack_registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "packs": [
                    {
                        "id": "pack-a",
                        "filename": "pack-a.zip",
                        "drive": {"fileId": "drive-pack-a", "sizeBytes": len(payload)},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class InvalidPackDrive:
        def download_file(self, file_id: str, target: Path) -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    target = tmp_path / "packs" / "pack-a.zip"
    with pytest.raises(ValueError, match="valid manifest ZIP"):
        fetch_pack_file_from_drive(
            "pack-a",
            client=InvalidPackDrive(),
            data_dir=tmp_path,
            packs_dir=target.parent,
        )
    assert not target.exists()


def test_build_pack_registry_ignores_stale_registry_overlay(monkeypatch, tmp_path) -> None:
    from modular_ontology import project_store

    pack_path = tmp_path / "04_Ontology_Packs" / "indexed" / "pack-a.zip"
    pack_path.parent.mkdir(parents=True)
    pack_path.write_bytes(_pack_bytes("pack-a"))
    stale_registry = tmp_path / "01_Database" / "pack_registry.json"
    stale_registry.parent.mkdir(parents=True)
    stale_registry.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": [],
                "commonPackIds": [],
                "packs": [{"id": "ghost-pack"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(project_store, "list_projects", lambda _packs, db_path=None: [])

    registry = json.loads(build_pack_registry(data_dir=tmp_path).read_text(encoding="utf-8"))

    assert [pack["id"] for pack in registry["packs"]] == ["pack-a"]


def test_build_pack_registry_rejects_duplicate_physical_pack_identity(tmp_path) -> None:
    indexed = tmp_path / "04_Ontology_Packs" / "indexed"
    legacy = tmp_path / "02_Ontology_Packs" / "indexed"
    indexed.mkdir(parents=True)
    legacy.mkdir(parents=True)
    (indexed / "pack-a.zip").write_bytes(_pack_bytes("pack-a"))
    (legacy / "copy-of-pack-a.zip").write_bytes(_pack_bytes("pack-a"))

    with pytest.raises(RuntimeError, match="Duplicate physical ZIP pack_id"):
        build_pack_registry(data_dir=tmp_path)


def test_database_registry_pair_activation_rolls_back_both_files(monkeypatch, tmp_path) -> None:
    from modular_ontology import google_drive_sync

    database = tmp_path / "01_Database" / "modular_ontology.sqlite3"
    registry = tmp_path / "01_Database" / "pack_registry.json"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"old-database")
    registry.write_bytes(b"old-registry")
    staging = tmp_path / "staging" / "01_Database"
    staging.mkdir(parents=True)
    staged_database = staging / "modular_ontology.sqlite3"
    staged_registry = staging / "pack_registry.json"
    staged_database.write_bytes(b"new-database")
    staged_registry.write_bytes(b"new-registry")
    original_replace = os.replace
    failed = False

    def fail_registry_activation(source, target):
        nonlocal failed
        if not failed and Path(source) == staged_registry and Path(target) == registry:
            failed = True
            raise OSError("simulated registry activation failure")
        return original_replace(source, target)

    monkeypatch.setattr(google_drive_sync.os, "replace", fail_registry_activation)

    with pytest.raises(OSError, match="simulated registry activation failure"):
        google_drive_sync._activate_staged_database_registry(
            data_dir=tmp_path,
            staged_registry=staged_registry,
            staged_database=staged_database,
        )

    assert database.read_bytes() == b"old-database"
    assert registry.read_bytes() == b"old-registry"


def test_generation_activation_rolls_back_project_assets_and_links_with_pair(
    monkeypatch,
    tmp_path,
) -> None:
    from modular_ontology import google_drive_sync

    database = tmp_path / "01_Database" / "modular_ontology.sqlite3"
    registry = tmp_path / "01_Database" / "pack_registry.json"
    pack = tmp_path / "04_Ontology_Packs" / "indexed" / "project-a__pack-a.zip"
    links = tmp_path / "02_Projects" / ".drive-project-pack-links.json"
    folders = tmp_path / "02_Projects" / ".drive-project-folders.json"
    for target, payload in (
        (database, b"old-database"),
        (registry, b"old-registry"),
        (pack, b"old-pack"),
        (links, b"old-links"),
        (folders, b"old-folders"),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    staging_root = tmp_path / "staging"
    staging_database = staging_root / "01_Database" / "modular_ontology.sqlite3"
    staging_registry = staging_root / "01_Database" / "pack_registry.json"
    staging_assets = staging_root / "project-assets"
    staged_pack = staging_assets / "04_Ontology_Packs" / "indexed" / pack.name
    staged_links = staging_assets / "02_Projects" / links.name
    staged_folders = staging_assets / "02_Projects" / folders.name
    for target, payload in (
        (staging_database, b"new-database"),
        (staging_registry, b"new-registry"),
        (staged_pack, b"new-pack"),
        (staged_links, b"new-links"),
        (staged_folders, b"new-folders"),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    original_replace = os.replace
    failed = False

    def fail_registry_activation(source, target):
        nonlocal failed
        if not failed and Path(source) == staging_registry and Path(target) == registry:
            failed = True
            raise OSError("simulated generation commit failure")
        return original_replace(source, target)

    monkeypatch.setattr(google_drive_sync.os, "replace", fail_registry_activation)

    with pytest.raises(OSError, match="generation commit failure"):
        google_drive_sync._activate_staged_database_registry(
            data_dir=tmp_path,
            staged_registry=staging_registry,
            staged_database=staging_database,
            staged_project_assets=staging_assets,
        )

    assert database.read_bytes() == b"old-database"
    assert registry.read_bytes() == b"old-registry"
    assert pack.read_bytes() == b"old-pack"
    assert links.read_bytes() == b"old-links"
    assert folders.read_bytes() == b"old-folders"


def test_serverless_metadata_snapshot_does_not_walk_pack_categories() -> None:
    from modular_ontology import google_drive_sync

    folder = "application/vnd.google-apps.folder"

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.children = {
                "projects": [DriveItem("project", "project-a", folder)],
                "project": [
                    DriveItem("metadata", "metadata", folder),
                    DriveItem("ifc", "ifc-models", folder),
                    DriveItem("packs", "ontology-packs", folder),
                ],
                "metadata": [],
                "ifc": [],
                "packs": [DriveItem("category", "drawings", folder)],
                "category": [DriveItem("pack", "pack.zip", "application/zip")],
            }

        def list_children(self, folder_id: str):
            self.calls.append(folder_id)
            return self.children.get(folder_id, [])

    client = FakeClient()
    snapshot = google_drive_sync._capture_project_metadata_inventory(client, "projects")

    assert client.calls == ["projects", "project", "metadata", "ifc"]
    assert snapshot.list_children("projects")[0].name == "project-a"
    with pytest.raises(RuntimeError, match="snapshot is incomplete"):
        snapshot.list_children("packs")
