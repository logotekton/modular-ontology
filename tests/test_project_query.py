from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest

from modular_ontology.project_query import (
    ProjectQueryError,
    answer_project_question,
    execute_structured_project_query,
    validate_execution_claim,
)
from modular_ontology.snapshot_store import VerificationIssue, sha256_path


def _snapshot(tmp_path: Path) -> Path:
    pack = tmp_path / "inventory.zip"
    nodes = [
        {
            "id": f"element:{index}",
            "node_type": "BIMElement",
            "properties": {
                "element_id": index,
                "category": "문",
                "module_name": "2-01-A",
                "level": "지상 2층",
                "type_name": "D1" if index < 2 else "D2",
                "area": 1.0,
            },
            "evidence_refs": [{"source_file": "elements.jsonl", "source_line": index + 1}],
        }
        for index in range(3)
    ]
    with ZipFile(pack, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"pack_id": "inventory"}))
        archive.writestr(
            "graph/nodes.jsonl",
            "\n".join(json.dumps(node, ensure_ascii=False) for node in nodes) + "\n",
        )
    manifest = tmp_path / "snapshot.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "yeoju",
                "source_signature": "canonical-test",
                "packs": [
                    {
                        "pack_id": "inventory",
                        "path": pack.name,
                        "sha256": sha256_path(pack),
                        "role": "model_inventory",
                        "included": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _plan() -> dict:
    return {
        "project_id": "yeoju",
        "entity": "element",
        "intent": "count",
        "scope": {"module_id": "2-01-A", "category": "문", "level_name": "지상 2층"},
        "filters": [],
        "group_by": [],
        "metrics": [],
    }


def test_validated_contract_plan_runs_directly_in_deterministic_executor(tmp_path: Path) -> None:
    execution = execute_structured_project_query(_plan(), _snapshot(tmp_path))

    assert execution.verification.valid
    assert execution.result.values == {"count": 3}
    assert execution.result.complete


def test_llm_planner_is_only_used_for_question_to_plan(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    def planner(question: str, contract: dict) -> str:
        calls.append((question, contract))
        return json.dumps(_plan(), ensure_ascii=False)

    execution = answer_project_question("2층 2-01-A 문은 몇 개인가?", _snapshot(tmp_path), planner)

    assert execution.result.values["count"] == 3
    assert calls[0][1]["rule"].startswith("Return only a query-plan")


def test_claim_validation_catches_value_and_scope_but_supports_exact_envelope(tmp_path: Path) -> None:
    execution = execute_structured_project_query(_plan(), _snapshot(tmp_path))
    scope = {
        "project_id": "yeoju",
        "entity": "element",
        "scope": {"module_id": "2-01-A", "category": "문", "level_name": "지상 2층"},
        "filters": [],
        "group_by": [],
    }
    supported = validate_execution_claim(
        execution,
        {"claim_id": "A01", "metric": "count", "value": 3, "unit": "개", "scope": scope},
    )
    contradicted = validate_execution_claim(
        execution,
        {"claim_id": "A01", "metric": "count", "value": 4, "unit": "개", "scope": scope},
    )
    wrong_scope = {**scope, "scope": {**scope["scope"], "module_id": "1-01-A"}}
    mixed = validate_execution_claim(
        execution,
        {"claim_id": "A01", "metric": "count", "value": 3, "unit": "개", "scope": wrong_scope},
    )

    assert supported.verdict == "supported"
    assert contradicted.verdict == "contradicted"
    assert mixed.verdict == "mixed_scope"


def test_project_mismatch_is_rejected_before_execution(tmp_path: Path) -> None:
    plan = _plan()
    plan["project_id"] = "other-project"

    with pytest.raises(ProjectQueryError, match="does not match"):
        execute_structured_project_query(plan, _snapshot(tmp_path))


def test_execution_serializes_slotted_verification_issues(tmp_path: Path) -> None:
    execution = execute_structured_project_query(_plan(), _snapshot(tmp_path))
    verification = replace(
        execution.verification,
        warnings=(VerificationIssue(code="example", message="test warning", pack_id="inventory"),),
    )

    payload = replace(execution, verification=verification).as_dict()

    assert payload["verification"]["warnings"] == [
        {"code": "example", "message": "test warning", "pack_id": "inventory"}
    ]


def test_claim_validation_does_not_copy_expected_unit_into_evidence(tmp_path: Path) -> None:
    plan = _plan()
    plan["intent"] = "aggregate"
    plan["metrics"] = [{"name": "sum", "field": "area", "alias": "total_area"}]
    execution = execute_structured_project_query(plan, _snapshot(tmp_path))

    validation = validate_execution_claim(
        execution,
        {"claim_id": "AREA", "metric": "total_area", "value": 3, "unit": "m2"},
    )

    assert validation.verdict == "insufficient"
    assert any(issue.code == "missing_unit" for issue in validation.issues)
