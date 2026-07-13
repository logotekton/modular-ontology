from __future__ import annotations

import copy
import hashlib
import json
import math
import zipfile
from pathlib import Path

import pytest

from modular_ontology.query_contract import (
    PLAN_FIELDS,
    try_validate_plan_bundle,
    validate_plan_bundle,
)
from modular_ontology.query_engine import (
    BundleReductionError,
    QueryExecutionError,
    execute_query_bundle,
)


PROJECT_ID = "yeoju"
SNAPSHOT_ID = "YEOJU-CANONICAL-20260710-092149-R1"


def _pack(tmp_path: Path, name: str, nodes: list[dict]) -> tuple[Path, str]:
    path = tmp_path / f"{name}.zip"
    payload = "\n".join(json.dumps(node, ensure_ascii=False) for node in nodes) + "\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("graph/nodes.jsonl", payload)
        archive.writestr("graph/edges.jsonl", "")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(
    tmp_path: Path,
    packs: list[tuple[str, str, list[dict]]],
) -> dict:
    entries = []
    for pack_id, role, nodes in packs:
        path, digest = _pack(tmp_path, pack_id, nodes)
        entries.append(
            {
                "pack_id": pack_id,
                "path": str(path),
                "sha256": digest,
                "role": role,
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
    intent: str = "aggregate",
    scope: dict | None = None,
    filters: list[dict] | None = None,
    group_by: list[str] | None = None,
    metrics: list[dict] | None = None,
) -> dict:
    plan = {
        "project_id": PROJECT_ID,
        "entity": entity,
        "intent": intent,
        "scope": scope or {},
        "filters": filters or [],
        "group_by": group_by or [],
        "metrics": metrics or [],
    }
    assert set(plan) == PLAN_FIELDS
    return plan


def _eq(field: str, value: object) -> dict:
    return {"field": field, "operator": "eq", "value": value}


def _requirement(
    requirement_id: str,
    standard_code: str,
    evidence_ref: str,
    requirement_text: str,
) -> dict:
    return {
        "id": requirement_id,
        "node_type": "Requirement",
        "properties": {
            "standard_code": standard_code,
            "evidence_ref": evidence_ref,
            "requirement_text": requirement_text,
        },
    }


def _spec_plan(requirement_id: str, standard_code: str) -> dict:
    return _plan(
        "specification",
        scope={"standard_code": standard_code},
        filters=[_eq("id", requirement_id)],
        group_by=["id", "standard_code", "evidence_ref", "requirement_text"],
        metrics=[{"name": "count", "field": "*", "alias": "source_row_count"}],
    )


def _phase_b_fixture(tmp_path: Path) -> tuple[dict, dict]:
    granite_id = "requirement:8744af36e78ed0da6db2"
    sg_id = "requirement:fb3847994b223c74e476"
    urethane_id = "requirement:9742285ee43cf871cd51"
    eps_id = "requirement:46b3b88f6eb36eca1c4f"
    d05_id = "requirement:449b69e4cd91ab445fd3"
    d06_id = "requirement:ed675ee21afcd53dc0ab"

    requirements = [
        _requirement(
            granite_id,
            "EXCS 41 35 01",
            "chunk:ff3243fdbd32d4f7cb3e",
            "2.1.1 화강석 판재 (1) 화강석 판재 "
            "① 압축강도 : 50 MPa 이상 ② 흡수율 : 5 % 미만 (2) 대리석",
        ),
        _requirement(
            sg_id,
            "EXCS 41 51 02",
            "chunk:d631c6900fcec6991452",
            "S.G 패널 칸막이 2.2.3.2 패널심재 "
            "방화석고보드 두께 12.5 mm의 것을 사용한다.",
        ),
        _requirement(
            urethane_id,
            "LHCS 41 40 06",
            "chunk:3a3f3bae6207adc801d2",
            "3.3.1 방수공사 ② 우레탄 도막방수 표 3.3-2 우레탄 도막방수의 시공순서 "
            "우레탄 방수재 총 3.0 kg/m2 이상"
            " (1,2,3차 총두께 3 mm 이상)",
        ),
        _requirement(
            eps_id,
            "LHCS 41 42 00",
            "chunk:07746367464fe96187e7",
            "2.1.2 단열재 (1) 비드법 발포 폴리스티렌 단열재의 초기열전도율은 "
            "0.031 W/mk 이하 (평균온도 23±2 ℃)로 한다. "
            "(2) 압출법 발포 폴리스티렌 단열재",
        ),
        _requirement(
            d05_id,
            "KCS 41 43 01",
            "chunk:a06552f063e3d828269c",
            "1.2.1 관련 법규 ∙ 건축법 시행령 "
            "∙ 건축물의 피난·방화구조 등의 기준 "
            "∙ 내화구조의 인정 및 관리기준 "
            "∙ 건축물의 구조기준 등에 관한 규칙 "
            "∙ 내화충전구조 세부운영지침 1.2.2 관련 기준",
        ),
        _requirement(
            d06_id,
            "KCS 41 42 02",
            "chunk:c6fcf4c58b63c8866513",
            "1.2.1 관련 법규 "
            "• 국토교통부고시, 건축물의 에너지절약설계기준 "
            "• 국토교통부고시, 에너지절약형 친환경주택의 건설기준 "
            "• 국토교통부고시, 공동주택 결로 방지를 위한 설계기준 "
            "1.2.2 관련 기준",
        ),
    ]

    elements = [
        {
            "id": "element:granite",
            "node_type": "BIMElement",
            "properties": {
                "source_element_id": "granite",
                "category": "바닥",
                "type_name": "화강석(버너구이)(T=25)",
            },
        },
        *[
            {
                "id": f"element:urethane:{index}",
                "node_type": "BIMElement",
                "properties": {
                    "source_element_id": f"urethane:{index}",
                    "category": "바닥",
                    "type_name": "도막방수(T=3)",
                },
            }
            for index in range(18)
        ],
        *[
            {
                "id": f"element:df3:{index}",
                "node_type": "BIMElement",
                "properties": {
                    "source_element_id": f"df3:{index}",
                    "category": "벽",
                    "type_name": "DF3-방화석고보드",
                    "workset_name": "현장공사분-수장공사",
                },
            }
            for index in range(4)
        ],
    ]

    modules = ["2-02-A", "2-03-A", "2-04-A", "2-08-A", "2-09-A", "2-10-A", "2-11-A"]
    areas = [
        18.949469975443336,
        18.949469975501966,
        18.949469975127908,
        18.949469975538253,
        18.94946997510819,
        18.949469975549057,
        18.949469975275974,
    ]
    quantity_facts = [
        {
            "id": f"quantity:{module}",
            "node_type": "BIMQuantityFact",
            "properties": {
                "category": "바닥",
                "type_name": "비드법보온판(2종1호)(T=50)",
                "level_name": "지상 2층",
                "module_id": module,
                "quantity_role": "primary_element_area",
                "measure_kind": "area",
                "is_primary_measure": True,
                "aggregation_scope": "element",
                "source_element_id": f"eps:{module}",
                "area_m2": area,
            },
        }
        for module, area in zip(modules, areas, strict=True)
    ]

    boq_nodes = [
        {
            "id": "aggregated_boq_item:sheet_8a45cfd923d1:0133",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "work_category": "수장공사",
                "item_name": "DF3-방화석고보드",
                "specification": "벽체, 방화G/B T12.5*1겹",
                "quantity": 128.34,
                "unit": "㎡",
                "normalized_unit": "square_meter",
                "source_sheet": "건축_품목집계(전체)",
                "source_row": 133,
            },
        },
        {
            "id": "aggregated_boq_item:sheet_8a45cfd923d1:0074",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "work_category": "바닥",
                "item_name": "비드법보온판2종1호",
                "specification": "T50",
                "quantity": 132.65,
                "unit": "㎡",
                "normalized_unit": "square_meter",
                "source_sheet": "건축_품목집계(전체)",
                "source_row": 74,
            },
        },
    ]

    snapshot = _snapshot(
        tmp_path,
        [
            ("specs", "spec", requirements),
            ("inventory", "model_inventory", elements),
            ("quantity", "model_quantity", quantity_facts),
            ("boq", "boq", boq_nodes),
        ],
    )
    plans = {
        "granite_spec": _spec_plan(granite_id, "EXCS 41 35 01"),
        "sg_spec": _spec_plan(sg_id, "EXCS 41 51 02"),
        "urethane_spec": _spec_plan(urethane_id, "LHCS 41 40 06"),
        "eps_spec": _spec_plan(eps_id, "LHCS 41 42 00"),
        "d05_spec": _spec_plan(d05_id, "KCS 41 43 01"),
        "d06_spec": _spec_plan(d06_id, "KCS 41 42 02"),
        "granite_model": _plan(
            "element",
            filters=[
                _eq("category", "바닥"),
                _eq("type_name", "화강석(버너구이)(T=25)"),
            ],
            metrics=[
                {
                    "name": "count_distinct",
                    "field": "source_element_id",
                    "alias": "instance_count",
                }
            ],
        ),
        "urethane_model": _plan(
            "element",
            filters=[_eq("category", "바닥"), _eq("type_name", "도막방수(T=3)")],
            group_by=["type_name"],
            metrics=[
                {
                    "name": "count_distinct",
                    "field": "source_element_id",
                    "alias": "instance_count",
                }
            ],
        ),
        "df3_model": _plan(
            "element",
            filters=[
                _eq("category", "벽"),
                _eq("type_name", "DF3-방화석고보드"),
                _eq("workset_name", "현장공사분-수장공사"),
            ],
            metrics=[
                {
                    "name": "count_distinct",
                    "field": "source_element_id",
                    "alias": "instance_count",
                }
            ],
        ),
        "eps_quantity": _plan(
            "quantity_fact",
            scope={
                "category": "바닥",
                "type_name": "비드법보온판(2종1호)(T=50)",
                "level_name": "지상 2층",
                "quantity_role": "primary_element_area",
                "measure_kind": "area",
                "is_primary_measure": True,
                "aggregation_scope": "element",
            },
            metrics=[
                {"name": "sum", "field": "area_m2", "alias": "model_area"},
                {
                    "name": "count_distinct",
                    "field": "source_element_id",
                    "alias": "instance_count",
                },
                {"name": "collect_distinct", "field": "module_id", "alias": "modules"},
            ],
        ),
        "boq_c07": _plan(
            "boq_item",
            scope={"source_sheet": "건축_품목집계(전체)"},
            filters=[
                _eq("work_category", "수장공사"),
                _eq("item_name", "DF3-방화석고보드"),
            ],
            group_by=["id", "specification"],
            metrics=[{"name": "count", "field": "*", "alias": "source_row_count"}],
        ),
        "boq_c08": _plan(
            "boq_item",
            scope={"source_sheet": "건축_품목집계(전체)"},
            filters=[
                _eq("work_category", "바닥"),
                _eq("item_name", "비드법보온판2종1호"),
                _eq("specification", "T50"),
            ],
            metrics=[{"name": "sum", "field": "quantity", "alias": "boq_quantity"}],
        ),
    }
    reducers = [
        {
            "id": "granite_strength",
            "op": "extract_constraint",
            "source": "plans.granite_spec.rows",
            "profile": "granite_compressive_strength",
        },
        {
            "id": "granite_absorption",
            "op": "extract_constraint",
            "source": "plans.granite_spec.rows",
            "profile": "granite_water_absorption",
        },
        {
            "id": "sg_thickness",
            "op": "extract_constraint",
            "source": "plans.sg_spec.rows",
            "profile": "sg_panel_fire_gypsum_board_thickness",
        },
        {
            "id": "urethane_constraint",
            "op": "extract_constraint",
            "source": "plans.urethane_spec.rows",
            "profile": "urethane_waterproofing_total_thickness",
        },
        {
            "id": "eps_constraint",
            "op": "extract_constraint",
            "source": "plans.eps_spec.rows",
            "profile": "eps_bead_initial_thermal_conductivity",
        },
        {
            "id": "strength_claim_satisfies",
            "op": "satisfies_constraint",
            "constraint": "reducers.granite_strength",
            "actual": {"value": 24, "unit": "MPa"},
        },
        {
            "id": "strength_shortfall",
            "op": "subtract",
            "left": {"ref": "reducers.granite_strength.value"},
            "right": {"value": 24, "unit": "MPa"},
        },
        {
            "id": "sg_claim_satisfies",
            "op": "satisfies_constraint",
            "constraint": "reducers.sg_thickness",
            "actual": {"value": 9.5, "unit": "mm"},
        },
        {
            "id": "urethane_model_dimension",
            "op": "extract_dimension_token",
            "source": "plans.urethane_model.rows",
            "profile": "type_name_t_thickness_mm",
        },
        {
            "id": "urethane_model_meets",
            "op": "satisfies_constraint",
            "constraint": "reducers.urethane_constraint",
            "actual": {"ref": "reducers.urethane_model_dimension.value"},
        },
        {
            "id": "urethane_boundary_is_minimum",
            "op": "equals",
            "left": {"ref": "reducers.urethane_constraint.operator_token"},
            "right": {"value": "gte"},
        },
        {
            "id": "claim_five_matches",
            "op": "equals",
            "left": {"ref": "reducers.urethane_constraint.value"},
            "right": {"value": 5, "unit": "mm"},
        },
        {
            "id": "boq_c07_dimension",
            "op": "extract_dimension_token",
            "source": "plans.boq_c07.rows",
            "profile": "boq_spec_t_thickness_mm",
        },
        {
            "id": "c07_thickness_text_matches",
            "op": "equals",
            "left": {"ref": "reducers.sg_thickness.value"},
            "right": {"ref": "reducers.boq_c07_dimension.value"},
        },
        {
            "id": "d05_titles",
            "op": "extract_section_list",
            "source": "plans.d05_spec.rows",
            "section": "1.2.1",
            "heading": "관련 법규",
            "max_items": 5,
        },
        {
            "id": "d06_titles",
            "op": "extract_section_list",
            "source": "plans.d06_spec.rows",
            "section": "1.2.1",
            "heading": "관련 법규",
            "item_prefix": "국토교통부고시,",
            "max_items": 3,
        },
        {
            "id": "model_area_rounded",
            "op": "round",
            "source": "plans.eps_quantity.values.model_area",
            "digits": 2,
        },
        {
            "id": "c08_rounded_match",
            "op": "equals",
            "left": {"ref": "reducers.model_area_rounded"},
            "right": {"ref": "plans.boq_c08.values.boq_quantity"},
        },
    ]
    return snapshot, {"project_id": PROJECT_ID, "plans": plans, "reducers": reducers}


def test_phase_b_canonical_building_blocks_and_qset_projection(tmp_path: Path) -> None:
    snapshot, bundle = _phase_b_fixture(tmp_path)
    validate_plan_bundle(bundle)

    result = execute_query_bundle(bundle, snapshot)
    repeated = execute_query_bundle(bundle, snapshot)
    values = result.reducer_values

    projections = {
        reducer_id: {
            key: values[reducer_id][key]
            for key in expected
        }
        for reducer_id, expected in {
            "granite_strength": {
                "operator": ">=",
                "value": 50,
                "unit": "MPa",
                "section": "2.1.1 화강석 판재",
            },
            "granite_absorption": {
                "operator": "<",
                "value": 5,
                "unit": "%",
                "section": "2.1.1 화강석 판재",
            },
            "sg_thickness": {
                "operator": "eq",
                "value": 12.5,
                "unit": "mm",
                "section": "2.2.3.2 패널심재",
                "context": "S.G 패널 칸막이",
            },
            "urethane_constraint": {
                "operator": ">=",
                "value": 3,
                "unit": "mm",
                "section": "3.3.1",
            },
            "eps_constraint": {
                "operator": "<=",
                "value": 0.031,
                "unit": "W/m·K",
                "section": "2.1.2(1)",
                "mean_temperature": "23±2℃",
            },
        }.items()
    }
    assert projections == {
        "granite_strength": {
            "operator": ">=",
            "value": 50,
            "unit": "MPa",
            "section": "2.1.1 화강석 판재",
        },
        "granite_absorption": {
            "operator": "<",
            "value": 5,
            "unit": "%",
            "section": "2.1.1 화강석 판재",
        },
        "sg_thickness": {
            "operator": "eq",
            "value": 12.5,
            "unit": "mm",
            "section": "2.2.3.2 패널심재",
            "context": "S.G 패널 칸막이",
        },
        "urethane_constraint": {
            "operator": ">=",
            "value": 3,
            "unit": "mm",
            "section": "3.3.1",
        },
        "eps_constraint": {
            "operator": "<=",
            "value": 0.031,
            "unit": "W/m·K",
            "section": "2.1.2(1)",
            "mean_temperature": "23±2℃",
        },
    }
    assert values["granite_strength"]["operator_token"] == "gte"
    assert values["granite_absorption"]["operator_token"] == "lt"
    assert values["sg_thickness"]["operator_token"] == "eq"
    assert values["urethane_constraint"]["operator_token"] == "gte"
    assert values["eps_constraint"]["operator_token"] == "lte"
    assert values["eps_constraint"]["mean_temperature_components"] == {
        "value": 23,
        "tolerance": 2,
        "unit": "degree_celsius",
    }

    assert values["strength_claim_satisfies"] is False
    assert values["strength_shortfall"] == 26
    assert values["sg_claim_satisfies"] is False
    assert values["urethane_model_meets"] is True
    assert values["urethane_boundary_is_minimum"] is True
    assert values["claim_five_matches"] is False
    assert values["boq_c07_dimension"] == {
        "profile": "boq_spec_t_thickness_mm",
        "parameter": "thickness",
        "boq_item_id": "aggregated_boq_item:sheet_8a45cfd923d1:0133",
        "specification": "벽체, 방화G/B T12.5*1겹",
        "token": "T12.5",
        "value": 12.5,
        "unit": "millimeter",
    }
    assert "requirement_id" not in values["boq_c07_dimension"]
    assert values["c07_thickness_text_matches"] is True

    assert values["d05_titles"]["items"] == [
        "건축법 시행령",
        "건축물의 피난·방화구조 등의 기준",
        "내화구조의 인정 및 관리기준",
        "건축물의 구조기준 등에 관한 규칙",
        "내화충전구조 세부운영지침",
    ]
    assert values["d05_titles"]["count"] == 5
    assert values["d05_titles"]["last_item"] == "내화충전구조 세부운영지침"
    assert values["d06_titles"]["items"] == [
        "건축물의 에너지절약설계기준",
        "에너지절약형 친환경주택의 건설기준",
        "공동주택 결로 방지를 위한 설계기준",
    ]
    assert values["d06_titles"]["count"] == 3

    quantity = result.plan_results["eps_quantity"].values
    assert quantity["model_area"] == 132.646289827545
    assert quantity["model_area_unit"] == "square_meter"
    assert quantity["instance_count"] == 7
    assert quantity["modules"] == [
        "2-02-A",
        "2-03-A",
        "2-04-A",
        "2-08-A",
        "2-09-A",
        "2-10-A",
        "2-11-A",
    ]
    assert values["model_area_rounded"] == 132.65
    assert values["c08_rounded_match"] is True

    value_units = result.evidence["value_units"]
    assert value_units["reducers.granite_strength.value"] == "megapascal"
    assert value_units["reducers.granite_absorption.value"] == "percent"
    assert value_units["reducers.sg_thickness.value"] == "millimeter"
    assert value_units["reducers.eps_constraint.value"] == "watt_per_meter_kelvin"
    assert (
        value_units["reducers.eps_constraint.mean_temperature_components.value"]
        == "degree_celsius"
    )
    assert value_units["reducers.d06_titles.count"] == "count"
    assert result.result_hash == repeated.result_hash
    assert result.query_hash == result.bundle_hash


def test_phase_b_contract_rejects_unsafe_profiles_sources_and_literals() -> None:
    plan = _spec_plan("requirement:x", "EXCS 41 35 01")
    base = {"project_id": PROJECT_ID, "plans": {"spec": plan}, "reducers": []}

    bad_reducers = [
        (
            {
                "id": "bad",
                "op": "extract_constraint",
                "source": "plans.spec.rows",
                "profile": "user_regex",
            },
            "unsupported_profile",
        ),
        (
            {
                "id": "bad",
                "op": "extract_constraint",
                "source": "plans.spec.values.source_row_count",
                "profile": "granite_compressive_strength",
            },
            "invalid_source_reference",
        ),
        (
            {
                "id": "bad",
                "op": "extract_constraint",
                "source": "plans.spec.rows",
                "profile": "granite_compressive_strength",
                "regex": ".*",
            },
            "unknown_field",
        ),
        (
            {
                "id": "bad",
                "op": "extract_section_list",
                "source": "plans.spec.rows",
                "section": "1",
                "heading": "관련 법규",
            },
            "invalid_section",
        ),
        (
            {
                "id": "bad",
                "op": "extract_section_list",
                "source": "plans.spec.rows",
                "section": "1.2.1",
                "heading": "관련 법규",
                "max_items": 0,
            },
            "invalid_max_items",
        ),
        (
            {
                "id": "bad",
                "op": "round",
                "source": "plans.spec.rows",
                "digits": 7,
            },
            "invalid_digits",
        ),
    ]
    for reducer, expected_code in bad_reducers:
        payload = copy.deepcopy(base)
        payload["reducers"] = [reducer]
        validation = try_validate_plan_bundle(payload)
        assert not validation.valid
        assert expected_code in {issue.code for issue in validation.issues}

    missing_unit = copy.deepcopy(base)
    missing_unit["reducers"] = [
        {
            "id": "constraint",
            "op": "extract_constraint",
            "source": "plans.spec.rows",
            "profile": "granite_compressive_strength",
        },
        {
            "id": "bad",
            "op": "satisfies_constraint",
            "constraint": "reducers.constraint",
            "actual": {"value": 50},
        },
    ]
    validation = try_validate_plan_bundle(missing_unit)
    assert not validation.valid
    assert "unit_required" in {issue.code for issue in validation.issues}

    literal_only_subtract = copy.deepcopy(base)
    literal_only_subtract["reducers"] = [
        {
            "id": "bad",
            "op": "subtract",
            "left": {"value": 5, "unit": "mm"},
            "right": {"value": 3, "unit": "mm"},
        }
    ]
    validation = try_validate_plan_bundle(literal_only_subtract)
    assert not validation.valid
    assert "source_required" in {issue.code for issue in validation.issues}

    valid_source_literal_subtract = copy.deepcopy(base)
    valid_source_literal_subtract["reducers"] = [
        {
            "id": "constraint",
            "op": "extract_constraint",
            "source": "plans.spec.rows",
            "profile": "granite_compressive_strength",
        },
        {
            "id": "difference",
            "op": "subtract",
            "left": {"ref": "reducers.constraint.value"},
            "right": {"value": 24, "unit": "MPa"},
        },
    ]
    assert try_validate_plan_bundle(valid_source_literal_subtract).valid

    regex_filter = copy.deepcopy(base)
    regex_filter["plans"]["spec"]["filters"] = [
        {"field": "requirement_text", "operator": "regex", "value": ".*"}
    ]
    validation = try_validate_plan_bundle(regex_filter)
    assert not validation.valid
    assert "operator_not_allowed" in {issue.code for issue in validation.issues}


@pytest.mark.parametrize("bad_value", [None, "18.9", float("nan"), float("inf")])
def test_quantity_fact_area_is_fail_closed(
    tmp_path: Path,
    bad_value: object,
) -> None:
    node = {
        "id": "quantity:bad",
        "node_type": "BIMQuantityFact",
        "properties": {
            "category": "바닥",
            "type_name": "T",
            "area_m2": bad_value,
            "source_element_id": "bad",
        },
    }
    snapshot = _snapshot(tmp_path, [("quantity", "model_quantity", [node])])
    bundle = {
        "project_id": PROJECT_ID,
        "plans": {
            "quantity": _plan(
                "quantity_fact",
                metrics=[{"name": "sum", "field": "area_m2", "alias": "area"}],
            )
        },
        "reducers": [],
    }

    with pytest.raises(QueryExecutionError, match="numeric|non-numeric"):
        execute_query_bundle(bundle, snapshot)


def test_quantity_fact_area_rejects_empty_result(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, [("quantity", "model_quantity", [])])
    bundle = {
        "project_id": PROJECT_ID,
        "plans": {
            "quantity": _plan(
                "quantity_fact",
                metrics=[{"name": "sum", "field": "area_m2", "alias": "area"}],
            )
        },
        "reducers": [],
    }

    with pytest.raises(QueryExecutionError, match="empty result"):
        execute_query_bundle(bundle, snapshot)


@pytest.mark.parametrize(
    "specification",
    [
        "벽체 T0",
        "벽체 T-1",
        "벽체 T1e3",
        "벽체 T12.5~T15",
        "벽체 T12.5 T15",
    ],
)
def test_boq_dimension_profile_rejects_ambiguous_or_invalid_tokens(
    tmp_path: Path,
    specification: str,
) -> None:
    node = {
        "id": "boq:bad",
        "node_type": "AggregatedBOQItem",
        "properties": {
            "item_name": "DF3",
            "specification": specification,
            "source_sheet": "S",
        },
    }
    snapshot = _snapshot(tmp_path, [("boq", "boq", [node])])
    bundle = {
        "project_id": PROJECT_ID,
        "plans": {
            "boq": _plan(
                "boq_item",
                scope={"source_sheet": "S"},
                filters=[_eq("item_name", "DF3")],
                group_by=["id", "specification"],
                metrics=[{"name": "count", "field": "*", "alias": "source_row_count"}],
            )
        },
        "reducers": [
            {
                "id": "dimension",
                "op": "extract_dimension_token",
                "source": "plans.boq.rows",
                "profile": "boq_spec_t_thickness_mm",
            }
        ],
    }

    with pytest.raises(BundleReductionError, match="token|positive"):
        execute_query_bundle(bundle, snapshot)


def test_section_prefix_mismatch_is_not_silently_filtered(tmp_path: Path) -> None:
    requirement = _requirement(
        "requirement:prefix",
        "KCS 41 42 02",
        "chunk:prefix",
        "1.2.1 관련 법규 • 국토교통부고시, 첫째 • 다른기관고시, 둘째 1.2.2 관련 기준",
    )
    snapshot = _snapshot(tmp_path, [("spec", "spec", [requirement])])
    bundle = {
        "project_id": PROJECT_ID,
        "plans": {"spec": _spec_plan("requirement:prefix", "KCS 41 42 02")},
        "reducers": [
            {
                "id": "titles",
                "op": "extract_section_list",
                "source": "plans.spec.rows",
                "section": "1.2.1",
                "heading": "관련 법규",
                "item_prefix": "국토교통부고시,",
            }
        ],
    }

    with pytest.raises(BundleReductionError, match="required item_prefix"):
        execute_query_bundle(bundle, snapshot)


def test_round_uses_half_up_and_rejects_count_sources(tmp_path: Path) -> None:
    nodes = [
        {
            "id": "boq:round",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "quantity": 1.005,
                "normalized_unit": "square_meter",
                "source_sheet": "S",
            },
        }
    ]
    snapshot = _snapshot(tmp_path, [("boq", "boq", nodes)])
    good = {
        "project_id": PROJECT_ID,
        "plans": {
            "boq": _plan(
                "boq_item",
                scope={"source_sheet": "S"},
                metrics=[
                    {"name": "sum", "field": "quantity", "alias": "quantity"},
                    {"name": "count", "field": "*", "alias": "row_count"},
                ],
            )
        },
        "reducers": [
            {
                "id": "rounded",
                "op": "round",
                "source": "plans.boq.values.quantity",
                "digits": 2,
            }
        ],
    }
    assert execute_query_bundle(good, snapshot).reducer_values["rounded"] == 1.01

    bad = copy.deepcopy(good)
    bad["reducers"][0]["source"] = "plans.boq.values.row_count"
    with pytest.raises(BundleReductionError, match="non-count unit"):
        execute_query_bundle(bad, snapshot)
