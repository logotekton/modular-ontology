from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from modular_ontology.query_contract import PlanBundleContractError, validate_plan_bundle
from modular_ontology.query_engine import QueryExecutionError, execute_query_bundle


def _pack(tmp_path: Path, name: str, nodes: list[dict]) -> tuple[Path, str]:
    path = tmp_path / f"{name}.zip"
    payload = "\n".join(json.dumps(node, ensure_ascii=False) for node in nodes) + "\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("graph/nodes.jsonl", payload)
        archive.writestr("graph/edges.jsonl", "")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _plan(
    entity: str,
    *,
    filters: list[dict] | None = None,
    group_by: list[str] | None = None,
    metrics: list[dict] | None = None,
) -> dict:
    return {
        "project_id": "yeoju",
        "entity": entity,
        "intent": "aggregate",
        "scope": {},
        "filters": filters or [],
        "group_by": group_by or [],
        "metrics": metrics or [],
    }


def _snapshot(tmp_path: Path) -> dict:
    project_path, project_hash = _pack(
        tmp_path,
        "project",
        [
            {
                "id": "project:yeoju",
                "node_type": "BIMProject",
                "properties": {"revit_version": "2025", "project_name": "Yeoju"},
            },
            {
                "id": "export:yeoju",
                "node_type": "BIMExportRun",
                "properties": {
                    "schema_version": "0.8.4",
                    "exported_at": "2026-07-10T09:21:49.3733692+09:00",
                },
            },
        ],
    )
    drawing_path, drawing_hash = _pack(
        tmp_path,
        "drawing",
        [
            {
                "id": "sheet:A-531",
                "node_type": "DrawingSheet",
                "properties": {"sheet_number": "A-531", "sheet_name": "접합부 상세도"},
            },
            {
                "id": "sheet:A-531:dxf",
                "node_type": "DxfDrawingFile",
                "properties": {
                    "sheet_id": 531,
                    "sheet_number": "A-531",
                    "sheet_name": "접합부 상세도",
                    "dxf_file_name": "A-531_접합부 상세도.dxf",
                    "file_size_bytes": 287649,
                    "source_kind": "sheet_dxf",
                    "record_type": "sheet_dxf",
                },
            },
            {
                "id": "sheet:A-531:pdf",
                "node_type": "SheetPdfFileResource",
                "properties": {
                    "sheet_id": 531,
                    "sheet_number": "A-531",
                    "sheet_name": "접합부 상세도",
                    "pdf_file_name": "A-531_접합부 상세도.pdf",
                    "file_size_bytes": 100520,
                    "status": "exported",
                    "source_kind": "sheet_pdf",
                    "record_type": "sheet_pdf",
                },
            },
        ],
    )
    layer_path, layer_hash = _pack(
        tmp_path,
        "drawing-layers",
        [
            {
                "id": "dxf:file:content-hash",
                "node_type": "DxfDrawingFile",
                "properties": {
                    "dxf_file_name": "A-531_접합부 상세도.dxf",
                    "source_context": {
                        "sheet_id": 531,
                        "sheet_number": "A-531",
                        "sheet_name": "접합부 상세도",
                    },
                    "source_kind": "sheet_dxf",
                },
            }
        ],
    )
    return {
        "snapshot_id": "yeoju-test",
        "project_id": "yeoju",
        "packs": [
            {
                "pack_id": "project",
                "path": str(project_path),
                "sha256": project_hash,
                "role": "project_reference",
            },
            {
                "pack_id": "drawing",
                "path": str(drawing_path),
                "sha256": drawing_hash,
                "role": "drawing_reference",
            },
            {
                "pack_id": "drawing-layers",
                "path": str(layer_path),
                "sha256": layer_hash,
                "role": "drawing_reference",
            },
        ],
    }


def test_strict_bundle_queries_project_metadata_and_one_canonical_drawing_file(
    tmp_path: Path,
) -> None:
    complete_file_filters = [
        {"field": "file_size_bytes", "operator": "exists", "value": True},
        {"field": "file_size_bytes", "operator": "gt", "value": 0},
        {"field": "sheet_number", "operator": "eq", "value": "A-531"},
    ]
    bundle = {
        "project_id": "yeoju",
        "plans": {
            "project": _plan(
                "project_metadata",
                filters=[{"field": "node_type", "operator": "eq", "value": "BIMProject"}],
                metrics=[
                    {"name": "count_distinct", "field": "id", "alias": "node_count"},
                    {"name": "collect_distinct", "field": "revit_version", "alias": "versions"},
                ],
            ),
            "export": _plan(
                "project_metadata",
                filters=[{"field": "node_type", "operator": "eq", "value": "BIMExportRun"}],
                metrics=[
                    {"name": "count_distinct", "field": "id", "alias": "node_count"},
                    {"name": "collect_distinct", "field": "schema_version", "alias": "schemas"},
                    {"name": "collect_distinct", "field": "exported_at", "alias": "exported_ats"},
                ],
            ),
            "sheet": _plan(
                "sheet",
                filters=[{"field": "sheet_number", "operator": "eq", "value": "A-531"}],
                metrics=[
                    {"name": "count_distinct", "field": "id", "alias": "sheet_count"},
                    {"name": "collect_distinct", "field": "sheet_name", "alias": "sheet_names"},
                ],
            ),
            "dxf": _plan(
                "drawing_file",
                filters=[
                    {"field": "node_type", "operator": "eq", "value": "DxfDrawingFile"},
                    *complete_file_filters,
                    {"field": "source_kind", "operator": "eq", "value": "sheet_dxf"},
                    {"field": "record_type", "operator": "eq", "value": "sheet_dxf"},
                ],
                metrics=[
                    {"name": "count_distinct", "field": "id", "alias": "file_count"},
                    {"name": "collect_distinct", "field": "file_name", "alias": "file_names"},
                    {"name": "collect_distinct", "field": "file_size_bytes", "alias": "file_sizes"},
                ],
            ),
            "layer_dxf": _plan(
                "drawing_file",
                filters=[
                    {"field": "node_type", "operator": "eq", "value": "DxfDrawingFile"},
                    {"field": "file_size_bytes", "operator": "exists", "value": False},
                    {"field": "sheet_number", "operator": "eq", "value": "A-531"},
                ],
                metrics=[
                    {"name": "collect_distinct", "field": "sheet_number", "alias": "sheet_numbers"},
                    {"name": "collect_distinct", "field": "sheet_name", "alias": "sheet_names"},
                    {"name": "collect_distinct", "field": "file_name", "alias": "file_names"},
                ],
            ),
            "pdf": _plan(
                "drawing_file",
                filters=[
                    {"field": "node_type", "operator": "eq", "value": "SheetPdfFileResource"},
                    *complete_file_filters,
                    {"field": "status", "operator": "eq", "value": "exported"},
                    {"field": "source_kind", "operator": "eq", "value": "sheet_pdf"},
                    {"field": "record_type", "operator": "eq", "value": "sheet_pdf"},
                ],
                metrics=[
                    {"name": "count_distinct", "field": "id", "alias": "file_count"},
                    {"name": "collect_distinct", "field": "file_name", "alias": "file_names"},
                    {"name": "collect_distinct", "field": "status", "alias": "statuses"},
                ],
            ),
        },
        "reducers": [
            {"id": "revit_version", "op": "only", "source": "plans.project.values.versions"},
            {"id": "schema_version", "op": "only", "source": "plans.export.values.schemas"},
            {"id": "exported_at", "op": "only", "source": "plans.export.values.exported_ats"},
            {"id": "sheet_name", "op": "only", "source": "plans.sheet.values.sheet_names"},
            {"id": "dxf_name", "op": "only", "source": "plans.dxf.values.file_names"},
            {"id": "dxf_size", "op": "only", "source": "plans.dxf.values.file_sizes"},
            {
                "id": "layer_sheet_number",
                "op": "only",
                "source": "plans.layer_dxf.values.sheet_numbers",
            },
            {"id": "layer_sheet_name", "op": "only", "source": "plans.layer_dxf.values.sheet_names"},
            {"id": "layer_file_name", "op": "only", "source": "plans.layer_dxf.values.file_names"},
            {"id": "pdf_name", "op": "only", "source": "plans.pdf.values.file_names"},
            {"id": "pdf_status", "op": "only", "source": "plans.pdf.values.statuses"},
        ],
    }

    result = execute_query_bundle(bundle, _snapshot(tmp_path))

    assert result.reducer_values == {
        "revit_version": "2025",
        "schema_version": "0.8.4",
        "exported_at": "2026-07-10T09:21:49.3733692+09:00",
        "sheet_name": "접합부 상세도",
        "dxf_name": "A-531_접합부 상세도.dxf",
        "dxf_size": 287649,
        "layer_sheet_number": "A-531",
        "layer_sheet_name": "접합부 상세도",
        "layer_file_name": "A-531_접합부 상세도.dxf",
        "pdf_name": "A-531_접합부 상세도.pdf",
        "pdf_status": "exported",
    }
    assert result.plan_results["dxf"].matched_records == 1
    assert result.plan_results["dxf"].values["file_count"] == 1
    assert result.plan_results["pdf"].values["file_count"] == 1


@pytest.mark.parametrize(
    "conflicting_properties",
    [
        {
            "sheet_number": "A-531",
            "source_context": {"sheet_number": "A-999"},
            "dxf_file_name": "A-531.dxf",
        },
        {
            "sheet_number": "A-531",
            "dxf_file_name": "A-531.dxf",
            "pdf_file_name": "A-531.pdf",
        },
    ],
)
def test_drawing_file_alias_conflicts_fail_closed(
    tmp_path: Path,
    conflicting_properties: dict,
) -> None:
    path, digest = _pack(
        tmp_path,
        "ambiguous",
        [
            {
                "id": "ambiguous-file",
                "node_type": "DxfDrawingFile",
                "properties": {
                    **conflicting_properties,
                    "file_size_bytes": 1,
                    "source_kind": "sheet_dxf",
                    "record_type": "sheet_dxf",
                },
            }
        ],
    )
    snapshot = {
        "snapshot_id": "ambiguous",
        "project_id": "yeoju",
        "packs": [
            {
                "pack_id": "ambiguous",
                "path": str(path),
                "sha256": digest,
                "role": "drawing_reference",
            }
        ],
    }
    plan = _plan(
        "drawing_file",
        metrics=[
            {"name": "collect_distinct", "field": "sheet_number", "alias": "sheet_numbers"},
            {"name": "collect_distinct", "field": "file_name", "alias": "file_names"},
        ],
    )

    with pytest.raises(QueryExecutionError, match="ambiguous field alias"):
        execute_query_bundle(
            {"project_id": "yeoju", "plans": {"file": plan}, "reducers": []},
            snapshot,
        )


def test_public_entities_do_not_admit_hidden_source_selection() -> None:
    plan = _plan(
        "drawing_file",
        metrics=[{"name": "count_distinct", "field": "id", "alias": "file_count"}],
    )
    plan["source_roles"] = ["drawing_reference"]

    with pytest.raises(PlanBundleContractError, match="source_roles"):
        validate_plan_bundle(
            {"project_id": "yeoju", "plans": {"files": plan}, "reducers": []}
        )
