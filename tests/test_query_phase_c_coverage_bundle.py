from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import modular_ontology.query_engine as query_engine
from modular_ontology.coverage_scan import scan_snapshot_nodes as core_scan_snapshot_nodes
from modular_ontology.query_contract import PLAN_FIELDS, try_validate_plan_bundle
from modular_ontology.query_engine import (
    BundleReductionError,
    QueryExecutionError,
    execute_query,
    execute_query_bundle,
)
from modular_ontology.snapshot_store import load_snapshot, verify_snapshot


PROJECT_ID = "yeoju"
SNAPSHOT_ID = "SYNTHETIC-COVERAGE-R1"
DRAWING_ROLES = [
    "drawing_entity",
    "drawing_representation_primary",
    "drawing_representation_topology",
    "drawing_representation_index",
    "drawing_representation_review",
    "drawing_representation_qa",
    "drawing_reference",
]


def _node(
    node_id: str,
    node_type: str,
    *,
    label: str,
    **properties: object,
) -> dict:
    return {
        "id": node_id,
        "node_type": node_type,
        "label": label,
        "properties": properties,
    }


def _pack(
    tmp_path: Path,
    *,
    pack_id: str,
    role: str,
    nodes: list[dict],
) -> dict:
    path = tmp_path / f"{pack_id}.zip"
    manifest = {
        "pack_id": pack_id,
        "entrypoints": {"nodes": "graph/nodes.jsonl"},
        "counts": {"nodes": len(nodes)},
    }
    payload = "\n".join(
        json.dumps(node, ensure_ascii=False, sort_keys=True) for node in nodes
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        )
        archive.writestr("graph/nodes.jsonl", payload)
    return {
        "pack_id": pack_id,
        "role": role,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "included": True,
    }


def _boq_nodes(*, include_d9: bool = False) -> list[dict]:
    wall_name = "D9" if include_d9 else "D8"
    return [
        _node(
            "boq-item",
            "BOQItem",
            label=f"벽체 {wall_name}",
            item_name=wall_name,
            specification="T12",
            work_category="벽체",
        ),
        _node(
            "aggregate-item",
            "AggregatedBOQItem",
            label=f"벽체 집계 {wall_name}",
            item_name=wall_name,
            specification="T12",
            work_category="벽체",
            note="일반 벽체",
        ),
        _node(
            "estimate-item",
            "EstimateItem",
            label="지상 마감 품목",
            module_type="1-01-A",
            item_name="도장",
            specification="친환경",
            work_category="마감",
            note="지상층",
        ),
        _node(
            "site-item",
            "SiteWorkItem",
            label="현장 토공 품목",
            item_name="되메우기",
            specification="일반",
            work_category="토공",
            note="외부",
        ),
        _node(
            "takeoff-item",
            "QuantityTakeoffEvidence",
            label="모듈 수량 근거",
            module_type="1-01-A",
            item_name="도장",
            specification="친환경",
            work_category="마감",
        ),
        _node(
            "work-category",
            "WorkCategory",
            label="건축 공종",
            name="건축",
        ),
        _node(
            "bim-reference",
            "BIMReference",
            label="BIM 참조",
            measurement_value=1,
        ),
        _node(
            "estimate-sheet",
            "EstimateSheet",
            label="건축 내역 시트",
            name="건축_내역",
        ),
        _node(
            "estimate-workbook",
            "EstimateWorkbook",
            label="건축 내역서",
            source_file="estimate.xlsx",
        ),
        _node(
            "formula",
            "Formula",
            label="수량 공식",
            formula="1+1",
        ),
        _node(
            "module-type",
            "ModuleType",
            label="모듈 유형 1-01-A",
            module_type_id="1-01-A",
        ),
        _node("project", "Project", label="여주 합성 프로젝트", name="여주"),
        _node("unit", "Unit", label="제곱미터 단위", name="㎡"),
    ]


def _synthetic_snapshot(
    tmp_path: Path,
    *,
    include_d9: bool = False,
    include_structural_role: bool = False,
) -> dict:
    entries = [
        _pack(
            tmp_path,
            pack_id="boq-pack",
            role="boq",
            nodes=_boq_nodes(include_d9=include_d9),
        ),
        _pack(
            tmp_path,
            pack_id="model-pack",
            role="model_inventory",
            nodes=[
                _node(
                    "element-1",
                    "BIMElement",
                    label="지상 1층 기본 벽 D8",
                    level="지상 1층",
                    category="벽",
                    family_name="Basic Wall",
                    type_name="D8",
                    parameters_sample=[{"name": "폭", "value": "120"}],
                )
            ],
        ),
        _pack(
            tmp_path,
            pack_id="quantity-pack",
            role="model_quantity",
            nodes=[
                _node(
                    "quantity-1",
                    "BIMQuantityFact",
                    label="기본 벽 면적",
                    type_name="D8",
                )
            ],
        ),
        _pack(
            tmp_path,
            pack_id="spec-pack",
            role="spec",
            nodes=[
                _node(
                    f"requirement-{index:02d}",
                    "Requirement",
                    label=f"일반 구조 기준 {index:02d}",
                    requirement_text="구조계산서 일반 제출 기준",
                )
                for index in range(33)
            ],
        ),
        _pack(
            tmp_path,
            pack_id="law-pack",
            role="law",
            nodes=[
                _node(
                    "law-entity",
                    "Entity",
                    label="건축 일반 법규",
                    name="건축법",
                )
            ],
        ),
    ]
    for role in DRAWING_ROLES:
        nodes: list[dict] = []
        if role == "drawing_reference":
            nodes = [
                _node(
                    "sheet-1",
                    "DrawingSheet",
                    label="A-101 지상 평면도",
                    sheet_number="A-101",
                    sheet_name="지상 평면도",
                ),
                _node(
                    "schedule-1",
                    "DrawingSchedule",
                    label="문 일람표",
                    schedule_name="문 일람표",
                ),
                _node(
                    "cell-1",
                    "ScheduleCell",
                    label="문 유형 셀",
                    text="문 유형 D8",
                ),
            ]
        entries.append(
            _pack(
                tmp_path,
                pack_id=f"{role}-pack",
                role=role,
                nodes=nodes,
            )
        )
    if include_structural_role:
        entries.append(
            _pack(
                tmp_path,
                pack_id="structural-calculation-pack",
                role="project_structural_calculation",
                nodes=[],
            )
        )
    return {
        "project_id": PROJECT_ID,
        "snapshot_id": SNAPSHOT_ID,
        "canonical_id": SNAPSHOT_ID,
        "packs": entries,
    }


def _plan(entity: str, scope: dict) -> dict:
    plan = {
        "project_id": PROJECT_ID,
        "entity": entity,
        "intent": "coverage",
        "scope": scope,
        "filters": [],
        "group_by": [],
        "metrics": [],
    }
    assert set(plan) == PLAN_FIELDS
    return plan


def _coverage_plan(profile: str, terms: list[str]) -> dict:
    return _plan("coverage_scan", {"profile": profile, "terms": terms})


def _role_plan(role: str) -> dict:
    return _plan("snapshot_pack", {"role": role})


def _ref(plan_id: str) -> dict[str, str]:
    return {"ref": f"plans.{plan_id}.values"}


def _coverage_bundle() -> dict:
    floor_terms = ["지하 1층", "지하1층", "B1", "주차장"]
    lift_terms = ["승강기", "엘리베이터", "정격속도", "인승"]
    drift_terms = ["최대 층간변위비", "층간변위비", "구조계산서"]
    contract_terms = ["지체상금율", "하자보수보증금율", "하자보수보증금"]
    plans = {
        "b09_boq": _coverage_plan("boq_wall_item_exact_token", ["D9"]),
        "b09_model": _coverage_plan("model_wall_type_exact_token", ["D9"]),
        "e01_boq": _coverage_plan("boq_estimate_fact_text", floor_terms),
        "e01_model": _coverage_plan("model_floor_context_text", floor_terms),
        "e01_drawing": _coverage_plan("drawing_sheet_text", floor_terms),
        "e02_model": _coverage_plan("model_lift_context_text", lift_terms),
        "e02_drawing": _coverage_plan("drawing_schedule_text", lift_terms),
        "e03_boq": _coverage_plan(
            "boq_work_item_text", ["소방", "스프링클러"]
        ),
        "e04_role": _role_plan("project_structural_calculation"),
        "e04_model": _coverage_plan("model_structural_context_text", drift_terms),
        "e04_quantity": _coverage_plan("model_quantity_text", drift_terms),
        "e04_spec": _coverage_plan("spec_requirement_text", drift_terms),
        "e05_role": _role_plan("contract_document"),
        "e05_boq": _coverage_plan("boq_graph_text", contract_terms),
        "e05_law": _coverage_plan("law_entity_text", contract_terms),
    }
    reducers = [
        {
            "id": "b09_absence",
            "op": "coverage_absence",
            "inputs": [_ref("b09_boq"), _ref("b09_model")],
        },
        {
            "id": "e01_absence",
            "op": "coverage_absence",
            "inputs": [_ref("e01_boq"), _ref("e01_model"), _ref("e01_drawing")],
        },
        {
            "id": "e02_absence",
            "op": "coverage_absence",
            "inputs": [_ref("e02_model"), _ref("e02_drawing")],
        },
        {
            "id": "e03_absence",
            "op": "coverage_absence",
            "inputs": [_ref("e03_boq")],
        },
        {
            "id": "e04_absence",
            "op": "artifact_role_absence",
            "source": "plans.e04_role.values",
            "generic_context": [
                _ref("e04_model"),
                _ref("e04_quantity"),
                _ref("e04_spec"),
            ],
        },
        {
            "id": "e05_absence",
            "op": "artifact_role_absence",
            "source": "plans.e05_role.values",
            "generic_context": [_ref("e05_boq"), _ref("e05_law")],
        },
    ]
    return {"project_id": PROJECT_ID, "plans": plans, "reducers": reducers}


def _single_coverage_bundle() -> dict:
    return {
        "project_id": PROJECT_ID,
        "plans": {
            "scan": _coverage_plan("boq_wall_item_exact_token", ["D9"])
        },
        "reducers": [
            {
                "id": "absence",
                "op": "coverage_absence",
                "inputs": [_ref("scan")],
            }
        ],
    }


def _single_artifact_bundle() -> dict:
    return {
        "project_id": PROJECT_ID,
        "plans": {
            "role": _role_plan("project_structural_calculation"),
            "context": _coverage_plan(
                "spec_requirement_text", ["구조계산서"]
            ),
        },
        "reducers": [
            {
                "id": "absence",
                "op": "artifact_role_absence",
                "source": "plans.role.values",
                "generic_context": [_ref("context")],
            }
        ],
    }


def test_b09_e01_to_e05_coverage_bundle_is_deterministic_and_fail_closed(
    tmp_path: Path,
) -> None:
    snapshot = _synthetic_snapshot(tmp_path)
    bundle = _coverage_bundle()
    validation = try_validate_plan_bundle(bundle)
    assert validation.valid, validation.as_dict()

    result = execute_query_bundle(bundle, snapshot)
    repeated = execute_query_bundle(bundle, snapshot)
    assert result.result_hash == repeated.result_hash

    for reducer_id in ("b09_absence", "e01_absence", "e02_absence", "e03_absence"):
        value = result.reducer_values[reducer_id]
        assert set(value) == {"classification", "total_match_count", "scans"}
        assert value["classification"] == "not_in_snapshot"
        assert value["total_match_count"] == 0
        assert all(scan["coverage_complete"] is True for scan in value["scans"])

    e04 = result.reducer_values["e04_absence"]
    assert set(e04) == {
        "classification",
        "missing_role",
        "role_certificate",
        "generic_context",
    }
    assert e04["classification"] == "not_in_snapshot"
    assert e04["missing_role"] == "project_structural_calculation"
    assert e04["role_certificate"]["role_match_count"] == 0
    assert [scan["match_count"] for scan in e04["generic_context"]] == [0, 0, 33]
    assert len(e04["generic_context"][2]["matched_id_sample"]) == 20

    e05 = result.reducer_values["e05_absence"]
    assert e05["missing_role"] == "contract_document"
    assert e05["role_certificate"]["role_match_count"] == 0
    assert [scan["match_count"] for scan in e05["generic_context"]] == [0, 0]

    for plan_id, plan_result in result.plan_results.items():
        assert plan_result.rows == ()
        assert plan_result.evidence["kind"] == "COVERAGE"
        assert len(plan_result.evidence["contribution_digest"]) == 64
        assert plan_result.evidence["contribution_count"] == plan_result.scanned_records
        values = plan_result.values
        if values["schema_version"] == "mo-coverage-scan/1.0":
            assert list(plan_result.source_packs) == values["selected_source_packs"]
            assert plan_result.scanned_records == values["scanned_record_count"]
            assert plan_result.matched_records == values["match_count"]
            assert (
                plan_result.duplicate_records_removed
                == values["duplicate_records_removed"]
            )
        else:
            assert plan_id in {"e04_role", "e05_role"}
            assert list(plan_result.source_packs) == values["source_pack_descriptors"]
            assert plan_result.scanned_records == values["manifest_entry_count"]
            assert plan_result.matched_records == values["role_match_count"]


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda plan: plan.update(intent="aggregate"),
            "coverage_intent_required",
        ),
        (
            lambda plan: plan["scope"].update(profile="unknown_profile"),
            "unsupported_profile",
        ),
        (
            lambda plan: plan["scope"].update(regex="D9.*"),
            "field_not_allowed",
        ),
        (
            lambda plan: plan["scope"].update(roles=["law"]),
            "field_not_allowed",
        ),
        (
            lambda plan: plan["scope"].update(terms=[f"t-{index}" for index in range(17)]),
            "invalid_terms",
        ),
        (
            lambda plan: plan["scope"].update(terms=["x" * 101]),
            "invalid_term",
        ),
        (
            lambda plan: plan["scope"].update(terms=["D9", "Ｄ９"]),
            "duplicate_term",
        ),
    ],
)
def test_coverage_contract_rejects_profile_scope_and_term_bypasses(
    mutate,
    expected_code: str,
) -> None:
    bundle = _single_coverage_bundle()
    mutate(bundle["plans"]["scan"])
    validation = try_validate_plan_bundle(bundle)
    assert not validation.valid
    assert expected_code in {issue.code for issue in validation.issues}


def test_coverage_intent_and_reducer_sources_are_kind_bound() -> None:
    wrong_intent = _single_coverage_bundle()
    wrong_intent["plans"]["scan"]["entity"] = "element"
    validation = try_validate_plan_bundle(wrong_intent)
    assert not validation.valid
    assert "coverage_entity_required" in {issue.code for issue in validation.issues}

    wrong_source = _single_coverage_bundle()
    wrong_source["plans"]["role"] = _role_plan("contract_document")
    wrong_source["reducers"][0]["inputs"] = [_ref("role")]
    validation = try_validate_plan_bundle(wrong_source)
    assert not validation.valid
    assert "invalid_coverage_source" in {issue.code for issue in validation.issues}

    wrong_artifact = _single_artifact_bundle()
    wrong_artifact["reducers"][0]["source"] = "plans.context.values"
    validation = try_validate_plan_bundle(wrong_artifact)
    assert not validation.valid
    assert "invalid_coverage_source" in {issue.code for issue in validation.issues}

    duplicate = _single_coverage_bundle()
    duplicate["reducers"][0]["inputs"] = [_ref("scan"), _ref("scan")]
    validation = try_validate_plan_bundle(duplicate)
    assert not validation.valid
    assert "duplicate_operand" in {issue.code for issue in validation.issues}


def test_coverage_absence_rejects_nonzero_match(tmp_path: Path) -> None:
    snapshot = _synthetic_snapshot(tmp_path, include_d9=True)
    with pytest.raises(BundleReductionError, match="match_count=2"):
        execute_query_bundle(_single_coverage_bundle(), snapshot)


def test_artifact_absence_rejects_an_existing_role(tmp_path: Path) -> None:
    snapshot = _synthetic_snapshot(tmp_path, include_structural_role=True)
    with pytest.raises(BundleReductionError, match="role_match_count=1"):
        execute_query_bundle(_single_artifact_bundle(), snapshot)


def test_incomplete_and_tampered_certificates_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _synthetic_snapshot(tmp_path)
    plan = _coverage_plan("boq_wall_item_exact_token", ["D9"])

    def incomplete(*args, **kwargs):
        return {"coverage_complete": False}

    monkeypatch.setattr(query_engine, "scan_snapshot_nodes", incomplete)
    with pytest.raises(QueryExecutionError, match="incomplete certificate"):
        execute_query(plan, snapshot)

    def tampered(*args, **kwargs):
        result = core_scan_snapshot_nodes(*args, **kwargs)
        result["coverage_digest"] = "0" * 64
        return result

    monkeypatch.setattr(query_engine, "scan_snapshot_nodes", tampered)
    with pytest.raises(BundleReductionError, match="digest verification failed"):
        execute_query_bundle(_single_coverage_bundle(), snapshot)


@pytest.mark.parametrize("entity", ["coverage_scan", "snapshot_pack"])
def test_direct_coverage_entities_reject_snapshot_sha_drift(
    tmp_path: Path,
    entity: str,
) -> None:
    snapshot = _synthetic_snapshot(tmp_path)
    broken = copy.deepcopy(snapshot)
    broken["packs"][0]["sha256"] = "0" * 64
    plan = (
        _coverage_plan("boq_wall_item_exact_token", ["D9"])
        if entity == "coverage_scan"
        else _role_plan("contract_document")
    )
    with pytest.raises(QueryExecutionError, match="SHA-256 mismatch"):
        execute_query(plan, broken)


def test_yeoju_r1_public_coverage_bundle_counts_and_hash() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "snapshots"
        / "YEOJU-CANONICAL-20260710-092149-R1.json"
    )
    if not manifest_path.is_file():
        pytest.skip("Yeoju R1 canonical manifest is not available")
    snapshot = load_snapshot(manifest_path)
    verification = verify_snapshot(snapshot)
    verification.require_valid()

    result = execute_query_bundle(_coverage_bundle(), snapshot)
    expected = {
        "b09_boq": (5853, 74, 0, 1),
        "b09_model": (16861, 2327, 0, 6),
        "e01_boq": (5853, 4280, 0, 1),
        "e01_model": (16861, 16861, 0, 6),
        "e01_drawing": (75437, 84, 0, 33),
        "e02_model": (16861, 16861, 0, 6),
        "e02_drawing": (75437, 576, 0, 33),
        "e03_boq": (5853, 2300, 0, 1),
        "e04_model": (16861, 16861, 0, 6),
        "e04_quantity": (50098, 50098, 0, 11),
        "e04_spec": (34071, 10887, 33, 105),
        "e05_boq": (5853, 5853, 0, 1),
        "e05_law": (84, 84, 0, 1),
    }
    for plan_id, counts in expected.items():
        plan_result = result.plan_results[plan_id]
        assert (
            plan_result.scanned_records,
            plan_result.values["scoped_record_count"],
            plan_result.matched_records,
            len(plan_result.source_packs),
        ) == counts
    for role_plan in ("e04_role", "e05_role"):
        plan_result = result.plan_results[role_plan]
        assert (
            plan_result.scanned_records,
            plan_result.matched_records,
            len(plan_result.source_packs),
        ) == (173, 0, 173)
    assert result.reducer_values["e04_absence"]["missing_role"] == (
        "project_structural_calculation"
    )
    assert [
        item["match_count"]
        for item in result.reducer_values["e04_absence"]["generic_context"]
    ] == [0, 0, 33]
    assert result.reducer_values["e05_absence"]["missing_role"] == "contract_document"
    assert result.result_hash == (
        "b87f6d7a70518ad78c592120d9653af2e754830bfc64a7a03fdab05c4e612741"
    )
