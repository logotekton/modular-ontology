from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
DERIVING_DIR = ROOT / "tools" / "deriving"
if str(DERIVING_DIR) not in sys.path:
    sys.path.insert(0, str(DERIVING_DIR))

from build_opencrab_pack_suite import compact_source_refs  # noqa: E402
from deriving import source_ref  # noqa: E402


def test_source_ref_preserves_revit_element_anchor_fields_without_changing_source_id() -> None:
    row = {
        "node_id": "revit:sample:element:2074383",
        "element_id": 2074383,
        "unique_id": "8f4a-revit-unique",
        "ifc_guid": "2JeAf55EvF2fKbarU8OXp5",
    }

    ref = source_ref("elements.jsonl", row)

    assert ref["source_id"] == "revit:sample:element:2074383"
    assert ref["source_node_id"] == "revit:sample:element:2074383"
    assert ref["element_id"] == 2074383
    assert ref["unique_id"] == "8f4a-revit-unique"
    assert ref["ifc_guid"] == "2JeAf55EvF2fKbarU8OXp5"


def test_source_ref_preserves_zero_schedule_row_and_column() -> None:
    row = {
        "node_id": "revit:sample:schedule-cell:1",
        "schedule_id": 44,
        "schedule_unique_id": "schedule-unique",
        "schedule_name": "창호 일람표",
        "section": "Header",
        "row": 0,
        "column": 0,
    }

    ref = source_ref("schedule_cells.jsonl", row)

    assert ref["source_id"] == "revit:sample:schedule-cell:1"
    assert ref["schedule_id"] == 44
    assert ref["schedule_unique_id"] == "schedule-unique"
    assert ref["row"] == 0
    assert ref["column"] == 0


def test_source_ref_does_not_promote_material_unique_id_as_element_unique_id() -> None:
    ref = source_ref("materials.jsonl", {"material_id": 12, "unique_id": "material-unique", "name": "STEEL"})

    assert ref["source_id"] == "material-unique"
    assert "unique_id" not in ref
    assert "element_id" not in ref


def test_source_ref_prefers_nested_view_unique_id_over_row_unique_id() -> None:
    ref = source_ref(
        "drawing_stage45_element_representation_links.jsonl",
        {
            "view_id": 77,
            "unique_id": "row-level-other-unique",
            "view": {"element_id": 77, "name": "평면도", "unique_id": "view-unique"},
        },
    )

    assert ref["view_unique_id"] == "view-unique"


def test_compact_source_refs_keeps_enriched_fields_and_zero_values() -> None:
    compact = compact_source_refs(
        {
            "source_refs": [
                {
                    "source_file": "schedule_cells.jsonl",
                    "source_id": "cell-1",
                    "raw_record_hash": "abc",
                    "schedule_id": 44,
                    "row": 0,
                    "column": 0,
                    "unused": "drop-me",
                }
            ]
        }
    )

    assert compact == [
        {
            "source_file": "schedule_cells.jsonl",
            "source_id": "cell-1",
            "raw_record_hash": "abc",
            "schedule_id": 44,
            "row": 0,
            "column": 0,
        }
    ]
