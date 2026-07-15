from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .pack_index import search_tokens


QUERY_DATABASE_FILENAME = "modular_ontology_query.sqlite3.gz"


def _remove_sqlite_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def _table_exists(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _source_bm25_is_complete(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "source", "documents_bm25"):
        return False
    document_count = int(conn.execute("SELECT COUNT(*) FROM source.documents").fetchone()[0])
    bm25_count = int(conn.execute("SELECT COUNT(*) FROM source.documents_bm25").fetchone()[0])
    return document_count == bm25_count


def _bm25_terms(text: object) -> str:
    return " ".join(search_tokens(str(text or "")))


def _rebuild_bm25(conn: sqlite3.Connection, batch_size: int = 1000) -> int:
    indexed = 0
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
        conn.executemany(
            """
            INSERT INTO documents_bm25(rowid, title_terms, path_terms, body_terms)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    int(row[0]),
                    _bm25_terms(row[2]),
                    _bm25_terms(row[1]),
                    _bm25_terms(row[3]),
                )
                for row in rows
            ],
        )
        indexed += len(rows)
        last_id = int(rows[-1][0])
    conn.execute("INSERT INTO documents_bm25(documents_bm25) VALUES ('optimize')")
    return indexed


def build_query_database(source_path: Path, target_path: Path) -> dict[str, Any]:
    """Build a compact MCP query database without graph node/edge payloads."""

    source_path = source_path.expanduser().resolve()
    target_path = target_path.expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if source_path == target_path:
        raise ValueError("Query database target must differ from the source database.")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_sqlite_files(target_path)
    conn = sqlite3.connect(target_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("ATTACH DATABASE ? AS source", (str(source_path),))
        conn.executescript(
            """
            CREATE TABLE packs (
              id TEXT PRIMARY KEY,
              filename TEXT NOT NULL,
              title TEXT NOT NULL,
              source TEXT NOT NULL,
              validation_status TEXT NOT NULL,
              summary_json TEXT NOT NULL,
              indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pack_id TEXT NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
              path TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              UNIQUE(pack_id, path)
            );

            CREATE TABLE nodes (
              id TEXT NOT NULL,
              pack_id TEXT NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              type TEXT NOT NULL,
              properties_json TEXT NOT NULL,
              PRIMARY KEY (pack_id, id)
            );

            CREATE TABLE edges (
              id TEXT NOT NULL,
              pack_id TEXT NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
              source TEXT NOT NULL,
              target TEXT NOT NULL,
              relation TEXT NOT NULL,
              properties_json TEXT NOT NULL,
              PRIMARY KEY (pack_id, id)
            );

            CREATE TABLE projects (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              company TEXT NOT NULL DEFAULT '',
              manager TEXT NOT NULL DEFAULT '',
              discipline TEXT NOT NULL DEFAULT '',
              description TEXT NOT NULL DEFAULT '',
              role TEXT NOT NULL DEFAULT 'Admin',
              drive_folder_id TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE project_packs (
              project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              pack_id TEXT NOT NULL,
              linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (project_id, pack_id)
            );

            CREATE VIRTUAL TABLE documents_bm25 USING fts5(
              title_terms,
              path_terms,
              body_terms,
              content='',
              tokenize='unicode61'
            );

            INSERT INTO packs (id, filename, title, source, validation_status, summary_json, indexed_at)
            SELECT id, filename, title, source, validation_status, summary_json, indexed_at FROM source.packs;
            INSERT INTO documents (id, pack_id, path, title, body)
            SELECT id, pack_id, path, title, body FROM source.documents;
            INSERT INTO projects (
              id, name, company, manager, discipline, description, role, drive_folder_id, created_at, updated_at
            )
            SELECT
              id, name, company, manager, discipline, description, role, drive_folder_id, created_at, updated_at
            FROM source.projects;
            INSERT INTO project_packs (project_id, pack_id, linked_at)
            SELECT project_id, pack_id, linked_at FROM source.project_packs;
            """
        )

        if _source_bm25_is_complete(conn):
            for table in (
                "documents_bm25_data",
                "documents_bm25_idx",
                "documents_bm25_docsize",
                "documents_bm25_config",
            ):
                conn.execute(f"DELETE FROM {table}")
                conn.execute(f"INSERT INTO {table} SELECT * FROM source.{table}")
            bm25_source = "copied"
        else:
            _rebuild_bm25(conn)
            bm25_source = "rebuilt"

        # Repeated pack reindexing leaves many valid but redundant FTS segments.
        # Merge them in the disposable snapshot so serverless size is stable even
        # when the persistent authoring database is fragmented.
        conn.execute("INSERT INTO documents_bm25(documents_bm25) VALUES ('optimize')")

        conn.executescript(
            """
            CREATE INDEX idx_documents_pack ON documents(pack_id);
            CREATE INDEX idx_nodes_pack_type ON nodes(pack_id, type);
            CREATE INDEX idx_edges_pack_relation ON edges(pack_id, relation);
            CREATE INDEX idx_project_packs_pack ON project_packs(pack_id);
            """
        )
        conn.commit()
        conn.execute("DETACH DATABASE source")
        conn.execute("VACUUM")
        counts = validate_query_database(target_path, connection=conn)
    finally:
        conn.close()

    return {
        "path": str(target_path),
        "bytes": target_path.stat().st_size,
        "bm25Source": bm25_source,
        **counts,
    }


def validate_query_database(
    path: Path,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, int]:
    close_after = connection is None
    conn = connection or sqlite3.connect(path)
    try:
        integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if integrity.casefold() != "ok":
            raise sqlite3.DatabaseError(f"Query database integrity check failed: {integrity}")
        counts = {
            "packs": int(conn.execute("SELECT COUNT(*) FROM packs").fetchone()[0]),
            "documents": int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]),
            "bm25Documents": int(conn.execute("SELECT COUNT(*) FROM documents_bm25").fetchone()[0]),
            "projects": int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]),
            "projectPacks": int(conn.execute("SELECT COUNT(*) FROM project_packs").fetchone()[0]),
        }
        if counts["documents"] != counts["bm25Documents"]:
            raise sqlite3.DatabaseError(
                "Query database BM25 coverage is incomplete: "
                f"documents={counts['documents']} bm25={counts['bm25Documents']}"
            )
        return counts
    finally:
        if close_after:
            conn.close()


def compress_query_database(source_path: Path, target_path: Path, *, level: int = 6) -> dict[str, int]:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.tmp")
    temp_path.unlink(missing_ok=True)
    try:
        with source_path.open("rb") as source, temp_path.open("wb") as raw_target:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_target, compresslevel=level, mtime=0) as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            raw_target.flush()
            os.fsync(raw_target.fileno())
        os.replace(temp_path, target_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return {"rawBytes": source_path.stat().st_size, "compressedBytes": target_path.stat().st_size}
