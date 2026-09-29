from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest

from modular_ontology import pack_index, store


def make_pack(tmp_path, files, *, name="fixture", entrypoints=None):
    path = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"pack_id": name, "entrypoints": entrypoints or {}}))
        for filename, rows in files.items():
            archive.writestr(filename, "\n".join(json.dumps(row) for row in rows))
    return pack_index.PackFile(path)


def test_zip_filters_edges_before_budget_and_preserves_parallel_assertions(tmp_path):
    pack = make_pack(tmp_path, {
        "graph/nodes.jsonl": [{"id": "a", "type": "Module"}, {"id": "b"}, {"id": "hidden"}],
        "graph/edges.jsonl": [
            *[{"source": "hidden", "target": "a"} for _ in range(20)],
            {"id": "e1", "source": "a", "target": "b", "relation": "contains"},
            {"id": "e2", "source": {"id": "a"}, "target": "b", "relation": "contains"},
        ],
    })
    graph = pack_index.build_graph_from_pack(pack, max_nodes=2, max_edges=2)
    assert [edge["id"] for edge in graph["edges"]] == ["e1", "e2"]
    assert graph["nodes"][0]["type"] == "Module"
    assert graph["stats"]["nodesTruncated"] is True
    assert graph["stats"]["totalNodes"] == 3


@pytest.mark.parametrize("limit", [0, -3])
def test_zip_nonpositive_limits_emit_no_nodes_or_edges(tmp_path, limit):
    pack = make_pack(tmp_path, {
        "graph/nodes.jsonl": [{"id": "a"}, {"id": "b"}],
        "graph/edges.jsonl": [{"source": "a", "target": "b"}],
    })
    assert pack_index.build_graph_from_pack(pack, max_nodes=limit)["nodes"] == []
    assert pack_index.build_graph_from_pack(pack, max_edges=limit)["edges"] == []


def test_custom_entrypoints_nodes_only_and_unlimited_loading(tmp_path):
    pack = make_pack(tmp_path, {
        "custom/nodes.jsonl": [None, {}, {"id": None}, {"id": "a", "labels": ["Beam", "Element"]}],
    }, entrypoints={"nodes": "custom/nodes.jsonl", "edges": "custom/edges.jsonl"})
    graph = pack_index.build_graph_from_pack(pack, max_nodes=None, max_edges=None)
    assert [node["id"] for node in graph["nodes"]] == ["a"]
    assert graph["nodes"][0]["type"] == "Beam"
    assert graph["nodes"][0]["labels"] == ["Beam", "Element"]
    assert graph["edges"] == []
    assert graph["stats"]["totalNodes"] == 1
    assert graph["stats"]["nodesTruncated"] is False


def test_anonymous_edge_ids_do_not_collide_on_delimiters():
    first = pack_index._edge("a:b", "d", "c", "p")
    second = pack_index._edge("a", "d", "b:c", "p")
    assert first["id"] != second["id"]


def test_producer_unlimited_nodes_do_not_have_hidden_per_type_caps(tmp_path):
    pack = make_pack(tmp_path, {
        "backdata/jsonl/modules.jsonl": [{"id": f"m{i}"} for i in range(170)],
        "backdata/jsonl/assemblies.jsonl": [{"id": f"a{i}"} for i in range(370)],
    })
    graph = pack_index.build_graph_from_pack(pack, max_nodes=None, max_edges=0)
    assert len(graph["nodes"]) == 540
    assert graph["nodes"][0]["type"] == "Module"
    assert graph["nodes"][-1]["type"] == "Assembly"
    assert graph["stats"]["nodesTruncated"] is False
    sample = pack_index.build_graph_from_pack(pack, max_nodes=1, max_edges=0)
    assert sample["stats"]["totalNodes"] == 540
    assert sample["stats"]["nodesTruncated"] is True


@pytest.mark.parametrize("limit", [None, 0, -2])
def test_producer_inferred_edges_are_unique_and_counted_without_self_links(tmp_path, limit):
    pack = make_pack(tmp_path, {
        "backdata/jsonl/modules.jsonl": [{"id": "m", "module_id": "m"}],
        "backdata/jsonl/assemblies.jsonl": [{"id": "a", "module_id": "m"}],
        "backdata/jsonl/single_parts.jsonl": [{"id": "part", "assembly_id": "a"}],
        "backdata/jsonl/edges.jsonl": [
            {"id": "explicit", "from": "a", "to": "m", "relation": "belongs_to_module"},
            {"id": "explicit", "from": "a", "to": "m", "relation": "belongs_to_module"},
            {"from": "unknown", "to": "a"},
        ],
    })
    graph = pack_index.build_graph_from_pack(pack, max_nodes=None, max_edges=limit)
    assert graph["stats"]["totalEdges"] == (2 if limit is None else 3)
    assert len(graph["edges"]) == (2 if limit is None else 0)
    assert all(edge["source"] != edge["target"] for edge in graph["edges"])
    if limit is None:
        assert graph["edges"][0]["id"] == "explicit"
        assert graph["stats"]["truncated"] is False


def test_producer_nodes_only_skips_edge_parsing(tmp_path, monkeypatch):
    pack = make_pack(tmp_path, {
        "backdata/jsonl/modules.jsonl": [{"id": "m"}],
        "backdata/jsonl/assemblies.jsonl": [{"id": "a", "module_id": "m"}],
        "backdata/jsonl/edges.jsonl": [{"from": "a", "to": "m"}],
    })
    original = pack_index._iter_jsonl

    def node_rows_only(archive, path, limit=None):
        assert path != "backdata/jsonl/edges.jsonl"
        yield from original(archive, path, limit)

    monkeypatch.setattr(pack_index, "_iter_jsonl", node_rows_only)
    graph = pack_index.build_graph_from_pack(pack, max_nodes=None, max_edges=0)
    assert len(graph["nodes"]) == 2
    assert graph["edges"] == []


@pytest.fixture
def indexed_graph(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE nodes (id TEXT, pack_id TEXT, label TEXT, type TEXT, properties_json TEXT);
        CREATE TABLE edges (id TEXT, pack_id TEXT, source TEXT, target TEXT, relation TEXT, properties_json TEXT);
    """)
    conn.executemany("INSERT INTO nodes VALUES (?, 'p', ?, 'Element', '{}')", [(name, name) for name in ["a", "b", "z"]])
    conn.executemany("INSERT INTO edges VALUES (?, 'p', ?, ?, 'related', '{}')", [
        *[(f"hidden-{i:03}", "z", "a") for i in range(210)],
        ("visible", "a", "b"),
    ])
    conn.commit()
    conn.close()

    def connect():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(pack_index, "_db_connect", connect)
    monkeypatch.setattr(pack_index, "_db_pack_summary", lambda _: {"id": "p"})


def test_db_filters_edges_before_limiting(indexed_graph):
    graph = pack_index._build_graph_from_db("p", max_nodes=2, max_edges=1)
    assert [edge["id"] for edge in graph["edges"]] == ["visible"]
    assert graph["stats"]["totalEdges"] == 211


@pytest.mark.parametrize("limit", [0, -1])
def test_db_nonpositive_limits_and_unlimited_nodes(indexed_graph, limit):
    graph = pack_index._build_graph_from_db("p", max_nodes=None, max_edges=limit)
    assert len(graph["nodes"]) == 3
    assert graph["edges"] == []
    assert pack_index._build_graph_from_db("p", max_nodes=limit)["nodes"] == []


@pytest.mark.parametrize("node_limit, edge_limit", [(0, 0), (-2, -1), (1, 1), (4, 3)])
def test_multi_pack_budgets_are_global_and_carry_forward_unused_slots(monkeypatch, node_limit, edge_limit):
    def graph(pack_id, max_nodes, max_edges):
        count = min(max_nodes, 0 if pack_id == "empty" else 100)
        nodes = [{"id": f"n{i}", "type": "Element"} for i in range(count)]
        edges = [{"id": "e", "source": "n0", "target": "n1"}] if count > 1 and max_edges else []
        return {"pack": {"id": pack_id, "title": pack_id}, "nodes": nodes, "edges": edges,
                "stats": {"totalNodes": 0 if pack_id == "empty" else 100, "totalEdges": 1}}

    monkeypatch.setattr(pack_index, "build_graph", graph)
    result = pack_index.build_multi_pack_graph(["empty", "second", "third"], max_nodes=node_limit, max_edges=edge_limit)
    assert len(result["nodes"]) == max(0, node_limit)
    assert len(result["edges"]) <= max(0, edge_limit)
    node_ids = {node["id"] for node in result["nodes"]}
    assert len(node_ids) == len(result["nodes"])
    assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in result["edges"])


def test_multi_pack_aliases_do_not_duplicate_nodes(tmp_path, monkeypatch):
    pack = make_pack(tmp_path, {"graph/nodes.jsonl": [{"id": "a"}]})
    monkeypatch.setattr(pack_index, "find_pack", lambda _: pack)
    result = pack_index.build_multi_pack_graph(["fixture", "fixture.zip"])
    assert len(result["packs"]) == 1
    assert [node["id"] for node in result["nodes"]] == ["fixture::a"]
    assert result["stats"]["totalNodes"] == 1


def test_sqlite_index_is_complete_beyond_old_visualization_caps(tmp_path):
    pack = make_pack(tmp_path, {
        "graph/nodes.jsonl": [{"id": f"n{i}"} for i in range(5001)],
        "graph/edges.jsonl": [
            {"id": f"e{i}", "source": "n0", "target": f"n{i % 5000 + 1}"}
            for i in range(12001)
        ],
    })
    conn = store.connect(tmp_path / "index.sqlite3")
    try:
        store.init_db(conn)
        result = store.index_pack(conn, pack)
        assert result["nodes"] == 5001
        assert result["edges"] == 12001
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 5001
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 12001
        # Normal reindexing uses FK cascades and must not retain stale rows.
        conn.execute("INSERT INTO nodes VALUES ('stale', 'fixture', 'old', 'Element', '{}')")
        conn.commit()
        store.index_pack(conn, pack)
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE id = 'stale'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 12001
    finally:
        conn.close()


@pytest.mark.parametrize("node_file", [True, False])
def test_index_preserves_cross_pack_edges_in_relationship_only_shards(tmp_path, node_file):
    files = {"graph/edges.jsonl": [
        {"id": "external", "source": "other:a", "target": "other:b", "relation": "contains"},
        {"id": "invalid", "source": "", "target": "other:b"},
    ]}
    if node_file:
        files["graph/nodes.jsonl"] = []
    pack = make_pack(tmp_path, files)
    # Drawing a pack alone still requires visible endpoints.
    assert pack_index.build_graph_from_pack(pack)["edges"] == []
    conn = store.connect(tmp_path / "index.sqlite3")
    try:
        store.init_db(conn)
        result = store.index_pack(conn, pack)
        assert result["nodes"] == 0
        assert result["edges"] == 1
        row = conn.execute("SELECT id, source, target FROM edges").fetchone()
        assert tuple(row) == ("external", "other:a", "other:b")
    finally:
        conn.close()


def test_producer_index_preserves_external_assertions(tmp_path):
    pack = make_pack(tmp_path, {
        "backdata/jsonl/modules.jsonl": [{"id": "a"}],
        "backdata/jsonl/edges.jsonl": [{"id": "external", "from": "a", "to": "other:b"}],
    })
    assert pack_index.build_graph_from_pack(pack)["edges"] == []
    graph = pack_index.build_graph_from_pack(pack, None, None, include_external_edges=True)
    assert [e["id"] for e in graph["edges"]] == ["external"]


@pytest.fixture
def tail_match_pack(tmp_path, monkeypatch):
    pack = make_pack(tmp_path, {
        "graph/nodes.jsonl": [
            *[{"id": f"n{i}", "type": "Element"} for i in range(5000)],
            {"id": "tail", "type": "RareType", "label": "DistinctiveNeedle"},
        ],
        "graph/edges.jsonl": [{"id": "tail-edge", "source": "tail", "target": "n0", "relation": "rare"}],
    })
    monkeypatch.setattr(pack_index, "find_pack", lambda _: pack)


@pytest.mark.parametrize("query", [
    lambda: pack_index.list_nodes("fixture", node_type="RareType", limit=1),
    lambda: pack_index.search_nodes("fixture", "DistinctiveNeedle", limit=1),
    lambda: pack_index.list_edges("fixture", source="tail", relation="rare", limit=1),
])
def test_bounded_queries_mark_missing_tail_matches_as_sampled(tail_match_pack, query):
    result = query()
    assert result["count"] == 0
    assert result["truncated"] is True
    assert result["scope"]["sampled"] is True
    assert result["scope"]["available_nodes"] == 5001
    assert "sample" in result["scope"]["warning"]


def test_sampled_context_does_not_claim_tail_node_is_absent(tail_match_pack):
    result = pack_index.get_node_context("fixture", "tail", limit=1)
    assert result["node"] is None
    assert result["node_status"] == "not_in_sample"
    assert result["scope"]["sampled"] is True
    assert result["truncated"] is True


def test_complete_small_graph_queries_have_no_sample_warning(tmp_path, monkeypatch):
    pack = make_pack(tmp_path, {"graph/nodes.jsonl": [{"id": "a"}]})
    monkeypatch.setattr(pack_index, "find_pack", lambda _: pack)
    result = pack_index.list_nodes("fixture")
    assert result["scope"]["coverage"] == "available_graph"
    assert result["truncated"] is False
    missing = pack_index.get_node_context("fixture", "missing")
    assert missing["node_status"] == "not_found"
    assert "warning" not in missing["scope"]
