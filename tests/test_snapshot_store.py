from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from modular_ontology.snapshot_store import (
    SnapshotManifestError,
    SnapshotRegistryError,
    SnapshotVerificationError,
    activate,
    get_active,
    load_snapshot,
    resolve_snapshot_packs,
    sha256_path,
    verify_snapshot,
)


def _make_pack(root: Path, pack_id: str, body: str = "payload") -> Path:
    pack_path = root / f"{pack_id}.zip"
    with ZipFile(pack_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"pack_id": pack_id}))
        archive.writestr("graph/nodes.jsonl", body)
    return pack_path


def _write_manifest(
    path: Path,
    project_id: str,
    packs: list[dict[str, object]],
    *,
    source_signature: str = "sha256:raw-yeoju-export-v1",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": project_id,
                "source_signature": source_signature,
                "packs": packs,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _entry(
    pack_path: Path,
    pack_id: str,
    role: str,
    *,
    shard_index: int | None = None,
    included: bool = True,
) -> dict[str, object]:
    return {
        "pack_id": pack_id,
        "path": pack_path.name,
        "sha256": sha256_path(pack_path),
        "role": role,
        "shard_index": shard_index,
        "included": included,
    }


def test_snapshot_identity_is_independent_of_json_and_pack_order(tmp_path: Path) -> None:
    model = _make_pack(tmp_path, "yeoju-model")
    quantity = _make_pack(tmp_path, "yeoju-quantity")
    model_entry = _entry(model, "yeoju-model", "model")
    quantity_entry = _entry(quantity, "yeoju-quantity", "quantity")

    first = load_snapshot(
        _write_manifest(tmp_path / "snapshot-a.json", "yeoju", [model_entry, quantity_entry])
    )
    second_manifest = tmp_path / "snapshot-b.json"
    second_manifest.write_text(
        json.dumps(
            {
                "packs": [
                    dict(reversed(list(quantity_entry.items()))),
                    dict(reversed(list(model_entry.items()))),
                ],
                "source_signature": "sha256:raw-yeoju-export-v1",
                "project_id": "yeoju",
                "schema_version": 1,
            },
            indent=4,
        ),
        encoding="utf-8",
    )
    second = load_snapshot(second_manifest)

    assert first.snapshot_hash == second.snapshot_hash
    assert first.snapshot_id == second.snapshot_id


def test_verify_and_resolve_only_included_contiguous_shards(tmp_path: Path) -> None:
    quantity_1 = _make_pack(tmp_path, "quantity-01")
    quantity_2 = _make_pack(tmp_path, "quantity-02")
    retired = tmp_path / "retired-pack-no-longer-present.zip"
    manifest = _write_manifest(
        tmp_path / "snapshot.json",
        "yeoju",
        [
            _entry(quantity_2, "quantity-02", "quantity", shard_index=2),
            {
                "pack_id": "old-model",
                "path": retired.name,
                "sha256": "0" * 64,
                "role": "model",
                "shard_index": None,
                "included": False,
            },
            _entry(quantity_1, "quantity-01", "quantity", shard_index=1),
        ],
    )

    snapshot = load_snapshot(manifest)
    resolved = resolve_snapshot_packs(snapshot, "quantity")
    result = verify_snapshot(snapshot)

    assert [pack.pack_id for pack in resolved] == ["quantity-01", "quantity-02"]
    assert "old-model" not in result.checked_pack_ids
    assert result.valid


@pytest.mark.parametrize(
    ("indices", "expected_code"),
    [([1, 3], "shard_gap"), ([2, 3], "invalid_shard_start")],
)
def test_verify_rejects_broken_shard_sequences(
    tmp_path: Path, indices: list[int], expected_code: str
) -> None:
    packs: list[dict[str, object]] = []
    for index in indices:
        pack_id = f"quantity-{index:02d}"
        pack_path = _make_pack(tmp_path, pack_id)
        packs.append(_entry(pack_path, pack_id, "quantity", shard_index=index))
    snapshot = load_snapshot(_write_manifest(tmp_path / "snapshot.json", "yeoju", packs))

    result = verify_snapshot(snapshot)

    assert not result.valid
    assert expected_code in {issue.code for issue in result.issues}


def test_verify_detects_hash_and_embedded_pack_id_mismatches(tmp_path: Path) -> None:
    wrong_id_pack = _make_pack(tmp_path, "embedded-id")
    declared = _entry(wrong_id_pack, "declared-id", "model")
    manifest = _write_manifest(tmp_path / "snapshot.json", "yeoju", [declared])
    wrong_id_result = verify_snapshot(manifest)

    assert "pack_id_mismatch" in {issue.code for issue in wrong_id_result.issues}

    with wrong_id_pack.open("ab") as stream:
        stream.write(b"tampered")
    tampered_result = verify_snapshot(manifest)
    assert "sha256_mismatch" in {issue.code for issue in tampered_result.issues}


def test_activate_and_get_active_reject_stale_manifest(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path, "yeoju-model")
    manifest_path = _write_manifest(
        tmp_path / "snapshot.json",
        "yeoju",
        [_entry(pack, "yeoju-model", "model")],
    )
    registry_path = tmp_path / "registry" / "active.json"

    activated = activate(manifest_path, registry_path)
    loaded = get_active("yeoju", registry_path)

    assert loaded is not None
    assert loaded.snapshot_id == activated.snapshot_id
    registry_payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry_payload["active"]["yeoju"]["manifest_path"] == "../snapshot.json"

    _write_manifest(
        manifest_path,
        "yeoju",
        [_entry(pack, "yeoju-model", "model")],
        source_signature="sha256:different-export",
    )
    with pytest.raises(SnapshotRegistryError, match="stale"):
        get_active("yeoju", registry_path, verify=False)


def test_activation_requires_a_valid_snapshot(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path, "yeoju-model")
    entry = _entry(pack, "yeoju-model", "model")
    entry["sha256"] = "f" * 64
    manifest = _write_manifest(tmp_path / "snapshot.json", "yeoju", [entry])

    with pytest.raises(SnapshotVerificationError, match="failed verification"):
        activate(manifest, tmp_path / "active.json")


def test_load_rejects_declared_identity_drift(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path, "yeoju-model")
    manifest = _write_manifest(
        tmp_path / "snapshot.json",
        "yeoju",
        [_entry(pack, "yeoju-model", "model")],
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "snapshot-not-the-computed-id"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SnapshotManifestError, match="does not match computed"):
        load_snapshot(manifest)


def test_load_uses_content_addressed_pack_fallback_without_changing_identity(
    tmp_path: Path, monkeypatch
) -> None:
    source = _make_pack(tmp_path, "yeoju-model")
    declared = _entry(source, "yeoju-model", "model")
    declared["path"] = "missing/original/location.zip"
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    manifest = _write_manifest(snapshot_dir / "snapshot.json", "yeoju", [declared])

    pack_root = tmp_path / "canonical-packs"
    pack_root.mkdir()
    fallback = pack_root / f"{declared['sha256']}.zip"
    fallback.write_bytes(source.read_bytes())
    monkeypatch.setenv("MODULAR_ONTOLOGY_SNAPSHOT_PACK_DIR", str(pack_root))

    snapshot = load_snapshot(manifest)

    assert snapshot.packs[0].declared_path == "missing/original/location.zip"
    assert snapshot.packs[0].path == fallback.resolve()
    assert verify_snapshot(snapshot).valid
