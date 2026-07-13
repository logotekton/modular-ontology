from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_ontology.canonical_ai import build_canonical_answer, openai_plan_question
from modular_ontology.project_query import ProjectQueryExecution
from modular_ontology.query_contract import validate_query_plan
from modular_ontology.query_engine import execute_query
from modular_ontology.snapshot_store import load_snapshot, verify_snapshot


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "snapshots" / "YEOJU-CANONICAL-20260710-092149-R1.json"


class _FakeResponses:
    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(self.output, ensure_ascii=False))


def test_openai_planner_normalizes_boq_catalogue_to_exact_seven_field_plan() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw = {
        "project_id": "yeoju-modular-dormitory",
        "entity": "aggregated_boq_item",
        "intent": "compare",
        "scope": {"source_sheet": "건축_품목집계(전체)", "work_category": "구조"},
        "filters": [],
        "group_by": ["specification"],
        "metrics": [{"aggregation": "sum", "property": "quantity", "output": "total"}],
    }
    responses = _FakeResponses(raw)
    client = SimpleNamespace(responses=responses)

    plan, metadata = openai_plan_question(
        "건축_품목집계(전체)의 구조 공종에서 SM355 H형강 3개 규격 수량 합계는 몇 TON인가?",
        manifest["planner_context"],
        api_key="test-only",
        llm_client=client,
    )

    assert list(plan) == ["project_id", "entity", "intent", "scope", "filters", "group_by", "metrics"]
    assert plan == {
        "project_id": "yeoju",
        "entity": "boq_item",
        "intent": "aggregate",
        "scope": {"source_sheet": "건축_품목집계(전체)", "work_category": "구조"},
        "filters": [
            {"field": "item_name", "operator": "eq", "value": "용접구조용 압연강재"},
            {
                "field": "specification",
                "operator": "in",
                "value": [
                    "H 150x150x7x10, SM355",
                    "H 200x200x8x12, SM355",
                    "H 300x150x6.5x9, SM355",
                ],
            },
            {"field": "normalized_unit", "operator": "eq", "value": "metric_ton"},
        ],
        "group_by": ["specification"],
        "metrics": [{"name": "sum", "field": "quantity", "alias": "total"}],
    }
    assert metadata["attempts"] == 1
    assert len(metadata["plan_hash"]) == 64
    assert responses.calls[0]["text"] == {"format": {"type": "json_object"}}


CASES = [
    (
        "여주 기준 스냅샷의 Revit 문 카테고리 물리 객체 수와 고유 타입 수는 각각 얼마인가?",
        {"project_id": "yeoju", "entity": "element", "intent": "aggregate", "scope": {"category": "문"}, "filters": [], "group_by": [], "metrics": [{"name": "count_distinct", "field": "source_element_id", "alias": "object_count"}, {"name": "count_distinct", "field": "type_name", "alias": "type_count"}]},
        {"object_count": 94, "type_count": 13}, {"object_count": "개", "type_count": "종"}, {"category": "문", "granularity": "physical_instance"},
    ),
    (
        "여주 기준 스냅샷에서 창 카테고리 객체를 지상 1층과 지상 2층으로 나누면 각 몇 개이며 어느 층이 더 많은가?",
        {"project_id": "yeoju", "entity": "element", "intent": "aggregate", "scope": {"category": "창"}, "filters": [{"field": "level_name", "operator": "in", "value": ["지상 1층", "지상 2층"]}], "group_by": ["level_name"], "metrics": [{"name": "count_distinct", "field": "source_element_id", "alias": "object_count"}]},
        {"지상 1층": 31, "지상 2층": 32, "total": 63, "larger_level": "지상 2층", "difference": 1}, "개", {"category": "창", "levels": ["지상 1층", "지상 2층"]},
    ),
    (
        "벽 카테고리 물리 객체가 2,500개라는 주장이 canonical Revit 0.8.4 스냅샷과 일치하는가?",
        {"project_id": "yeoju", "entity": "element", "intent": "aggregate", "scope": {"category": "벽"}, "filters": [], "group_by": [], "metrics": [{"name": "count_distinct", "field": "source_element_id", "alias": "actual"}]},
        {"claim": 2500, "actual": 2327, "matches": False, "overstatement": 173}, "개", {"category": "벽", "granularity": "physical_instance"},
    ),
    (
        "창 타입이 12종이라는 주장이 canonical Revit 0.8.4 스냅샷과 일치하는가? 실제 타입 목록도 제시하라.",
        {"project_id": "yeoju", "entity": "element", "intent": "aggregate", "scope": {"category": "창"}, "filters": [], "group_by": [], "metrics": [{"name": "count_distinct", "field": "type_name", "alias": "actual"}, {"name": "collect_distinct", "field": "type_name", "alias": "types"}]},
        {"claim": 12, "actual": 10, "matches": False, "overstatement": 2, "types": ["AW1", "AW3", "AW4", "AW5", "AW6", "AW7", "AW8", "PW1", "PW2", "PW3"]}, "종", {"category": "창"},
    ),
    (
        "Revit의 정확한 '계단' 카테고리 인스턴스가 15개라는 주장이 맞는가? 계단진행·계단참은 합산하지 않는다.",
        {"project_id": "yeoju", "entity": "element", "intent": "aggregate", "scope": {"category": "계단"}, "filters": [], "group_by": [], "metrics": [{"name": "count_distinct", "field": "source_element_id", "alias": "actual"}]},
        {"claim": 15, "actual": 5, "matches": False, "overstatement": 10}, "개", {"category": "계단", "excluded_categories": ["계단진행", "계단참"]},
    ),
    (
        "canonical BOQ의 건축_품목집계(전체) 시트에서 벽체 공종이면서 단위가 ㎡인 행의 수량 합계는 얼마인가? 이 값은 발주량이 아니라 해당 시트의 면적 단위 수량 합계로만 답하라.",
        {"project_id": "yeoju", "entity": "boq_item", "intent": "aggregate", "scope": {"source_sheet": "건축_품목집계(전체)", "work_category": "벽체"}, "filters": [{"field": "normalized_unit", "operator": "eq", "value": "square_meter"}], "group_by": [], "metrics": [{"name": "sum", "field": "quantity", "alias": "quantity_sum"}]},
        3446.77, "㎡", {"source_sheet": "건축_품목집계(전체)", "work_category": "벽체", "normalized_unit": "square_meter", "semantic": "worksheet_quantity_sum_not_order_quantity"},
    ),
    (
        "건축_품목집계(전체)의 구조 공종에서 SM355 H형강 3개 규격 수량 합계는 몇 TON인가?",
        {"project_id": "yeoju", "entity": "boq_item", "intent": "aggregate", "scope": {"source_sheet": "건축_품목집계(전체)", "work_category": "구조"}, "filters": [{"field": "item_name", "operator": "eq", "value": "용접구조용 압연강재"}, {"field": "specification", "operator": "in", "value": ["H 150x150x7x10, SM355", "H 200x200x8x12, SM355", "H 300x150x6.5x9, SM355"]}, {"field": "normalized_unit", "operator": "eq", "value": "metric_ton"}], "group_by": ["specification"], "metrics": [{"name": "sum", "field": "quantity", "alias": "total"}]},
        {"total": 72.553, "breakdown": {"H 150x150x7x10, SM355": 25.464, "H 200x200x8x12, SM355": 22.748, "H 300x150x6.5x9, SM355": 24.341}}, "TON", {"source_sheet": "건축_품목집계(전체)", "work_category": "구조", "material_grade": "SM355"},
    ),
]


@pytest.mark.parametrize("question,plan,expected_values,expected_unit,expected_scope", CASES)
def test_m3_deterministic_answers_match_the_seven_eligible_gold_cases(
    question, plan, expected_values, expected_unit, expected_scope
) -> None:
    snapshot = load_snapshot(MANIFEST)
    verification = verify_snapshot(snapshot)
    result = execute_query(plan, snapshot)
    execution = ProjectQueryExecution(snapshot, verification, validate_query_plan(plan), result, question)

    answer = build_canonical_answer(question, execution)

    assert answer["values"] == expected_values
    assert answer["unit"] == expected_unit
    assert answer["scope"] == expected_scope
    assert answer["complete"] is True
    assert answer["evidence"]["query_hash"] == result.query_hash
    assert answer["evidence"]["result_hash"] == result.result_hash
    assert answer["evidence"]["snapshot_id"] == snapshot.snapshot_id
