from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import (
    DATA_DIR,
    DATABASE_FOLDER,
    DB_PATH,
    EPHEMERAL_STORAGE,
    LEGACY_STRUCTURED_PACKS_DIR,
    PACKS_DIR,
    PROJECTS_FOLDER,
    ROOT,
    USE_STRUCTURED_DATA_DIR,
)

UPLOAD_DIR = PACKS_DIR


# Astryx theme-neutral palette (light) — @astryxdesign/theme-neutral 0.1.3
TYPE_COLORS = {
    "Document": "#6d9cfe",
    "Chunk": "#a3a3a3",
    "Module": "#63ab9d",
    "ModuleType": "#67a7b8",
    "Assembly": "#c0990e",
    "SinglePart": "#ff6f6c",
    "Material": "#737373",
    "Section": "#dd74f0",
    "Category": "#69ad67",
    "Element": "#0074e2",
    "Evidence": "#84c980",
}


@dataclass(frozen=True)
class PackFile:
    path: Path

    @property
    def id(self) -> str:
        return self.path.stem


def discover_pack_files(root: Path = ROOT) -> list[PackFile]:
    seen: set[Path] = set()
    packs: list[PackFile] = []
    search_dirs = (
        [PACKS_DIR, LEGACY_STRUCTURED_PACKS_DIR]
        if USE_STRUCTURED_DATA_DIR
        else [root, PACKS_DIR, root / "data" / "packs", LEGACY_STRUCTURED_PACKS_DIR]
    )
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*.zip"):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                packs.append(PackFile(resolved))
    return sorted(packs, key=lambda item: item.path.name.lower())


def _pack_scope_rank(pack: PackFile) -> tuple[int, float, str]:
    # Project-scoped Drive cache files are named "<project-id>__<zip-name>".
    # Prefer those over older flat uploads when the manifest pack_id matches.
    scoped = 1 if "__" in pack.path.name else 0
    try:
        modified = pack.path.stat().st_mtime
    except OSError:
        modified = 0
    return (scoped, modified, pack.path.name)


def unique_pack_files(root: Path = ROOT) -> list[PackFile]:
    packs_by_id: dict[str, PackFile] = {}
    for pack in discover_pack_files(root):
        try:
            pack_id = str(summarize_pack(pack)["id"])
        except Exception:
            pack_id = pack.id
        existing = packs_by_id.get(pack_id)
        if not existing or _pack_scope_rank(pack) > _pack_scope_rank(existing):
            packs_by_id[pack_id] = pack
    return sorted(packs_by_id.values(), key=lambda item: item.path.name.lower())


def _read_json(zf: zipfile.ZipFile, name: str) -> dict[str, Any] | None:
    if name not in zf.namelist():
        return None
    try:
        return json.loads(zf.read(name).decode("utf-8-sig"))
    except Exception:
        return None


def _iter_jsonl(zf: zipfile.ZipFile, name: str, limit: int | None = None) -> Iterable[dict[str, Any]]:
    if name not in zf.namelist():
        return
    with zf.open(name) as stream:
        count = 0
        for raw in stream:
            if limit is not None and count >= limit:
                break
            line = raw.decode("utf-8-sig", errors="replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
                count += 1
            except json.JSONDecodeError:
                continue


def _count_lines(zf: zipfile.ZipFile, name: str) -> int:
    if name not in zf.namelist():
        return 0
    with zf.open(name) as stream:
        return sum(1 for line in stream if line.strip())


def _label_for(obj: dict[str, Any], fallback: str) -> str:
    props = obj.get("properties") if isinstance(obj.get("properties"), dict) else {}
    candidates = [
        obj.get("label"),
        obj.get("title"),
        obj.get("name"),
        obj.get("module_id"),
        obj.get("module_type"),
        obj.get("assembly_mark"),
        obj.get("single_part_mark"),
        obj.get("material_name"),
        obj.get("section_name"),
        props.get("title"),
        props.get("name"),
        props.get("path"),
        fallback,
    ]
    for value in candidates:
        if value:
            return str(value)
    return fallback


def _kind_for(node_id: str, obj: dict[str, Any]) -> str:
    labels = obj.get("labels")
    if isinstance(labels, list) and labels:
        return str(labels[0])
    if ":module_type:" in node_id:
        return "ModuleType"
    if ":module:" in node_id:
        return "Module"
    if ":assembly:" in node_id:
        return "Assembly"
    if ":single_part:" in node_id:
        return "SinglePart"
    if ":material:" in node_id:
        return "Material"
    if ":section:" in node_id:
        return "Section"
    if "category" in obj or "category_name" in obj:
        return "Category"
    return "Element"


def _node(node_id: str, obj: dict[str, Any], pack_id: str) -> dict[str, Any]:
    kind = _kind_for(node_id, obj)
    size = 5
    if kind in {"Module", "Document"}:
        size = 11
    elif kind in {"Assembly", "Category"}:
        size = 8
    elif kind == "Chunk":
        size = 4
    return {
        "id": node_id,
        "label": _label_for(obj, node_id),
        "type": kind,
        "packId": pack_id,
        "size": size,
        "color": TYPE_COLORS.get(kind, "#737373"),
        "properties": obj.get("properties", obj),
    }


def _edge(source: str, target: str, relation: str, pack_id: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    key = f"{pack_id}:{source}:{relation}:{target}"
    return {
        "id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
        "source": source,
        "target": target,
        "label": relation,
        "relation": relation,
        "packId": pack_id,
        "properties": raw or {},
    }


def _display_filename_from_drive_cache(filename: str) -> str:
    if "__" not in filename:
        return filename
    return filename.rsplit("__", 1)[1] or filename


def _drive_scope_and_category_from_cache(filename: str) -> tuple[str | None, str | None]:
    parts = filename.split("__", 2)
    if len(parts) == 3 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None, None


def _common_category_from_drive_cache(filename: str) -> str | None:
    scope, category = _drive_scope_and_category_from_cache(filename)
    return category if scope == "_Common" else None


def _title_from_drive_filename(filename: str) -> str | None:
    display_name = _display_filename_from_drive_cache(filename)
    stem = Path(display_name).stem.strip()
    if not stem:
        return None
    return re.sub(r"[_-]+", " ", stem).strip() or None


# (mtime, size) 서명 기반 메모이즈 — 한 번의 동기화가 unique_pack_files/index_pack/
# list_packs 경로에서 같은 zip을 3회씩 재파싱하던 것을 제거한다.
_SUMMARIZE_CACHE: dict[str, tuple[tuple[float, int], dict[str, Any]]] = {}
_PACK_LOOKUP_CACHE: dict[str, PackFile] = {}


class _LazySharedDriveClient:
    """Create one Drive client only if this graph request has a cache miss.

    A project graph can span many lazy pack ZIPs.  Creating a client inside
    every ``find_pack`` call also creates fresh service-account credentials,
    which turns one graph request into one OAuth refresh per pack on a cold
    serverless worker.  This small proxy keeps the normal local-cache path
    free of Drive setup while sharing one authenticated client across all
    misses in the request.
    """

    def __init__(self) -> None:
        self._client: Any | None = None

    def download_file(self, file_id: str, target: Path) -> None:
        if self._client is None:
            from .google_drive_sync import GoogleDriveClient

            self._client = GoogleDriveClient.from_env()
        self._client.download_file(file_id, target)


def summarize_pack(pack: PackFile) -> dict[str, Any]:
    try:
        stat = pack.path.stat()
        cache_key = str(pack.path.resolve())
        signature = (stat.st_mtime, stat.st_size)
    except OSError:
        return _summarize_pack_uncached(pack)
    cached = _SUMMARIZE_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return copy.deepcopy(cached[1])
    summary = _summarize_pack_uncached(pack)
    _SUMMARIZE_CACHE[cache_key] = (signature, copy.deepcopy(summary))
    return summary


def _summarize_pack_uncached(pack: PackFile) -> dict[str, Any]:
    with zipfile.ZipFile(pack.path) as zf:
        manifest = _read_json(zf, "manifest.json") or {}
        build_summary = _read_json(zf, "06_reports/build_summary.json") or {}
        model_summary = _read_json(zf, "backdata/summary/model_summary.json") or {}
        names = zf.namelist()
        graph_nodes_path = manifest.get("entrypoints", {}).get("nodes", "graph/nodes.jsonl")
        graph_edges_path = manifest.get("entrypoints", {}).get("edges", "graph/edges.jsonl")

        counts = manifest.get("counts") or {}
        derived_node_count = sum(
            int(counts.get(key, 0) or 0)
            for key in (
                "module_type_count",
                "module_count",
                "assembly_count",
                "single_part_count",
                "material_count",
                "section_count",
                "documents",
                "chunks",
            )
        )
        # An explicit zero is meaningful for relationship-only shards.  Using
        # ``or`` here used to turn ``nodes: 0`` into ``chunks + documents`` and
        # made those shards look like they contained thousands of graph nodes.
        node_count = (
            int(counts.get("nodes") or 0)
            if "nodes" in counts
            else _count_lines(zf, graph_nodes_path) or derived_node_count
        )
        edge_count = (
            int(counts.get("edges") or 0)
            if "edges" in counts
            else _count_lines(zf, graph_edges_path) or _count_lines(zf, "backdata/jsonl/edges.jsonl")
        )
        document_count = (
            int(counts.get("documents") or 0)
            if "documents" in counts
            else len([name for name in names if name.startswith("documents/") and name.endswith(".md")])
        )

        pack_id = str(manifest.get("pack_id") or pack.id)
        source_key = pack_id.lower()
        source = "Revit IFC" if "revit" in source_key else "Advance Steel" if "advance" in source_key else "Ontology"
        validation = build_summary.get("validation_status") or "READY"
        display_filename = _display_filename_from_drive_cache(pack.path.name)
        drive_scope, drive_category = _drive_scope_and_category_from_cache(pack.path.name)
        common_category = _common_category_from_drive_cache(pack.path.name)
        project_category = drive_category if drive_scope and drive_scope != "_Common" else None
        drive_title = _title_from_drive_filename(pack.path.name)
        title = drive_title or manifest.get("title") or pack.id.replace("-", " ").title()

        return {
            "id": pack_id,
            "filename": pack.path.name,
            "displayFilename": display_filename,
            "displayName": title,
            "title": title,
            "commonCategory": common_category,
            "driveScope": drive_scope,
            "driveCategory": drive_category,
            "projectCategory": project_category,
            "commonScoped": pack.path.name.startswith("_Common__"),
            "format": manifest.get("format", "ontology-pack"),
            "description": manifest.get("description") or _extract_readme(zf),
            "source": source,
            "projectScoped": bool(drive_scope and drive_scope != "_Common"),
            "sizeBytes": pack.path.stat().st_size,
            "validationStatus": validation,
            "counts": {
                **counts,
                "nodes": node_count,
                "edges": edge_count,
                "documents": document_count,
            },
            "modelSummary": model_summary,
            "entrypoints": manifest.get("entrypoints") or manifest.get("documents") or {},
        }


def _extract_readme(zf: zipfile.ZipFile) -> str:
    if "README.md" not in zf.namelist():
        return ""
    text = zf.read("README.md").decode("utf-8-sig", errors="replace")
    return re.sub(r"\s+", " ", text.replace("#", "")).strip()[:280]


def list_packs(*, include_query_database: bool = True) -> list[dict[str, Any]]:
    registry_summaries: list[dict[str, Any]] = []
    if EPHEMERAL_STORAGE:
        if include_query_database or not _pack_registry_path().exists():
            _sync_registry_from_drive(include_database=include_query_database)
        registry_summaries = _registry_pack_summaries()
    packs = unique_pack_files()
    if packs:
        direct_summaries = [summarize_pack(pack) for pack in packs]
        if not EPHEMERAL_STORAGE:
            return direct_summaries
        return _merge_pack_summaries(registry_summaries, direct_summaries)
    if registry_summaries:
        return registry_summaries
    summaries = _db_pack_summaries()
    if summaries:
        return summaries
    registry_summaries = _registry_pack_summaries()
    if registry_summaries:
        return registry_summaries
    _sync_registry_from_drive(include_database=include_query_database)
    registry_summaries = _registry_pack_summaries()
    return registry_summaries or _db_pack_summaries()


def _pack_registry_path() -> Path:
    from .google_drive_sync import PACK_REGISTRY_FILENAME

    return DATA_DIR / DATABASE_FOLDER / PACK_REGISTRY_FILENAME


def _registry_payload() -> dict[str, Any]:
    try:
        payload = json.loads(_pack_registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _registry_pack_summaries() -> list[dict[str, Any]]:
    packs = _registry_payload().get("packs")
    if not isinstance(packs, list):
        return []
    return [copy.deepcopy(pack) for pack in packs if isinstance(pack, dict) and pack.get("id")]


def _merge_pack_summaries(
    registry_summaries: list[dict[str, Any]],
    direct_summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {str(summary["id"]): summary for summary in registry_summaries if summary.get("id")}
    for summary in direct_summaries:
        pack_id = str(summary.get("id") or "").strip()
        if pack_id:
            merged[pack_id] = summary
    return sorted(
        merged.values(),
        key=lambda item: (str(item.get("title") or "").casefold(), str(item.get("id") or "")),
    )


def _sync_registry_from_drive(*, include_database: bool = True) -> None:
    try:
        from .google_drive_sync import (
            google_drive_sync_enabled,
            sync_google_drive_registry_files,
            sync_google_drive_runtime_metadata,
        )

        if google_drive_sync_enabled():
            if include_database:
                sync_google_drive_registry_files()
            else:
                sync_google_drive_runtime_metadata()
    except Exception:
        return


def _db_connect(db_path: Path = DB_PATH) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _pack_summary_from_db_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        summary = json.loads(row["summary_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    return {
        **summary,
        "id": str(summary.get("id") or row["id"]),
        "filename": str(summary.get("filename") or row["filename"]),
        "displayFilename": str(summary.get("displayFilename") or summary.get("filename") or row["filename"]),
        "displayName": str(summary.get("displayName") or summary.get("title") or row["title"]),
        "title": str(summary.get("title") or row["title"]),
        "source": str(summary.get("source") or row["source"]),
        "validationStatus": str(summary.get("validationStatus") or row["validation_status"]),
        "counts": {
            "documents": int(counts.get("documents") or 0),
            "nodes": int(counts.get("nodes") or 0),
            "edges": int(counts.get("edges") or 0),
        },
    }


def _db_pack_summaries(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    conn = _db_connect(db_path)
    if not conn:
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, filename, title, source, validation_status, summary_json
            FROM packs
            ORDER BY title COLLATE NOCASE, filename COLLATE NOCASE
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [_pack_summary_from_db_row(row) for row in rows]


def _db_pack_summary(pack_id: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    conn = _db_connect(db_path)
    if not conn:
        return None
    try:
        row = conn.execute(
            """
            SELECT id, filename, title, source, validation_status, summary_json
            FROM packs
            WHERE id = ? OR filename = ?
            LIMIT 1
            """,
            (pack_id, pack_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return _pack_summary_from_db_row(row) if row else None


def find_pack(pack_id: str, *, drive_client: Any | None = None) -> PackFile:
    cached = _PACK_LOOKUP_CACHE.get(pack_id)
    if cached is not None and cached.path.exists():
        try:
            summary_id = str(summarize_pack(cached)["id"])
        except Exception:
            summary_id = cached.id
        if pack_id in {cached.id, cached.path.name, summary_id}:
            return cached
        _PACK_LOOKUP_CACHE.pop(pack_id, None)

    for pack in unique_pack_files():
        summary_id = pack.id
        try:
            summary_id = summarize_pack(pack)["id"]
        except Exception:
            pass
        for alias in {pack.id, pack.path.name, str(summary_id)}:
            _PACK_LOOKUP_CACHE[alias] = pack
        if pack.id == pack_id or pack.path.name == pack_id or summary_id == pack_id:
            return pack
    try:
        from .google_drive_sync import fetch_pack_file_from_drive

        fetched = PackFile(fetch_pack_file_from_drive(pack_id, client=drive_client))
        summary_id = str(summarize_pack(fetched)["id"])
        for alias in {fetched.id, fetched.path.name, summary_id}:
            _PACK_LOOKUP_CACHE[alias] = fetched
        if pack_id in {fetched.id, fetched.path.name, summary_id}:
            return fetched
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise RuntimeError(
            f"Ontology pack {pack_id!r} could not be restored from Google Drive."
        ) from exc
    raise FileNotFoundError(pack_id)


def _safe_zip_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part == ".." for part in normalized.split("/"))
    ):
        raise ValueError(f"Unsafe pack document path: {path}")
    return normalized


def list_pack_documents(
    pack_id: str,
    prefix: str = "documents/",
    suffix: str = ".md",
    limit: int = 200,
) -> dict[str, Any]:
    try:
        pack = find_pack(pack_id)
    except FileNotFoundError:
        return _list_pack_documents_from_db(pack_id, prefix=prefix, suffix=suffix, limit=limit)
    safe_prefix = _safe_zip_path(prefix) if prefix else ""
    safe_suffix = suffix or ""
    with zipfile.ZipFile(pack.path) as zf:
        documents = sorted(
            name
            for name in zf.namelist()
            if not name.endswith("/")
            and (not safe_prefix or name.startswith(safe_prefix))
            and (not safe_suffix or name.endswith(safe_suffix))
        )
    return {
        "pack_id": summarize_pack(pack)["id"],
        "prefix": safe_prefix,
        "suffix": safe_suffix,
        "count": len(documents),
        "documents": documents[: max(0, limit)],
        "truncated": len(documents) > max(0, limit),
    }


def read_pack_document(pack_id: str, path: str, max_chars: int = 12000) -> dict[str, Any]:
    try:
        pack = find_pack(pack_id)
    except FileNotFoundError:
        return _read_pack_document_from_db(pack_id, path, max_chars=max_chars)
    safe_path = _safe_zip_path(path)
    with zipfile.ZipFile(pack.path) as zf:
        if safe_path not in zf.namelist():
            raise FileNotFoundError(f"{pack_id}:{safe_path}")
        text = zf.read(safe_path).decode("utf-8-sig", errors="replace")
    clipped = text[: max(0, max_chars)]
    return {
        "pack_id": summarize_pack(pack)["id"],
        "path": safe_path,
        "content": clipped,
        "truncated": len(text) > len(clipped),
        "chars": len(text),
    }


def _list_pack_documents_from_db(
    pack_id: str,
    prefix: str = "documents/",
    suffix: str = ".md",
    limit: int = 200,
) -> dict[str, Any]:
    summary = _db_pack_summary(pack_id)
    if not summary:
        _sync_registry_from_drive()
        summary = _db_pack_summary(pack_id)
    if not summary:
        raise FileNotFoundError(pack_id)
    safe_prefix = _safe_zip_path(prefix) if prefix else ""
    safe_suffix = suffix or ""
    conn = _db_connect()
    if not conn:
        raise FileNotFoundError(pack_id)
    try:
        clauses = ["pack_id = ?"]
        params: list[Any] = [summary["id"]]
        if safe_prefix:
            clauses.append("path LIKE ?")
            params.append(f"{safe_prefix}%")
        if safe_suffix:
            clauses.append("path LIKE ?")
            params.append(f"%{safe_suffix}")
        rows = conn.execute(
            f"SELECT path FROM documents WHERE {' AND '.join(clauses)} ORDER BY path LIMIT ?",
            (*params, max(0, limit) + 1),
        ).fetchall()
    except sqlite3.Error as exc:
        raise FileNotFoundError(pack_id) from exc
    finally:
        conn.close()
    documents = [str(row["path"]) for row in rows]
    return {
        "pack_id": summary["id"],
        "prefix": safe_prefix,
        "suffix": safe_suffix,
        "count": len(documents),
        "documents": documents[: max(0, limit)],
        "truncated": len(documents) > max(0, limit),
        "source": "sqlite-index",
    }


def _read_pack_document_from_db(pack_id: str, path: str, max_chars: int = 12000) -> dict[str, Any]:
    summary = _db_pack_summary(pack_id)
    if not summary:
        _sync_registry_from_drive()
        summary = _db_pack_summary(pack_id)
    if not summary:
        raise FileNotFoundError(pack_id)
    safe_path = _safe_zip_path(path)
    conn = _db_connect()
    if not conn:
        raise FileNotFoundError(f"{pack_id}:{safe_path}")
    try:
        row = conn.execute(
            "SELECT title, body FROM documents WHERE pack_id = ? AND path = ? LIMIT 1",
            (summary["id"], safe_path),
        ).fetchone()
    except sqlite3.Error as exc:
        raise FileNotFoundError(f"{pack_id}:{safe_path}") from exc
    finally:
        conn.close()
    if not row:
        raise FileNotFoundError(f"{pack_id}:{safe_path}")
    text = str(row["body"] or "")
    clipped = text[: max(0, max_chars)]
    return {
        "pack_id": summary["id"],
        "path": safe_path,
        "title": row["title"],
        "content": clipped,
        "truncated": len(text) > len(clipped),
        "chars": len(text),
        "source": "sqlite-index",
    }


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "-", "None", "null"}:
        return None
    if re.fullmatch(r"-?\d+", value.replace(",", "")):
        return int(value.replace(",", ""))
    if re.fullmatch(r"-?\d+(?:\.\d+)?", value.replace(",", "")):
        return float(value.replace(",", ""))
    if (value.startswith("{") and value.endswith("}")) or (value.startswith("[") and value.endswith("]")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _parse_key_values(markdown: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for line in markdown.splitlines():
        match = re.match(r"^([A-Za-z0-9가-힣_ -]+):\s*(.+?)\s*$", line.strip())
        if not match:
            continue
        key = match.group(1).strip().replace(" ", "_")
        parsed[key] = _parse_scalar(match.group(2))
    return parsed


def _parse_summary_table(markdown: str) -> dict[str, Any]:
    aliases = {
        "객체 수": "element_count",
        "층 수": "storey_count",
        "카테고리 수": "category_count",
        "타입 수": "type_count",
    }
    parsed: dict[str, Any] = {}
    in_summary = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Summary"):
            in_summary = True
            continue
        if in_summary and stripped.startswith("## "):
            break
        if not in_summary or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"항목", "---"}:
            continue
        parsed[aliases.get(cells[0], cells[0])] = _parse_scalar(cells[1])
    return parsed


def _module_document_summary(path: str, content: str, include_content: bool = False) -> dict[str, Any]:
    module_id = Path(path).stem
    parsed = {**_parse_summary_table(content), **_parse_key_values(content)}
    module_type = parsed.get("module_type")
    if not module_type:
        suffix_match = re.search(r"-([A-Z]+)$", module_id)
        module_type = suffix_match.group(1) if suffix_match else None
    summary_match = re.search(r"\*\*요약\*\*:\s*(.+)", content)
    result = {
        "module_id": str(parsed.get("module_id") or module_id),
        "module_type": module_type,
        "summary": summary_match.group(1).strip() if summary_match else None,
        "assembly_count": parsed.get("assembly_count"),
        "single_part_count": parsed.get("single_part_count"),
        "element_count": parsed.get("element_count"),
        "total_weight_kg": parsed.get("total_weight_kg"),
        "total_length_m": parsed.get("total_length_m"),
        "evidence_path": path,
    }
    if include_content:
        result["evidence_content"] = content
        result["parsed"] = parsed
    return result


def list_modules(pack_id: str, limit: int = 300) -> dict[str, Any]:
    docs = list_pack_documents(pack_id, prefix="documents/modules/", suffix=".md", limit=limit)
    modules = []
    for path in docs["documents"]:
        try:
            document = read_pack_document(pack_id, path, max_chars=2400)
        except FileNotFoundError:
            continue
        modules.append(_module_document_summary(path, document["content"]))
    modules.sort(key=lambda item: item["module_id"])
    return {
        "pack_id": docs["pack_id"],
        "module_count": docs["count"],
        "modules": modules,
        "truncated": docs["truncated"],
    }


def get_module(pack_id: str, module_id: str, max_chars: int = 18000) -> dict[str, Any]:
    candidates = [
        f"documents/modules/{module_id}.md",
        f"documents/modules/{module_id.upper()}.md",
        f"documents/modules/{module_id.lower()}.md",
    ]
    docs = list_pack_documents(pack_id, prefix="documents/modules/", suffix=".md", limit=1000)["documents"]
    candidates.extend(path for path in docs if Path(path).stem.lower() == module_id.lower())

    for path in dict.fromkeys(candidates):
        try:
            document = read_pack_document(pack_id, path, max_chars=max_chars)
        except (FileNotFoundError, ValueError):
            continue
        result = _module_document_summary(path, document["content"], include_content=True)
        result["pack_id"] = document["pack_id"]
        result["truncated"] = document["truncated"]
        return result

    evidence = search_pack(pack_id, module_id, limit=3)
    graph = build_graph(pack_id, max_nodes=300, max_edges=500)
    matched_nodes = [
        node
        for node in graph["nodes"]
        if module_id.lower() in str(node.get("id", "")).lower()
        or module_id.lower() in str(node.get("label", "")).lower()
    ][:8]
    return {
        "pack_id": pack_id,
        "module_id": module_id,
        "evidence_path": evidence[0]["path"] if evidence else None,
        "evidence": evidence,
        "graph_nodes": matched_nodes,
    }


def list_assembly_marks(pack_id: str, limit: int = 300) -> dict[str, Any]:
    docs = list_pack_documents(pack_id, prefix="documents/assembly_marks/", suffix=".md", limit=limit)
    return {
        "pack_id": docs["pack_id"],
        "count": docs["count"],
        "assembly_marks": [
            {"mark": Path(path).stem, "evidence_path": path}
            for path in docs["documents"]
        ],
        "truncated": docs["truncated"],
    }


def get_assembly_mark(pack_id: str, mark: str, max_chars: int = 14000) -> dict[str, Any]:
    candidates = [
        f"documents/assembly_marks/{mark}.md",
        f"documents/assembly_marks/{mark.upper()}.md",
        f"documents/assembly_marks/{mark.lower()}.md",
    ]
    docs = list_pack_documents(pack_id, prefix="documents/assembly_marks/", suffix=".md", limit=2000)["documents"]
    candidates.extend(path for path in docs if Path(path).stem.lower() == mark.lower())
    for path in dict.fromkeys(candidates):
        try:
            document = read_pack_document(pack_id, path, max_chars=max_chars)
        except (FileNotFoundError, ValueError):
            continue
        return {
            "pack_id": document["pack_id"],
            "mark": Path(path).stem,
            "evidence_path": path,
            "content": document["content"],
            "parsed": _parse_key_values(document["content"]),
            "truncated": document["truncated"],
        }
    raise FileNotFoundError(f"{pack_id}:assembly_mark:{mark}")


def _extract_json_blocks(markdown: str) -> list[Any]:
    blocks = []
    for match in re.finditer(r"```json\s*(.*?)```", markdown, flags=re.S | re.I):
        try:
            blocks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return blocks


def get_fasteners(pack_id: str) -> dict[str, Any]:
    path = "documents/fasteners/model_fasteners.md"
    try:
        document = read_pack_document(pack_id, path, max_chars=60000)
    except FileNotFoundError:
        return {"pack_id": pack_id, "evidence_path": None, "bolt_quantity": None, "anchor_quantity": None}

    content = document["content"]
    parsed = _parse_key_values(content)
    json_blocks = _extract_json_blocks(content)
    bolts_by_spec: list[dict[str, Any]] = []
    for block in json_blocks:
        if isinstance(block, dict) and str(block.get("fastener_type", "")).lower() == "bolt":
            bolts_by_spec = block.get("by_spec") if isinstance(block.get("by_spec"), list) else []

    anchors_by_spec: dict[tuple[str, str, str], dict[str, Any]] = {}
    in_table = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("| kind | description | quantity | grade | standard |"):
            in_table = True
            continue
        if in_table and (not stripped.startswith("|") or stripped.startswith("| ---")):
            continue
        if in_table and stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 5:
                continue
            description, quantity, grade, standard = cells[1], _parse_scalar(cells[2]), cells[3], cells[4]
            if "anchor" not in standard.lower():
                continue
            key = (description, grade, standard)
            row = anchors_by_spec.setdefault(
                key,
                {"description": description, "grade": grade, "standard": standard, "quantity": 0},
            )
            row["quantity"] += int(quantity or 0)

    return {
        "pack_id": document["pack_id"],
        "bolt_quantity": parsed.get("bolt_quantity"),
        "anchor_quantity": parsed.get("anchor_quantity"),
        "fastener_total_quantity": parsed.get("fastener_total_quantity"),
        "bolts_by_spec": bolts_by_spec,
        "anchors_by_spec": sorted(anchors_by_spec.values(), key=lambda item: item["description"]),
        "evidence_path": path,
    }


def _parse_markdown_table(markdown: str, heading: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    in_section = False
    headers: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = heading.lower() in stripped.lower()
            headers = []
            continue
        if not in_section or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not headers:
            headers = cells
            continue
        if all(set(cell) <= {"-"} for cell in cells):
            continue
        if len(cells) != len(headers):
            continue
        rows.append({header: _parse_scalar(value) for header, value in zip(headers, cells)})
    return rows


def get_section_weight_index(pack_id: str) -> dict[str, Any]:
    path = "documents/section_weight_index.md"
    try:
        document = read_pack_document(pack_id, path, max_chars=100000)
    except FileNotFoundError:
        return {"pack_id": pack_id, "sections": [], "evidence_path": None}
    sections = _parse_markdown_table(document["content"], "All Sections")
    return {
        "pack_id": document["pack_id"],
        "sections": sections,
        "evidence_path": path,
        "section_count": len(sections),
    }


_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]+|[가-힣]+")
_QUERY_STOPWORDS = {
    "the", "and", "for", "with", "what", "which", "how", "are", "is", "of", "to",
    "this", "that", "from", "about", "tell", "show", "list", "give", "please",
    "설명", "알려", "무엇", "어떤", "어떻게", "그리고", "그것", "대해", "해줘", "주세요", "입니까", "인가요",
}


def search_tokens(text: str) -> list[str]:
    """Tokenize text into searchable words and Hangul bigrams, preserving frequency."""

    tokens: list[str] = []
    for token in _QUERY_TOKEN_RE.findall(text.lower()):
        if "가" <= token[0] <= "힣":  # Hangul run
            if 2 <= len(token) <= 4 and token not in _QUERY_STOPWORDS:
                tokens.append(token)
            tokens.extend(token[i : i + 2] for i in range(len(token) - 1))
        elif token.isdigit():
            tokens.append(token)
        elif len(token) >= 2 and token not in _QUERY_STOPWORDS:
            tokens.append(token)
    return tokens


def query_terms(query: str) -> list[str]:
    """Tokenize a query into unique searchable substrings.

    ASCII/number words are kept whole; Hangul runs are kept whole (when short) and
    also split into character bigrams so morphological variants (조사 등) still match
    document text. Without this, substring search requires the whole phrase verbatim,
    which makes natural-language Korean questions return nothing.
    """
    return list(dict.fromkeys(search_tokens(query)))


def score_terms(text_lower: str, terms: list[str]) -> float:
    """Score text by how many distinct query terms it contains, weighting longer
    terms and rewarding coverage of more distinct terms."""
    if not terms:
        return 0.0
    score = 0.0
    matched = 0
    for term in terms:
        count = text_lower.count(term)
        if count:
            matched += 1
            length_weight = 1.0 + 0.4 * (len(term) - 1)
            score += length_weight * (1.0 + 0.2 * min(count, 5))
    if not matched:
        return 0.0
    return score * (1.0 + 0.5 * matched)


def first_term_hit(text_lower: str, terms: list[str]) -> int:
    first = -1
    for term in terms:
        pos = text_lower.find(term)
        if pos >= 0 and (first < 0 or pos < first):
            first = pos
    return first


def search_packs(query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    packs = list_packs()
    terms = query_terms(query)
    if not terms:
        return packs[:limit]
    scored: list[tuple[float, dict[str, Any]]] = []
    for pack in packs:
        haystack = " ".join(
            [
                str(pack.get("id", "")),
                str(pack.get("title", "")),
                str(pack.get("source", "")),
                str(pack.get("description", "")),
            ]
        ).lower()
        score = score_terms(haystack, terms)
        if score > 0:
            scored.append((score, pack))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [pack for _, pack in scored][:limit]


def list_sources(pack_id: str | None = None) -> dict[str, Any]:
    if pack_id:
        try:
            packs = [summarize_pack(find_pack(pack_id))]
        except FileNotFoundError:
            summary = _db_pack_summary(pack_id)
            if not summary:
                _sync_registry_from_drive()
                summary = _db_pack_summary(pack_id)
            packs = [summary] if summary else []
    else:
        packs = list_packs()
    return {
        "count": len(packs),
        "sources": [
            {
                "pack_id": pack["id"],
                "title": pack["title"],
                "displayName": pack.get("displayName", pack["title"]),
                "displayFilename": pack.get("displayFilename", pack.get("filename")),
                "source": pack["source"],
                "documents": pack["counts"].get("documents"),
                "nodes": pack["counts"].get("nodes"),
                "edges": pack["counts"].get("edges"),
                "entrypoints": pack.get("entrypoints", {}),
            }
            for pack in packs
        ],
    }


def build_graph(pack_id: str, max_nodes: int = 900, max_edges: int = 1600) -> dict[str, Any]:
    try:
        pack = find_pack(pack_id)
        return build_graph_from_pack(pack, max_nodes=max_nodes, max_edges=max_edges)
    except FileNotFoundError:
        graph = _build_graph_from_db(pack_id, max_nodes=max_nodes, max_edges=max_edges)
        if graph:
            expected_counts = graph.get("pack", {}).get("counts", {})
            actual_stats = graph.get("stats", {})
            expected_graph_items = int(expected_counts.get("nodes") or 0) + int(expected_counts.get("edges") or 0)
            actual_graph_items = int(actual_stats.get("totalNodes") or 0) + int(actual_stats.get("totalEdges") or 0)
            if EPHEMERAL_STORAGE and expected_graph_items > 0 and actual_graph_items == 0:
                raise RuntimeError(
                    f"Graph payload for pack {pack_id!r} could not be restored from Drive."
                )
            return graph
        raise


def _node_size_for_type(node_type: str) -> int:
    if node_type in {"Module", "Document"}:
        return 11
    if node_type in {"Assembly", "Category"}:
        return 8
    if node_type == "Chunk":
        return 4
    return 5


def _build_graph_from_db(pack_id: str, max_nodes: int = 900, max_edges: int = 1600) -> dict[str, Any] | None:
    summary = _db_pack_summary(pack_id)
    if not summary:
        _sync_registry_from_drive()
        summary = _db_pack_summary(pack_id)
    if not summary:
        return None
    conn = _db_connect()
    if not conn:
        return None
    try:
        total_nodes = int(conn.execute("SELECT COUNT(*) FROM nodes WHERE pack_id = ?", (summary["id"],)).fetchone()[0])
        total_edges = int(conn.execute("SELECT COUNT(*) FROM edges WHERE pack_id = ?", (summary["id"],)).fetchone()[0])
        node_rows = conn.execute(
            """
            SELECT id, label, type, properties_json
            FROM nodes
            WHERE pack_id = ?
            ORDER BY type, label
            LIMIT ?
            """,
            (summary["id"], max(0, max_nodes)),
        ).fetchall()
        visible_node_ids = {str(row["id"]) for row in node_rows}
        edge_rows = conn.execute(
            """
            SELECT id, source, target, relation, properties_json
            FROM edges
            WHERE pack_id = ?
            LIMIT ?
            """,
            (summary["id"], max(0, max_edges) * 4 + 200),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    nodes = []
    for row in node_rows:
        node_type = str(row["type"] or "Element")
        try:
            properties = json.loads(row["properties_json"] or "{}")
        except json.JSONDecodeError:
            properties = {}
        nodes.append(
            {
                "id": str(row["id"]),
                "label": str(row["label"] or row["id"]),
                "type": node_type,
                "packId": summary["id"],
                "size": _node_size_for_type(node_type),
                "color": TYPE_COLORS.get(node_type, "#737373"),
                "properties": properties,
            }
        )

    edges = []
    for row in edge_rows:
        source = str(row["source"] or "")
        target = str(row["target"] or "")
        if source not in visible_node_ids or target not in visible_node_ids:
            continue
        try:
            properties = json.loads(row["properties_json"] or "{}")
        except json.JSONDecodeError:
            properties = {}
        edges.append(
            {
                "id": str(row["id"]),
                "source": source,
                "target": target,
                "relation": str(row["relation"] or "related_to"),
                "label": str(row["relation"] or "related_to"),
                "packId": summary["id"],
                "properties": properties,
            }
        )
        if len(edges) >= max_edges:
            break

    return {
        "pack": summary,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "visibleNodes": len(nodes),
            "visibleEdges": len(edges),
            "totalNodes": total_nodes,
            "totalEdges": total_edges,
        },
        "source": "sqlite-index",
    }


@dataclass(frozen=True, slots=True)
class _MultiPackNodeCandidate:
    pack_id: str
    pack_order: int
    original_id: str
    merged_id: str
    pack_title: str
    payload: bytes


_MULTI_PACK_MAX_VISIBLE_NODES = 1_000
_MULTI_PACK_MAX_VISIBLE_EDGES = 2_000
_MULTI_PACK_MAX_SCANNED_NODES = 50_000
_MULTI_PACK_MAX_SCANNED_EDGES_PER_PACK = 20_000
# Vercel Functions reject response bodies above 4.5 MB. Keep a safety margin for
# response headers and the framework serializer, then trim unusually large node
# properties deterministically if the count limits alone are not sufficient.
_MULTI_PACK_MAX_RESPONSE_BYTES = 4_000_000


def _fair_limits(keys: list[str], total: int) -> dict[str, int]:
    """Split a hard result/scan budget deterministically across pack ids."""
    if not keys:
        return {}
    base, remainder = divmod(max(0, total), len(keys))
    return {key: base + (1 if index < remainder else 0) for index, key in enumerate(keys)}


def _capacity_aware_fair_limits(
    keys: list[str],
    total: int,
    capacities: dict[str, int],
) -> dict[str, int]:
    """Reassign unused fair shares from small shards to larger shards."""

    ordered_keys = list(dict.fromkeys(keys))
    limits = {key: 0 for key in ordered_keys}
    remaining = max(0, int(total))
    active = ordered_keys
    while active and remaining > 0:
        share = max(1, remaining // len(active))
        next_active: list[str] = []
        progressed = False
        for key in active:
            # Unknown or under-reported manifest counts must not starve a real
            # payload shard, so they retain access to the remaining hard budget.
            raw_capacity = int(capacities.get(key, 0) or 0)
            capacity = raw_capacity if raw_capacity > 0 else total
            available = max(0, capacity - limits[key])
            allocation = min(share, available, remaining)
            if allocation:
                limits[key] += allocation
                remaining -= allocation
                progressed = True
            if limits[key] < capacity and remaining > 0:
                next_active.append(key)
        if not progressed:
            break
        active = next_active
    return limits


def _multi_pack_entrypoints(zf: zipfile.ZipFile) -> tuple[str, str]:
    manifest = _read_json(zf, "manifest.json") or {}
    entrypoints = manifest.get("entrypoints") if isinstance(manifest.get("entrypoints"), dict) else {}
    return (
        str(entrypoints.get("nodes") or "graph/nodes.jsonl"),
        str(entrypoints.get("edges") or "graph/edges.jsonl"),
    )


def _multi_pack_payload_flags(pack: PackFile) -> tuple[bool, bool]:
    """Return actual (node, edge) payload presence, not manifest-derived counts."""
    with zipfile.ZipFile(pack.path) as zf:
        names = set(zf.namelist())
        nodes_path, edges_path = _multi_pack_entrypoints(zf)
        has_nodes = nodes_path in names and zf.getinfo(nodes_path).file_size > 0
        has_edges = edges_path in names and zf.getinfo(edges_path).file_size > 0
        if not has_nodes:
            has_nodes = any(
                path in names and zf.getinfo(path).file_size > 0
                for path in (
                    "backdata/jsonl/module_types.jsonl",
                    "backdata/jsonl/modules.jsonl",
                    "backdata/jsonl/materials.jsonl",
                    "backdata/jsonl/sections.jsonl",
                    "backdata/jsonl/assemblies.jsonl",
                    "backdata/jsonl/single_parts.jsonl",
                )
            )
        if not has_edges:
            has_edges = "backdata/jsonl/edges.jsonl" in names and zf.getinfo("backdata/jsonl/edges.jsonl").file_size > 0
        return has_nodes, has_edges


def _iter_jsonl_payloads(
    zf: zipfile.ZipFile,
    name: str,
    limit: int,
) -> Iterable[tuple[dict[str, Any], bytes]]:
    if name not in zf.namelist() or limit <= 0:
        return
    with zf.open(name) as stream:
        count = 0
        for raw in stream:
            if count >= limit:
                break
            line = raw.decode("utf-8-sig", errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield obj, line.encode("utf-8")
            count += 1


def _iter_multi_pack_nodes(pack: PackFile, limit: int) -> Iterable[tuple[dict[str, Any], bytes]]:
    if limit <= 0:
        return
    with zipfile.ZipFile(pack.path) as zf:
        names = set(zf.namelist())
        nodes_path, _ = _multi_pack_entrypoints(zf)
        if nodes_path in names:
            yield from _iter_jsonl_payloads(zf, nodes_path, limit)
            return

        remaining = limit
        for path in (
            "backdata/jsonl/module_types.jsonl",
            "backdata/jsonl/modules.jsonl",
            "backdata/jsonl/materials.jsonl",
            "backdata/jsonl/sections.jsonl",
            "backdata/jsonl/assemblies.jsonl",
            "backdata/jsonl/single_parts.jsonl",
        ):
            for obj, payload in _iter_jsonl_payloads(zf, path, remaining):
                yield obj, payload
                remaining -= 1
                if remaining <= 0:
                    return


def _iter_multi_pack_edges(pack: PackFile, limit: int) -> Iterable[dict[str, Any]]:
    if limit <= 0:
        return
    with zipfile.ZipFile(pack.path) as zf:
        names = set(zf.namelist())
        _, edges_path = _multi_pack_entrypoints(zf)
        if edges_path in names:
            yield from _iter_jsonl(zf, edges_path, limit)
            return
        yield from _iter_jsonl(zf, "backdata/jsonl/edges.jsonl", limit)


def _raw_node_id(obj: dict[str, Any]) -> str:
    value = obj.get("id") or obj.get("module_id") or obj.get("name")
    return "" if value is None else str(value).strip()


def _compact_json_bytes(payload: object) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _refresh_visible_graph_stats(payload: dict[str, Any]) -> None:
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    stats = payload.setdefault("stats", {})
    stats["visibleNodes"] = len(nodes)
    stats["visibleEdges"] = len(edges)

    node_pack_ids: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        node_pack_ids[str(node.get("id") or "")] = str(
            node.get("packId") or node.get("pack_id") or properties.get("pack_id") or ""
        )
    visible_cross_pack_edges = 0
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        edge_pack_id = str(edge.get("packId") or edge.get("pack_id") or "")
        source_pack_id = node_pack_ids.get(edge_endpoint_id(edge.get("source", "")), "")
        target_pack_id = node_pack_ids.get(edge_endpoint_id(edge.get("target", "")), "")
        if (
            edge_pack_id != source_pack_id
            or edge_pack_id != target_pack_id
            or source_pack_id != target_pack_id
        ):
            visible_cross_pack_edges += 1
    diagnostics = payload.setdefault("diagnostics", {})
    diagnostics["crossPackEdges"] = visible_cross_pack_edges
    diagnostics["visibleCrossPackEdges"] = visible_cross_pack_edges


def _compact_multi_pack_metadata(payload: dict[str, Any]) -> None:
    """Drop bulky optional summaries before refusing an oversized response."""

    compact_packs: list[dict[str, Any]] = []
    for pack in payload.get("packs", []):
        if not isinstance(pack, dict):
            continue
        compact_packs.append(
            {
                "id": str(pack.get("id") or "")[:512],
                "title": str(pack.get("title") or pack.get("displayName") or "")[:512],
                "displayName": str(pack.get("displayName") or "")[:512],
                "source": str(pack.get("source") or "")[:256],
                "counts": pack.get("counts") if isinstance(pack.get("counts"), dict) else {},
            }
        )
    payload["packs"] = compact_packs
    project = payload.get("project")
    if isinstance(project, dict):
        payload["project"] = {
            key: str(project.get(key) or "")[:512]
            for key in ("id", "name", "company", "manager", "discipline")
            if project.get(key) is not None
        }
    diagnostics = payload.setdefault("diagnostics", {})
    diagnostics["metadataCompacted"] = True
    diagnostics["ambiguousEndpoints"] = list(diagnostics.get("ambiguousEndpoints") or [])[:5]


def _fit_multi_pack_response_budget(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep graph JSON below the serverless response limit without dangling edges."""

    diagnostics = payload.setdefault("diagnostics", {})
    diagnostics.update(
        {
            "responseBudgetBytes": _MULTI_PACK_MAX_RESPONSE_BYTES,
            "responseTruncated": False,
            "responseBytes": 0,
        }
    )
    response_bytes = _compact_json_bytes(payload)
    while response_bytes > _MULTI_PACK_MAX_RESPONSE_BYTES and payload.get("nodes"):
        nodes = payload["nodes"]
        ratio = min(0.9, (_MULTI_PACK_MAX_RESPONSE_BYTES / response_bytes) * 0.95)
        keep_count = max(0, int(len(nodes) * ratio))
        if keep_count >= len(nodes):
            keep_count = len(nodes) - 1
        payload["nodes"] = nodes[:keep_count]
        visible_node_ids = {str(node.get("id") or "") for node in payload["nodes"]}
        payload["edges"] = [
            edge
            for edge in payload.get("edges", [])
            if edge_endpoint_id(edge.get("source", "")) in visible_node_ids
            and edge_endpoint_id(edge.get("target", "")) in visible_node_ids
        ]
        _refresh_visible_graph_stats(payload)
        diagnostics["responseTruncated"] = True
        response_bytes = _compact_json_bytes(payload)

    if response_bytes > _MULTI_PACK_MAX_RESPONSE_BYTES:
        _compact_multi_pack_metadata(payload)
        diagnostics["responseTruncated"] = True
        response_bytes = _compact_json_bytes(payload)

    diagnostics["responseBytes"] = response_bytes
    # Recording the byte count can add a handful of digits. Recalculate once so
    # the diagnostic describes the returned payload and preserve the hard bound.
    final_bytes = _compact_json_bytes(payload)
    diagnostics["responseBytes"] = final_bytes
    if final_bytes > _MULTI_PACK_MAX_RESPONSE_BYTES and payload.get("nodes"):
        payload["nodes"] = payload["nodes"][:-1]
        visible_node_ids = {str(node.get("id") or "") for node in payload["nodes"]}
        payload["edges"] = [
            edge
            for edge in payload.get("edges", [])
            if edge_endpoint_id(edge.get("source", "")) in visible_node_ids
            and edge_endpoint_id(edge.get("target", "")) in visible_node_ids
        ]
        _refresh_visible_graph_stats(payload)
        diagnostics["responseTruncated"] = True
        diagnostics["responseBytes"] = _compact_json_bytes(payload)
    if _compact_json_bytes(payload) > _MULTI_PACK_MAX_RESPONSE_BYTES:
        raise RuntimeError(
            "Project graph metadata exceeds the safe serverless response budget."
        )
    return payload


def _edge_endpoint_pack_hint(endpoint: Any, edge: dict[str, Any], role: str) -> str:
    if isinstance(endpoint, dict):
        for key in ("packId", "pack_id", "pack"):
            if endpoint.get(key):
                return str(endpoint[key])
    properties = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
    for container in (edge, properties):
        for key in (f"{role}PackId", f"{role}_pack_id", f"{role}Pack", f"{role}_pack"):
            if container.get(key):
                return str(container[key])
    return ""


def build_multi_pack_graph(
    pack_ids: list[str],
    *,
    title: str = "Project Graph",
    project: dict[str, Any] | None = None,
    max_nodes: int = 900,
    max_edges: int = 1600,
) -> dict[str, Any]:
    """Build a project graph in two passes across the selected pack ZIPs.

    Pass one indexes namespaced nodes from every node-bearing shard. Pass two
    resolves every scanned raw edge against that global endpoint index. This is
    deliberately separate from ``build_graph``: a relationship shard can have
    zero nodes and still connect nodes owned by other selected packs.
    """
    active_pack_ids = [pack_id for pack_id in dict.fromkeys(pack_ids) if pack_id]
    requested_max_nodes = max(0, int(max_nodes))
    requested_max_edges = max(0, int(max_edges))
    max_nodes = min(requested_max_nodes, _MULTI_PACK_MAX_VISIBLE_NODES)
    max_edges = min(requested_max_edges, _MULTI_PACK_MAX_VISIBLE_EDGES)
    empty_diagnostics = {
        "mode": "zip-two-pass",
        "requestedMaxNodes": requested_max_nodes,
        "requestedMaxEdges": requested_max_edges,
        "appliedMaxNodes": max_nodes,
        "appliedMaxEdges": max_edges,
        "nodeScanBudget": 0,
        "edgeScanBudget": 0,
        "scannedNodes": 0,
        "scannedEdges": 0,
        "duplicateNodes": 0,
        "unresolvedEdges": 0,
        "nodeLimitedEdges": 0,
        "ambiguousEndpointCount": 0,
        "ambiguousResolutionCount": 0,
        "ambiguousEndpoints": [],
        "crossPackEdges": 0,
        "visibleCrossPackEdges": 0,
        "nodeScanTruncated": False,
        "edgeScanTruncated": False,
    }
    if not active_pack_ids:
        return {
            "pack": {
                "id": project.get("id", "project") if project else "project",
                "title": title,
                "filename": "",
                "source": "Project",
                "validationStatus": "READY",
                "counts": {"nodes": 0, "edges": 0, "documents": 0},
            },
            "project": project,
            "packs": [],
            "activePackIds": [],
            "nodes": [],
            "edges": [],
            "stats": {"visibleNodes": 0, "visibleEdges": 0, "totalNodes": 0, "totalEdges": 0},
            "diagnostics": empty_diagnostics,
        }

    sources: list[tuple[str, PackFile, dict[str, Any], bool, bool]] = []
    seen_summary_ids: set[str] = set()
    drive_client = _LazySharedDriveClient()
    for requested_id in active_pack_ids:
        pack = find_pack(requested_id, drive_client=drive_client)
        summary = summarize_pack(pack)
        summary_id = str(summary["id"])
        if summary_id in seen_summary_ids:
            continue
        seen_summary_ids.add(summary_id)
        has_nodes, has_edges = _multi_pack_payload_flags(pack)
        sources.append((summary_id, pack, summary, has_nodes, has_edges))

    pack_summaries = [summary for _, _, summary, _, _ in sources]
    total_nodes = sum(int(summary.get("counts", {}).get("nodes") or 0) for summary in pack_summaries)
    total_edges = sum(int(summary.get("counts", {}).get("edges") or 0) for summary in pack_summaries)
    document_count = sum(int(summary.get("counts", {}).get("documents") or 0) for summary in pack_summaries)
    node_source_ids = [pack_id for pack_id, _, _, has_nodes, _ in sources if has_nodes]
    edge_source_ids = [pack_id for pack_id, _, _, _, has_edges in sources if has_edges]

    # Scan more nodes than can be displayed so relationship shards can select
    # connected endpoints, while keeping memory bounded on very large projects.
    node_scan_budget = min(
        _MULTI_PACK_MAX_SCANNED_NODES,
        max(max_nodes, max_nodes * 32, len(node_source_ids) * 64),
    ) if max_nodes else 0
    node_capacities = {
        pack_id: int(summary.get("counts", {}).get("nodes") or 0)
        for pack_id, _, summary, has_nodes, _ in sources
        if has_nodes
    }
    node_scan_limits = _capacity_aware_fair_limits(
        node_source_ids,
        node_scan_budget,
        node_capacities,
    )
    visible_node_limits = _fair_limits(node_source_ids, max_nodes)
    candidates_by_original: dict[str, list[_MultiPackNodeCandidate]] = {}
    candidates_by_merged: dict[str, _MultiPackNodeCandidate] = {}
    candidate_order: list[_MultiPackNodeCandidate] = []
    scanned_nodes_by_pack: dict[str, int] = {}
    duplicate_nodes = 0

    # Pass 1: collect the selected ZIPs' nodes before inspecting any edge.
    for pack_order, (pack_id, pack, summary, has_nodes, _) in enumerate(sources):
        if not has_nodes:
            continue
        scanned = 0
        for obj, payload in _iter_multi_pack_nodes(pack, node_scan_limits.get(pack_id, 0)):
            scanned += 1
            original_id = _raw_node_id(obj)
            if not original_id:
                continue
            merged_id = f"{pack_id}::{original_id}"
            if merged_id in candidates_by_merged:
                duplicate_nodes += 1
                continue
            # Keeping tens of thousands of nested property dicts is expensive
            # on a serverless worker. Store compact bytes and only materialize
            # the at-most ``max_nodes`` records that survive edge selection.
            candidate = _MultiPackNodeCandidate(
                pack_id,
                pack_order,
                original_id,
                merged_id,
                str(summary["title"]),
                payload,
            )
            candidates_by_merged[merged_id] = candidate
            candidates_by_original.setdefault(original_id, []).append(candidate)
            candidate_order.append(candidate)
        scanned_nodes_by_pack[pack_id] = scanned

    selected_nodes: dict[str, _MultiPackNodeCandidate] = {}
    selected_nodes_by_pack = {pack_id: 0 for pack_id in node_source_ids}
    merged_edges: list[dict[str, Any]] = []
    merged_edge_ids: set[str] = set()
    ambiguous_keys: set[tuple[str, str]] = set()
    ambiguous_examples: list[dict[str, Any]] = []
    ambiguous_resolution_count = 0
    unresolved_edges = 0
    node_limited_edges = 0
    visible_cross_pack_edges = 0
    scanned_edges_by_pack: dict[str, int] = {}

    def resolve_endpoint(endpoint: Any, edge: dict[str, Any], role: str, edge_pack_id: str) -> _MultiPackNodeCandidate | None:
        nonlocal ambiguous_resolution_count
        endpoint_id = edge_endpoint_id(endpoint).strip()
        if not endpoint_id:
            return None
        exact = candidates_by_merged.get(endpoint_id)
        if exact:
            return exact
        candidates = candidates_by_original.get(endpoint_id, [])
        if not candidates:
            return None
        hint = _edge_endpoint_pack_hint(endpoint, edge, role)
        if hint:
            hinted = [candidate for candidate in candidates if candidate.pack_id == hint]
            if hinted:
                return hinted[0]
        local = [candidate for candidate in candidates if candidate.pack_id == edge_pack_id]
        if local:
            return local[0]
        if len(candidates) == 1:
            return candidates[0]

        # Input pack order, then namespaced id, is the stable tie breaker. The
        # ambiguity is surfaced rather than silently collapsing node identity.
        selected = min(candidates, key=lambda candidate: (candidate.pack_order, candidate.merged_id))
        ambiguous_resolution_count += 1
        ambiguity_key = (edge_pack_id, endpoint_id)
        if ambiguity_key not in ambiguous_keys:
            ambiguous_keys.add(ambiguity_key)
            if len(ambiguous_examples) < 20:
                ambiguous_examples.append(
                    {
                        "edgePackId": edge_pack_id,
                        "endpointId": endpoint_id,
                        "candidateNodeIds": [candidate.merged_id for candidate in candidates[:8]],
                        "selectedNodeId": selected.merged_id,
                    }
                )
        return selected

    def select_edge_nodes(source: _MultiPackNodeCandidate, target: _MultiPackNodeCandidate) -> bool:
        additions = {
            candidate.merged_id: candidate
            for candidate in (source, target)
            if candidate.merged_id not in selected_nodes
        }
        if len(selected_nodes) + len(additions) > max_nodes:
            return False
        additions_by_pack: dict[str, int] = {}
        for candidate in additions.values():
            additions_by_pack[candidate.pack_id] = additions_by_pack.get(candidate.pack_id, 0) + 1
        for pack_id, count in additions_by_pack.items():
            if selected_nodes_by_pack.get(pack_id, 0) + count > visible_node_limits.get(pack_id, 0):
                return False
        for candidate in additions.values():
            selected_nodes[candidate.merged_id] = candidate
            selected_nodes_by_pack[candidate.pack_id] = selected_nodes_by_pack.get(candidate.pack_id, 0) + 1
        return True

    # Pass 2: scan raw edges after the global node index exists. A fair output
    # quota prevents an early large shard from consuming the entire edge limit.
    edge_capacities = {
        pack_id: int(summary.get("counts", {}).get("edges") or 0)
        for pack_id, _, summary, _, has_edges in sources
        if has_edges
    }
    visible_edge_limits = _capacity_aware_fair_limits(
        edge_source_ids,
        max_edges,
        edge_capacities,
    )
    for pack_id, pack, _, _, has_edges in sources:
        if not has_edges:
            continue
        visible_edge_limit = visible_edge_limits.get(pack_id, 0)
        if visible_edge_limit <= 0:
            continue
        scan_limit = min(
            _MULTI_PACK_MAX_SCANNED_EDGES_PER_PACK,
            max(256, visible_edge_limit * 32),
        )
        accepted = 0
        scanned = 0
        for edge_index, raw_edge in enumerate(_iter_multi_pack_edges(pack, scan_limit)):
            scanned += 1
            source_endpoint = raw_edge.get("source", raw_edge.get("from", ""))
            target_endpoint = raw_edge.get("target", raw_edge.get("to", ""))
            source_id = edge_endpoint_id(source_endpoint).strip()
            target_id = edge_endpoint_id(target_endpoint).strip()
            source = resolve_endpoint(source_endpoint, raw_edge, "source", pack_id)
            target = resolve_endpoint(target_endpoint, raw_edge, "target", pack_id)
            if not source or not target:
                unresolved_edges += 1
                continue
            if not select_edge_nodes(source, target):
                node_limited_edges += 1
                continue
            relation = str(raw_edge.get("relation") or "related_to")
            edge = _edge(source_id, target_id, relation, pack_id, raw_edge)
            raw_edge_id = str(raw_edge.get("id") or edge["id"])
            merged_edge_id = f"{pack_id}::{raw_edge_id}"
            if merged_edge_id in merged_edge_ids:
                merged_edge_id = f"{merged_edge_id}::{edge_index}"
            merged_edge_ids.add(merged_edge_id)
            merged_edges.append(
                {
                    **edge,
                    "id": merged_edge_id,
                    "source": source.merged_id,
                    "target": target.merged_id,
                    "packId": pack_id,
                }
            )
            if pack_id != source.pack_id or pack_id != target.pack_id or source.pack_id != target.pack_id:
                visible_cross_pack_edges += 1
            accepted += 1
            if accepted >= visible_edge_limit or len(merged_edges) >= max_edges:
                break
        scanned_edges_by_pack[pack_id] = scanned
        if len(merged_edges) >= max_edges:
            break

    # Preserve useful isolated nodes too. First honour each pack's fair share,
    # then use any genuinely unused global slots in deterministic source order.
    for candidate in candidate_order:
        if len(selected_nodes) >= max_nodes:
            break
        if candidate.merged_id in selected_nodes:
            continue
        if selected_nodes_by_pack.get(candidate.pack_id, 0) >= visible_node_limits.get(candidate.pack_id, 0):
            continue
        selected_nodes[candidate.merged_id] = candidate
        selected_nodes_by_pack[candidate.pack_id] = selected_nodes_by_pack.get(candidate.pack_id, 0) + 1
    for candidate in candidate_order:
        if len(selected_nodes) >= max_nodes:
            break
        if candidate.merged_id not in selected_nodes:
            selected_nodes[candidate.merged_id] = candidate

    merged_nodes: list[dict[str, Any]] = []
    for candidate in selected_nodes.values():
        obj = json.loads(candidate.payload)
        raw_node = _node(candidate.original_id, obj, candidate.pack_id)
        properties = raw_node.get("properties") if isinstance(raw_node.get("properties"), dict) else {}
        merged_nodes.append(
            {
                **raw_node,
                "id": candidate.merged_id,
                "properties": {
                    **properties,
                    "original_id": candidate.original_id,
                    "pack_id": candidate.pack_id,
                    "pack_title": candidate.pack_title,
                },
            }
        )
    node_scan_truncated = any(
        int(summary.get("counts", {}).get("nodes") or 0) > scanned_nodes_by_pack.get(pack_id, 0)
        for pack_id, _, summary, has_nodes, _ in sources
        if has_nodes
    )
    edge_scan_truncated = any(
        int(summary.get("counts", {}).get("edges") or 0) > scanned_edges_by_pack.get(pack_id, 0)
        for pack_id, _, summary, _, has_edges in sources
        if has_edges
    )
    diagnostics = {
        "mode": "zip-two-pass",
        "requestedMaxNodes": requested_max_nodes,
        "requestedMaxEdges": requested_max_edges,
        "appliedMaxNodes": max_nodes,
        "appliedMaxEdges": max_edges,
        "nodeScanBudget": node_scan_budget,
        "edgeScanBudget": sum(
            min(_MULTI_PACK_MAX_SCANNED_EDGES_PER_PACK, max(256, visible_limit * 32))
            for visible_limit in visible_edge_limits.values()
            if visible_limit > 0
        ),
        "scannedNodes": sum(scanned_nodes_by_pack.values()),
        "scannedEdges": sum(scanned_edges_by_pack.values()),
        "duplicateNodes": duplicate_nodes,
        "unresolvedEdges": unresolved_edges,
        "nodeLimitedEdges": node_limited_edges,
        "ambiguousEndpointCount": len(ambiguous_keys),
        "ambiguousResolutionCount": ambiguous_resolution_count,
        "ambiguousEndpoints": ambiguous_examples,
        "crossPackEdges": visible_cross_pack_edges,
        "visibleCrossPackEdges": visible_cross_pack_edges,
        "nodeScanTruncated": node_scan_truncated,
        "edgeScanTruncated": edge_scan_truncated,
    }
    return _fit_multi_pack_response_budget({
        "pack": {
            "id": project.get("id", "project") if project else "project",
            "title": title,
            "filename": "",
            "source": "Project",
            "validationStatus": "READY",
            "counts": {"nodes": total_nodes, "edges": total_edges, "documents": document_count},
        },
        "project": project,
        "packs": pack_summaries,
        "activePackIds": active_pack_ids,
        "nodes": merged_nodes,
        "edges": merged_edges,
        "stats": {
            "visibleNodes": len(merged_nodes),
            "visibleEdges": len(merged_edges),
            "totalNodes": total_nodes,
            "totalEdges": total_edges,
        },
        "diagnostics": diagnostics,
        "source": "zip-two-pass",
    })


def list_nodes(pack_id: str, node_type: str | None = None, limit: int = 100) -> dict[str, Any]:
    graph = build_graph(pack_id, max_nodes=max(1000, limit * 5), max_edges=0)
    nodes = graph["nodes"]
    if node_type:
        nodes = [node for node in nodes if str(node.get("type", "")).lower() == node_type.lower()]
    return {
        "pack_id": graph["pack"]["id"],
        "count": len(nodes),
        "nodes": nodes[: max(0, limit)],
        "truncated": len(nodes) > max(0, limit),
    }


def search_nodes(pack_id: str, query: str, limit: int = 20) -> dict[str, Any]:
    terms = query_terms(query)
    if not terms:
        return {"pack_id": pack_id, "count": 0, "nodes": []}
    graph = build_graph(pack_id, max_nodes=5000, max_edges=0)
    scored: list[tuple[float, dict[str, Any]]] = []
    for node in graph["nodes"]:
        haystack = (
            str(node.get("id", "")) + " " + str(node.get("label", "")) + " "
            + json.dumps(node.get("properties", {}), ensure_ascii=False)
        ).lower()
        score = score_terms(haystack, terms)
        if score > 0:
            scored.append((score, node))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("type")) != "Module", len(str(item[1].get("label", "")))))
    matches = [node for _, node in scored]
    return {
        "pack_id": graph["pack"]["id"],
        "query": query,
        "count": len(matches),
        "nodes": matches[: max(0, limit)],
        "truncated": len(matches) > max(0, limit),
    }


def list_edges(
    pack_id: str,
    source: str | None = None,
    target: str | None = None,
    relation: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    graph = build_graph(pack_id, max_nodes=5000, max_edges=max(1000, limit * 5))
    edges = graph["edges"]
    if source:
        edges = [edge for edge in edges if edge_endpoint_id(edge["source"]) == source]
    if target:
        edges = [edge for edge in edges if edge_endpoint_id(edge["target"]) == target]
    if relation:
        edges = [edge for edge in edges if str(edge.get("relation", "")).lower() == relation.lower()]
    return {
        "pack_id": graph["pack"]["id"],
        "count": len(edges),
        "edges": edges[: max(0, limit)],
        "truncated": len(edges) > max(0, limit),
    }


def get_node_context(pack_id: str, node_id: str, limit: int = 50) -> dict[str, Any]:
    graph = build_graph(pack_id, max_nodes=5000, max_edges=12000)
    node_by_id = {node["id"]: node for node in graph["nodes"]}
    edges = [
        edge
        for edge in graph["edges"]
        if edge_endpoint_id(edge["source"]) == node_id or edge_endpoint_id(edge["target"]) == node_id
    ][: max(0, limit)]
    neighbor_ids = {
        endpoint
        for edge in edges
        for endpoint in (edge_endpoint_id(edge["source"]), edge_endpoint_id(edge["target"]))
        if endpoint and endpoint != node_id
    }
    return {
        "pack_id": graph["pack"]["id"],
        "node": node_by_id.get(node_id),
        "neighbors": [node_by_id[node] for node in sorted(neighbor_ids) if node in node_by_id],
        "edges": edges,
    }


def edge_endpoint_id(endpoint: Any) -> str:
    if isinstance(endpoint, str):
        return endpoint
    if isinstance(endpoint, dict):
        return str(endpoint.get("id", ""))
    return "" if endpoint is None else str(endpoint)


def build_graph_from_pack(pack: PackFile, max_nodes: int = 900, max_edges: int = 1600) -> dict[str, Any]:
    summary = summarize_pack(pack)
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    with zipfile.ZipFile(pack.path) as zf:
        if "graph/nodes.jsonl" in zf.namelist() and "graph/edges.jsonl" in zf.namelist():
            for obj in _iter_jsonl(zf, "graph/nodes.jsonl", max_nodes):
                node_id = str(obj.get("id"))
                nodes[node_id] = _node(node_id, obj, summary["id"])
            for obj in _iter_jsonl(zf, "graph/edges.jsonl", max_edges * 3):
                source = str(obj.get("source", ""))
                target = str(obj.get("target", ""))
                if source in nodes and target in nodes:
                    edges.append(_edge(source, target, str(obj.get("relation", "related_to")), summary["id"], obj))
                    if len(edges) >= max_edges:
                        break
        else:
            _build_producer_graph(zf, summary["id"], nodes, edges, max_nodes, max_edges)

    return {
        "pack": summary,
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {
            "visibleNodes": len(nodes),
            "visibleEdges": len(edges),
            "totalNodes": summary["counts"].get("nodes") or len(nodes),
            "totalEdges": summary["counts"].get("edges") or len(edges),
        },
    }


def _build_producer_graph(
    zf: zipfile.ZipFile,
    pack_id: str,
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    max_nodes: int,
    max_edges: int,
) -> None:
    node_sources = [
        ("backdata/jsonl/module_types.jsonl", 120),
        ("backdata/jsonl/modules.jsonl", 160),
        ("backdata/jsonl/materials.jsonl", 120),
        ("backdata/jsonl/sections.jsonl", 180),
        ("backdata/jsonl/assemblies.jsonl", 360),
        ("backdata/jsonl/single_parts.jsonl", 500),
    ]
    for path, limit in node_sources:
        for obj in _iter_jsonl(zf, path, limit):
            if len(nodes) >= max_nodes:
                break
            node_id = str(obj.get("id") or obj.get("module_id") or obj.get("name"))
            if node_id and node_id != "None":
                nodes[node_id] = _node(node_id, obj, pack_id)

    for obj in _iter_jsonl(zf, "backdata/jsonl/edges.jsonl", max_edges * 4):
        source = str(obj.get("from") or obj.get("source") or "")
        target = str(obj.get("to") or obj.get("target") or "")
        if source in nodes and target in nodes:
            edges.append(_edge(source, target, str(obj.get("relation", "related_to")), pack_id, obj))
            if len(edges) >= max_edges:
                return

    for node in list(nodes.values()):
        props = node["properties"]
        for key, relation in [
            ("module_id", "belongs_to_module"),
            ("assembly_id", "belongs_to_assembly"),
            ("material_id", "uses_material"),
            ("section_id", "uses_section"),
        ]:
            target_value = props.get(key)
            if target_value and target_value in nodes:
                edges.append(_edge(node["id"], str(target_value), relation, pack_id))
                if len(edges) >= max_edges:
                    return


def list_projects(*, include_query_database: bool = True) -> list[dict[str, Any]]:
    packs = (
        list_packs()
        if include_query_database
        else list_packs(include_query_database=False)
    )
    from .project_store import list_projects as list_stored_projects

    if EPHEMERAL_STORAGE:
        registry_projects = _registry_projects(packs)
        if registry_projects:
            return registry_projects
    try:
        stored_projects = list_stored_projects(packs)
    except sqlite3.Error:
        stored_projects = []
    projects = stored_projects or _registry_projects(packs)
    common_pack_ids = _common_pack_ids(packs)
    if not common_pack_ids:
        return projects
    return [
        {
            **project,
            "packIds": list(dict.fromkeys([*common_pack_ids, *project.get("packIds", [])])),
        }
        for project in projects
    ]


def _common_pack_ids(
    packs: list[dict[str, Any]],
    *,
    payload: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
) -> list[str]:
    from .google_drive_sync import COMMON_PROJECT_PACK_LINKS_KEY, PROJECT_PACK_LINKS_FILENAME

    registry_payload = payload if payload is not None else _registry_payload()
    if links is None:
        links_path = DATA_DIR / PROJECTS_FOLDER / PROJECT_PACK_LINKS_FILENAME
        try:
            raw_links = json.loads(links_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_links = {}
        project_links = raw_links if isinstance(raw_links, dict) else {}
    else:
        project_links = links

    candidates = [
        *project_links.get(COMMON_PROJECT_PACK_LINKS_KEY, []),
        *registry_payload.get("commonPackIds", []),
    ]
    valid_pack_ids = {str(pack.get("id")) for pack in packs if pack.get("id")}
    return list(
        dict.fromkeys(
            str(pack_id)
            for pack_id in candidates
            if str(pack_id) in valid_pack_ids
        )
    )


def _registry_projects(packs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .google_drive_sync import PROJECT_FOLDERS_FILENAME, PROJECT_PACK_LINKS_FILENAME

    payload = _registry_payload()
    raw_projects = payload.get("projects")
    registry_projects = (
        [project for project in raw_projects if isinstance(project, dict)]
        if isinstance(raw_projects, list)
        else []
    )
    by_folder_id = {
        str(project.get("driveFolderId")): project
        for project in registry_projects
        if project.get("driveFolderId")
    }
    by_project_id = {
        str(project.get("id")): project
        for project in registry_projects
        if project.get("id")
    }

    folders_path = DATA_DIR / PROJECTS_FOLDER / PROJECT_FOLDERS_FILENAME
    links_path = DATA_DIR / PROJECTS_FOLDER / PROJECT_PACK_LINKS_FILENAME
    try:
        raw_folders = json.loads(folders_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw_folders = []
    try:
        raw_links = json.loads(links_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw_links = {}
    folders = [folder for folder in raw_folders if isinstance(folder, dict)] if isinstance(raw_folders, list) else []
    links = raw_links if isinstance(raw_links, dict) else {}
    valid_pack_ids = {str(pack.get("id")) for pack in packs if pack.get("id")}
    common_pack_ids = _common_pack_ids(packs, payload=payload, links=links)

    project_rows: list[dict[str, Any]] = []
    if folders:
        for folder in folders:
            project_id = str(folder.get("projectId") or "").strip()
            if not project_id:
                continue
            source = by_folder_id.get(str(folder.get("folderId") or "")) or by_project_id.get(project_id) or {}
            project_rows.append(
                {
                    **source,
                    "id": project_id,
                    "name": str(folder.get("name") or project_id),
                    "driveFolderId": str(folder.get("folderId") or source.get("driveFolderId") or ""),
                    "packIds": links.get(project_id, source.get("packIds", [])),
                }
            )
    else:
        project_rows = [dict(project) for project in registry_projects]

    projects: list[dict[str, Any]] = []
    for project in project_rows:
        pack_ids = project.get("packIds")
        effective_pack_ids = (
            list(
                dict.fromkeys(
                    [
                        *common_pack_ids,
                        *(
                            str(pack_id)
                            for pack_id in pack_ids
                            if str(pack_id) in valid_pack_ids
                        ),
                    ]
                )
            )
            if isinstance(pack_ids, list)
            else common_pack_ids
        )
        projects.append(
            {
                "id": str(project.get("id") or ""),
                "name": str(project.get("name") or project.get("id") or ""),
                "company": str(project.get("company") or ""),
                "manager": str(project.get("manager") or ""),
                "discipline": str(project.get("discipline") or ""),
                "description": str(project.get("description") or ""),
                "role": str(project.get("role") or "Admin"),
                "driveFolderId": str(project.get("driveFolderId") or ""),
                "packIds": effective_pack_ids,
            }
        )
    return projects


def search_pack(pack_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
    try:
        pack = find_pack(pack_id)
    except FileNotFoundError:
        return _search_pack_from_db(pack_id, query, limit=limit)
    terms = query_terms(query)
    if not terms:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    with zipfile.ZipFile(pack.path) as zf:
        for info in zf.infolist():
            if not (info.filename.startswith("documents/") and info.filename.endswith(".md")):
                continue
            text = zf.read(info.filename).decode("utf-8-sig", errors="replace")
            lower = text.lower()
            score = score_terms(lower + " " + info.filename.lower(), terms)
            if score <= 0:
                continue
            hit = first_term_hit(lower, terms)
            if hit < 0:
                hit = 0
            start = max(0, hit - 120)
            end = min(len(text), hit + 260)
            scored.append(
                (
                    score,
                    {
                        "path": info.filename,
                        "title": Path(info.filename).stem,
                        "snippet": re.sub(r"\s+", " ", text[start:end]).strip(),
                        "score": round(score, 3),
                    },
                )
            )
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
                content = str(chunk.get("content") or chunk.get("text") or "")
                chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "").strip()
                title = str(chunk.get("title") or chunk.get("heading") or chunk_id or "Evidence chunk")
                metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
                haystack = " ".join(
                    [
                        content,
                        title,
                        chunk_id,
                        str(chunk.get("document_id") or chunk.get("parent_document_id") or ""),
                        json.dumps(metadata, ensure_ascii=False),
                    ]
                ).lower()
                score = score_terms(haystack, terms)
                if score <= 0:
                    continue
                hit = first_term_hit(content.lower(), terms)
                if hit < 0:
                    hit = 0
                start = max(0, hit - 120)
                end = min(len(content), hit + 260)
                scored.append(
                    (
                        score,
                        {
                            "path": f"cloud/chunks.jsonl#{chunk_id}" if chunk_id else "cloud/chunks.jsonl",
                            "title": title,
                            "snippet": re.sub(r"\s+", " ", content[start:end]).strip(),
                            "score": round(score, 3),
                            "source": "cloud-chunk",
                            "chunkId": chunk_id or None,
                            "documentId": chunk.get("document_id") or chunk.get("parent_document_id"),
                            "sourceUrl": chunk.get("source_url"),
                            "sourceRef": chunk.get("source_ref"),
                            "compactSourceRefs": chunk.get("compact_source_refs"),
                            "metadata": metadata,
                        },
                    )
                )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit]]


def _search_pack_from_db(pack_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
    terms = query_terms(query)
    if not terms:
        return []
    summary = _db_pack_summary(pack_id)
    if not summary:
        _sync_registry_from_drive()
        summary = _db_pack_summary(pack_id)
    if not summary:
        return []
    conn = _db_connect()
    if not conn:
        return []
    try:
        clauses = []
        params: list[Any] = [summary["id"]]
        for term in terms:
            like = f"%{term}%"
            clauses.append("(body LIKE ? OR title LIKE ? OR path LIKE ?)")
            params.extend([like, like, like])
        rows = conn.execute(
            "SELECT path, title, body FROM documents "
            f"WHERE pack_id = ? AND ({' OR '.join(clauses)})",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        text = str(row["body"] or "")
        lower = text.lower()
        score = score_terms(lower + " " + str(row["title"]).lower() + " " + str(row["path"]).lower(), terms)
        if score <= 0:
            continue
        hit = first_term_hit(lower, terms)
        if hit < 0:
            hit = 0
        start = max(0, hit - 120)
        end = min(len(text), hit + 260)
        scored.append(
            (
                score,
                {
                    "path": row["path"],
                    "title": row["title"],
                    "snippet": re.sub(r"\s+", " ", text[start:end]).strip(),
                    "score": round(score, 3),
                    "source": "sqlite-index",
                },
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit]]


def save_uploaded_pack(filename: str, content: bytes) -> dict[str, Any]:
    safe_name = Path(filename.replace("\\", "/")).name
    if not safe_name.lower().endswith(".zip"):
        raise ValueError("Only .zip ontology packs are accepted.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / safe_name
    target.write_bytes(content)
    try:
        summary = summarize_pack(PackFile(target))
        summary["_path"] = str(target)
        return summary
    except zipfile.BadZipFile as exc:
        target.unlink(missing_ok=True)
        raise ValueError("Uploaded file is not a valid ZIP archive.") from exc
