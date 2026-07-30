from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest


def _write_graph_pack(path: Path, pack_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "pack_id": pack_id,
                    "title": "Runtime graph pack",
                    "counts": {"nodes": 2, "edges": 1, "documents": 0},
                    "entrypoints": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl"},
                }
            ),
        )
        archive.writestr(
            "graph/nodes.jsonl",
            "\n".join(
                [
                    json.dumps({"id": "node-a", "label": "Node A", "type": "Element"}),
                    json.dumps({"id": "node-b", "label": "Node B", "type": "Element"}),
                ]
            ),
        )
        archive.writestr(
            "graph/edges.jsonl",
            json.dumps({"source": "node-a", "target": "node-b", "relation": "connects"}),
        )
    return path


def test_graph_and_mcp_share_exact_registry_lazy_fetch(monkeypatch, tmp_path: Path) -> None:
    from modular_ontology import mcp_server, pack_index

    pack_id = "runtime-graph-pack"
    pack_path = _write_graph_pack(tmp_path / "runtime-graph-pack.zip", pack_id)
    requested: list[str] = []

    monkeypatch.setattr(pack_index, "unique_pack_files", lambda: [])
    monkeypatch.setattr(
        "modular_ontology.google_drive_sync.fetch_pack_file_from_drive",
        lambda requested_id, **_kwargs: requested.append(requested_id) or pack_path,
    )
    pack_index._PACK_LOOKUP_CACHE.clear()

    graph = pack_index.build_graph(pack_id, max_nodes=20, max_edges=20)

    assert requested == [pack_id]
    assert graph["stats"]["totalNodes"] == 2
    assert graph["stats"]["totalEdges"] == 1

    monkeypatch.setattr(mcp_server, "_pack_is_visible", lambda _pack_id: True)
    monkeypatch.setattr(mcp_server, "read_graph", pack_index.build_graph)
    mcp_payload = json.loads(mcp_server.mo_graph_get(pack_id, max_nodes=20, max_edges=20))

    assert mcp_payload["stats"]["totalNodes"] == 2
    assert mcp_payload["stats"]["totalEdges"] == 1
    assert requested == [pack_id]


def test_ephemeral_summary_merge_keeps_registry_identity_after_lazy_fetch() -> None:
    from modular_ontology import graph_preview, pack_index

    registry_summary = {
        "id": "law-pack",
        "title": "Registry title",
        "sizeBytes": 120,
        "counts": {"nodes": 4, "edges": 3, "documents": 2},
        "drive": {
            "fileId": "drive-file",
            "modifiedTime": "2026-07-07T22:46:44.000Z",
            "sizeBytes": 120,
        },
    }
    direct_summary = {
        "id": "law-pack",
        "title": "Direct cache title",
        "sizeBytes": 120,
        "counts": {"nodes": 4, "edges": 3, "documents": 2},
        "cacheOnly": True,
    }

    merged = pack_index._merge_pack_summaries(
        [registry_summary],
        [direct_summary],
    )

    assert merged == [{**direct_summary, **registry_summary}]
    assert merged[0]["drive"] == registry_summary["drive"]
    assert merged[0]["cacheOnly"] is True
    assert graph_preview.graph_pack_signature(
        ["law-pack"],
        merged,
        registry_generation="registry-v1",
    ) == graph_preview.graph_pack_signature(
        ["law-pack"],
        [registry_summary],
        registry_generation="registry-v1",
    )


def test_compact_db_never_silently_returns_empty_graph_when_payload_is_expected(monkeypatch) -> None:
    from modular_ontology import pack_index

    monkeypatch.setattr(pack_index, "EPHEMERAL_STORAGE", True)
    monkeypatch.setattr(pack_index, "find_pack", lambda _pack_id: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(
        pack_index,
        "_build_graph_from_db",
        lambda *_args, **_kwargs: {
            "pack": {"counts": {"nodes": 2, "edges": 1}},
            "nodes": [],
            "edges": [],
            "stats": {"totalNodes": 0, "totalEdges": 0},
            "source": "sqlite-index",
        },
    )

    with pytest.raises(RuntimeError, match="could not be restored from Drive"):
        pack_index.build_graph("missing-runtime-pack")


def test_full_reindex_refuses_to_prune_after_partial_drive_sync(monkeypatch) -> None:
    from fastapi import HTTPException

    from modular_ontology import app

    monkeypatch.setattr(app, "require_admin", lambda _authorization: None)
    monkeypatch.setattr(
        app,
        "run_google_drive_sync",
        lambda **_kwargs: {"enabled": True, "status": "synced", "missing": ["02_Projects"], "warnings": []},
    )
    monkeypatch.setattr(
        app,
        "index_all_packs",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("partial sync must not prune")),
    )

    with pytest.raises(HTTPException) as exc_info:
        app.reindex(full=True, authorization="Bearer test")

    assert exc_info.value.status_code == 502
    assert "refusing to prune" in str(exc_info.value.detail)
