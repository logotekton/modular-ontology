from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from .config import DB_PATH
from .pack_index import (
    PackFile,
    build_graph_from_pack,
    discover_pack_files,
    first_term_hit,
    query_terms,
    score_terms,
    summarize_pack,
)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS packs (
          id TEXT PRIMARY KEY,
          filename TEXT NOT NULL,
          title TEXT NOT NULL,
          source TEXT NOT NULL,
          validation_status TEXT NOT NULL,
          summary_json TEXT NOT NULL,
          indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS documents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          pack_id TEXT NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
          path TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          UNIQUE(pack_id, path)
        );

        CREATE TABLE IF NOT EXISTS nodes (
          id TEXT NOT NULL,
          pack_id TEXT NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
          label TEXT NOT NULL,
          type TEXT NOT NULL,
          properties_json TEXT NOT NULL,
          PRIMARY KEY (pack_id, id)
        );

        CREATE TABLE IF NOT EXISTS edges (
          id TEXT NOT NULL,
          pack_id TEXT NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
          source TEXT NOT NULL,
          target TEXT NOT NULL,
          relation TEXT NOT NULL,
          properties_json TEXT NOT NULL,
          PRIMARY KEY (pack_id, id)
        );

        CREATE INDEX IF NOT EXISTS idx_documents_pack ON documents(pack_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_pack_type ON nodes(pack_id, type);
        CREATE INDEX IF NOT EXISTS idx_edges_pack_relation ON edges(pack_id, relation);
        """
    )
    conn.commit()


def index_all_packs(db_path: Path | None = None) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        init_db(conn)
        indexed = []
        for pack in discover_pack_files():
            indexed.append(index_pack(conn, pack))
        return {"status": "indexed", "packs": indexed, "stats": index_stats(conn)}
    finally:
        conn.close()


def index_pack(conn: sqlite3.Connection, pack: PackFile) -> dict[str, Any]:
    summary = summarize_pack(pack)
    pack_id = summary["id"]
    with conn:
        conn.execute("DELETE FROM packs WHERE id = ?", (pack_id,))
        conn.execute(
            """
            INSERT INTO packs (id, filename, title, source, validation_status, summary_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                pack_id,
                summary["filename"],
                summary["title"],
                summary["source"],
                summary["validationStatus"],
                json.dumps(summary, ensure_ascii=False),
            ),
        )

    document_count = _index_documents(conn, pack, pack_id)
    graph_count = _index_graph(conn, pack, pack_id)
    return {"id": pack_id, "documents": document_count, **graph_count}


def _index_documents(conn: sqlite3.Connection, pack: PackFile, pack_id: str) -> int:
    count = 0
    with zipfile.ZipFile(pack.path) as zf:
        with conn:
            for info in zf.infolist():
                if not (info.filename.startswith("documents/") and info.filename.endswith(".md")):
                    continue
                body = zf.read(info.filename).decode("utf-8-sig", errors="replace")
                title = Path(info.filename).stem
                conn.execute(
                    """
                    INSERT INTO documents (pack_id, path, title, body)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(pack_id, path) DO UPDATE SET title = excluded.title, body = excluded.body
                    """,
                    (pack_id, info.filename, title, body),
                )
                count += 1
    return count


def _index_graph(conn: sqlite3.Connection, pack: PackFile, pack_id: str) -> dict[str, int]:
    graph = build_graph_from_pack(pack, max_nodes=5000, max_edges=12000)
    with conn:
        for node in graph["nodes"]:
            conn.execute(
                """
                INSERT INTO nodes (id, pack_id, label, type, properties_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pack_id, id) DO UPDATE SET
                  label = excluded.label,
                  type = excluded.type,
                  properties_json = excluded.properties_json
                """,
                (
                    node["id"],
                    pack_id,
                    node["label"],
                    node["type"],
                    json.dumps(node.get("properties", {}), ensure_ascii=False),
                ),
            )
        for edge in graph["edges"]:
            source = edge["source"]
            target = edge["target"]
            if isinstance(source, dict):
                source = source.get("id", "")
            if isinstance(target, dict):
                target = target.get("id", "")
            conn.execute(
                """
                INSERT INTO edges (id, pack_id, source, target, relation, properties_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(pack_id, id) DO UPDATE SET
                  source = excluded.source,
                  target = excluded.target,
                  relation = excluded.relation,
                  properties_json = excluded.properties_json
                """,
                (
                    edge["id"],
                    pack_id,
                    str(source),
                    str(target),
                    edge.get("relation") or edge.get("label") or "related_to",
                    json.dumps(edge.get("properties", {}), ensure_ascii=False),
                ),
            )
    return {"nodes": len(graph["nodes"]), "edges": len(graph["edges"])}


def index_stats(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    close_after = conn is None
    conn = conn or connect()
    try:
        init_db(conn)
        return {
            "packs": _count(conn, "packs"),
            "documents": _count(conn, "documents"),
            "nodes": _count(conn, "nodes"),
            "edges": _count(conn, "edges"),
        }
    finally:
        if close_after:
            conn.close()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def search_documents(pack_id: str, query: str, limit: int = 8, db_path: Path | None = None) -> list[dict[str, Any]]:
    terms = query_terms(query)
    if not terms:
        return []
    conn = connect(db_path)
    try:
        init_db(conn)
        clauses = []
        params: list[Any] = [pack_id]
        for term in terms:
            like = f"%{term}%"
            clauses.append("(body LIKE ? OR title LIKE ? OR path LIKE ?)")
            params.extend([like, like, like])
        sql = (
            "SELECT path, title, body FROM documents "
            f"WHERE pack_id = ? AND ({' OR '.join(clauses)})"
        )
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        body = row["body"] or ""
        lower = body.lower()
        score = score_terms(lower + " " + str(row["title"]).lower() + " " + str(row["path"]).lower(), terms)
        if score <= 0:
            continue
        hit = first_term_hit(lower, terms)
        if hit < 0:
            hit = 0
        start = max(0, hit - 120)
        end = min(len(body), hit + 260)
        scored.append(
            (
                score,
                {
                    "path": row["path"],
                    "title": row["title"],
                    "snippet": re.sub(r"\s+", " ", body[start:end]).strip(),
                    "score": round(score, 3),
                    "source": "sqlite-index",
                },
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit]]


def search_nodes(pack_id: str, query: str, limit: int = 8, db_path: Path | None = None) -> list[dict[str, Any]]:
    terms = query_terms(query)
    if not terms:
        return []
    conn = connect(db_path)
    try:
        init_db(conn)
        clauses = []
        params: list[Any] = [pack_id]
        for term in terms:
            like = f"%{term}%"
            clauses.append("(label LIKE ? OR id LIKE ? OR properties_json LIKE ?)")
            params.extend([like, like, like])
        sql = (
            "SELECT id, label, type, properties_json FROM nodes "
            f"WHERE pack_id = ? AND ({' OR '.join(clauses)})"
        )
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    type_rank = {"Module": 1, "Assembly": 2, "SinglePart": 3, "Document": 4}
    scored: list[tuple[float, int, int, dict[str, Any]]] = []
    for row in rows:
        try:
            properties = json.loads(row["properties_json"])
        except json.JSONDecodeError:
            properties = {}
        haystack = (str(row["label"]) + " " + str(row["id"]) + " " + str(row["properties_json"])).lower()
        score = score_terms(haystack, terms)
        if score <= 0:
            continue
        scored.append(
            (
                score,
                type_rank.get(row["type"], 5),
                len(str(row["label"] or "")),
                {
                    "id": row["id"],
                    "label": row["label"],
                    "type": row["type"],
                    "properties": properties,
                    "source": "sqlite-index",
                },
            )
        )
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in scored[:limit]]


def node_neighborhood(pack_id: str, node_id: str, limit: int = 12, db_path: Path | None = None) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT source, target, relation
            FROM edges
            WHERE pack_id = ? AND (source = ? OR target = ?)
            LIMIT ?
            """,
            (pack_id, node_id, node_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
