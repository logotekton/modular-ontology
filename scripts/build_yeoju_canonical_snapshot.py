"""Build the pinned Yeoju canonical snapshot manifest.

The manifest is deliberately generated from the exact ZIP artifacts selected
for the 2026 Smart Construction Challenge benchmark.  Running this helper is
also a guard against silently accepting another rebuild: every expected suite
size, embedded pack ID, and duplicate ID is checked before output is written.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from zipfile import ZipFile


REPOSITORY = Path(__file__).resolve().parents[1]
CONTEST = REPOSITORY.parent / "24.공모전" / "01.스마트건설챌린지"
BASE_SUITE = (
    CONTEST
    / "온톨로지팩"
    / "여주"
    / "건축"
    / "revit-yeoju-architecture-acadsharp-schema-0_8_4-20260710_092149-rebuild-20260710_102852"
    / "zips"
)
DRAWING_SUITE = (
    CONTEST
    / "온톨로지팩"
    / "여주"
    / "건축"
    / "revit-yeoju-architecture-drawing-representation-20260710_141336"
    / "zips"
)
SPEC_SUITE = CONTEST / "온톨로지팩" / "시방서_v0.2.0_universal"
BOQ_PACK = CONTEST / "99.팩테스트" / "여주_건축_내역서_온톨로지팩.zip"
LAW_PACK = CONTEST / "온톨로지팩" / "법규" / "kr_building_law-ontology-pack.zip"
OUTPUT = REPOSITORY / "snapshots" / "YEOJU-CANONICAL-20260710-092149-R1.json"

CANONICAL_SNAPSHOT_ID = "YEOJU-CANONICAL-20260710-092149-R1"
SOURCE_COMPOSITE_SHA256 = "B17100532752E6012050F421107AAA80BB9F7E7DFB934E4C98346F852286EEBE"
EXPORTED_AT = "2026-07-10T09:21:49.3733692+09:00"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pack_manifest(path: Path) -> dict[str, Any]:
    with ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        exact = [name for name in names if name.casefold() == "manifest.json"]
        nested = [name for name in names if name.casefold().endswith("/manifest.json")]
        candidates = exact or nested
        if len(candidates) != 1:
            raise ValueError(f"{path} must contain exactly one readable manifest.json")
        payload = json.loads(archive.read(candidates[0]).decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} manifest must be a JSON object")
    pack_id = payload.get("pack_id", payload.get("id"))
    if not isinstance(pack_id, str) or not pack_id.strip():
        raise ValueError(f"{path} manifest has no pack_id")
    return payload


def _declared_path(path: Path) -> str:
    return os.path.relpath(path.resolve(), OUTPUT.parent.resolve()).replace(os.sep, "/")


def _entry(
    path: Path,
    role: str,
    *,
    suite: str,
    shard_index: int | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    source_manifest = _pack_manifest(path)
    source = source_manifest.get("source")
    source = source if isinstance(source, dict) else {}
    entry: dict[str, Any] = {
        "pack_id": source_manifest.get("pack_id", source_manifest.get("id")),
        "path": _declared_path(path),
        "sha256": _sha256(path),
        "role": role,
        "shard_index": shard_index,
        "included": True,
        "suite": suite,
    }
    version = source_manifest.get("version")
    if isinstance(version, str) and version.strip():
        entry["version"] = version.strip()
    schema_version = source.get("schema_version", source_manifest.get("schema_version"))
    if isinstance(schema_version, str) and schema_version.strip():
        entry["source_schema_version"] = schema_version.strip()
    exported_at = source.get("exported_at", source_manifest.get("exported_at"))
    if isinstance(exported_at, str) and exported_at.strip():
        entry["exported_at"] = exported_at.strip()
    return entry


def _numbered_index(name: str) -> int:
    match = re.search(r"-(\d{2})\.zip$", name)
    if match is None:
        raise ValueError(f"cannot derive shard index from {name}")
    return int(match.group(1))


def _base_entries() -> list[dict[str, Any]]:
    paths = sorted(BASE_SUITE.glob("*.zip"), key=lambda path: path.name.casefold())
    if len(paths) != 45:
        raise ValueError(f"expected 45 final 0.8.4 base ZIPs, found {len(paths)}")

    drawing_entities = [
        path
        for path in paths
        if re.search(r"-drawing-evidence-acadsharp-\d{2}\.zip$", path.name)
    ]
    drawing_references = [
        path
        for path in paths
        if path.name.endswith("-drawing-evidence-acadsharp-layers.zip")
        or path.name.endswith("-drawing-evidence-revit.zip")
    ]
    model_shards = [
        path
        for path in paths
        if re.search(r"-model-inventory-\d{2}\.zip$", path.name)
    ]
    quantity_shards = [
        path
        for path in paths
        if re.search(r"-quantity-facts-\d{2}\.zip$", path.name)
    ]
    relationship_shards = [
        path
        for path in paths
        if re.search(r"-relationships-\d{2}\.zip$", path.name)
    ]
    if (
        len(drawing_entities),
        len(drawing_references),
        len(model_shards),
        len(quantity_shards),
        len(relationship_shards),
    ) != (
        10,
        2,
        6,
        11,
        11,
    ):
        raise ValueError("final base suite family counts do not match the release contract")

    entries: list[dict[str, Any]] = []
    for path in drawing_entities:
        entries.append(
            _entry(
                path,
                "drawing_entity",
                suite="revit_0_8_4_final_base",
                shard_index=_numbered_index(path.name),
            )
        )
    for path in drawing_references:
        entries.append(
            _entry(path, "drawing_reference", suite="revit_0_8_4_final_base")
        )
    for path in model_shards:
        entries.append(
            _entry(
                path,
                "model_inventory",
                suite="revit_0_8_4_final_base",
                shard_index=_numbered_index(path.name),
            )
        )
    entries.append(
        _entry(
            BASE_SUITE / "revit-yeoju-architecture-model-inventory-aggregates.zip",
            "inventory_aggregate",
            suite="revit_0_8_4_final_base",
        )
    )
    entries.append(
        _entry(
            BASE_SUITE / "revit-yeoju-architecture-project-mcp-bundle.zip",
            "project_bundle",
            suite="revit_0_8_4_final_base",
        )
    )
    entries.append(
        _entry(
            BASE_SUITE / "revit-yeoju-architecture-project-references.zip",
            "project_reference",
            suite="revit_0_8_4_final_base",
        )
    )
    for path in quantity_shards:
        entries.append(
            _entry(
                path,
                "model_quantity",
                suite="revit_0_8_4_final_base",
                shard_index=_numbered_index(path.name),
            )
        )
    entries.append(
        _entry(
            BASE_SUITE / "revit-yeoju-architecture-quantity-aggregates.zip",
            "quantity_aggregate",
            suite="revit_0_8_4_final_base",
        )
    )
    for path in relationship_shards:
        entries.append(
            _entry(
                path,
                "model_relationship",
                suite="revit_0_8_4_final_base",
                shard_index=_numbered_index(path.name),
            )
        )
    entries.append(
        _entry(
            BASE_SUITE / "revit-yeoju-architecture-spec-law-bridge.zip",
            "bridge",
            suite="revit_0_8_4_final_base",
        )
    )
    if len(entries) != 45:
        raise AssertionError(f"base entry assembly produced {len(entries)} entries")
    return entries


def _drawing_entries() -> list[dict[str, Any]]:
    paths = sorted(DRAWING_SUITE.glob("*.zip"), key=lambda path: path.name.casefold())
    if len(paths) != 21:
        raise ValueError(f"expected 21 drawing-representation ZIPs, found {len(paths)}")
    family_contracts = (
        ("-representation-index-", "drawing_representation_index", 5),
        ("-representation-links-primary-", "drawing_representation_primary", 6),
        ("-representation-links-review-", "drawing_representation_review", 3),
        ("-representation-topology-", "drawing_representation_topology", 6),
    )
    entries: list[dict[str, Any]] = []
    selected: set[Path] = set()
    for marker, role, expected_count in family_contracts:
        family = [path for path in paths if marker in path.name]
        if len(family) != expected_count:
            raise ValueError(f"expected {expected_count} {role} ZIPs, found {len(family)}")
        for path in family:
            selected.add(path)
            entries.append(
                _entry(
                    path,
                    role,
                    suite="drawing_representation_20260710_141336",
                    shard_index=_numbered_index(path.name),
                )
            )
    qa_paths = [path for path in paths if "-representation-qa-" in path.name]
    if len(qa_paths) != 1:
        raise ValueError(f"expected 1 drawing representation QA ZIP, found {len(qa_paths)}")
    selected.update(qa_paths)
    entries.append(
        _entry(
            qa_paths[0],
            "drawing_representation_qa",
            suite="drawing_representation_20260710_141336",
        )
    )
    if selected != set(paths):
        raise ValueError(f"unclassified drawing representation ZIPs: {sorted(set(paths) - selected)}")
    return entries


def _spec_entries() -> list[dict[str, Any]]:
    paths = sorted(SPEC_SUITE.glob("*.zip"), key=lambda path: path.name.casefold())
    if len(paths) != 105:
        raise ValueError(f"expected 105 common specification ZIPs, found {len(paths)}")
    entries = []
    for path in paths:
        entries.append(_entry(path, "spec", suite="common_specification_105"))
    return entries


def build_manifest() -> dict[str, Any]:
    packs = [
        *_base_entries(),
        *_drawing_entries(),
        _entry(BOQ_PACK, "boq", suite="yeoju_semantic_boq_v0_1_0"),
        *_spec_entries(),
        _entry(LAW_PACK, "law", suite="kr_building_law_v0_2_0"),
    ]
    ids = [str(pack["pack_id"]) for pack in packs]
    duplicates = sorted(pack_id for pack_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"canonical pack IDs must be unique: {duplicates}")
    if len(packs) != 173:
        raise AssertionError(f"canonical snapshot must contain 173 ZIPs, got {len(packs)}")

    return {
        "schema_version": 1,
        "project_id": "yeoju",
        "source_signature": f"sha256:{SOURCE_COMPOSITE_SHA256.lower()}",
        "canonical_id": CANONICAL_SNAPSHOT_ID,
        "source_composite_sha256": SOURCE_COMPOSITE_SHA256,
        "policy": {
            "selection": "explicit_allowlist",
            "implicit_sibling_expansion": False,
            "answer_only_from_included_packs": True,
            "model_schema_version": "0.8.4",
            "model_exported_at": EXPORTED_AT,
            "base_suite_release": "20260710_102852",
            "drawing_representation_release": "20260710_141336",
        },
        "excluded_legacy_families": [
            {
                "pattern": "revit-yeoju-architecture-acadsharp-schema-0_8_3-*",
                "reason": "superseded Revit export schema and rebuild family",
            },
            {
                "pattern": "revit-yeoju-architecture-acadsharp-schema-0_8_4-20260710_092149/zips/*",
                "reason": "pre-rebuild intermediate suite",
            },
            {
                "pattern": "revit-yeoju-architecture-acadsharp-schema-0_8_4-20260710_092149-rebuild-20260710_{094317,094754,102723}/zips/*",
                "reason": "non-final 0.8.4 rebuilds",
            },
            {
                "pattern": "revit-yeoju-architecture-schema-0_8_3-*_deprecated_summary_dxf/*",
                "reason": "explicitly deprecated summary-DXF family",
            },
            {
                "pattern": "revit-yeoju-ar-ifc-workset-module-localcrab-pack*",
                "reason": "older IFC-derived Yeoju query source",
            },
            {
                "pattern": "bimgraph_*_185a*",
                "reason": "earlier BIMGraph upload family",
            },
            {
                "pattern": "*yeoju*ifc*{v2,v4,v5,v6,geometry,image,mesh}*",
                "reason": "June IFC geometry and visualization derivatives",
            },
            {
                "pattern": "kordoc_yeoju_modular_boq_20260623*.zip",
                "reason": "duplicate full/lite BOQ retrieval packs",
            },
            {
                "pattern": "팩검수/backup_before_fix/*.zip",
                "reason": "pre-fix duplicate specification packs",
            },
            {
                "pattern": "kr_building_law-ontology-pack versions before 0.2.0",
                "reason": "superseded law snapshot",
            },
        ],
        "packs": packs,
    }


def main() -> None:
    payload = build_manifest()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT} with {len(payload['packs'])} pinned ZIPs")


if __name__ == "__main__":
    main()
