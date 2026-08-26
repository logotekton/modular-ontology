from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from modular_ontology import pack_index
from modular_ontology import google_drive_sync


def _write_pack(
    root: Path,
    pack_id: str,
    *,
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
    extra_counts: dict[str, int] | None = None,
) -> pack_index.PackFile:
    nodes = nodes or []
    edges = edges or []
    counts = {
        "nodes": len(nodes),
        "edges": len(edges),
        "documents": 0,
        **(extra_counts or {}),
    }
    path = root / f"{pack_id}.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "opencrab-cloud-pack-v1",
                    "pack_id": pack_id,
                    "title": pack_id,
                    "counts": counts,
                    "entrypoints": {
                        "nodes": "graph/nodes.jsonl",
                        "edges": "graph/edges.jsonl",
                    },
                }
            ),
        )
        archive.writestr("graph/nodes.jsonl", "\n".join(json.dumps(row) for row in nodes))
        archive.writestr("graph/edges.jsonl", "\n".join(json.dumps(row) for row in edges))
    return pack_index.PackFile(path)


def _use_packs(monkeypatch, packs: list[pack_index.PackFile]) -> None:
    by_id = {pack_index.summarize_pack(pack)["id"]: pack for pack in packs}

    def find_pack(pack_id: str, **_kwargs) -> pack_index.PackFile:
        if pack_id not in by_id:
            raise FileNotFoundError(pack_id)
        return by_id[pack_id]

    monkeypatch.setattr(pack_index, "find_pack", find_pack)


def test_multi_pack_graph_connects_an_edge_only_relationship_shard(tmp_path: Path, monkeypatch) -> None:
    left = _write_pack(tmp_path, "left", nodes=[{"id": "node:left", "label": "Left"}])
    right = _write_pack(tmp_path, "right", nodes=[{"id": "node:right", "label": "Right"}])
    relationships = _write_pack(
        tmp_path,
        "relationships",
        edges=[
            {
                "id": "edge:cross-pack",
                "source": "node:left",
                "target": "node:right",
                "relation": "REFERENCES",
            }
        ],
        extra_counts={"chunks": 12_000},
    )
    _use_packs(monkeypatch, [left, right, relationships])

    graph = pack_index.build_multi_pack_graph(
        ["left", "right", "relationships"],
        max_nodes=10,
        max_edges=10,
    )

    assert graph["source"] == "zip-two-pass"
    assert {node["id"] for node in graph["nodes"]} == {"left::node:left", "right::node:right"}
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["id"] == "relationships::edge:cross-pack"
    assert graph["edges"][0]["source"] == "left::node:left"
    assert graph["edges"][0]["target"] == "right::node:right"
    assert graph["edges"][0]["packId"] == "relationships"
    assert graph["stats"]["visibleEdges"] == 1
    assert graph["diagnostics"]["unresolvedEdges"] == 0
    assert graph["diagnostics"]["crossPackEdges"] == 1
    assert graph["diagnostics"]["visibleCrossPackEdges"] == 1


def test_multi_pack_graph_namespaces_collisions_and_reports_deterministic_ambiguity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = _write_pack(
        tmp_path,
        "first",
        nodes=[{"id": "shared", "label": "First shared"}, {"id": "first-only"}],
    )
    second = _write_pack(
        tmp_path,
        "second",
        nodes=[{"id": "shared", "label": "Second shared"}, {"id": "second-only"}],
    )
    relationships = _write_pack(
        tmp_path,
        "relationships",
        edges=[{"id": "edge:ambiguous", "source": "shared", "target": "second-only", "relation": "LINKS"}],
    )
    _use_packs(monkeypatch, [first, second, relationships])

    graph = pack_index.build_multi_pack_graph(
        ["first", "second", "relationships"],
        max_nodes=4,
        max_edges=4,
    )

    assert {node["id"] for node in graph["nodes"]} == {
        "first::shared",
        "first::first-only",
        "second::shared",
        "second::second-only",
    }
    assert graph["edges"][0]["source"] == "first::shared"
    assert graph["edges"][0]["target"] == "second::second-only"
    diagnostics = graph["diagnostics"]
    assert diagnostics["ambiguousEndpointCount"] == 1
    assert diagnostics["ambiguousResolutionCount"] == 1
    assert diagnostics["ambiguousEndpoints"][0] == {
        "edgePackId": "relationships",
        "endpointId": "shared",
        "candidateNodeIds": ["first::shared", "second::shared"],
        "selectedNodeId": "first::shared",
    }


def test_multi_pack_graph_enforces_zero_and_positive_limits(tmp_path: Path, monkeypatch) -> None:
    nodes = [{"id": f"node:{index}"} for index in range(6)]
    edges = [
        {
            "id": f"edge:{index}",
            "source": f"node:{index}",
            "target": f"node:{index + 1}",
            "relation": "NEXT",
        }
        for index in range(5)
    ]
    pack = _write_pack(tmp_path, "limited", nodes=nodes, edges=edges)
    _use_packs(monkeypatch, [pack])

    empty = pack_index.build_multi_pack_graph(["limited"], max_nodes=0, max_edges=0)
    assert empty["nodes"] == []
    assert empty["edges"] == []

    limited = pack_index.build_multi_pack_graph(["limited"], max_nodes=3, max_edges=10)
    assert len(limited["nodes"]) == 3
    assert len(limited["edges"]) == 2
    assert limited["diagnostics"]["nodeLimitedEdges"] > 0

    protected = pack_index.build_multi_pack_graph(["limited"], max_nodes=1_000_000, max_edges=1_000_000)
    assert protected["diagnostics"]["appliedMaxNodes"] == 1_000
    assert protected["diagnostics"]["appliedMaxEdges"] == 2_000


def test_multi_pack_graph_trims_large_properties_to_serverless_response_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack = _write_pack(
        tmp_path,
        "large-properties",
        nodes=[
            {"id": f"node:{index}", "properties": {"payload": "x" * 350_000}}
            for index in range(16)
        ],
    )
    _use_packs(monkeypatch, [pack])

    graph = pack_index.build_multi_pack_graph(
        ["large-properties"],
        max_nodes=100,
        max_edges=100,
    )
    encoded = json.dumps(graph, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    assert len(encoded) <= 4_000_000
    assert graph["diagnostics"]["responseTruncated"] is True
    assert graph["diagnostics"]["responseBytes"] <= 4_000_000
    assert graph["stats"]["visibleNodes"] == len(graph["nodes"])
    visible_node_ids = {node["id"] for node in graph["nodes"]}
    assert all(edge["source"] in visible_node_ids and edge["target"] in visible_node_ids for edge in graph["edges"])


def test_explicit_zero_node_count_is_authoritative_for_relationship_shards(tmp_path: Path) -> None:
    relationship = _write_pack(
        tmp_path,
        "relationship-only",
        edges=[{"source": "external:a", "target": "external:b", "relation": "LINKS"}],
        extra_counts={"chunks": 12_000, "documents": 2},
    )

    summary = pack_index.summarize_pack(relationship)

    assert summary["counts"]["nodes"] == 0
    assert summary["counts"]["edges"] == 1
    assert summary["counts"]["documents"] == 2


def test_find_pack_preserves_drive_restore_failures_as_runtime_errors(monkeypatch) -> None:
    monkeypatch.setattr(pack_index, "unique_pack_files", lambda: [])
    monkeypatch.setattr(
        google_drive_sync,
        "fetch_pack_file_from_drive",
        lambda _pack_id, **_kwargs: (_ for _ in ()).throw(RuntimeError("Drive unavailable")),
    )

    with pytest.raises(RuntimeError, match="could not be restored"):
        pack_index.find_pack("remote-pack")


def test_multi_pack_graph_reuses_one_lazy_drive_client_for_all_cache_misses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = _write_pack(tmp_path, "remote-first", nodes=[{"id": "first"}])
    second = _write_pack(tmp_path, "remote-second", nodes=[{"id": "second"}])
    paths = {"remote-first": first.path, "remote-second": second.path}
    created_clients: list[object] = []
    downloads: list[str] = []

    class FakeDriveClient:
        def download_file(self, file_id: str, _target: Path) -> None:
            downloads.append(file_id)

    def create_client(_cls):
        client = FakeDriveClient()
        created_clients.append(client)
        return client

    def fetch_pack(pack_id: str, *, client=None) -> Path:
        assert client is not None
        client.download_file(pack_id, tmp_path / f"unused-{pack_id}.zip")
        return paths[pack_id]

    monkeypatch.setattr(pack_index, "unique_pack_files", lambda: [])
    monkeypatch.setattr(pack_index, "_PACK_LOOKUP_CACHE", {})
    monkeypatch.setattr(google_drive_sync.GoogleDriveClient, "from_env", classmethod(create_client))
    monkeypatch.setattr(google_drive_sync, "fetch_pack_file_from_drive", fetch_pack)

    graph = pack_index.build_multi_pack_graph(
        ["remote-first", "remote-second"],
        max_nodes=10,
        max_edges=0,
    )

    assert len(created_clients) == 1
    assert downloads == ["remote-first", "remote-second"]
    assert graph["stats"]["visibleNodes"] == 2


def test_response_budget_compacts_oversized_metadata_without_nodes() -> None:
    payload = {
        "pack": {"id": "project", "title": "Project"},
        "project": {"id": "project", "name": "Project", "description": "x" * 4_100_000},
        "packs": [{"id": "pack", "title": "y" * 4_100_000, "counts": {}}],
        "activePackIds": ["pack"],
        "nodes": [],
        "edges": [],
        "stats": {"visibleNodes": 0, "visibleEdges": 0},
        "diagnostics": {"ambiguousEndpoints": []},
    }

    fitted = pack_index._fit_multi_pack_response_budget(payload)
    encoded = json.dumps(fitted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    assert len(encoded) <= 4_000_000
    assert fitted["diagnostics"]["responseTruncated"] is True
    assert fitted["diagnostics"]["metadataCompacted"] is True


def test_capacity_aware_limits_reassign_small_shard_surplus() -> None:
    limits = pack_index._capacity_aware_fair_limits(
        ["small", "large-a", "large-b"],
        12,
        {"small": 1, "large-a": 20, "large-b": 20},
    )

    assert limits == {"small": 1, "large-a": 6, "large-b": 5}
    assert sum(limits.values()) == 12
