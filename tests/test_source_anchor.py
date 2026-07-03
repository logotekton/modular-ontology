from __future__ import annotations

import json
from pathlib import Path

import pytest

from modular_ontology.source_anchor import (
    SCHEMA_VERSION,
    SourceAnchor,
    anchors_from_chunk,
    anchors_from_node_properties,
    coverage,
    open_hint,
)


def test_revit_element_anchor_prefers_unique_id_for_exact_confidence() -> None:
    anchors = anchors_from_node_properties(
        {
            "document_key": "rvt:yeoju",
            "source_element_id": 2074383,
            "unique_id": "8f4a-revit-unique",
            "ifc_guid": "2O2Fr$abc",
            "category": "Walls",
            "workset_name": "1-02-A",
        }
    )

    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor.anchor_type == "revit_element"
    assert anchor.confidence == "exact"
    assert anchor.ids["element_id"] == 2074383
    assert anchor.ids["unique_id"] == "8f4a-revit-unique"
    assert "UniqueId" in open_hint(anchor)
    assert anchor.to_dict()["schema_version"] == SCHEMA_VERSION


def test_revit_element_anchor_from_source_node_id_is_partial() -> None:
    anchors = anchors_from_node_properties(
        {
            "source_node_id": "revit:yeoju:element:2074383",
            "source_file": "elements.jsonl",
        }
    )

    assert anchors[0].anchor_type == "revit_element"
    assert anchors[0].confidence == "partial"
    assert anchors[0].ids["element_id"] == 2074383
    assert "ID로 선택" in open_hint(anchors[0])


def test_boq_sheet_row_anchor_is_exact_when_sheet_and_row_exist() -> None:
    anchors = anchors_from_node_properties(
        {
            "module_type": "1-02-A",
            "item_name": "DF2",
            "source_sheet": "건축_모듈산출근거",
            "source_row": 80,
        }
    )

    assert anchors[0].anchor_type == "boq_sheet_row"
    assert anchors[0].source_kind == "boq"
    assert anchors[0].confidence == "exact"
    assert anchors[0].document_key == "건축_모듈산출근거"
    assert anchors[0].ids["source_row"] == 80
    assert "row 80" in open_hint(anchors[0])


def test_chunk_anchor_collects_metadata_compact_row_and_source_refs() -> None:
    chunk = {
        "chunk_id": "chunk-1",
        "metadata": {
            "derived_file": "bim_objects.jsonl",
            "compact_row": {
                "document_key": "rvt:yeoju",
                "source_element_id": "2074383",
                "workset_name": "1-02-A",
            },
            "source_refs": [
                {
                    "source_file": "elements.jsonl",
                    "source_node_id": "revit:yeoju:element:2074383",
                    "raw_record_hash": "abc123",
                }
            ],
        },
    }

    anchors = anchors_from_chunk(chunk)

    assert len(anchors) == 2
    assert {anchor.anchor_type for anchor in anchors} == {"revit_element"}
    assert {anchor.confidence for anchor in anchors} == {"partial"}
    assert all(anchor.ids["element_id"] == 2074383 for anchor in anchors)


def test_dxf_pdf_and_unknown_anchors() -> None:
    dxf = anchors_from_node_properties(
        {
            "evidence_type": "dxf_entity",
            "source_file": "A-711_창호일람표-1.dxf",
            "entity_type": "TEXT",
            "handle": "2AF",
            "layer": "A-ANNO-TEXT",
        }
    )[0]
    pdf = anchors_from_node_properties({"source_file": "A-711.pdf", "page": 2})[0]
    unknown = anchors_from_node_properties({"name": "no anchor here"})[0]

    assert dxf.anchor_type == "dxf_entity"
    assert dxf.confidence == "exact"
    assert "entity 2AF" in open_hint(dxf)
    assert pdf.anchor_type == "pdf_page"
    assert pdf.confidence == "exact"
    assert unknown.anchor_type == "unknown"
    assert unknown.confidence == "unresolvable"


def test_sheet_and_view_anchors_require_explicit_context() -> None:
    generic_element = anchors_from_node_properties(
        {
            "element_id": 12,
            "unique_id": "element-unique",
            "sheet_number": "A-101",
        }
    )
    sheet = anchors_from_node_properties(
        {
            "record_type": "sheet",
            "element_id": 33,
            "unique_id": "sheet-unique",
            "sheet_number": "A-101",
            "name": "평면도",
        }
    )
    view = anchors_from_node_properties(
        {
            "record_type": "view",
            "element_id": 44,
            "unique_id": "view-unique",
            "view_name": "1F Plan",
            "view_type": "FloorPlan",
        }
    )

    assert {anchor.anchor_type for anchor in generic_element} == {"revit_element", "revit_sheet"}
    generic_sheet = [anchor for anchor in generic_element if anchor.anchor_type == "revit_sheet"][0]
    assert "sheet_unique_id" not in generic_sheet.ids
    assert generic_sheet.confidence == "partial"
    assert sheet[0].anchor_type == "revit_element"
    assert sheet[1].anchor_type == "revit_sheet"
    assert sheet[1].ids["sheet_unique_id"] == "sheet-unique"
    assert sheet[1].confidence == "exact"
    assert view[0].anchor_type == "revit_element"
    assert view[1].anchor_type == "revit_view"
    assert view[1].ids["view_unique_id"] == "view-unique"
    assert view[1].confidence == "exact"


def test_schedule_row_anchor_and_pdf_extension_guard() -> None:
    schedule = anchors_from_node_properties(
        {
            "schedule_unique_id": "schedule-unique",
            "schedule_name": "창호 일람표",
            "row_index": 7,
            "column_index": 3,
        }
    )
    not_pdf = anchors_from_node_properties({"source_file": "schedule_cells.jsonl", "page": 2})

    assert schedule[0].anchor_type == "revit_schedule_row"
    assert schedule[0].confidence == "exact"
    assert "row 7" in open_hint(schedule[0])
    assert not_pdf[0].anchor_type == "unknown"


def test_schedule_row_anchor_preserves_zero_row_and_column() -> None:
    schedule = anchors_from_node_properties(
        {
            "schedule_unique_id": "schedule-unique",
            "schedule_name": "창호 일람표",
            "section": "Header",
            "row_index": 0,
            "column_index": 0,
        }
    )[0]

    assert schedule.anchor_type == "revit_schedule_row"
    assert schedule.confidence == "exact"
    assert schedule.ids["section"] == "Header"
    assert schedule.ids["row"] == 0
    assert schedule.ids["cell"] == 0
    assert "Header > row 0, cell 0" in open_hint(schedule)


def test_dxf_fallback_anchor_prefers_real_dxf_file_and_entity_key() -> None:
    dxf = anchors_from_node_properties(
        {
            "source_file": "drawing_entities.jsonl",
            "dxf_file_name": "A-711_창호일람표-1.dxf",
            "entity_key": "A-711:2AF",
            "entity_type": "TEXT",
            "layer": "A-ANNO-TEXT",
        }
    )[0]

    assert dxf.anchor_type == "dxf_entity"
    assert dxf.confidence == "exact"
    assert dxf.document_key == "A-711_창호일람표-1.dxf"
    assert dxf.ids["source_file"] == "A-711_창호일람표-1.dxf"
    assert dxf.ids["entity_key"] == "A-711:2AF"
    assert "A-711_창호일람표-1.dxf > entity A-711:2AF" in open_hint(dxf)


def test_coverage_counts_resolvable_exact_partial_and_type() -> None:
    anchors = [
        anchors_from_node_properties({"unique_id": "u1", "element_id": 1})[0],
        anchors_from_node_properties({"source_element_id": 2})[0],
        anchors_from_node_properties({"name": "missing"})[0],
    ]

    summary = coverage(anchors)

    assert summary["total"] == 3
    assert summary["resolvable"] == 2
    assert summary["exact"] == 1
    assert summary["partial"] == 1
    assert summary["unresolvable"] == 1
    assert summary["by_type"]["revit_element"] == 2
    assert summary["by_type"]["unknown"] == 1
    assert summary["resolvable_rate"] == 0.6667


def test_schema_file_declares_same_anchor_version_and_types() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).parents[1] / "modular_ontology" / "schemas" / "source_anchor.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert "revit_element" in schema["properties"]["anchor_type"]["enum"]
    assert "boq_sheet_row" in schema["properties"]["anchor_type"]["enum"]

    anchor = anchors_from_node_properties({"unique_id": "u1", "element_id": 1})[0]
    jsonschema.validate(anchor.to_dict(), schema)


def test_source_kind_is_validated() -> None:
    with pytest.raises(ValueError, match="Unsupported source_kind"):
        SourceAnchor(anchor_type="unknown", source_kind="spreadsheet")
