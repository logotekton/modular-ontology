from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from modular_ontology.google_drive_sync import DriveItem, sync_google_drive_project_storage, sync_google_drive_storage


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


def test_google_drive_sync_can_skip_shared_packs_for_admin_button(tmp_path) -> None:
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
    assert fake_drive.download_calls == ["project-pack"]
    assert json.loads(links_path.read_text(encoding="utf-8")) == {"project-a": ["project-pack"]}


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
