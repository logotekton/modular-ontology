from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DATA_DIR, DATABASE_FOLDER, ROOT, env


GRAPH_PREVIEW_VERSION = 2
GRAPH_PREVIEW_ALGORITHM = "multi-pack-graph-v2-order-sensitive"
GRAPH_PREVIEW_MAX_NODES = 1_000
GRAPH_PREVIEW_MAX_EDGES = 2_000
GRAPH_PREVIEW_LIVE_BUILD_MAX_PACKS = 16
DEFAULT_GRAPH_PREVIEW_DIR = ROOT / "snapshots" / "graph-previews"

_CACHE_LOCK = threading.Lock()
_GRAPH_CACHE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_INFLIGHT: dict[str, "_InflightGraphBuild"] = {}
_MANIFEST_CACHE: tuple[Path, int, dict[str, Any]] | None = None
_REGISTRY_GENERATION_CACHE: tuple[Path, int, str] | None = None


class GraphPreviewUnavailableError(RuntimeError):
    """A large graph selection cannot safely fall back to live ZIP fan-out."""


class GraphBuildInProgressError(RuntimeError):
    """A concurrent graph build did not complete within the bounded wait."""


class GraphBuildFailedError(RuntimeError):
    """A concurrent graph build failed and must not be repeated by followers."""


@dataclass
class _InflightGraphBuild:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None


def graph_preview_dir() -> Path:
    configured = str(env("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", "") or "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_GRAPH_PREVIEW_DIR


def normalize_graph_pack_ids(pack_ids: list[str]) -> list[str]:
    """Trim and deduplicate pack ids without changing graph-semantic order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_pack_id in pack_ids:
        pack_id = str(raw_pack_id).strip()
        if pack_id and pack_id not in seen:
            normalized.append(pack_id)
            seen.add(pack_id)
    return normalized


def graph_selection_key(pack_ids: list[str], max_nodes: int, max_edges: int) -> str:
    payload = {
        "algorithm": GRAPH_PREVIEW_ALGORITHM,
        "packIds": normalize_graph_pack_ids(pack_ids),
        "maxNodes": min(GRAPH_PREVIEW_MAX_NODES, max(0, int(max_nodes))),
        "maxEdges": min(GRAPH_PREVIEW_MAX_EDGES, max(0, int(max_edges))),
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def graph_pack_signature(
    pack_ids: list[str],
    pack_summaries: list[dict[str, Any]],
    *,
    registry_generation: str | None = None,
) -> str:
    if registry_generation is None:
        registry_generation = _runtime_registry_generation()
    summaries = {
        str(summary.get("id") or ""): summary
        for summary in pack_summaries
        if isinstance(summary, dict) and summary.get("id")
    }
    rows: list[dict[str, Any]] = []
    for pack_id in normalize_graph_pack_ids(pack_ids):
        summary = summaries.get(pack_id, {})
        drive = summary.get("drive") if isinstance(summary.get("drive"), dict) else {}
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        rows.append(
            {
                "id": pack_id,
                "fileId": str(drive.get("fileId") or summary.get("fileId") or ""),
                "modifiedTime": str(drive.get("modifiedTime") or ""),
                "sizeBytes": int(drive.get("sizeBytes") or summary.get("sizeBytes") or 0),
                "checksum": str(
                    drive.get("md5Checksum")
                    or drive.get("sha256Checksum")
                    or drive.get("checksum")
                    or summary.get("md5Checksum")
                    or summary.get("sha256Checksum")
                    or summary.get("checksum")
                    or summary.get("contentHash")
                    or ""
                ),
                "nodes": int(counts.get("nodes") or 0),
                "edges": int(counts.get("edges") or 0),
                "documents": int(counts.get("documents") or 0),
            }
        )
    encoded = json.dumps(
        {
            "algorithm": GRAPH_PREVIEW_ALGORITHM,
            "registryGeneration": str(registry_generation or ""),
            "packs": rows,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_registry_generation() -> str:
    global _REGISTRY_GENERATION_CACHE

    path = DATA_DIR / DATABASE_FOLDER / "pack_registry.json"
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return ""
    with _CACHE_LOCK:
        cached = _REGISTRY_GENERATION_CACHE
        if cached and cached[0] == path and cached[1] == mtime_ns:
            return cached[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    generation = str(payload.get("generatedAt") or payload.get("generation") or "")
    with _CACHE_LOCK:
        _REGISTRY_GENERATION_CACHE = (path, mtime_ns, generation)
    return generation


def _manifest() -> dict[str, Any]:
    global _MANIFEST_CACHE

    path = graph_preview_dir() / "manifest.json"
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    with _CACHE_LOCK:
        cached = _MANIFEST_CACHE
        if cached and cached[0] == path and cached[1] == mtime_ns:
            return cached[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != GRAPH_PREVIEW_VERSION
        or payload.get("algorithm") != GRAPH_PREVIEW_ALGORITHM
        or not isinstance(payload.get("entries"), dict)
    ):
        return {}
    with _CACHE_LOCK:
        _MANIFEST_CACHE = (path, mtime_ns, payload)
    return payload


def load_graph_preview(
    pack_ids: list[str],
    *,
    pack_summaries: list[dict[str, Any]],
    max_nodes: int,
    max_edges: int,
) -> dict[str, Any] | None:
    if str(env("MODULAR_ONTOLOGY_GRAPH_PREVIEWS", "1") or "1").strip().lower() in {"0", "false", "off", "no"}:
        return None

    key = graph_selection_key(pack_ids, max_nodes, max_edges)
    entry = _manifest().get("entries", {}).get(key)
    if not isinstance(entry, dict):
        return None
    expected_signature = graph_pack_signature(pack_ids, pack_summaries)
    if str(entry.get("packSignature") or "") != expected_signature:
        return None
    filename = str(entry.get("file") or "")
    if not filename or Path(filename).name != filename:
        return None
    path = graph_preview_dir() / filename
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if normalize_graph_pack_ids(payload.get("activePackIds") or []) != normalize_graph_pack_ids(pack_ids):
        return None
    return payload


def _cache_limits() -> tuple[int, float]:
    try:
        size = max(1, min(32, int(str(env("MODULAR_ONTOLOGY_GRAPH_CACHE_SIZE", "8")))))
    except (TypeError, ValueError):
        size = 8
    try:
        ttl = max(0.0, float(str(env("MODULAR_ONTOLOGY_GRAPH_CACHE_TTL_SECONDS", "900"))))
    except (TypeError, ValueError):
        ttl = 900.0
    return size, ttl


def _live_build_max_packs() -> int:
    try:
        return max(
            0,
            min(
                500,
                int(
                    str(
                        env(
                            "MODULAR_ONTOLOGY_GRAPH_LIVE_BUILD_MAX_PACKS",
                            str(GRAPH_PREVIEW_LIVE_BUILD_MAX_PACKS),
                        )
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        return GRAPH_PREVIEW_LIVE_BUILD_MAX_PACKS


def _inflight_wait_seconds() -> float:
    try:
        return max(
            0.0,
            min(
                60.0,
                float(str(env("MODULAR_ONTOLOGY_GRAPH_INFLIGHT_WAIT_SECONDS", "30"))),
            ),
        )
    except (TypeError, ValueError):
        return 30.0


def _cache_get(key: str) -> dict[str, Any] | None:
    _, ttl = _cache_limits()
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _GRAPH_CACHE.get(key)
        if cached is None:
            return None
        created_at, payload = cached
        if ttl <= 0 or now - created_at > ttl:
            _GRAPH_CACHE.pop(key, None)
            return None
        _GRAPH_CACHE.move_to_end(key)
        return payload


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    size, _ = _cache_limits()
    with _CACHE_LOCK:
        _GRAPH_CACHE[key] = (time.monotonic(), payload)
        _GRAPH_CACHE.move_to_end(key)
        while len(_GRAPH_CACHE) > size:
            _GRAPH_CACHE.popitem(last=False)


def _project_payload(
    payload: dict[str, Any],
    *,
    title: str,
    project: dict[str, Any] | None,
    cache_source: str,
) -> dict[str, Any]:
    pack = payload.get("pack") if isinstance(payload.get("pack"), dict) else {}
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    return {
        **payload,
        "pack": {
            **pack,
            "id": str(project.get("id") or "project") if project else str(pack.get("id") or "project"),
            "title": title,
        },
        "project": project,
        "diagnostics": {
            **diagnostics,
            "graphCache": cache_source,
        },
    }


def get_or_build_project_graph(
    pack_ids: list[str],
    *,
    pack_summaries: list[dict[str, Any]],
    title: str,
    project: dict[str, Any] | None,
    max_nodes: int,
    max_edges: int,
    builder: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    normalized_ids = normalize_graph_pack_ids(pack_ids)
    selection_key = graph_selection_key(normalized_ids, max_nodes, max_edges)
    pack_signature = graph_pack_signature(normalized_ids, pack_summaries)
    cache_key = f"{selection_key}:{pack_signature}"

    cached = _cache_get(cache_key)
    if cached is not None:
        return _project_payload(cached, title=title, project=project, cache_source="memory")

    preview = load_graph_preview(
        normalized_ids,
        pack_summaries=pack_summaries,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    if preview is not None:
        _cache_put(cache_key, preview)
        return _project_payload(preview, title=title, project=project, cache_source="preview")

    live_build_max_packs = _live_build_max_packs()
    if len(normalized_ids) > live_build_max_packs:
        raise GraphPreviewUnavailableError(
            "A precomputed graph preview is required for "
            f"{len(normalized_ids)} packs (live-build limit: {live_build_max_packs}), "
            "but the exact order/signature/limits preview is missing, disabled, or stale. "
            "Rebuild and deploy graph previews before loading this selection."
        )

    with _CACHE_LOCK:
        inflight = _INFLIGHT.get(cache_key)
        if inflight is None:
            inflight = _InflightGraphBuild()
            _INFLIGHT[cache_key] = inflight
            leader = True
        else:
            leader = False

    if not leader:
        if not inflight.event.wait(timeout=_inflight_wait_seconds()):
            raise GraphBuildInProgressError(
                "The same graph selection is still being built. Retry after the current build completes."
            )
        if inflight.error is not None:
            raise GraphBuildFailedError(
                "The concurrent graph build failed; this follower did not repeat the build."
            ) from inflight.error
        if inflight.result is not None:
            return _project_payload(
                inflight.result,
                title=title,
                project=project,
                cache_source="inflight",
            )
        raise GraphBuildFailedError(
            "The concurrent graph build completed without a reusable result."
        )

    try:
        payload = builder(
            normalized_ids,
            title=title,
            project=project,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
        _cache_put(cache_key, payload)
        with _CACHE_LOCK:
            inflight.result = payload
        return _project_payload(payload, title=title, project=project, cache_source="built")
    except BaseException as exc:
        with _CACHE_LOCK:
            inflight.error = exc
        raise
    finally:
        with _CACHE_LOCK:
            _INFLIGHT.pop(cache_key, None)
            inflight.event.set()


def invalidate_graph_cache() -> None:
    global _MANIFEST_CACHE, _REGISTRY_GENERATION_CACHE

    with _CACHE_LOCK:
        _GRAPH_CACHE.clear()
        _MANIFEST_CACHE = None
        _REGISTRY_GENERATION_CACHE = None
