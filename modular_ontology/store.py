from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from .config import DB_PATH, EPHEMERAL_STORAGE
from .pack_index import (
    PackFile,
    build_graph_from_pack,
    first_term_hit,
    query_terms,
    search_tokens,
    score_terms,
    summarize_pack,
    unique_pack_files,
)


_INDEX_CACHE_TTL_SECONDS = 60.0
_INDEX_CACHE_LOCK = threading.RLock()
_INDEXED_PACK_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
_BM25_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    journal_mode = "MEMORY" if EPHEMERAL_STORAGE else "WAL"
    try:
        conn.execute(f"PRAGMA journal_mode={journal_mode};")
    except sqlite3.OperationalError as exc:
        conn.close()
        raise sqlite3.OperationalError(
            f"{exc} (db_path={path}, ephemeral={EPHEMERAL_STORAGE}, journal_mode={journal_mode})"
        ) from exc
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
    _ensure_bm25_schema(conn)
    conn.commit()


def _ensure_bm25_schema(conn: sqlite3.Connection) -> bool:
    existing = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'documents_bm25'"
    ).fetchone()
    if existing is not None:
        schema_sql = str(existing[0] or "").casefold().replace(" ", "")
        if "content=''" in schema_sql and "contentless_delete" not in schema_sql:
            return True
        conn.execute("DROP TABLE documents_bm25")
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE documents_bm25 USING fts5(
              title_terms,
              path_terms,
              body_terms,
              content='',
              tokenize='unicode61'
            )
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def index_all_packs(
    db_path: Path | None = None,
    *,
    prune_missing: bool = False,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        init_db(conn)
        indexed = []
        for pack in unique_pack_files():
            indexed.append(index_pack(conn, pack))
        pruned_pack_ids: list[str] = []
        if prune_missing:
            active_pack_ids = {str(item["id"]) for item in indexed}
            stale_rows = conn.execute("SELECT id FROM packs ORDER BY id").fetchall()
            pruned_pack_ids = [str(row["id"]) for row in stale_rows if str(row["id"]) not in active_pack_ids]
            if pruned_pack_ids:
                project_links_exist = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_packs'"
                ).fetchone() is not None
                bm25_available = _ensure_bm25_schema(conn)
                with conn:
                    for pack_id in pruned_pack_ids:
                        if bm25_available:
                            _delete_bm25_pack_rows(conn, pack_id)
                        if project_links_exist:
                            conn.execute("DELETE FROM project_packs WHERE pack_id = ?", (pack_id,))
                        conn.execute("DELETE FROM packs WHERE id = ?", (pack_id,))
        if _ensure_bm25_schema(conn):
            with conn:
                conn.execute("INSERT INTO documents_bm25(documents_bm25) VALUES ('optimize')")
            invalidate_index_caches(_connection_db_path(conn))
        stats: dict[str, Any] = index_stats(conn)
        stats["bm25"] = bm25_index_status(db_path, ttl_seconds=0)
        return {
            "status": "indexed",
            "packs": indexed,
            "prunedPackIds": pruned_pack_ids,
            "stats": stats,
        }
    finally:
        conn.close()


def index_pack(conn: sqlite3.Connection, pack: PackFile) -> dict[str, Any]:
    summary = summarize_pack(pack)
    pack_id = summary["id"]
    document_count, document_rows = _document_rows(pack, pack_id)
    graph_count, node_rows, edge_rows = _graph_rows(pack, pack_id)
    bm25_available = _ensure_bm25_schema(conn)

    with conn:
        conn.execute(
            """
            INSERT INTO packs (id, filename, title, source, validation_status, summary_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              filename = excluded.filename,
              title = excluded.title,
              source = excluded.source,
              validation_status = excluded.validation_status,
              summary_json = excluded.summary_json,
              indexed_at = CURRENT_TIMESTAMP
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
        if bm25_available:
            _delete_bm25_pack_rows(conn, pack_id)
        conn.execute("DELETE FROM edges WHERE pack_id = ?", (pack_id,))
        conn.execute("DELETE FROM nodes WHERE pack_id = ?", (pack_id,))
        conn.execute("DELETE FROM documents WHERE pack_id = ?", (pack_id,))
        conn.executemany(
            """
            INSERT INTO documents (pack_id, path, title, body)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pack_id, path) DO UPDATE SET title = excluded.title, body = excluded.body
            """,
            document_rows,
        )
        if bm25_available:
            _index_bm25_pack_rows(conn, pack_id)
        conn.executemany(
            """
            INSERT INTO nodes (id, pack_id, label, type, properties_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pack_id, id) DO UPDATE SET
              label = excluded.label,
              type = excluded.type,
              properties_json = excluded.properties_json
            """,
            node_rows,
        )
        conn.executemany(
            """
            INSERT INTO edges (id, pack_id, source, target, relation, properties_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pack_id, id) DO UPDATE SET
              source = excluded.source,
              target = excluded.target,
              relation = excluded.relation,
              properties_json = excluded.properties_json
            """,
            edge_rows,
        )
    invalidate_index_caches(_connection_db_path(conn))
    return {"id": pack_id, "documents": document_count, **graph_count}


def _document_rows(pack: PackFile, pack_id: str) -> tuple[int, list[tuple[str, str, str, str]]]:
    count = 0
    rows: list[tuple[str, str, str, str]] = []
    with zipfile.ZipFile(pack.path) as zf:
        for info in zf.infolist():
            if not (info.filename.startswith("documents/") and info.filename.endswith(".md")):
                continue
            body = zf.read(info.filename).decode("utf-8-sig", errors="replace")
            rows.append((pack_id, info.filename, Path(info.filename).stem, body))
            count += 1
        if "cloud/chunks.jsonl" in zf.namelist():
            for raw_line in zf.read("cloud/chunks.jsonl").decode("utf-8-sig", errors="replace").splitlines():
                if not raw_line.strip():
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "").strip()
                body = str(chunk.get("content") or chunk.get("text") or "")
                title = str(chunk.get("title") or chunk.get("heading") or chunk_id or "Evidence chunk")
                path = f"cloud/chunks.jsonl#{chunk_id}" if chunk_id else f"cloud/chunks.jsonl#{count + 1}"
                rows.append((pack_id, path, title, body))
                count += 1
    return count, rows


def _bm25_terms(text: object) -> str:
    return " ".join(search_tokens(str(text or "")))


def _index_bm25_pack_rows(conn: sqlite3.Connection, pack_id: str) -> int:
    rows = conn.execute(
        "SELECT id, path, title, body FROM documents WHERE pack_id = ? ORDER BY id",
        (pack_id,),
    ).fetchall()
    conn.executemany(
        """
        INSERT INTO documents_bm25(rowid, title_terms, path_terms, body_terms)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                int(row["id"]),
                _bm25_terms(row["title"]),
                _bm25_terms(row["path"]),
                _bm25_terms(row["body"]),
            )
            for row in rows
        ],
    )
    return len(rows)


def _delete_bm25_pack_rows(conn: sqlite3.Connection, pack_id: str) -> int:
    rows = conn.execute(
        "SELECT id, path, title, body FROM documents WHERE pack_id = ? ORDER BY id",
        (pack_id,),
    ).fetchall()
    conn.executemany(
        """
        INSERT INTO documents_bm25(documents_bm25, rowid, title_terms, path_terms, body_terms)
        VALUES ('delete', ?, ?, ?, ?)
        """,
        [
            (
                int(row["id"]),
                _bm25_terms(row["title"]),
                _bm25_terms(row["path"]),
                _bm25_terms(row["body"]),
            )
            for row in rows
        ],
    )
    return len(rows)


def _graph_rows(
    pack: PackFile, pack_id: str
) -> tuple[dict[str, int], list[tuple[str, str, str, str, str]], list[tuple[str, str, str, str, str, str]]]:
    graph = build_graph_from_pack(pack, max_nodes=5000, max_edges=12000)
    node_rows = [
        (
            node["id"],
            pack_id,
            node["label"],
            node["type"],
            json.dumps(node.get("properties", {}), ensure_ascii=False),
        )
        for node in graph["nodes"]
    ]
    edge_rows = []
    for edge in graph["edges"]:
        source = edge["source"]
        target = edge["target"]
        if isinstance(source, dict):
            source = source.get("id", "")
        if isinstance(target, dict):
            target = target.get("id", "")
        edge_rows.append(
            (
                edge["id"],
                pack_id,
                str(source),
                str(target),
                edge.get("relation") or edge.get("label") or "related_to",
                json.dumps(edge.get("properties", {}), ensure_ascii=False),
            )
        )
    return {"nodes": len(graph["nodes"]), "edges": len(graph["edges"])}, node_rows, edge_rows


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


def _connection_db_path(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return None
    raw_path = str(row[2] or "").strip()
    return Path(raw_path) if raw_path else None


def _db_cache_key(db_path: Path | None = None) -> str:
    return str((db_path or DB_PATH).expanduser().resolve())


def invalidate_index_caches(db_path: Path | None = None) -> None:
    """Invalidate cached index coverage after an index write completes."""

    with _INDEX_CACHE_LOCK:
        if db_path is None:
            _INDEXED_PACK_CACHE.clear()
            _BM25_STATUS_CACHE.clear()
        else:
            key = _db_cache_key(db_path)
            _INDEXED_PACK_CACHE.pop(key, None)
            _BM25_STATUS_CACHE.pop(key, None)


def pack_ids_with_documents(
    db_path: Path | None = None,
    *,
    ttl_seconds: float = _INDEX_CACHE_TTL_SECONDS,
) -> frozenset[str]:
    """Return packs with a completed pack row and at least one indexed document."""

    key = _db_cache_key(db_path)
    now = time.monotonic()
    with _INDEX_CACHE_LOCK:
        cached = _INDEXED_PACK_CACHE.get(key)
        if cached is not None and now - cached[0] < max(0.0, ttl_seconds):
            return cached[1]

    conn = connect(db_path)
    try:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT p.id
            FROM packs AS p
            JOIN documents AS d ON d.pack_id = p.id
            WHERE p.indexed_at IS NOT NULL
            """
        ).fetchall()
        pack_ids = frozenset(str(row[0]) for row in rows)
    finally:
        conn.close()

    with _INDEX_CACHE_LOCK:
        _INDEXED_PACK_CACHE[key] = (now, pack_ids)
    return pack_ids


def bm25_index_status(
    db_path: Path | None = None,
    *,
    ttl_seconds: float = _INDEX_CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    """Report whether the free local BM25 index fully covers indexed documents."""

    key = _db_cache_key(db_path)
    now = time.monotonic()
    with _INDEX_CACHE_LOCK:
        cached = _BM25_STATUS_CACHE.get(key)
        if cached is not None and now - cached[0] < max(0.0, ttl_seconds):
            return dict(cached[1])

    conn = connect(db_path)
    try:
        init_db(conn)
        available = _ensure_bm25_schema(conn)
        document_count = _count(conn, "documents")
        indexed_document_count = _count(conn, "documents_bm25") if available else 0
        status = {
            "available": available,
            "ready": available and indexed_document_count == document_count,
            "documentCount": document_count,
            "indexedDocumentCount": indexed_document_count,
        }
    finally:
        conn.close()

    with _INDEX_CACHE_LOCK:
        _BM25_STATUS_CACHE[key] = (now, status)
    return dict(status)


def rebuild_bm25_index(
    db_path: Path | None = None,
    *,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Backfill the contentless FTS5 BM25 index from existing document rows."""

    started = time.perf_counter()
    batch_size = max(1, min(5000, int(batch_size)))
    invalidate_index_caches(db_path)
    conn = connect(db_path)
    indexed = 0
    try:
        init_db(conn)
        if not _ensure_bm25_schema(conn):
            return {"available": False, "ready": False, "indexedDocumentCount": 0}
        with conn:
            conn.execute("INSERT INTO documents_bm25(documents_bm25) VALUES ('delete-all')")
        last_id = 0
        while True:
            rows = conn.execute(
                """
                SELECT id, path, title, body
                FROM documents
                WHERE id > ?
                ORDER BY id
                LIMIT ?
                """,
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            with conn:
                conn.executemany(
                    """
                    INSERT INTO documents_bm25(rowid, title_terms, path_terms, body_terms)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            int(row["id"]),
                            _bm25_terms(row["title"]),
                            _bm25_terms(row["path"]),
                            _bm25_terms(row["body"]),
                        )
                        for row in rows
                    ],
                )
            indexed += len(rows)
            last_id = int(rows[-1]["id"])
        with conn:
            conn.execute("INSERT INTO documents_bm25(documents_bm25) VALUES ('optimize')")
    finally:
        conn.close()
        invalidate_index_caches(db_path)

    status = bm25_index_status(db_path, ttl_seconds=0)
    return {
        **status,
        "indexedDocumentCount": indexed,
        "elapsedSeconds": round(time.perf_counter() - started, 3),
    }


def _bm25_match_query(terms: list[str]) -> str:
    return " OR ".join(f'"{term}"' for term in terms)


def search_documents_bm25_multi(
    pack_ids: list[str] | tuple[str, ...],
    query: str,
    limit_per_pack: int = 3,
    db_path: Path | None = None,
) -> dict[str, list[dict[str, Any]]] | None:
    """Search a ready FTS5 index; return None when BM25 is unavailable or incomplete."""

    unique_pack_ids = list(dict.fromkeys(str(pack_id) for pack_id in pack_ids if str(pack_id)))
    results: dict[str, list[dict[str, Any]]] = {pack_id: [] for pack_id in unique_pack_ids}
    terms = query_terms(query)[:250]
    limit_per_pack = max(0, int(limit_per_pack))
    if not unique_pack_ids or not terms or limit_per_pack == 0:
        return results
    if not bm25_index_status(db_path).get("ready"):
        return None

    match_query = _bm25_match_query(terms)
    candidate_limit = max(200, min(5000, len(unique_pack_ids) * limit_per_pack * 3))
    rows: list[sqlite3.Row] = []
    conn = connect(db_path)
    try:
        init_db(conn)
        for start in range(0, len(unique_pack_ids), 500):
            batch = unique_pack_ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows.extend(
                conn.execute(
                    """
                    SELECT d.pack_id, d.path, d.title, d.body,
                           bm25(documents_bm25, 5.0, 2.0, 1.0) AS bm25_rank
                    FROM documents_bm25
                    JOIN documents AS d ON d.id = documents_bm25.rowid
                    WHERE documents_bm25 MATCH ?
                    """
                    + f" AND d.pack_id IN ({placeholders})"
                    + " ORDER BY bm25_rank LIMIT ?",
                    [match_query, *batch, candidate_limit],
                ).fetchall()
            )
    finally:
        conn.close()

    scored_by_pack: dict[str, list[tuple[float, dict[str, Any]]]] = {
        pack_id: [] for pack_id in unique_pack_ids
    }
    for row in rows:
        raw_rank = float(row["bm25_rank"] or 0.0)
        score = max(0.0, -raw_rank)
        body = str(row["body"] or "")
        lower = body.lower()
        hit = first_term_hit(lower, terms)
        if hit < 0:
            hit = 0
        start = max(0, hit - 120)
        end = min(len(body), hit + 260)
        path = str(row["path"] or "")
        chunk_id = path.split("#", 1)[1] if path.startswith("cloud/chunks.jsonl#") and "#" in path else None
        scored_by_pack[str(row["pack_id"])].append(
            (
                score,
                {
                    "path": path,
                    "title": row["title"],
                    "snippet": re.sub(r"\s+", " ", body[start:end]).strip(),
                    "score": round(score, 6),
                    "source": "sqlite-bm25",
                    "retrievalSource": "bm25",
                    "chunkId": chunk_id,
                },
            )
        )
    for pack_id, scored in scored_by_pack.items():
        scored.sort(key=lambda item: item[0], reverse=True)
        results[pack_id] = [item[1] for item in scored[:limit_per_pack]]
    return results


def _score_document_row(row: sqlite3.Row, terms: list[str]) -> tuple[float, dict[str, Any]] | None:
    body = row["body"] or ""
    path = str(row["path"] or "")
    chunk_id = path.split("#", 1)[1] if path.startswith("cloud/chunks.jsonl#") and "#" in path else None
    lower = body.lower()
    score = score_terms(lower + " " + str(row["title"]).lower() + " " + path.lower(), terms)
    if score <= 0:
        return None
    hit = first_term_hit(lower, terms)
    if hit < 0:
        hit = 0
    start = max(0, hit - 120)
    end = min(len(body), hit + 260)
    return (
        score,
        {
            "path": path,
            "title": row["title"],
            "snippet": re.sub(r"\s+", " ", body[start:end]).strip(),
            "score": round(score, 3),
            "source": "sqlite-index",
            "chunkId": chunk_id,
        },
    )


def search_documents_multi(
    pack_ids: list[str] | tuple[str, ...],
    query: str,
    limit_per_pack: int = 3,
    db_path: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Search many packs with bounded SQLite queries and one connection."""

    unique_pack_ids = list(dict.fromkeys(str(pack_id) for pack_id in pack_ids if str(pack_id)))
    results: dict[str, list[dict[str, Any]]] = {pack_id: [] for pack_id in unique_pack_ids}
    # Cap natural-language query expansion so SQLite stays below its variable limit.
    terms = query_terms(query)[:250]
    limit_per_pack = max(0, int(limit_per_pack))
    if not unique_pack_ids or not terms or limit_per_pack == 0:
        return results

    term_clauses = []
    term_params: list[Any] = []
    for term in terms:
        like = f"%{term}%"
        term_clauses.append("(body LIKE ? OR title LIKE ? OR path LIKE ?)")
        term_params.extend([like, like, like])

    # Stay below common SQLite variable limits while never exceeding 500 pack IDs.
    batch_size = min(500, max(1, 900 - len(term_params)))
    rows: list[sqlite3.Row] = []
    conn = connect(db_path)
    try:
        init_db(conn)
        for start in range(0, len(unique_pack_ids), batch_size):
            batch = unique_pack_ids[start : start + batch_size]
            placeholders = ",".join("?" for _ in batch)
            sql = (
                "SELECT pack_id, path, title, body FROM documents "
                f"WHERE pack_id IN ({placeholders}) AND ({' OR '.join(term_clauses)})"
            )
            rows.extend(conn.execute(sql, [*batch, *term_params]).fetchall())
    finally:
        conn.close()

    scored_by_pack: dict[str, list[tuple[float, dict[str, Any]]]] = {
        pack_id: [] for pack_id in unique_pack_ids
    }
    for row in rows:
        scored = _score_document_row(row, terms)
        if scored is not None:
            scored_by_pack[str(row["pack_id"])].append(scored)
    for pack_id, scored in scored_by_pack.items():
        scored.sort(key=lambda item: item[0], reverse=True)
        results[pack_id] = [item[1] for item in scored[:limit_per_pack]]
    return results


def search_documents(pack_id: str, query: str, limit: int = 8, db_path: Path | None = None) -> list[dict[str, Any]]:
    terms = query_terms(query)
    if not terms:
        return []
    return search_documents_multi([pack_id], query, limit_per_pack=limit, db_path=db_path)[pack_id]


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
