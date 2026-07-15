from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from modular_ontology.query_contract import (
    PLAN_FIELDS,
    PlanBundleContractError,
    try_validate_plan_bundle,
    validate_plan_bundle,
)
from modular_ontology.query_engine import BundleReductionError, execute_query_bundle


def _pack(tmp_path: Path, name: str, nodes: list[dict]) -> tuple[Path, str]:
    path = tmp_path / f"{name}.zip"
    payload = "\n".join(json.dumps(node, ensure_ascii=False) for node in nodes) + "\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("graph/nodes.jsonl", payload)
        archive.writestr("graph/edges.jsonl", "")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(tmp_path: Path, packs: list[tuple[str, str, list[dict]]]) -> dict:
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
    return {"project_id": "yeoju", "snapshot_id": "YEOJU-CANONICAL-TEST", "packs": entries}


def _plan(
    entity: str,
    *,
    intent: str = "aggregate",
    scope: dict | None = None,
    filters: list[dict] | None = None,
    group_by: list[str] | None = None,
    metrics: list[dict] | None = None,
) -> dict:
    return {
        "project_id": "yeoju",
        "entity": entity,
        "intent": intent,
        "scope": scope or {},
        "filters": filters or [],
        "group_by": group_by or [],
        "metrics": metrics or [],
    }


def _element(index: int, *, module: str, category: str, type_name: str = "T") -> dict:
    return {
        "id": f"element:{index}",
        "node_type": "BIMElement",
        "properties": {
            "element_id": index,
            "module_name": module,
            "category": category,
            "type_name": type_name,
        },
    }


def test_bundle_requires_exact_seven_field_plans_and_rejects_unsafe_reducers() -> None:
    exact_plan = _plan("element", intent="count")
    assert set(exact_plan) == PLAN_FIELDS
    bundle = validate_plan_bundle(
        {"project_id": "yeoju", "plans": {"objects": exact_plan}, "reducers": []}
    )
    assert set(bundle.plans[0].plan.as_dict()) == PLAN_FIELDS

    missing_field = dict(exact_plan)
    missing_field.pop("filters")
    invalid = try_validate_plan_bundle(
        {
            "project_id": "yeoju",
            "plans": {"objects": missing_field},
            "reducers": [
                {"id": "unsafe", "op": "python_eval", "code": "open('secret')"},
            ],
        }
    )

    assert not invalid.valid
    assert any(issue.code == "missing_field" and issue.path.endswith(".filters") for issue in invalid.issues)
    assert any(issue.code == "unsupported_reducer" for issue in invalid.issues)

    literal_only = try_validate_plan_bundle(
        {
            "project_id": "yeoju",
            "plans": {"objects": exact_plan},
            "reducers": [
                {
                    "id": "manufactured",
                    "op": "sum_values",
                    "inputs": [
                        {"value": 1, "unit": "count"},
                        {"value": 2, "unit": "count"},
                    ],
                }
            ],
        }
    )
    assert not literal_only.valid
    assert any(issue.code == "source_required" for issue in literal_only.issues)


def test_bundle_rejects_forward_references_and_pack_snapshot_selection() -> None:
    invalid = try_validate_plan_bundle(
        {
            "project_id": "yeoju",
            "plans": {
                "objects": {
                    **_plan("element", intent="count"),
                    "scope": {"pack_id": "old-pack"},
                }
            },
            "reducers": [
                {"id": "early", "op": "count", "source": "reducers.later"},
                {"id": "later", "op": "only", "source": "plans.objects.rows"},
            ],
            "snapshot_id": "old-snapshot",
        }
    )

    assert not invalid.valid
    assert {issue.path for issue in invalid.issues if issue.code == "forbidden_field"} == {
        "$.plans.objects.scope.pack_id",
        "$.snapshot_id",
    }
    assert any(issue.code == "forward_or_unknown_reference" for issue in invalid.issues)


def test_bundle_contract_rejects_self_comparison_and_duplicate_operands() -> None:
    plan = _plan("element", intent="count")
    duplicate_ref = {"ref": "plans.objects.values.count"}
    reducers = (
        {"id": "self_equal", "op": "equals", "left": duplicate_ref, "right": duplicate_ref},
        {"id": "self_subtract", "op": "subtract", "left": duplicate_ref, "right": duplicate_ref},
        {
            "id": "double_count",
            "op": "sum_values",
            "inputs": [duplicate_ref, duplicate_ref],
        },
    )

    for reducer in reducers:
        validation = try_validate_plan_bundle(
            {"project_id": "yeoju", "plans": {"objects": plan}, "reducers": [reducer]}
        )
        assert not validation.valid
        assert any(issue.code == "duplicate_operand" for issue in validation.issues)


def test_group_summary_reducers_cover_total_top_and_type_count_patterns(tmp_path: Path) -> None:
    nodes = [
        *[_element(index, module="2-01-A", category="Framing") for index in range(3)],
        *[_element(index, module="2-01-A", category="Door") for index in range(3, 5)],
        _element(6, module="1-01-A", category="Framing"),
    ]
    snapshot = _snapshot(tmp_path, [("inventory", "model_inventory", nodes)])
    grouped = _plan(
        "element",
        scope={"module_id": "2-01-A"},
        group_by=["category"],
        metrics=[{"name": "count_distinct", "field": "source_element_id", "alias": "object_count"}],
    )
    bundle = {
        "project_id": "yeoju",
        "plans": {"category_counts": grouped},
        "reducers": [
            {
                "id": "total_physical_objects",
                "op": "sum_field",
                "source": "plans.category_counts.rows",
                "field": "object_count",
            },
            {
                "id": "top_category",
                "op": "argmax",
                "source": "plans.category_counts.rows",
                "field": "object_count",
            },
            {
                "id": "top_two_categories",
                "op": "top",
                "source": "plans.category_counts.rows",
                "field": "object_count",
                "limit": 2,
            },
            {"id": "category_count", "op": "count", "source": "plans.category_counts.rows"},
        ],
    }

    result = execute_query_bundle(bundle, snapshot)
    repeated = execute_query_bundle(bundle, snapshot)

    assert result.reducer_values == {
        "total_physical_objects": 5,
        "top_category": {"category": "Framing", "object_count": 3},
        "top_two_categories": [
            {"category": "Framing", "object_count": 3},
            {"category": "Door", "object_count": 2},
        ],
        "category_count": 2,
    }
    assert result.reducer_units["total_physical_objects"] == "count"
    assert result.query_hash == result.bundle_hash
    assert result.evidence["query_hash"] == result.query_hash
    assert result.evidence["plan_bindings"]["category_counts"]["source_packs"][0]["sha256"]
    assert result.result_hash == repeated.result_hash


def test_join_reducers_cover_zero_module_and_ranked_module_patterns(tmp_path: Path) -> None:
    worksets = [
        {
            "id": f"workset:{name}",
            "node_type": "BIMWorkset",
            "properties": {"kind": "UserWorkset", "name": name},
        }
        for name in ("1-01-M", "1-02-A", "2-01-A")
    ]
    elements = [
        _element(1, module="1-02-A", category="Door"),
        _element(2, module="1-02-A", category="Door"),
        _element(3, module="2-01-A", category="Door"),
        *[_element(10 + index, module="1-01-M", category="Wall") for index in range(2)],
        *[_element(20 + index, module="1-02-A", category="Wall") for index in range(5)],
        *[_element(30 + index, module="2-01-A", category="Wall") for index in range(3)],
    ]
    snapshot = _snapshot(
        tmp_path,
        [
            ("reference", "project_reference", worksets),
            ("inventory", "model_inventory", elements),
        ],
    )
    plans = {
        "modules": _plan(
            "module",
            metrics=[
                {"name": "count_distinct", "field": "module_id", "alias": "module_count"},
                {"name": "collect_distinct", "field": "module_id", "alias": "module_ids"},
            ],
        ),
        "doors": _plan(
            "element",
            scope={"category": "Door"},
            group_by=["module_id"],
            metrics=[{"name": "count_distinct", "field": "source_element_id", "alias": "door_count"}],
        ),
        "walls": _plan(
            "element",
            scope={"category": "Wall"},
            group_by=["module_id"],
            metrics=[{"name": "count_distinct", "field": "source_element_id", "alias": "wall_count"}],
        ),
    }
    result = execute_query_bundle(
        {
            "project_id": "yeoju",
            "plans": plans,
            "reducers": [
                {
                    "id": "zero_door_modules",
                    "op": "left_join_zero",
                    "left": "plans.modules.values.module_ids",
                    "right": "plans.doors.rows",
                    "right_key": "module_id",
                    "value_field": "door_count",
                },
                {"id": "zero_door_count", "op": "count", "source": "reducers.zero_door_modules"},
                {
                    "id": "top_wall_modules",
                    "op": "inner_join_rank",
                    "left": "plans.modules.values.module_ids",
                    "right": "plans.walls.rows",
                    "right_key": "module_id",
                    "value_field": "wall_count",
                    "limit": 2,
                },
            ],
        },
        snapshot,
    )

    assert result.reducer_values["zero_door_modules"] == ["1-01-M"]
    assert result.reducer_values["zero_door_count"] == 1
    assert result.reducer_values["top_wall_modules"] == [
        {"module_id": "1-02-A", "wall_count": 5},
        {"module_id": "2-01-A", "wall_count": 3},
    ]


def test_argmax_rejects_a_tied_maximum(tmp_path: Path) -> None:
    nodes = [
        *[_element(index, module="1-01-A", category="A") for index in range(2)],
        *[_element(10 + index, module="1-01-A", category="B") for index in range(2)],
    ]
    snapshot = _snapshot(tmp_path, [("inventory", "model_inventory", nodes)])
    bundle = {
        "project_id": "yeoju",
        "plans": {
            "counts": _plan(
                "element",
                group_by=["category"],
                metrics=[
                    {
                        "name": "count_distinct",
                        "field": "source_element_id",
                        "alias": "object_count",
                    }
                ],
            )
        },
        "reducers": [
            {
                "id": "winner",
                "op": "argmax",
                "source": "plans.counts.rows",
                "field": "object_count",
            }
        ],
    }

    with pytest.raises(BundleReductionError, match="tie at rank boundary 1"):
        execute_query_bundle(bundle, snapshot)


def test_top_and_inner_join_rank_reject_a_tie_crossing_the_limit(tmp_path: Path) -> None:
    module_counts = {"1-01-A": 3, "1-02-A": 2, "1-03-A": 2}
    worksets = [
        {
            "id": f"workset:{module}",
            "node_type": "BIMWorkset",
            "properties": {"kind": "UserWorkset", "name": module},
        }
        for module in module_counts
    ]
    elements = [
        _element(100 * module_index + item, module=module, category="Wall")
        for module_index, (module, count) in enumerate(module_counts.items(), start=1)
        for item in range(count)
    ]
    snapshot = _snapshot(
        tmp_path,
        [("reference", "project_reference", worksets), ("inventory", "model_inventory", elements)],
    )
    plans = {
        "modules": _plan(
            "module",
            metrics=[{"name": "collect_distinct", "field": "module_id", "alias": "module_ids"}],
        ),
        "walls": _plan(
            "element",
            scope={"category": "Wall"},
            group_by=["module_id"],
            metrics=[
                {
                    "name": "count_distinct",
                    "field": "source_element_id",
                    "alias": "wall_count",
                }
            ],
        ),
    }
    boundary_reducers = (
        {
            "id": "top_two",
            "op": "top",
            "source": "plans.walls.rows",
            "field": "wall_count",
            "limit": 2,
        },
        {
            "id": "top_two_joined",
            "op": "inner_join_rank",
            "left": "plans.modules.values.module_ids",
            "right": "plans.walls.rows",
            "right_key": "module_id",
            "value_field": "wall_count",
            "limit": 2,
        },
    )

    for reducer in boundary_reducers:
        with pytest.raises(BundleReductionError, match="tie at rank boundary 2"):
            execute_query_bundle(
                {"project_id": "yeoju", "plans": plans, "reducers": [reducer]},
                snapshot,
            )

    all_rows = execute_query_bundle(
        {
            "project_id": "yeoju",
            "plans": plans,
            "reducers": [
                {
                    "id": "all_three",
                    "op": "top",
                    "source": "plans.walls.rows",
                    "field": "wall_count",
                    "limit": 3,
                }
            ],
        },
        snapshot,
    )
    assert len(all_rows.reducer_values["all_three"]) == 3


def test_join_keys_normalize_strings_and_finite_numbers(tmp_path: Path) -> None:
    worksets = [
        {
            "id": "workset:1-01-A",
            "node_type": "BIMWorkset",
            "properties": {"kind": "UserWorkset", "name": "1-01-A"},
        }
    ]
    elements = [_element(1, module=" 1-01-a ", category="Door")]
    requirements = [
        {
            "id": "requirement:left",
            "node_type": "Requirement",
            "properties": {"category": "left", "page": 1},
        },
        {
            "id": "requirement:right",
            "node_type": "Requirement",
            "properties": {"category": "right", "page": "1.0"},
        },
    ]
    snapshot = _snapshot(
        tmp_path,
        [
            ("reference", "project_reference", worksets),
            ("inventory", "model_inventory", elements),
            ("requirements", "spec", requirements),
        ],
    )
    plans = {
        "modules": _plan(
            "module",
            metrics=[{"name": "collect_distinct", "field": "module_id", "alias": "module_ids"}],
        ),
        "doors": _plan(
            "element",
            scope={"category": "Door"},
            group_by=["module_id"],
            metrics=[
                {
                    "name": "count_distinct",
                    "field": "source_element_id",
                    "alias": "door_count",
                }
            ],
        ),
        "left_pages": _plan(
            "specification",
            filters=[{"field": "category", "operator": "eq", "value": "left"}],
            metrics=[{"name": "collect_distinct", "field": "page", "alias": "pages"}],
        ),
        "right_pages": _plan(
            "specification",
            filters=[{"field": "category", "operator": "eq", "value": "right"}],
            group_by=["page"],
            metrics=[{"name": "count_distinct", "field": "id", "alias": "hit_count"}],
        ),
    }
    result = execute_query_bundle(
        {
            "project_id": "yeoju",
            "plans": plans,
            "reducers": [
                {
                    "id": "zero_doors",
                    "op": "left_join_zero",
                    "left": "plans.modules.values.module_ids",
                    "right": "plans.doors.rows",
                    "right_key": "module_id",
                    "value_field": "door_count",
                },
                {
                    "id": "numeric_join",
                    "op": "inner_join_rank",
                    "left": "plans.left_pages.values.pages",
                    "right": "plans.right_pages.rows",
                    "right_key": "page",
                    "value_field": "hit_count",
                    "limit": 1,
                },
            ],
        },
        snapshot,
    )

    assert result.reducer_values["zero_doors"] == []
    assert result.reducer_values["numeric_join"] == [{"page": "1.0", "hit_count": 1}]


def test_join_key_normalization_rejects_duplicates_and_complex_values(tmp_path: Path) -> None:
    requirements = [
        {
            "id": "requirement:first",
            "node_type": "Requirement",
            "properties": {"category": "duplicate", "page": " A-1 "},
        },
        {
            "id": "requirement:second",
            "node_type": "Requirement",
            "properties": {"category": "duplicate", "page": "a-1"},
        },
        {
            "id": "requirement:right",
            "node_type": "Requirement",
            "properties": {"category": "right", "page": "A-1"},
        },
        {
            "id": "requirement:complex",
            "node_type": "Requirement",
            "properties": {"category": "complex", "page": ["A-1"]},
        },
    ]
    snapshot = _snapshot(tmp_path, [("requirements", "spec", requirements)])

    def page_plan(category: str) -> dict:
        return _plan(
            "specification",
            filters=[{"field": "category", "operator": "eq", "value": category}],
            metrics=[{"name": "collect_distinct", "field": "page", "alias": "pages"}],
        )

    right = _plan(
        "specification",
        filters=[{"field": "category", "operator": "eq", "value": "right"}],
        group_by=["page"],
        metrics=[{"name": "count_distinct", "field": "id", "alias": "hit_count"}],
    )
    reducer = {
        "id": "zero_pages",
        "op": "left_join_zero",
        "left": "plans.left.values.pages",
        "right": "plans.right.rows",
        "right_key": "page",
        "value_field": "hit_count",
    }

    with pytest.raises(BundleReductionError, match="duplicate join key"):
        execute_query_bundle(
            {
                "project_id": "yeoju",
                "plans": {"left": page_plan("duplicate"), "right": right},
                "reducers": [reducer],
            },
            snapshot,
        )

    with pytest.raises(BundleReductionError, match="join key must be a scalar"):
        execute_query_bundle(
            {
                "project_id": "yeoju",
                "plans": {"left": page_plan("complex"), "right": right},
                "reducers": [reducer],
            },
            snapshot,
        )


def test_join_key_bool_is_not_normalized_as_number(tmp_path: Path) -> None:
    requirements = [
        {
            "id": "requirement:left",
            "node_type": "Requirement",
            "properties": {"category": "left", "page": True},
        },
        {
            "id": "requirement:right",
            "node_type": "Requirement",
            "properties": {"category": "right", "page": 1},
        },
    ]
    snapshot = _snapshot(tmp_path, [("requirements", "spec", requirements)])
    result = execute_query_bundle(
        {
            "project_id": "yeoju",
            "plans": {
                "left": _plan(
                    "specification",
                    filters=[{"field": "category", "operator": "eq", "value": "left"}],
                    metrics=[{"name": "collect_distinct", "field": "page", "alias": "pages"}],
                ),
                "right": _plan(
                    "specification",
                    filters=[{"field": "category", "operator": "eq", "value": "right"}],
                    group_by=["page"],
                    metrics=[{"name": "count_distinct", "field": "id", "alias": "hit_count"}],
                ),
            },
            "reducers": [
                {
                    "id": "zero_pages",
                    "op": "left_join_zero",
                    "left": "plans.left.values.pages",
                    "right": "plans.right.rows",
                    "right_key": "page",
                    "value_field": "hit_count",
                }
            ],
        },
        snapshot,
    )
    assert result.reducer_values["zero_pages"] == [True]


def test_boq_node_type_and_arithmetic_reducers_keep_module_and_project_rows_distinct(
    tmp_path: Path,
) -> None:
    boq_nodes = [
        {
            "id": "estimate:dw1",
            "node_type": "EstimateItem",
            "properties": {
                "module_type": "1-02-A",
                "work_category": "Wall",
                "item_name": "DW1",
                "quantity": 3.54,
                "normalized_unit": "square_meter",
                "source_sheet": "module",
            },
        },
        {
            "id": "aggregate:dw1",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "work_category": "Wall",
                "item_name": "DW1",
                "quantity": 4.19,
                "normalized_unit": "square_meter",
                "source_sheet": "project",
            },
        },
    ]
    snapshot = _snapshot(tmp_path, [("boq", "boq", boq_nodes)])
    metric = [{"name": "sum", "field": "quantity", "alias": "quantity"}]
    plans = {
        "module": _plan(
            "boq_item",
            scope={"source_sheet": "module", "module_type": "1-02-A"},
            filters=[
                {"field": "node_type", "operator": "eq", "value": "EstimateItem"},
                {"field": "item_name", "operator": "eq", "value": "DW1"},
            ],
            metrics=metric,
        ),
        "project": _plan(
            "boq_item",
            scope={"source_sheet": "project"},
            filters=[
                {"field": "node_type", "operator": "eq", "value": "AggregatedBOQItem"},
                {"field": "item_name", "operator": "eq", "value": "DW1"},
            ],
            metrics=metric,
        ),
    }
    result = execute_query_bundle(
        {
            "project_id": "yeoju",
            "plans": plans,
            "reducers": [
                {
                    "id": "claim_matches",
                    "op": "equals",
                    "left": {"ref": "plans.module.values.quantity"},
                    "right": {"value": 12.57, "unit": "square_meter"},
                },
                {
                    "id": "project_minus_module",
                    "op": "subtract",
                    "left": {"ref": "plans.project.values.quantity"},
                    "right": {"ref": "plans.module.values.quantity"},
                },
                {
                    "id": "combined_quantity",
                    "op": "sum_values",
                    "inputs": [
                        {"ref": "plans.module.values.quantity"},
                        {"ref": "plans.project.values.quantity"},
                    ],
                },
            ],
        },
        snapshot,
    )

    assert result.plan_results["module"].values["quantity"] == 3.54
    assert result.plan_results["project"].values["quantity"] == 4.19
    assert result.reducer_values == {
        "claim_matches": False,
        "project_minus_module": 0.65,
        "combined_quantity": 7.73,
    }
    assert result.reducer_units == {
        "claim_matches": None,
        "project_minus_module": "square_meter",
        "combined_quantity": "square_meter",
    }


def test_arithmetic_reducer_rejects_mixed_source_units(tmp_path: Path) -> None:
    nodes = [
        {
            "id": "area",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "item_name": "Area",
                "quantity": 10,
                "normalized_unit": "square_meter",
                "source_sheet": "project",
            },
        },
        {
            "id": "length",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "item_name": "Length",
                "quantity": 5,
                "normalized_unit": "meter",
                "source_sheet": "project",
            },
        },
    ]
    snapshot = _snapshot(tmp_path, [("boq", "boq", nodes)])
    metric = [{"name": "sum", "field": "quantity", "alias": "quantity"}]
    plans = {
        name.casefold(): _plan(
            "boq_item",
            scope={"source_sheet": "project"},
            filters=[{"field": "item_name", "operator": "eq", "value": name}],
            metrics=metric,
        )
        for name in ("Area", "Length")
    }
    bundle = {
        "project_id": "yeoju",
        "plans": plans,
        "reducers": [
            {
                "id": "invalid_sum",
                "op": "sum_values",
                "inputs": [
                    {"ref": "plans.area.values.quantity"},
                    {"ref": "plans.length.values.quantity"},
                ],
            }
        ],
    }

    with pytest.raises(BundleReductionError, match="incompatible units"):
        execute_query_bundle(bundle, snapshot)


def test_area_length_sum_cannot_hide_units_with_a_metric_alias(tmp_path: Path) -> None:
    nodes = [
        {
            "id": "area",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "item_name": "Area",
                "quantity": 10,
                "normalized_unit": "square_meter",
                "source_sheet": "project",
            },
        },
        {
            "id": "length",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "item_name": "Length",
                "quantity": 5,
                "normalized_unit": "meter",
                "source_sheet": "project",
            },
        },
    ]
    snapshot = _snapshot(tmp_path, [("boq", "boq", nodes)])
    poisoned_metrics = [
        {"name": "sum", "field": "quantity", "alias": "q"},
        {"name": "count", "field": "*", "alias": "q_unit"},
    ]
    plans = {
        name.casefold(): _plan(
            "boq_item",
            scope={"source_sheet": "project"},
            filters=[{"field": "item_name", "operator": "eq", "value": name}],
            metrics=poisoned_metrics,
        )
        for name in ("Area", "Length")
    }

    with pytest.raises(PlanBundleContractError, match="q_unit"):
        execute_query_bundle(
            {
                "project_id": "yeoju",
                "plans": plans,
                "reducers": [
                    {
                        "id": "invalid_sum",
                        "op": "sum_values",
                        "inputs": [
                            {"ref": "plans.area.values.q"},
                            {"ref": "plans.length.values.q"},
                        ],
                    }
                ],
            },
            snapshot,
        )


def test_cross_domain_count_comparison_normalizes_boq_each_to_count(tmp_path: Path) -> None:
    boq = [
        {
            "id": "boq:aw5",
            "node_type": "AggregatedBOQItem",
            "properties": {
                "item_name": "AW5",
                "quantity": 1,
                "normalized_unit": "location_count",
                "source_sheet": "project",
            },
        }
    ]
    model = [
        _element(1, module="1-01-A", category="Window", type_name="AW5"),
        _element(2, module="1-02-A", category="Window", type_name="AW5"),
    ]
    for node in model:
        node["properties"]["family_name"] = "Sliding Window"
    snapshot = _snapshot(
        tmp_path,
        [("boq", "boq", boq), ("inventory", "model_inventory", model)],
    )
    result = execute_query_bundle(
        {
            "project_id": "yeoju",
            "plans": {
                "boq": _plan(
                    "boq_item",
                    scope={"source_sheet": "project"},
                    filters=[
                        {"field": "node_type", "operator": "eq", "value": "AggregatedBOQItem"},
                        {"field": "item_name", "operator": "eq", "value": "AW5"},
                    ],
                    metrics=[{"name": "sum", "field": "quantity", "alias": "quantity"}],
                ),
                "model": _plan(
                    "element",
                    scope={"category": "Window"},
                    filters=[{"field": "type_name", "operator": "eq", "value": "AW5"}],
                    metrics=[
                        {
                            "name": "count_distinct",
                            "field": "source_element_id",
                            "alias": "instance_count",
                        },
                        {
                            "name": "collect_distinct",
                            "field": "family_name",
                            "alias": "families",
                        },
                    ],
                ),
            },
            "reducers": [
                {
                    "id": "model_minus_boq",
                    "op": "subtract",
                    "left": {"ref": "plans.model.values.instance_count"},
                    "right": {"ref": "plans.boq.values.quantity"},
                },
                {
                    "id": "counts_match",
                    "op": "equals",
                    "left": {"ref": "plans.model.values.instance_count"},
                    "right": {"ref": "plans.boq.values.quantity"},
                },
            ],
        },
        snapshot,
    )

    assert result.plan_results["model"].values == {
        "instance_count": 2,
        "families": ["Sliding Window"],
    }
    assert result.reducer_values == {"model_minus_boq": 1, "counts_match": False}
    assert result.reducer_units["model_minus_boq"] == "count"


def test_lookup_pair_can_reduce_exact_singletons_and_compare_claim(tmp_path: Path) -> None:
    sheets = [
        {
            "id": "sheet:401",
            "node_type": "DrawingSheet",
            "properties": {"sheet_number": "A-401", "sheet_name": "Vertical Circulation"},
        },
        {
            "id": "sheet:501",
            "node_type": "DrawingSheet",
            "properties": {"sheet_number": "A-501", "sheet_name": "Exterior Detail"},
        },
    ]
    snapshot = _snapshot(tmp_path, [("drawings", "drawing_reference", sheets)])
    result = execute_query_bundle(
        {
            "project_id": "yeoju",
            "plans": {
                "claimed_number": _plan(
                    "sheet",
                    scope={"sheet_number": "A-401"},
                    metrics=[
                        {
                            "name": "collect_distinct",
                            "field": "sheet_name",
                            "alias": "sheet_names",
                        }
                    ],
                ),
                "actual_name": _plan(
                    "sheet",
                    filters=[{"field": "sheet_name", "operator": "eq", "value": "Exterior Detail"}],
                    metrics=[
                        {
                            "name": "collect_distinct",
                            "field": "sheet_number",
                            "alias": "sheet_numbers",
                        }
                    ],
                ),
            },
            "reducers": [
                {
                    "id": "a401_name",
                    "op": "only",
                    "source": "plans.claimed_number.values.sheet_names",
                },
                {
                    "id": "actual_sheet_number",
                    "op": "only",
                    "source": "plans.actual_name.values.sheet_numbers",
                },
                {
                    "id": "claim_matches",
                    "op": "equals",
                    "left": {"ref": "reducers.actual_sheet_number"},
                    "right": {"value": "A-401"},
                },
            ],
        },
        snapshot,
    )

    assert result.reducer_values == {
        "a401_name": "Vertical Circulation",
        "actual_sheet_number": "A-501",
        "claim_matches": False,
    }


def test_only_reducer_is_fail_closed_for_ambiguous_lookup(tmp_path: Path) -> None:
    sheets = [
        {
            "id": f"sheet:{index}",
            "node_type": "DrawingSheet",
            "properties": {"sheet_number": f"A-{index}", "sheet_name": "Exterior Detail"},
        }
        for index in (401, 501)
    ]
    snapshot = _snapshot(tmp_path, [("drawings", "drawing_reference", sheets)])
    bundle = {
        "project_id": "yeoju",
        "plans": {
            "actual": _plan(
                "sheet",
                filters=[{"field": "sheet_name", "operator": "eq", "value": "Exterior Detail"}],
                metrics=[
                    {
                        "name": "collect_distinct",
                        "field": "sheet_number",
                        "alias": "sheet_numbers",
                    }
                ],
            )
        },
        "reducers": [
            {"id": "actual_sheet_number", "op": "only", "source": "plans.actual.values.sheet_numbers"}
        ],
    }

    with pytest.raises(BundleReductionError, match="expected exactly one value"):
        execute_query_bundle(bundle, snapshot)
