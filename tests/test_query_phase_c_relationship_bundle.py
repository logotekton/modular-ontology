from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from decimal import localcontext
from pathlib import Path

import pytest

from modular_ontology.query_contract import PLAN_FIELDS, try_validate_plan_bundle
from modular_ontology.query_engine import (
    BundleReductionError,
    QueryExecutionError,
    execute_query_bundle,
)


PROJECT_ID = "yeoju"
SNAPSHOT_ID = "YEOJU-CANONICAL-20260710-092149-R1"


def _pack(
    tmp_path: Path,
    name: str,
    nodes: list[dict],
    edges: list[dict],
) -> tuple[Path, str]:
    path = tmp_path / f"{name}.zip"
    node_payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in nodes)
    edge_payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in edges)
    if node_payload:
        node_payload += "\n"
    if edge_payload:
        edge_payload += "\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("graph/nodes.jsonl", node_payload)
        archive.writestr("graph/edges.jsonl", edge_payload)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(
    tmp_path: Path,
    packs: list[tuple[str, list[dict], list[dict]]],
) -> dict:
    entries = []
    for name, nodes, edges in packs:
        path, digest = _pack(tmp_path, name, nodes, edges)
        entries.append(
            {
                "pack_id": name,
                "path": str(path),
                "sha256": digest,
                "role": "boq",
                "included": True,
            }
        )
    return {
        "project_id": PROJECT_ID,
        "snapshot_id": SNAPSHOT_ID,
        "packs": entries,
    }


def _plan(
    entity: str,
    *,
    scope: dict | None = None,
    filters: list[dict] | None = None,
    group_by: list[str] | None = None,
    metrics: list[dict] | None = None,
) -> dict:
    plan = {
        "project_id": PROJECT_ID,
        "entity": entity,
        "intent": "aggregate",
        "scope": scope or {},
        "filters": filters or [],
        "group_by": group_by or [],
        "metrics": metrics or [],
    }
    assert set(plan) == PLAN_FIELDS
    return plan


def _eq(field: str, value: object) -> dict:
    return {"field": field, "operator": "eq", "value": value}


def _boq_plan(node_type: str, identity_filters: list[dict]) -> dict:
    group_by = [
        "id",
        "node_type",
        "module_type",
        "work_category",
        "item_name",
        "specification",
        "unit",
        "normalized_unit",
        "quantity",
        "source_sheet",
        "source_row",
        "source_pack_id",
        "source_pack_sha256",
    ]
    if node_type == "QuantityTakeoffEvidence":
        group_by.extend(["formula", "formula_source_type", "bim_reference_count"])
    return _plan(
        "boq_item",
        filters=[_eq("node_type", node_type), *identity_filters],
        group_by=group_by,
        metrics=[{"name": "count", "field": "*", "alias": "source_row_count"}],
    )


def _edge_plan() -> dict:
    return _plan(
        "relationship_edge",
        scope={"source_role": "boq"},
        filters=[
            {
                "field": "relation",
                "operator": "in",
                "value": ["CALCULATED_BY", "REFERENCES_BIM_ELEMENT"],
            }
        ],
        group_by=[
            "source",
            "relation",
            "target",
            "reference_index",
            "source_pack_id",
            "source_pack_sha256",
        ],
        metrics=[{"name": "count", "field": "*", "alias": "edge_row_count"}],
    )


def _bim_reference_plan() -> dict:
    return _plan(
        "bim_reference",
        group_by=[
            "id",
            "measurement_value",
            "type",
            "source_expression",
            "source_sheet",
            "source_row",
            "source_pack_id",
            "source_pack_sha256",
        ],
        metrics=[{"name": "count", "field": "*", "alias": "source_row_count"}],
    )


def _phase_c_records() -> tuple[list[dict], list[dict]]:
    nodes = [
        {
            "id": "estimate_item:sheet_e2427114c4ec:0474",
            "node_type": "EstimateItem",
            "properties": {
                "module_type": "1-07-G",
                "work_category": "벽체",
                "item_name": "일반석고보드",
                "specification": "T12",
                "unit": "㎡",
                "normalized_unit": "square_meter",
                "quantity": 0.22,
                "source_sheet": "건축_모듈내역",
                "source_row": 474,
            },
        },
        {
            "id": "quantity_evidence:sheet_e30719c74ab3:0546",
            "node_type": "QuantityTakeoffEvidence",
            "properties": {
                "module_type": "1-07-G",
                "work_category": "벽체",
                "item_name": "일반석고보드",
                "specification": "T12",
                "unit": "㎡",
                "normalized_unit": "square_meter",
                "formula": "('0.22'<BIM 데이터에 의한 면적 계산>)",
                "formula_source_type": "bim_area_based",
                "quantity": 0.22,
                "bim_reference_count": 1,
                "source_sheet": "건축_모듈산출근거",
                "source_row": 546,
            },
        },
        {
            "id": "bim_reference:744df356c85f2461",
            "node_type": "BIMReference",
            "properties": {
                "measurement_value": 0.22,
                "measurement_type": "area",
                "source_expression": "'0.22'<BIM 데이터에 의한 면적 계산>",
                "source_sheet": "건축_모듈산출근거",
                "source_row": 546,
            },
        },
        {
            "id": "estimate_item:sheet_e2427114c4ec:0014",
            "node_type": "EstimateItem",
            "properties": {
                "module_type": "1-01-M",
                "work_category": "구조",
                "item_name": "모듈러 잡철물 가공, 제작 및 설치",
                "specification": "규격철물 설치, 일반철재",
                "unit": "TON",
                "normalized_unit": "metric_ton",
                "quantity": 3.562,
                "source_sheet": "건축_모듈내역",
                "source_row": 14,
            },
        },
        {
            "id": "quantity_evidence:sheet_e30719c74ab3:0024",
            "node_type": "QuantityTakeoffEvidence",
            "properties": {
                "module_type": "1-01-M",
                "work_category": "구조",
                "item_name": "모듈러 잡철물 가공, 제작 및 설치",
                "specification": "규격철물 설치, 일반철재",
                "unit": "TON",
                "normalized_unit": "metric_ton",
                "formula": "(1.034+0.967+0.939+0.047+0.011+0.564)",
                "formula_source_type": "manual_formula",
                "quantity": 3.562,
                "bim_reference_count": 0,
                "source_sheet": "건축_모듈산출근거",
                "source_row": 24,
            },
        },
    ]
    edges = [
        {
            "source": "estimate_item:sheet_e2427114c4ec:0474",
            "relation": "CALCULATED_BY",
            "target": "quantity_evidence:sheet_e30719c74ab3:0546",
            "properties": {"original_relation": "CALCULATED_BY"},
        },
        {
            "source": "quantity_evidence:sheet_e30719c74ab3:0546",
            "relation": "REFERENCES_BIM_ELEMENT",
            "target": "bim_reference:744df356c85f2461",
            "properties": {
                "original_relation": "REFERENCES_BIM_ELEMENT",
                "reference_index": 1,
            },
        },
        {
            "source": "estimate_item:sheet_e2427114c4ec:0014",
            "relation": "CALCULATED_BY",
            "target": "quantity_evidence:sheet_e30719c74ab3:0024",
            "properties": {"original_relation": "CALCULATED_BY"},
        },
    ]
    return nodes, edges


def _phase_c_bundle() -> dict:
    b01_identity = [
        _eq("module_type", "1-07-G"),
        _eq("work_category", "벽체"),
        _eq("item_name", "일반석고보드"),
        _eq("specification", "T12"),
    ]
    # D07 deliberately contains only the identity stated in the question.
    d07_identity = [
        _eq("module_type", "1-01-M"),
        _eq("item_name", "모듈러 잡철물 가공, 제작 및 설치"),
    ]
    plans = {
        "b01_estimate": _boq_plan("EstimateItem", b01_identity),
        "b01_takeoff": _boq_plan("QuantityTakeoffEvidence", b01_identity),
        "d07_estimate": _boq_plan("EstimateItem", d07_identity),
        "d07_takeoff": _boq_plan("QuantityTakeoffEvidence", d07_identity),
        "edges": _edge_plan(),
        "bim_references": _bim_reference_plan(),
    }
    reducers = [
        {
            "id": "b01_calculation",
            "op": "traverse_edge",
            "profile": "boq_calculated_by",
            "source": "plans.b01_estimate.rows",
            "edges": "plans.edges.rows",
            "target": "plans.b01_takeoff.rows",
        },
        {
            "id": "b01_bim_reference",
            "op": "traverse_edge",
            "profile": "quantity_evidence_bim_reference",
            "source": "plans.b01_takeoff.rows",
            "edges": "plans.edges.rows",
            "target": "plans.bim_references.rows",
        },
        {
            "id": "b01_all_equal",
            "op": "all_equal",
            "inputs": [
                {"ref": "reducers.b01_calculation.estimate_quantity"},
                {"ref": "reducers.b01_calculation.takeoff_quantity"},
                {"ref": "reducers.b01_bim_reference.bim_measurement_value"},
            ],
        },
        {
            "id": "d07_calculation",
            "op": "traverse_edge",
            "profile": "boq_calculated_by",
            "source": "plans.d07_estimate.rows",
            "edges": "plans.edges.rows",
            "target": "plans.d07_takeoff.rows",
        },
        {
            "id": "d07_bim_reference",
            "op": "traverse_edge",
            "profile": "quantity_evidence_bim_reference",
            "source": "plans.d07_takeoff.rows",
            "edges": "plans.edges.rows",
            "target": "plans.bim_references.rows",
        },
        {
            "id": "d07_formula",
            "op": "evaluate_decimal_formula",
            "profile": "decimal_additive",
            "formula": "reducers.d07_calculation.formula",
            "source_type": "reducers.d07_calculation.formula_source_type",
            "unit": "reducers.d07_calculation.normalized_unit",
        },
        {
            "id": "d07_all_equal",
            "op": "all_equal",
            "inputs": [
                {"ref": "reducers.d07_calculation.estimate_quantity"},
                {"ref": "reducers.d07_calculation.takeoff_quantity"},
                {"ref": "reducers.d07_formula.value"},
            ],
        },
    ]
    return {"project_id": PROJECT_ID, "plans": plans, "reducers": reducers}


def _execute_fixture(
    tmp_path: Path,
    *,
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
    bundle: dict | None = None,
):
    base_nodes, base_edges = _phase_c_records()
    snapshot = _snapshot(
        tmp_path,
        [("boq", nodes if nodes is not None else base_nodes, edges if edges is not None else base_edges)],
    )
    return execute_query_bundle(bundle or _phase_c_bundle(), snapshot)


def _node(nodes: list[dict], node_id: str) -> dict:
    return next(row for row in nodes if row["id"] == node_id)


def test_phase_c_b01_d07_relationship_and_formula_pipeline(tmp_path: Path) -> None:
    bundle = _phase_c_bundle()
    assert try_validate_plan_bundle(bundle).valid
    result = _execute_fixture(tmp_path, bundle=bundle)
    repeated = _execute_fixture(tmp_path, bundle=bundle)
    values = result.reducer_values

    b01 = values["b01_calculation"]
    assert b01["source_id"] == "estimate_item:sheet_e2427114c4ec:0474"
    assert b01["target_id"] == "quantity_evidence:sheet_e30719c74ab3:0546"
    assert b01["estimate_quantity"] == 0.22
    assert b01["takeoff_quantity"] == 0.22
    assert b01["quantity_matches"] is True
    assert b01["unit"] == "㎡"
    assert b01["normalized_unit"] == "square_meter"
    assert b01["estimate"] == {
        "id": "estimate_item:sheet_e2427114c4ec:0474",
        "quantity": 0.22,
        "source_sheet": "건축_모듈내역",
        "source_row": 474,
    }
    assert b01["takeoff"]["source_sheet"] == "건축_모듈산출근거"
    assert b01["takeoff"]["source_row"] == 546
    b01_ref = values["b01_bim_reference"]
    assert b01_ref["declared_bim_reference_count"] == 1
    assert b01_ref["edge_count"] == 1
    assert b01_ref["resolved_target_count"] == 1
    assert b01_ref["bim_measurement_value"] == 0.22
    assert b01_ref["measurement_matches_quantity"] is True
    assert values["b01_all_equal"] is True

    d07 = values["d07_calculation"]
    assert d07["quantity"] == 3.562
    assert d07["estimate_source_sheet"] == "건축_모듈내역"
    assert d07["estimate_source_row"] == 14
    assert d07["takeoff_source_sheet"] == "건축_모듈산출근거"
    assert d07["takeoff_source_row"] == 24
    assert d07["formula"] == "(1.034+0.967+0.939+0.047+0.011+0.564)"
    assert d07["formula_source_type"] == "manual_formula"
    assert d07["bim_reference_count"] == 0
    assert values["d07_bim_reference"]["edge_count"] == 0
    assert values["d07_bim_reference"]["resolved_target_count"] == 0
    assert values["d07_bim_reference"]["bim_measurement_value"] is None
    assert values["d07_formula"]["value"] == 3.562
    assert values["d07_formula"]["unit"] == "metric_ton"
    assert values["d07_all_equal"] is True

    edge_result = result.plan_results["edges"]
    assert edge_result.scanned_records == 3
    assert edge_result.matched_records == 3
    assert edge_result.duplicate_records_removed == 0
    assert result.evidence["value_units"][
        "reducers.b01_calculation.estimate_quantity"
    ] == "square_meter"
    assert result.evidence["value_units"][
        "reducers.b01_bim_reference.bim_measurement_value"
    ] == "square_meter"
    assert result.evidence["value_units"]["reducers.d07_formula.value"] == "metric_ton"
    assert result.result_hash == repeated.result_hash

    for plan_id in ("d07_estimate", "d07_takeoff"):
        plan = bundle["plans"][plan_id]
        assert plan["scope"] == {}
        question_fields = {
            item["field"] for item in plan["filters"] if item["field"] != "node_type"
        }
        assert question_fields == {"module_type", "item_name"}
        assert not question_fields.intersection(
            {"source_sheet", "source_row", "formula", "quantity", "specification"}
        )


def test_relationship_contract_requires_boq_scope_and_fixed_reducer_references() -> None:
    bundle = _phase_c_bundle()
    missing_scope = copy.deepcopy(bundle)
    missing_scope["plans"]["edges"]["scope"] = {}
    validation = try_validate_plan_bundle(missing_scope)
    assert not validation.valid
    assert "required_source_role" in {issue.code for issue in validation.issues}

    wrong_scope = copy.deepcopy(bundle)
    wrong_scope["plans"]["edges"]["scope"] = {"source_role": ["boq"]}
    validation = try_validate_plan_bundle(wrong_scope)
    assert not validation.valid
    assert "required_source_role" in {issue.code for issue in validation.issues}

    bad_profile = copy.deepcopy(bundle)
    bad_profile["reducers"][0]["profile"] = "user_relation"
    validation = try_validate_plan_bundle(bad_profile)
    assert not validation.valid
    assert "unsupported_profile" in {issue.code for issue in validation.issues}

    bad_source = copy.deepcopy(bundle)
    bad_source["reducers"][0]["source"] = "plans.b01_estimate.values.count"
    validation = try_validate_plan_bundle(bad_source)
    assert not validation.valid
    assert "invalid_source_reference" in {issue.code for issue in validation.issues}

    mixed_formula = copy.deepcopy(bundle)
    formula_reducer = next(
        row for row in mixed_formula["reducers"] if row["id"] == "d07_formula"
    )
    formula_reducer["unit"] = "reducers.b01_calculation.normalized_unit"
    validation = try_validate_plan_bundle(mixed_formula)
    assert not validation.valid
    assert "mixed_formula_source" in {issue.code for issue in validation.issues}


@pytest.mark.parametrize(
    "inputs",
    [
        [{"ref": "reducers.b01_calculation.estimate_quantity"}],
        [
            {"ref": "reducers.b01_calculation.estimate_quantity"}
            for _ in range(9)
        ],
        [
            {"ref": "reducers.b01_calculation.estimate_quantity"},
            {"value": 0.22, "unit": "㎡"},
        ],
    ],
)
def test_all_equal_contract_accepts_only_two_to_eight_source_refs(inputs: list[dict]) -> None:
    bundle = _phase_c_bundle()
    reducer = next(row for row in bundle["reducers"] if row["id"] == "b01_all_equal")
    reducer["inputs"] = inputs
    validation = try_validate_plan_bundle(bundle)
    assert not validation.valid
    assert {issue.code for issue in validation.issues}.intersection(
        {"invalid_input_count", "source_required", "duplicate_operand"}
    )


def test_duplicate_edge_is_counted_and_rejected(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    edges.append(copy.deepcopy(edges[0]))
    with pytest.raises(BundleReductionError, match="edge_row_count=1"):
        _execute_fixture(tmp_path, nodes=nodes, edges=edges)


def test_quantity_mismatch_is_a_boolean_audit_result(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    takeoff = _node(nodes, "quantity_evidence:sheet_e30719c74ab3:0546")
    takeoff["properties"]["quantity"] = 0.21
    result = _execute_fixture(tmp_path, nodes=nodes, edges=edges)
    assert result.reducer_values["b01_calculation"]["quantity_matches"] is False
    assert (
        result.reducer_values["b01_bim_reference"]["measurement_matches_quantity"]
        is False
    )
    assert result.reducer_values["b01_all_equal"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("declared_count", "declared bim_reference_count"),
        ("missing_target", "does not exist"),
        ("identity", "identity mismatch"),
        ("ambiguous_takeoff", "exactly one grouped"),
    ],
)
def test_traversal_fail_closed_on_cardinality_target_and_identity_errors(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    nodes, edges = _phase_c_records()
    bundle = _phase_c_bundle()
    if mutation == "declared_count":
        _node(nodes, "quantity_evidence:sheet_e30719c74ab3:0546")["properties"][
            "bim_reference_count"
        ] = 0
    elif mutation == "missing_target":
        nodes[:] = [row for row in nodes if row["node_type"] != "BIMReference"]
    elif mutation == "identity":
        _node(nodes, "quantity_evidence:sheet_e30719c74ab3:0546")["properties"][
            "specification"
        ] = "T15"
        bundle["plans"]["b01_takeoff"]["filters"] = [
            item
            for item in bundle["plans"]["b01_takeoff"]["filters"]
            if item["field"] != "specification"
        ]
    elif mutation == "ambiguous_takeoff":
        duplicate = copy.deepcopy(
            _node(nodes, "quantity_evidence:sheet_e30719c74ab3:0546")
        )
        duplicate["id"] = "quantity_evidence:ambiguous"
        nodes.append(duplicate)
    with pytest.raises(BundleReductionError, match=message):
        _execute_fixture(tmp_path, nodes=nodes, edges=edges, bundle=bundle)


def test_traversal_rejects_multiple_boq_pack_bindings(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    estimates = [row for row in nodes if row["node_type"] == "EstimateItem"]
    evidence = [row for row in nodes if row["node_type"] != "EstimateItem"]
    snapshot = _snapshot(
        tmp_path,
        [("boq-a", estimates, edges), ("boq-b", evidence, [])],
    )
    with pytest.raises(BundleReductionError, match="exactly one source pack"):
        execute_query_bundle(_phase_c_bundle(), snapshot)


@pytest.mark.parametrize(
    ("formula", "source_type", "message"),
    [
        ("1*2", "manual_formula", "unsupported character"),
        ("1e2", "manual_formula", "unsupported character"),
        ("1..2", "manual_formula", "unsupported character"),
        ("1+", "manual_formula", "ended before a value"),
        ("(((((((((1)))))))))", "manual_formula", "parenthesis depth"),
        ("+".join(["1"] * 34), "manual_formula", "tokens"),
        ("1+2", "excel_formula", "manual_formula"),
        ("１+2", "manual_formula", "unsupported character"),
    ],
)
def test_decimal_formula_profile_rejects_unsafe_or_out_of_profile_inputs(
    tmp_path: Path,
    formula: str,
    source_type: str,
    message: str,
) -> None:
    nodes, edges = _phase_c_records()
    takeoff = _node(nodes, "quantity_evidence:sheet_e30719c74ab3:0024")
    takeoff["properties"]["formula"] = formula
    takeoff["properties"]["formula_source_type"] = source_type
    with pytest.raises(BundleReductionError, match=message):
        _execute_fixture(tmp_path, nodes=nodes, edges=edges)


def test_decimal_formula_uses_exact_decimal_arithmetic(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    takeoff = _node(nodes, "quantity_evidence:sheet_e30719c74ab3:0024")
    takeoff["properties"]["formula"] = "0.1 + 0.2 + 3.262"
    result = _execute_fixture(tmp_path, nodes=nodes, edges=edges)
    assert result.reducer_values["d07_formula"]["value"] == 3.562
    assert result.reducer_values["d07_all_equal"] is True


def test_all_equal_rejects_mixed_unit_envelopes(tmp_path: Path) -> None:
    bundle = _phase_c_bundle()
    bundle["reducers"].append(
        {
            "id": "mixed_units",
            "op": "all_equal",
            "inputs": [
                {"ref": "reducers.b01_calculation.estimate_quantity"},
                {"ref": "reducers.d07_calculation.estimate_quantity"},
            ],
        }
    )
    with pytest.raises(BundleReductionError, match="incompatible units"):
        _execute_fixture(tmp_path, bundle=bundle)


@pytest.mark.parametrize("field", ["source", "relation", "target"])
def test_relationship_structural_field_conflict_is_rejected(
    tmp_path: Path,
    field: str,
) -> None:
    nodes, edges = _phase_c_records()
    edges[0]["properties"][field] = "forged-structural-value"

    with pytest.raises(
        QueryExecutionError,
        match=rf"conflicting top-level/properties field '{field}'",
    ):
        _execute_fixture(tmp_path, nodes=nodes, edges=edges)


def test_relationship_structural_field_same_value_is_allowed(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    edges[0]["properties"]["source"] = edges[0]["source"]

    result = _execute_fixture(tmp_path, nodes=nodes, edges=edges)

    assert result.reducer_values["b01_all_equal"] is True


def test_bim_measurement_type_must_match_normalized_unit(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    reference = _node(nodes, "bim_reference:744df356c85f2461")
    reference["properties"]["measurement_type"] = "length"

    with pytest.raises(BundleReductionError, match="measurement type/unit mismatch"):
        _execute_fixture(tmp_path, nodes=nodes, edges=edges)


def test_bim_type_alias_conflict_is_rejected(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    reference = _node(nodes, "bim_reference:744df356c85f2461")
    reference["properties"]["type"] = "length"

    with pytest.raises(QueryExecutionError, match="ambiguous field alias 'type'"):
        _execute_fixture(tmp_path, nodes=nodes, edges=edges)


def test_traversal_rejects_plan_that_removed_duplicate_records(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    nodes.append(
        copy.deepcopy(_node(nodes, "estimate_item:sheet_e2427114c4ec:0474"))
    )

    with pytest.raises(BundleReductionError, match="removed duplicate records"):
        _execute_fixture(tmp_path, nodes=nodes, edges=edges)


def test_decimal_formula_is_independent_of_ambient_decimal_context(
    tmp_path: Path,
) -> None:
    with localcontext() as context:
        context.prec = 2
        result = _execute_fixture(tmp_path)

    assert result.reducer_values["d07_formula"]["value"] == 3.562
    assert result.reducer_values["d07_all_equal"] is True


@pytest.mark.parametrize(
    "member",
    ["graph/nodes.jsonl", "graph/edges.jsonl"],
)
def test_duplicate_zip_graph_member_is_rejected(
    tmp_path: Path,
    member: str,
) -> None:
    nodes, edges = _phase_c_records()
    node_payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in nodes) + "\n"
    edge_payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in edges) + "\n"
    payloads = {
        "graph/nodes.jsonl": node_payload,
        "graph/edges.jsonl": edge_payload,
    }
    path = tmp_path / "duplicate-member.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("graph/nodes.jsonl", node_payload)
            archive.writestr("graph/edges.jsonl", edge_payload)
            archive.writestr(member, payloads[member])
    snapshot = {
        "project_id": PROJECT_ID,
        "snapshot_id": SNAPSHOT_ID,
        "packs": [
            {
                "pack_id": "boq",
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "role": "boq",
                "included": True,
            }
        ],
    }

    with pytest.raises(
        QueryExecutionError,
        match=r"pack must contain exactly one graph/(?:nodes|edges)\.jsonl member",
    ):
        execute_query_bundle(_phase_c_bundle(), snapshot)


def test_directory_pack_uses_fixed_graph_jsonl_paths(tmp_path: Path) -> None:
    nodes, edges = _phase_c_records()
    path = tmp_path / "legacy-directory-pack"
    legacy_graph = path / "03_graph"
    graph = path / "graph"
    legacy_graph.mkdir(parents=True)
    graph.mkdir(parents=True)
    (legacy_graph / "nodes.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in nodes) + "\n",
        encoding="utf-8",
    )
    (graph / "edges.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in edges) + "\n",
        encoding="utf-8",
    )
    snapshot = {
        "project_id": PROJECT_ID,
        "snapshot_id": SNAPSHOT_ID,
        "packs": [
            {
                "pack_id": "boq",
                "path": str(path),
                "role": "boq",
                "included": True,
            }
        ],
    }

    with pytest.raises(QueryExecutionError, match="pack has no graph nodes JSONL"):
        execute_query_bundle(_phase_c_bundle(), snapshot)


@pytest.mark.parametrize(
    ("record_kind", "constant"),
    [
        ("node", float("nan")),
        ("node", float("inf")),
        ("edge", float("-inf")),
    ],
)
def test_non_finite_jsonl_constant_is_rejected_before_filtering(
    tmp_path: Path,
    record_kind: str,
    constant: float,
) -> None:
    nodes, edges = _phase_c_records()
    if record_kind == "node":
        nodes.append(
            {
                "id": "unrelated:non-finite",
                "node_type": "Unrelated",
                "properties": {"invalid_number": constant},
            }
        )
    else:
        edges.append(
            {
                "source": "unrelated:source",
                "relation": "UNRELATED",
                "target": "unrelated:target",
                "properties": {"invalid_number": constant},
            }
        )

    with pytest.raises(QueryExecutionError, match="invalid JSONL"):
        _execute_fixture(tmp_path, nodes=nodes, edges=edges)


@pytest.mark.parametrize("constant", [float("nan"), float("inf"), float("-inf")])
def test_query_contract_rejects_non_finite_json_values(constant: float) -> None:
    bundle = _phase_c_bundle()
    bundle["plans"]["b01_estimate"]["filters"].append(
        _eq("quantity", constant)
    )

    validation = try_validate_plan_bundle(bundle)

    assert not validation.valid
    assert "invalid_value" in {issue.code for issue in validation.issues}
