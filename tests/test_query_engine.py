from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from modular_ontology.query_engine import MixedUnitError, QueryExecutionError, execute_query


def _pack(tmp_path: Path, name: str, nodes: list[dict]) -> tuple[Path, str]:
    path = tmp_path / f"{name}.zip"
    payload = "\n".join(json.dumps(node, ensure_ascii=False) for node in nodes) + "\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("graph/nodes.jsonl", payload)
        archive.writestr("graph/edges.jsonl", "")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _node(index: int, *, category: str = "문", module: str = "1-01-A", type_name: str = "D1") -> dict:
    return {
        "id": f"element:{index}",
        "node_type": "BIMElement",
        "properties": {
            "element_id": index,
            "category": category,
            "module_name": module,
            "type_name": type_name,
        },
        "evidence_refs": [{"source_file": "elements.jsonl", "source_line": index + 1}],
    }


def test_full_scan_group_count_and_repeatable_hash(tmp_path: Path) -> None:
    first = [_node(index, type_name="D1" if index < 5_100 else "D2") for index in range(5_101)]
    path1, hash1 = _pack(tmp_path, "inventory-01", first)
    path2, hash2 = _pack(tmp_path, "inventory-02", [_node(6_000, type_name="D2"), first[0]])
    snapshot = {
        "snapshot_id": "yeoju-test",
        "packs": [
            {"pack_id": "inventory-01", "path": str(path1), "sha256": hash1, "role": "model_inventory", "shard_index": 1},
            {"pack_id": "inventory-02", "path": str(path2), "sha256": hash2, "role": "model_inventory", "shard_index": 2},
        ],
    }
    plan = {
        "entity": "bim_element",
        "scope": {"categories": ["문"]},
        "group_by": ["type_name"],
        "metrics": [{"agg": "count_distinct", "field": "object_id", "as": "object_count"}],
        "order_by": [{"field": "object_count", "direction": "desc"}],
    }

    result = execute_query(plan, snapshot)
    repeated = execute_query(plan, snapshot)

    assert result.scanned_records == 5_103
    assert result.matched_records == 5_102
    assert result.duplicate_records_removed == 1
    assert list(result.rows) == [
        {"type_name": "D1", "object_count": 5_100},
        {"type_name": "D2", "object_count": 2},
    ]
    assert result.result_hash == repeated.result_hash
    assert result.evidence["result_hash"] == result.result_hash


def test_canonical_bim_object_excludes_sketch_and_legend(tmp_path: Path) -> None:
    nodes = [
        _node(1, category="구조 프레임"),
        {
            **_node(2, category="<스케치>"),
            "properties": {**_node(2, category="<스케치>")["properties"], "class": "Autodesk.Revit.DB.ModelLine"},
        },
        _node(3, category="범례 구성요소"),
    ]
    path, digest = _pack(tmp_path, "inventory", nodes)
    result = execute_query(
        {
            "entity": "bim_element",
            "entity_granularity": "canonical_bim_object",
            "metrics": [{"agg": "count_distinct", "field": "object_id", "as": "count"}],
        },
        {"snapshot_id": "s", "packs": [{"pack_id": "p", "path": str(path), "sha256": digest, "role": "model_inventory"}]},
    )
    assert result.values == {"count": 1}


def test_domain_property_id_may_differ_from_graph_node_id(tmp_path: Path) -> None:
    node = _node(22)
    node["properties"]["id"] = 22
    path, digest = _pack(tmp_path, "raw-domain-id", [node])

    result = execute_query(
        {
            "entity": "bim_element",
            "entity_granularity": "canonical_bim_object",
            "metrics": [
                {"agg": "count_distinct", "field": "object_id", "as": "count"}
            ],
        },
        {
            "snapshot_id": "s",
            "packs": [
                {
                    "pack_id": "p",
                    "path": str(path),
                    "sha256": digest,
                    "role": "model_inventory",
                }
            ],
        },
    )

    assert result.values == {"count": 1}


def test_sum_rejects_heterogeneous_units(tmp_path: Path) -> None:
    nodes = [
        {"id": "q1", "node_type": "QuantityEvidence", "properties": {"quantity": 10, "unit": "㎡"}},
        {"id": "q2", "node_type": "QuantityEvidence", "properties": {"quantity": 5, "unit": "m"}},
    ]
    path, digest = _pack(tmp_path, "boq", nodes)
    snapshot = {"snapshot_id": "s", "packs": [{"pack_id": "boq", "path": str(path), "sha256": digest, "role": "boq"}]}
    plan = {
        "entity": "boq_item",
        "metrics": [{"agg": "sum", "field": "quantity", "unit_field": "unit", "as": "quantity"}],
    }

    with pytest.raises(MixedUnitError, match="heterogeneous_unit_sum"):
        execute_query(plan, snapshot)


@pytest.mark.parametrize(
    ("bad_properties", "message"),
    [
        ({"quantity": "BAD", "normalized_unit": "square_meter"}, "non-numeric"),
        ({"quantity": 5}, "unit"),
    ],
)
def test_numeric_aggregate_rejects_invalid_values_and_missing_units(
    tmp_path: Path,
    bad_properties: dict,
    message: str,
) -> None:
    nodes = [
        {
            "id": "good",
            "node_type": "AggregatedBOQItem",
            "properties": {"quantity": 10, "normalized_unit": "square_meter"},
        },
        {"id": "bad", "node_type": "AggregatedBOQItem", "properties": bad_properties},
    ]
    path, digest = _pack(tmp_path, f"bad-{message}", nodes)
    snapshot = {
        "snapshot_id": "s",
        "packs": [{"pack_id": "boq", "path": str(path), "sha256": digest, "role": "boq"}],
    }

    with pytest.raises(QueryExecutionError, match=message):
        execute_query(
            {
                "entity": "boq_item",
                "metrics": [
                    {
                        "agg": "sum",
                        "field": "quantity",
                        "unit_field": "normalized_unit",
                        "as": "q",
                    }
                ],
            },
            snapshot,
        )


@pytest.mark.parametrize(
    "metrics",
    [
        [
            {"agg": "sum", "field": "quantity", "unit_field": "normalized_unit", "as": "q"},
            {"agg": "count", "field": "*", "as": "q_unit"},
        ],
        [
            {"agg": "count", "field": "*", "as": "q_unit"},
            {"agg": "sum", "field": "quantity", "unit_field": "normalized_unit", "as": "q"},
        ],
    ],
)
def test_executor_rejects_metric_unit_companion_overwrite(
    tmp_path: Path,
    metrics: list[dict],
) -> None:
    nodes = [
        {
            "id": "area",
            "node_type": "AggregatedBOQItem",
            "properties": {"quantity": 10, "normalized_unit": "square_meter"},
        }
    ]
    path, digest = _pack(tmp_path, "alias-collision", nodes)
    snapshot = {
        "snapshot_id": "s",
        "packs": [{"pack_id": "boq", "path": str(path), "sha256": digest, "role": "boq"}],
    }

    with pytest.raises(QueryExecutionError, match="output name collision.*q_unit"):
        execute_query({"entity": "boq_item", "metrics": metrics}, snapshot)


def test_executor_rejects_group_key_and_metric_companion_collision(tmp_path: Path) -> None:
    nodes = [
        {
            "id": "area",
            "node_type": "AggregatedBOQItem",
            "properties": {"quantity": 10, "normalized_unit": "square_meter"},
        }
    ]
    path, digest = _pack(tmp_path, "group-alias-collision", nodes)
    snapshot = {
        "snapshot_id": "s",
        "packs": [{"pack_id": "boq", "path": str(path), "sha256": digest, "role": "boq"}],
    }

    with pytest.raises(QueryExecutionError, match="output name collision.*normalized_unit"):
        execute_query(
            {
                "entity": "boq_item",
                "filters": [{"field": "item_name", "operator": "eq", "value": "no-match"}],
                "group_by": ["normalized_unit"],
                "metrics": [
                    {
                        "agg": "sum",
                        "field": "quantity",
                        "unit_field": "normalized_unit",
                        "as": "normalized",
                    }
                ],
            },
            snapshot,
        )


def test_valid_jsonl_non_object_record_fails_closed(tmp_path: Path) -> None:
    path, digest = _pack(tmp_path, "non-object", [["valid", "json", "but", "not", "an", "object"]])
    snapshot = {
        "snapshot_id": "s",
        "packs": [{"pack_id": "boq", "path": str(path), "sha256": digest, "role": "boq"}],
    }

    with pytest.raises(QueryExecutionError, match="JSONL record must be an object"):
        execute_query({"entity": "boq_item", "metrics": [{"agg": "count", "as": "count"}]}, snapshot)


def test_module_entity_uses_complete_user_workset_registry(tmp_path: Path) -> None:
    nodes = [
        {"id": "w1", "node_type": "BIMWorkset", "properties": {"kind": "UserWorkset", "name": "1-01-M"}},
        {"id": "w2", "node_type": "BIMWorkset", "properties": {"kind": "UserWorkset", "name": "2-05-ST"}},
        {"id": "system", "node_type": "BIMWorkset", "properties": {"kind": "StandardWorkset", "name": "Level Types"}},
        {"id": "zone", "node_type": "BIMModuleZone", "properties": {"name": "1-01-M", "element_count": 0}},
    ]
    path, digest = _pack(tmp_path, "references", nodes)
    snapshot = {
        "snapshot_id": "s",
        "packs": [{"pack_id": "references", "path": str(path), "sha256": digest, "role": "project_reference"}],
    }

    result = execute_query(
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
        },
        {**snapshot, "project_id": "yeoju"},
    )

    assert result.values == {"module_count": 2, "module_ids": ["1-01-M", "2-05-ST"]}
