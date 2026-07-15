from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from modular_ontology.pack_index import PackFile, query_terms, search_tokens
from modular_ontology.store import (
    bm25_index_status,
    connect,
    index_pack,
    init_db,
    pack_ids_with_documents,
    rebuild_bm25_index,
    search_documents,
    search_documents_bm25_multi,
    search_documents_multi,
)


def _write_pack(
    path: Path,
    *,
    pack_id: str = "indexing-pack",
    title: str = "Indexed Pack",
    documents: dict[str, str] | None = None,
    nodes: list[dict[str, object]] | None = None,
    edges: list[dict[str, object]] | None = None,
) -> PackFile:
    if documents is None:
        documents = {"documents/guide.md": "Initial evidence"}
    if nodes is None:
        nodes = [
            {"id": "node:source", "labels": ["Document"], "properties": {"title": "Source"}},
            {"id": "node:target", "labels": ["Element"], "properties": {"name": "Target"}},
        ]
    if edges is None:
        edges = [{"source": "node:source", "target": "node:target", "relation": "references"}]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "pack_id": pack_id,
                    "title": title,
                    "entrypoints": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl"},
                    "counts": {"documents": len(documents), "nodes": len(nodes), "edges": len(edges)},
                }
            ),
        )
        for document_path, body in documents.items():
            archive.writestr(document_path, body)
        archive.writestr("graph/nodes.jsonl", "\n".join(json.dumps(node) for node in nodes))
        archive.writestr("graph/edges.jsonl", "\n".join(json.dumps(edge) for edge in edges))
    return PackFile(path)


def _rows(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    columns = {
        "documents": "path, title, body",
        "nodes": "id, label, type, properties_json",
        "edges": "id, source, target, relation, properties_json",
    }[table]
    return [tuple(row) for row in conn.execute(f"SELECT {columns} FROM {table} WHERE pack_id = ? ORDER BY 1", ("indexing-pack",))]


def test_index_pack_batches_equivalent_rows_and_removes_stale_rows(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path / "initial-pack.zip")
    conn = connect(tmp_path / "index.sqlite3")
    init_db(conn)

    assert index_pack(conn, pack) == {"id": "indexing-pack", "documents": 1, "nodes": 2, "edges": 1}
    assert _rows(conn, "documents") == [("documents/guide.md", "guide", "Initial evidence")]
    assert {row[0] for row in _rows(conn, "nodes")} == {"node:source", "node:target"}
    assert len(_rows(conn, "edges")) == 1

    pack = _write_pack(
        tmp_path / "reindexed-pack.zip",
        title="Reindexed Pack",
        documents={"documents/current.md": "Current evidence"},
        nodes=[{"id": "node:current", "labels": ["Element"], "properties": {"name": "Current"}}],
        edges=[],
    )

    assert index_pack(conn, pack) == {"id": "indexing-pack", "documents": 1, "nodes": 1, "edges": 0}
    assert _rows(conn, "documents") == [("documents/current.md", "current", "Current evidence")]
    assert [row[0] for row in _rows(conn, "nodes")] == ["node:current"]
    assert _rows(conn, "edges") == []
    assert conn.execute("SELECT title FROM packs WHERE id = ?", ("indexing-pack",)).fetchone()[0] == "reindexed pack"
    conn.close()


def test_index_pack_rolls_back_entire_reindex_when_batch_write_fails(tmp_path: Path) -> None:
    pack = _write_pack(tmp_path / "pack.zip")
    conn = connect(tmp_path / "index.sqlite3")
    init_db(conn)
    index_pack(conn, pack)
    before = {table: _rows(conn, table) for table in ("documents", "nodes", "edges")}
    before_pack = tuple(conn.execute("SELECT title, summary_json FROM packs WHERE id = ?", ("indexing-pack",)).fetchone())

    failed_pack = _write_pack(
        tmp_path / "failed-pack.zip",
        title="Should Not Persist",
        documents={"documents/replacement.md": "Replacement evidence"},
        nodes=[
            {"id": "node:replacement", "labels": ["Document"], "properties": {"title": "Replacement"}},
            {"id": "node:other", "labels": ["Element"], "properties": {"name": "Other"}},
        ],
        edges=[{"source": "node:replacement", "target": "node:other", "relation": "fails"}],
    )
    conn.execute("CREATE TRIGGER reject_edge_insert BEFORE INSERT ON edges BEGIN SELECT RAISE(ABORT, 'edge failure'); END")

    with pytest.raises(sqlite3.IntegrityError, match="edge failure"):
        index_pack(conn, failed_pack)

    assert {table: _rows(conn, table) for table in ("documents", "nodes", "edges")} == before
    assert tuple(conn.execute("SELECT title, summary_json FROM packs WHERE id = ?", ("indexing-pack",)).fetchone()) == before_pack
    conn.close()


def test_search_documents_multi_matches_single_pack_ranking(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    conn = connect(db_path)
    init_db(conn)
    index_pack(
        conn,
        _write_pack(
            tmp_path / "pack-a.zip",
            pack_id="pack-a",
            documents={
                "documents/primary.md": "Beam beam primary evidence",
                "documents/secondary.md": "Beam secondary evidence",
            },
        ),
    )
    index_pack(
        conn,
        _write_pack(
            tmp_path / "pack-b.zip",
            pack_id="pack-b",
            documents={"documents/guide.md": "Beam guidance from another pack"},
        ),
    )
    conn.close()

    multi = search_documents_multi(["pack-a", "pack-b"], "Beam", limit_per_pack=2, db_path=db_path)

    assert multi["pack-a"] == search_documents("pack-a", "Beam", limit=2, db_path=db_path)
    assert multi["pack-b"] == search_documents("pack-b", "Beam", limit=2, db_path=db_path)
    assert [match["path"] for match in multi["pack-a"]] == [
        "documents/primary.md",
        "documents/secondary.md",
    ]


def test_index_pack_invalidates_indexed_pack_cache(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    conn = connect(db_path)
    init_db(conn)
    index_pack(conn, _write_pack(tmp_path / "pack-a.zip", pack_id="pack-a"))

    assert pack_ids_with_documents(db_path) == frozenset({"pack-a"})

    index_pack(conn, _write_pack(tmp_path / "pack-b.zip", pack_id="pack-b"))
    index_pack(conn, _write_pack(tmp_path / "empty.zip", pack_id="empty-pack", documents={}))
    conn.close()

    assert pack_ids_with_documents(db_path) == frozenset({"pack-a", "pack-b"})


def test_search_tokens_preserve_frequency_while_query_terms_remain_unique() -> None:
    assert search_tokens("콘크리트 콘크리트 Beam Beam") == [
        "콘크리트",
        "콘크",
        "크리",
        "리트",
        "콘크리트",
        "콘크",
        "크리",
        "리트",
        "beam",
        "beam",
    ]
    assert query_terms("콘크리트 콘크리트 Beam Beam") == ["콘크리트", "콘크", "크리", "리트", "beam"]


def test_index_pack_populates_ready_bm25_and_ranks_term_frequency(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    conn = connect(db_path)
    init_db(conn)
    index_pack(
        conn,
        _write_pack(
            tmp_path / "pack-a.zip",
            pack_id="pack-a",
            documents={
                "documents/primary.md": "Beam Beam Beam primary evidence",
                "documents/secondary.md": "Beam secondary evidence",
            },
        ),
    )
    conn.close()

    status = bm25_index_status(db_path, ttl_seconds=0)
    results = search_documents_bm25_multi(["pack-a"], "Beam", limit_per_pack=2, db_path=db_path)

    assert status == {
        "available": True,
        "ready": True,
        "documentCount": 2,
        "indexedDocumentCount": 2,
    }
    assert results is not None
    assert [item["path"] for item in results["pack-a"]] == [
        "documents/primary.md",
        "documents/secondary.md",
    ]
    assert all(item["retrievalSource"] == "bm25" for item in results["pack-a"])


def test_rebuild_bm25_index_repairs_incomplete_coverage(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite3"
    conn = connect(db_path)
    init_db(conn)
    index_pack(conn, _write_pack(tmp_path / "pack.zip", pack_id="pack-a"))
    with conn:
        conn.execute("INSERT INTO documents_bm25(documents_bm25) VALUES ('delete-all')")
    conn.close()

    assert bm25_index_status(db_path, ttl_seconds=0)["ready"] is False

    rebuilt = rebuild_bm25_index(db_path, batch_size=1)

    assert rebuilt["available"] is True
    assert rebuilt["ready"] is True
    assert rebuilt["indexedDocumentCount"] == 1
