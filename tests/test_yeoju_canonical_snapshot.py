from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

from modular_ontology.snapshot_store import load_snapshot, resolve_snapshot_packs, verify_snapshot


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "snapshots" / "YEOJU-CANONICAL-20260710-092149-R1.json"
CANONICAL_ID = "YEOJU-CANONICAL-20260710-092149-R1"
COMPOSITE_SHA256 = "B17100532752E6012050F421107AAA80BB9F7E7DFB934E4C98346F852286EEBE"


def _payload() -> dict[str, object]:
    return json.loads(MANIFEST.read_text(encoding="utf-8-sig"))


def test_yeoju_manifest_pins_the_release_source_policy() -> None:
    payload = _payload()
    packs = payload["packs"]
    assert isinstance(packs, list)

    assert payload["canonical_id"] == CANONICAL_ID
    assert payload["source_composite_sha256"] == COMPOSITE_SHA256
    assert payload["source_signature"] == f"sha256:{COMPOSITE_SHA256.lower()}"
    assert payload["project_id"] == "yeoju"
    assert len(packs) == 173
    assert all(pack["included"] is True for pack in packs)

    roles = Counter(pack["role"] for pack in packs)
    assert roles == {
        "drawing_entity": 10,
        "drawing_reference": 2,
        "model_inventory": 6,
        "inventory_aggregate": 1,
        "project_bundle": 1,
        "project_reference": 1,
        "model_quantity": 11,
        "quantity_aggregate": 1,
        "model_relationship": 11,
        "bridge": 1,
        "drawing_representation_index": 5,
        "drawing_representation_primary": 6,
        "drawing_representation_review": 3,
        "drawing_representation_qa": 1,
        "drawing_representation_topology": 6,
        "boq": 1,
        "spec": 105,
        "law": 1,
    }
    assert len({pack["pack_id"] for pack in packs}) == len(packs)
    assert len({pack["path"] for pack in packs}) == len(packs)

    policy = payload["policy"]
    assert policy["selection"] == "explicit_allowlist"
    assert policy["implicit_sibling_expansion"] is False
    assert policy["model_schema_version"] == "0.8.4"
    assert policy["model_exported_at"] == "2026-07-10T09:21:49.3733692+09:00"


def test_yeoju_manifest_records_all_legacy_exclusions() -> None:
    exclusions = _payload()["excluded_legacy_families"]
    patterns = "\n".join(item["pattern"] for item in exclusions)

    assert "0_8_3" in patterns
    assert "094317,094754,102723" in patterns
    assert "deprecated_summary_dxf" in patterns
    assert "revit-yeoju-ar-ifc" in patterns
    assert "bimgraph_" in patterns
    assert "kordoc_yeoju_modular_boq" in patterns
    assert "backup_before_fix" in patterns


def test_yeoju_manifest_is_snapshot_store_compatible_and_fully_verifiable() -> None:
    snapshot = load_snapshot(MANIFEST)
    packs = resolve_snapshot_packs(snapshot)
    verification = verify_snapshot(snapshot)

    # snapshot_store owns its content-derived internal identity.  The release
    # ID above is the stable public/project identifier carried as metadata.
    assert snapshot.snapshot_id.startswith("snapshot-")
    assert len(snapshot.snapshot_hash) == 64
    assert len(packs) == 173
    assert verification.valid, [issue.message for issue in verification.issues]
    assert verification.checked_pack_ids == tuple(pack.pack_id for pack in packs)
    assert not verification.warnings

    for pack in packs:
        assert pack.path.is_file()
        assert pack.path.suffix.casefold() == ".zip"
