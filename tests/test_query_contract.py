from __future__ import annotations

from modular_ontology.query_contract import (
    QueryContractError,
    try_validate_query_plan,
    validate_query_plan,
)


def test_count_plan_is_typed_normalized_and_json_friendly() -> None:
    plan = validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "element",
            "intent": "count",
            "scope": {"module_id": "2-01-A", "category": "Structural Framing"},
            "filters": [{"field": "type_name", "operator": "eq", "value": "H-400x200"}],
            "group_by": ["level_name"],
            "metrics": [],
        }
    )

    assert plan.metrics[0].name == "count"
    assert plan.metrics[0].field == "*"
    assert plan.as_dict() == {
        "project_id": "yeoju",
        "entity": "element",
        "intent": "count",
        "scope": {"module_id": "2-01-A", "category": "Structural Framing"},
        "filters": [{"field": "type_name", "operator": "eq", "value": "H-400x200"}],
        "group_by": ["level_name"],
        "metrics": [{"name": "count", "field": "*"}],
    }


def test_module_contract_allows_deterministic_distinct_registry_list() -> None:
    plan = validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "module",
            "intent": "aggregate",
            "scope": {},
            "filters": [],
            "group_by": [],
            "metrics": [
                {"name": "count_distinct", "field": "module_id", "alias": "module_count"},
                {"name": "collect_distinct", "field": "module_id", "alias": "module_ids"},
            ],
        }
    )

    assert [metric.name for metric in plan.metrics] == ["count_distinct", "collect_distinct"]


def test_pack_and_snapshot_selection_is_forbidden_at_any_depth() -> None:
    validation = try_validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "element",
            "intent": "count",
            "scope": {"pack_id": "old-pack"},
            "filters": [],
            "group_by": [],
            "metrics": [],
            "snapshot_id": "snapshot-old",
        }
    )

    assert not validation.valid
    assert validation.plan is None
    assert {issue.path for issue in validation.issues if issue.code == "forbidden_field"} == {
        "$.scope.pack_id",
        "$.snapshot_id",
    }


def test_entity_policy_rejects_fields_operators_and_metrics_outside_allowlist() -> None:
    validation = try_validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "sheet",
            "intent": "aggregate",
            "scope": {},
            "filters": [{"field": "quantity", "operator": "gt", "value": 10}],
            "group_by": [],
            "metrics": [{"name": "sum", "field": "quantity"}],
        }
    )

    codes = {issue.code for issue in validation.issues}
    assert not validation.valid
    assert "field_not_allowed" in codes
    assert "operator_not_allowed" in codes
    assert "metric_field_not_allowed" in codes or "metric_not_allowed" in codes


def test_compare_requires_group_and_rejects_unknown_contract_fields() -> None:
    validation = try_validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "boq_item",
            "intent": "compare",
            "scope": {"module_type": "1-02-A"},
            "filters": [],
            "group_by": [],
            "metrics": [{"name": "sum", "field": "quantity", "alias": "total_quantity"}],
            "cypher": "MATCH (n) RETURN n",
        }
    )

    assert not validation.valid
    assert any(issue.code == "unknown_field" and issue.path == "$.cypher" for issue in validation.issues)
    assert any(issue.code == "missing_group" for issue in validation.issues)


def test_invalid_plan_raises_structured_contract_error() -> None:
    try:
        validate_query_plan({"project_id": "yeoju", "entity": "anything", "intent": "count"})
    except QueryContractError as exc:
        payload = exc.as_dict()
    else:  # pragma: no cover - assertion guard
        raise AssertionError("QueryContractError was not raised")

    assert payload["error"] == "invalid_query_plan"
    assert any(issue["code"] == "unsupported_entity" for issue in payload["issues"])


def test_unimplemented_lookup_list_and_trace_intents_are_rejected() -> None:
    for intent in ("lookup", "list", "trace"):
        validation = try_validate_query_plan(
            {
                "project_id": "yeoju",
                "entity": "element",
                "intent": intent,
                "scope": {},
                "filters": [],
                "group_by": [],
                "metrics": [],
            }
        )

        assert not validation.valid
        assert any(issue.code == "unsupported_intent" for issue in validation.issues)


def test_boq_quantity_sum_requires_one_source_projection() -> None:
    validation = try_validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "boq_item",
            "intent": "aggregate",
            "scope": {"work_category": "벽체"},
            "filters": [],
            "group_by": [],
            "metrics": [{"name": "sum", "field": "quantity", "alias": "quantity"}],
        }
    )

    assert not validation.valid
    assert any(issue.code == "ambiguous_source_projection" for issue in validation.issues)


def test_effective_metric_aliases_cannot_overwrite_each_other_or_group_keys() -> None:
    duplicate_defaults = try_validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "boq_item",
            "intent": "aggregate",
            "scope": {"source_sheet": "건축_품목집계(전체)"},
            "filters": [],
            "group_by": [],
            "metrics": [
                {"name": "sum", "field": "quantity"},
                {"name": "avg", "field": "quantity"},
            ],
        }
    )
    group_collision = try_validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "element",
            "intent": "aggregate",
            "scope": {},
            "filters": [],
            "group_by": ["category"],
            "metrics": [{"name": "count", "field": "*", "alias": "category"}],
        }
    )

    assert not duplicate_defaults.valid
    assert not group_collision.valid
    assert any(issue.code == "duplicate_alias" for issue in duplicate_defaults.issues)
    assert any(issue.code == "duplicate_alias" for issue in group_collision.issues)


def test_metric_unit_companion_names_are_reserved_independent_of_metric_order() -> None:
    colliding_metrics = [
        {"name": "sum", "field": "quantity", "alias": "q"},
        {"name": "count", "field": "*", "alias": "q_unit"},
    ]

    for metrics in (colliding_metrics, list(reversed(colliding_metrics))):
        validation = try_validate_query_plan(
            {
                "project_id": "yeoju",
                "entity": "boq_item",
                "intent": "aggregate",
                "scope": {"source_sheet": "project"},
                "filters": [],
                "group_by": [],
                "metrics": metrics,
            }
        )

        assert not validation.valid
        assert any(
            issue.code == "duplicate_alias" and "q_unit" in issue.message
            for issue in validation.issues
        )

    group_collision = try_validate_query_plan(
        {
            "project_id": "yeoju",
            "entity": "boq_item",
            "intent": "aggregate",
            "scope": {"source_sheet": "project"},
            "filters": [],
            "group_by": ["normalized_unit"],
            "metrics": [{"name": "sum", "field": "quantity", "alias": "normalized"}],
        }
    )
    assert not group_collision.valid
    assert any(
        issue.code == "duplicate_alias" and "normalized_unit" in issue.message
        for issue in group_collision.issues
    )


def test_boq_quantity_aggregation_requires_an_exact_single_source_sheet_pin() -> None:
    def plan(*, scope: dict, filters: list[dict], metric: str = "sum") -> dict:
        return {
            "project_id": "yeoju",
            "entity": "boq_item",
            "intent": "aggregate",
            "scope": scope,
            "filters": filters,
            "group_by": [],
            "metrics": [{"name": metric, "field": "quantity", "alias": "q"}],
        }

    valid_selectors = (
        ({"source_sheet": "project"}, []),
        ({}, [{"field": "source_sheet", "operator": "eq", "value": "project"}]),
        ({}, [{"field": "source_sheet", "operator": "in", "value": ["project"]}]),
    )
    for metric in ("sum", "avg", "min", "max"):
        for scope, filters in valid_selectors:
            assert try_validate_query_plan(plan(scope=scope, filters=filters, metric=metric)).valid

    invalid_selectors = (
        ({"source_sheet": ["project"]}, []),
        ({"source_sheet": ["project", "module"]}, []),
        ({"source_sheet": True}, []),
        ({}, [{"field": "source_sheet", "operator": "exists", "value": True}]),
        ({}, [{"field": "source_sheet", "operator": "ne", "value": "module"}]),
        ({}, [{"field": "source_sheet", "operator": "eq", "value": ["project"]}]),
        ({}, [{"field": "source_sheet", "operator": "in", "value": ["project", "module"]}]),
    )
    for metric in ("sum", "avg", "min", "max"):
        for scope, filters in invalid_selectors:
            validation = try_validate_query_plan(plan(scope=scope, filters=filters, metric=metric))
            assert not validation.valid
            assert any(issue.code == "ambiguous_source_projection" for issue in validation.issues)
