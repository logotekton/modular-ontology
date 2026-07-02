from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from modular_ontology.google_drive_sync import DriveItem, sync_google_drive_storage


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
