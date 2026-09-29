"""Query contracts run without private/source sample packs."""

import json
from decimal import Decimal

import pytest

from modular_ontology import mcp_server
from modular_ontology.query import compile_where, equal_values, numeric_value, sort_records


MISSING = object()


def getter(row, field):
    return row.get(field, MISSING)


@pytest.mark.parametrize("value", ["M12", "1-01-A", "12 mm", "1,2", "NaN", "Infinity", float("inf"), float("nan"), True, None, [12]])
def test_numeric_comparison_does_not_parse_identifiers_or_nonfinite_values(value):
    assert numeric_value(value) is None


@pytest.mark.parametrize("value, expected", [("1,234.5", "1234.5"), ("-.5", "-0.5"), ("2e3", "2000"), (" +10 ", "10")])
def test_numeric_comparison_accepts_full_numeric_values(value, expected):
    assert numeric_value(value) == Decimal(expected)


@pytest.mark.parametrize("left, right", [("1-01-A", "1-02-A"), ("M12", 12), (True, 1), (None, "None"), ("9007199254740992", "9007199254740993")])
def test_equality_preserves_identifiers_types_and_large_integer_precision(left, right):
    assert not equal_values(left, right)


def test_legacy_numeric_equality_is_retained():
    assert equal_values("1,200.0", 1200)
    assert equal_values("12 mm", "12 mm")
    assert mcp_server._to_number("12 mm") == 12  # Source display parsing remains available.


@pytest.mark.parametrize("where", [{"quantity": {"gte": 10, "lt": 20}}, {"quantity__gte": 10, "quantity__lt": 20}])
def test_graph_and_row_filters_share_operator_syntax(where):
    assert mcp_server._matches_where({"properties": {"quantity": "12"}}, where)
    assert mcp_server._row_matches_where({"quantity": "12"}, where)
    assert not mcp_server._matches_where({"properties": {"quantity": "M12"}}, where)


def test_nested_logical_conditions_and_property_paths():
    where = {"$and": [
        {"$or": [{"properties.module_id": "1-01-A"}, {"quantity__gte": 20}]},
        {"$not": {"status": "deleted"}},
    ]}
    matches = compile_where(where, mcp_server._node_field, mcp_server._MISSING)
    assert matches({"properties": {"module_id": "1-01-A", "status": "active"}})
    assert not matches({"properties": {"module_id": "1-02-A", "status": "active"}})
    assert not matches({"properties": {"quantity": 21, "status": "deleted"}})
    assert compile_where({"$and": []}, getter, MISSING)({})
    assert not compile_where({"$or": []}, getter, MISSING)({})


@pytest.mark.parametrize("where, message", [
    ([], "objects"),
    ({"q": {"greather_than": 1}}, "Unknown where operator"),
    ({"q": {"regex": "["}}, "Invalid where regex"),
    ({"q": {"exists": "false"}}, "boolean"),
    ({"q": {"gt": "M12"}}, "finite numeric"),
    ({"$or": {}}, "list"),
    ({"$not": []}, "objects"),
    ({"$nand": []}, "logical operator"),
    ({"$or": [{}, {"q": {"regex": "["}}]}, "Invalid where regex"),
])
def test_invalid_filters_are_rejected_before_scanning(where, message):
    with pytest.raises(ValueError, match=message):
        compile_where(where, getter, MISSING)


def test_excessive_nesting_is_rejected():
    where = {}
    for _ in range(34):
        where = {"$not": where}
    with pytest.raises(ValueError, match="nesting"):
        compile_where(where, getter, MISSING)


def test_missing_null_and_boolean_conditions_are_distinct():
    exists = compile_where({"q__exists": True}, getter, MISSING)
    equal_null = compile_where({"q": None}, getter, MISSING)
    assert exists({"q": None})
    assert not exists({})
    assert equal_null({"q": None})
    assert not equal_null({})
    assert not compile_where({"q": True}, getter, MISSING)({"q": 1})


def test_membership_regex_and_wildcard_preserve_existing_forms():
    assert compile_where({"q": {"in": ["12", "M20"]}}, getter, MISSING)({"q": 12})
    assert not compile_where({"q": {"in": ["1-01-A"]}}, getter, MISSING)({"q": "1-02-A"})
    assert compile_where({"q__not_in": [12]}, getter, MISSING)({"q": 13})
    assert compile_where({"q__wildcard": "M*"}, getter, MISSING)({"q": "M12"})
    assert compile_where({"q__regex": r"^M\d+$"}, getter, MISSING)({"q": "M12"})


@pytest.mark.parametrize("direction, expected", [("asc", [2, 10, None, None]), ("desc", [10, 2, None, None])])
def test_nulls_sort_last_in_both_directions(direction, expected):
    rows = [{"q": None}, {"q": 10}, {}, {"q": 2}]
    assert [row.get("q") for row in sort_records(rows, [{"field": "q", "direction": direction}], getter, MISSING)] == expected


def test_natural_sort_and_explicit_null_order():
    rows = [{"q": "M10"}, {}, {"q": "M2"}]
    ordered = sort_records(rows, [{"field": "q", "natural": True, "nulls": "first"}], getter, MISSING)
    assert [row.get("q") for row in ordered] == [None, "M2", "M10"]
    assert [row.get("q") for row in sort_records(rows, ["-q"], getter, MISSING)] == ["M2", "M10", None]


@pytest.mark.parametrize("order_by", [[{"field": "q", "direction": "dsec"}], [{"field": "q", "natural": "true"}], [4], [{"field": "q", "nulls": "middle"}]])
def test_invalid_sort_specs_are_rejected_for_empty_results(order_by):
    with pytest.raises(ValueError):
        sort_records([], order_by, getter, MISSING)


@pytest.fixture
def query_nodes(monkeypatch):
    nodes = [
        {"id": "n10", "type": "Element", "properties": {"quantity": 10, "module_id": "1-01-A", "source": {"rank": 2}}},
        {"id": "n2", "type": "Element", "properties": {"quantity": 2, "module_id": "1-02-A", "source": {"rank": 1}}},
        {"id": "missing", "type": "Element", "properties": {"quantity": None}},
    ]
    monkeypatch.setattr(mcp_server, "_pack_is_visible", lambda pack_id: True)
    monkeypatch.setattr(mcp_server, "_pack_nodes", lambda pack_id, **kwargs: ({"pack": {"id": pack_id}}, nodes))
    return nodes


def test_public_node_query_orders_unprojected_nested_fields_before_pagination(query_nodes, monkeypatch):
    projected = []
    original = mcp_server._project_node

    def capture_projection(node, *args, **kwargs):
        projected.append(node["id"])
        return original(node, *args, **kwargs)

    monkeypatch.setattr(mcp_server, "_project_node", capture_projection)
    payload = json.loads(mcp_server.mo_filtered_search_nodes("test", fields=["id"], order_by=["properties.source.rank"], offset=1, limit=1))
    assert payload["rows"] == [{"id": "n10"}]
    assert payload["count"] == 3
    assert payload["truncated"] is True
    assert projected == ["n10"]


def test_public_node_query_distinguishes_module_ids(query_nodes):
    payload = json.loads(mcp_server.mo_filtered_search_nodes("test", where={"module_id": "1-02-A"}, fields=["id"]))
    assert payload["rows"] == [{"id": "n2"}]


def test_request_compiles_filter_only_once(query_nodes, monkeypatch):
    calls = []
    original = mcp_server.compile_where

    def capture_compile(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "compile_where", capture_compile)
    mcp_server._filtered_nodes("test", where={"quantity__gt": 1})
    assert len(calls) == 1


def test_node_query_has_no_implicit_50000_node_cap(monkeypatch):
    captured = {}

    def read_graph(pack_id, **kwargs):
        captured.update(kwargs)
        return {"pack": {"id": pack_id}, "nodes": []}

    monkeypatch.setattr(mcp_server, "read_graph", read_graph)
    mcp_server._pack_nodes("test")
    assert captured == {"max_nodes": None, "max_edges": 0}


def test_chunk_query_uses_shared_syntax_aliases_and_projection(monkeypatch):
    monkeypatch.setattr(mcp_server, "_resolve_pack_ids", lambda **kwargs: ["test"])
    monkeypatch.setattr(mcp_server, "_iter_cloud_chunk_rows", lambda pack_id: iter([
        {"chunk_id": "one", "value": 20, "parameter_display": "20 m2"},
        {"chunk_id": "two", "value": 10, "parameter_display": "10 m2"},
        {"chunk_id": "wrong", "value": "M30"},
    ]))
    payload = mcp_server._query_chunk_rows_payload(logical_pack="quantity_facts", where={"$and": [{"value__gte": 10}, {"display__contains": "m2"}]}, select=["chunk_id"], order_by=["value"], limit=1)
    assert payload["count"] == 2
    assert payload["rows"] == [{"chunk_id": "two"}]


def test_aggregates_ignore_identifier_digits_and_cache_field_conversion(monkeypatch):
    nodes = [{"properties": {"q": value}} for value in ["1.2", 2.3, "M100", None]]
    reads = []
    original = mcp_server._node_field

    def capture_read(node, field):
        reads.append(field)
        return original(node, field)

    monkeypatch.setattr(mcp_server, "_node_field", capture_read)
    rows, diagnostics = mcp_server._aggregate_filtered_nodes(nodes, group_by=[], metrics=[{"field": "q", "agg": agg} for agg in ["sum", "avg", "min", "max", "count"]])
    assert rows == [{"q_sum": 3.5, "q_avg": 1.75, "q_min": 1.2, "q_max": 2.3, "q_count": 3}]
    assert diagnostics["skipped_rows"] == {"q_sum": 1, "q_avg": 1, "q_min": 1, "q_max": 1}
    assert reads == ["q"] * len(nodes)


def test_grouping_preserves_json_values_and_does_not_merge_serialized_strings():
    nodes = [{"properties": {"group": ["A"]}}, {"properties": {"group": '["A"]'}}]
    rows, _ = mcp_server._aggregate_filtered_nodes(nodes, group_by=["group"], metrics=None)
    assert rows == [{"group": ["A"], "row_count": 1}, {"group": '["A"]', "row_count": 1}]


@pytest.mark.parametrize("metrics", [[{"agg": "median", "field": "q"}], [{"agg": "sum"}], [{"agg": "count", "as": "q"}, {"agg": "count", "as": "q"}]])
def test_invalid_aggregates_fail_even_without_rows(metrics):
    with pytest.raises(ValueError):
        mcp_server._aggregate_filtered_nodes([], group_by=[], metrics=metrics)


def test_empty_global_aggregate_returns_zero_and_null():
    rows, _ = mcp_server._aggregate_filtered_nodes([], group_by=[], metrics=[{"agg": "count"}, {"agg": "sum", "field": "q"}, {"agg": "avg", "field": "q"}])
    assert rows == [{"count": 0, "q_sum": 0, "q_avg": None}]
    assert mcp_server._aggregate_filtered_nodes([], group_by=["group"], metrics=None)[0] == []


def test_default_metric_metadata_matches_output(query_nodes):
    payload = json.loads(mcp_server.mo_aggregate_nodes("test"))
    assert payload["metrics"] == ["row_count"]
    assert payload["rows"] == [{"row_count": 3}]


@pytest.mark.parametrize("source, declared, complete", [("pack", 2, True), ("sqlite-index", 5001, False), ("sqlite-index", None, None), ("sqlite-index", 0, None)])
def test_query_scope_reports_incomplete_or_unverifiable_legacy_indexes(source, declared, complete):
    graph = {"source": source, "pack": {"counts": {"nodes": declared}}, "nodes": [{}, {}], "stats": {"totalNodes": 2, "nodesTruncated": False, "edgesTruncated": True}}
    scope = mcp_server._node_query_scope(graph)
    assert scope["complete"] is complete
    assert scope["scanned_nodes"] == 2
    assert ("warning" in scope) is (complete is not True)


def test_scope_is_separate_from_result_pagination(query_nodes, monkeypatch):
    monkeypatch.setattr(mcp_server, "_pack_nodes", lambda pack_id, **kwargs: ({"source": "sqlite-index", "pack": {"id": pack_id, "counts": {"nodes": 5001}}, "nodes": query_nodes, "stats": {"totalNodes": 3}}, query_nodes))
    payload = json.loads(mcp_server.mo_aggregate_nodes("test"))
    assert payload["scope"]["complete"] is False
    assert payload["truncated"] is False


def test_projection_keeps_colliding_paths_without_changing_simple_names():
    node = {"id": "graph-id", "properties": {"id": "business-id", "a": {"value": 1}, "b": {"value": 2}}}
    assert mcp_server._project_node(node, ["id", "properties.id", "a.value", "b.value"]) == {"id": "graph-id", "properties.id": "business-id", "a.value": 1, "b.value": 2}
    assert mcp_server._project_node(node, ["properties.id"]) == {"id": "business-id"}
    assert mcp_server._projection_keys(["a.b.c", "b.c", "x.c"]) == [("a.b.c", "a.b.c"), ("b.c", "b.c"), ("x.c", "x.c")]
    assert mcp_server._project_row({"a": {"value": 1}, "b": {"value": 2}}, ["a.value", "b.value"]) == {"a.value": 1, "b.value": 2}


def test_large_numeric_aggregates_do_not_use_default_decimal_precision():
    value = 10 ** 40
    nodes = [{"properties": {"q": value}}, {"properties": {"q": 2}}]
    rows, _ = mcp_server._aggregate_filtered_nodes(nodes, group_by=[], metrics=[{"agg": "sum", "field": "q"}, {"agg": "avg", "field": "q"}])
    assert rows == [{"q_sum": value + 2, "q_avg": value // 2 + 1}]


def test_numeric_grouping_merges_int_float_but_preserves_text_and_bool():
    nodes = [{"properties": {"g": value}} for value in [1, 1.0, "1", "M1", True]]
    rows, _ = mcp_server._aggregate_filtered_nodes(nodes, group_by=["g"], metrics=None)
    assert rows == [{"g": 1, "row_count": 2}, {"g": "1", "row_count": 1}, {"g": "M1", "row_count": 1}, {"g": True, "row_count": 1}]


def test_quantity_query_sorts_before_projection_and_computes_consistent_totals(query_nodes):
    payload = json.loads(mcp_server.mo_query_quantity_evidence("test", include_fields=["id"], order_by=["quantity"], limit=1))
    assert payload["rows"] == [{"id": "n2"}]
    assert payload["totals"]["quantity_sum"] == 12


@pytest.fixture
def join_sources(monkeypatch):
    sources = {
        "left": [
            {"id": "left-1", "properties": {"reference": {"code": "M1"}}},
            {"id": "left-2", "properties": {"reference": {"code": "M1"}}},
            {"id": "left-only", "properties": {"reference": {"code": "M2"}}},
        ],
        "right": [
            {"id": "right-1", "properties": {"reference": {"code": "M1"}}},
            {"id": "right-2", "properties": {"reference": {"code": "M1"}}},
            {"id": "right-only", "properties": {"reference": {"code": "M3"}}},
        ],
    }
    monkeypatch.setattr(mcp_server, "_pack_is_visible", lambda pack_id: True)
    monkeypatch.setattr(mcp_server, "_pack_nodes", lambda pack_id, **kwargs: ({"pack": {"id": pack_id}, "nodes": sources[pack_id]}, sources[pack_id]))
    return {"pack_id": "left", "join_field": "properties.reference.code"}, {"pack_id": "right", "join_field": "reference.code"}


def test_join_includes_custom_dotted_key_with_default_projection(join_sources):
    left, right = join_sources
    payload = json.loads(mcp_server.mo_join_by_property(left, right, summarize_right=False))
    assert payload["matched_keys"] == 1
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["left"]["properties.reference.code"] == "M1"
    assert [row["id"] for row in payload["rows"][0]["right"]] == ["right-1", "right-2"]
    assert payload["rows"][0]["right"][0]["reference.code"] == "M1"
    assert payload["truncated"] is False


def test_join_keeps_requested_key_when_projection_omits_it(join_sources):
    left, right = join_sources
    payload = json.loads(mcp_server.mo_join_by_property({**left, "fields": ["id"]}, {**right, "fields": ["id"]}))
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["right_summary"]["count"] == 2


@pytest.mark.parametrize("limit, count, truncated", [(0, 0, True), (1, 1, True), (2, 2, False), (3, 2, False)])
def test_join_limits_count_rows_within_the_same_key(join_sources, limit, count, truncated):
    payload = json.loads(mcp_server.mo_join_by_property(*join_sources, limit=limit))
    assert len(payload["rows"]) == count
    assert payload["truncated"] is truncated


@pytest.mark.parametrize("join_type, keys", [("left", ["M1", "M1", "M2"]), ("right", ["M1", "M1", "M3"]), ("outer", ["M1", "M1", "M2", "M3"]), ("full", ["M1", "M1", "M2", "M3"])])
def test_join_preserves_grouped_right_outer_semantics(join_sources, join_type, keys):
    payload = json.loads(mcp_server.mo_join_by_property(*join_sources, join_type=join_type))
    assert [row["join_key"] for row in payload["rows"]] == keys
    assert payload["truncated"] is False


def test_join_rejects_invalid_type_and_where(join_sources):
    assert json.loads(mcp_server.mo_join_by_property(*join_sources, join_type="innre"))["error"]
    left, right = join_sources
    assert json.loads(mcp_server.mo_join_by_property({**left, "where": []}, right))["error"]
