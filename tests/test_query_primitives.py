from __future__ import annotations

from modular_ontology.query_primitives import (
    MISSING,
    loose_equal,
    match_one,
    matches_where,
    natural_key,
    node_field,
    project_node,
    sort_rows,
)


def test_nested_field_matching_and_projection() -> None:
    node = {
        "id": "node:1",
        "properties": {
            "module": {"code": "A-10"},
            "weight": "1,250.5 kg",
            "formula": "secret",
        },
    }

    assert node_field(node, "module.code") == "A-10"
    assert node_field(node, "missing") is MISSING
    assert matches_where(node, {"module.code": {"startswith": "a-"}, "weight": {"gte": 1250}})
    assert project_node(node, ["id", "module.code", "formula"]) == {"id": "node:1", "code": "A-10"}


def test_match_operators_and_loose_numeric_equality() -> None:
    assert loose_equal("1,000 mm", 1000)
    assert match_one("Door Type A", "contains", "type", exists=True)
    assert match_one("A-10", "in", ["A-2", "A-10"], exists=True)
    assert match_one(15, "lt", 20, exists=True)
    assert match_one(MISSING, "exists", False, exists=False)
    assert not match_one("abc", "regex", "[", exists=True)


def test_natural_multi_column_sorting() -> None:
    rows = [
        {"group": "B", "code": "A-10"},
        {"group": "A", "code": "A-10"},
        {"group": "A", "code": "A-2"},
    ]

    ordered = sort_rows(
        rows,
        [
            {"field": "group", "direction": "asc"},
            {"field": "code", "direction": "asc", "natural": True},
        ],
    )

    assert [(row["group"], row["code"]) for row in ordered] == [
        ("A", "A-2"),
        ("A", "A-10"),
        ("B", "A-10"),
    ]
    assert natural_key("A-2") < natural_key("A-10")


def test_mcp_server_uses_shared_query_primitives() -> None:
    from modular_ontology import mcp_server

    node = {"id": "node:1", "properties": {"code": "A-10", "weight": "25 kg"}}

    assert mcp_server._MISSING is MISSING
    assert mcp_server._matches_where(node, {"code": {"eq": "A-10"}, "weight": {"gt": 20}})
    assert mcp_server._project_node(node, ["id", "code"]) == {"id": "node:1", "code": "A-10"}
