from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import math
import os
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DB_PATH, ROOT, env
from .graph_preview import _runtime_registry_generation, normalize_graph_pack_ids
from .pack_index import (
    PackFile,
    _edge,
    _edge_endpoint_pack_hint,
    _iter_multi_pack_edges,
    _iter_multi_pack_nodes,
    _multi_pack_payload_flags,
    _node,
    _raw_node_id,
    edge_endpoint_id,
    find_pack,
    summarize_pack,
)


FULL_GRAPH_VERSION = 3
FULL_GRAPH_ALGORITHM = "full-project-graph-v3-grouped-content-addressed"
FULL_GRAPH_LAYOUT = "stable-pack-clusters-v1"
DEFAULT_FULL_GRAPH_DIR = ROOT / "snapshots" / "graph-full-v3"
DEFAULT_MAX_COMPRESSED_CHUNK_BYTES = 3_500_000
DEFAULT_TARGET_UNCOMPRESSED_CHUNK_BYTES = 24_000_000
DEFAULT_MAX_CHUNKS_PER_SELECTION = 32
DEFAULT_FULL_GRAPH_CAPABILITY_TTL_SECONDS = 10 * 60
MAX_FULL_GRAPH_CAPABILITY_TTL_SECONDS = 60 * 60

_MANIFEST_LOCK = threading.Lock()
_MANIFEST_CACHE: tuple[Path, int, dict[str, Any]] | None = None
_CHUNK_DIGEST_CACHE: dict[Path, tuple[int, int, str]] = {}


class FullGraphUnavailableError(RuntimeError):
    """The exact precomputed full-graph selection is missing or stale."""


class FullGraphChunkNotFoundError(FileNotFoundError):
    """A requested immutable chunk is not part of the authorized selection."""


@dataclass(frozen=True, slots=True)
class _NodeCandidate:
    pack_id: str
    pack_order: int
    original_id: str
    merged_id: str
    occurrence: int


def full_graph_dir() -> Path:
    configured = str(env("MODULAR_ONTOLOGY_FULL_GRAPH_DIR", "") or "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_FULL_GRAPH_DIR


def full_graph_selection_key(pack_ids: list[str]) -> str:
    payload = {
        "algorithm": FULL_GRAPH_ALGORITHM,
        "packIds": normalize_graph_pack_ids(pack_ids),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _full_graph_capability_secret() -> bytes | None:
    configured = env("MODULAR_ONTOLOGY_SESSION_SECRET") or env(
        "MODULAR_ONTOLOGY_AUTH_SECRET"
    )
    return str(configured).encode("utf-8") if configured else None


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _base64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _full_graph_capability_ttl_seconds() -> int:
    raw = env(
        "MODULAR_ONTOLOGY_FULL_GRAPH_CAPABILITY_TTL_SECONDS",
        str(DEFAULT_FULL_GRAPH_CAPABILITY_TTL_SECONDS),
    )
    try:
        ttl = int(str(raw))
    except (TypeError, ValueError):
        ttl = DEFAULT_FULL_GRAPH_CAPABILITY_TTL_SECONDS
    return max(1, min(ttl, MAX_FULL_GRAPH_CAPABILITY_TTL_SECONDS))


def issue_full_graph_capability(
    project_id: str,
    selection_key: str,
    *,
    now: int | None = None,
    ttl_seconds: int | None = None,
) -> str | None:
    """Issue a short-lived capability for one authorized graph selection."""

    secret = _full_graph_capability_secret()
    if not secret:
        return None
    issued_at = int(time.time()) if now is None else int(now)
    ttl = (
        _full_graph_capability_ttl_seconds()
        if ttl_seconds is None
        else max(1, min(int(ttl_seconds), MAX_FULL_GRAPH_CAPABILITY_TTL_SECONDS))
    )
    payload = _base64url_encode(
        json.dumps(
            {
                "exp": issued_at + ttl,
                "projectId": str(project_id),
                "selectionKey": str(selection_key),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signed = f"fg1.{payload}"
    signature = _base64url_encode(
        hmac.new(secret, signed.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{signed}.{signature}"


def validate_full_graph_capability(
    capability: str | None,
    project_id: str,
    selection_key: str,
    *,
    now: int | None = None,
) -> bool:
    """Validate a graph capability without hydrating runtime Drive metadata."""

    secret = _full_graph_capability_secret()
    token = str(capability or "")
    if not secret or not token or len(token) > 2_048:
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "fg1":
        return False
    signed = f"{parts[0]}.{parts[1]}"
    expected = _base64url_encode(
        hmac.new(secret, signed.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(parts[2], expected):
        return False
    try:
        payload = json.loads(_base64url_decode(parts[1]).decode("utf-8"))
        if not isinstance(payload, dict):
            return False
        expires_at = int(payload.get("exp") or 0)
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
    checked_at = int(time.time()) if now is None else int(now)
    return (
        expires_at > checked_at
        and str(payload.get("projectId") or "") == str(project_id)
        and str(payload.get("selectionKey") or "") == str(selection_key)
    )


def full_graph_pack_signature(
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
        counts = (
            summary.get("counts")
            if isinstance(summary.get("counts"), dict)
            else {}
        )
        rows.append(
            {
                "id": pack_id,
                "sizeBytes": int(summary.get("sizeBytes") or 0),
                "counts": {
                    "nodes": int(counts.get("nodes") or 0),
                    "edges": int(counts.get("edges") or 0),
                    "documents": int(counts.get("documents") or 0),
                },
            }
        )
    encoded = json.dumps(
        {
            "algorithm": FULL_GRAPH_ALGORITHM,
            "layout": FULL_GRAPH_LAYOUT,
            "registryGeneration": str(registry_generation or ""),
            "packs": rows,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest() -> dict[str, Any]:
    global _MANIFEST_CACHE

    path = full_graph_dir() / "manifest.json"
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {}
    with _MANIFEST_LOCK:
        cached = _MANIFEST_CACHE
        if cached and cached[0] == path and cached[1] == mtime_ns:
            return cached[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != FULL_GRAPH_VERSION
        or payload.get("algorithm") != FULL_GRAPH_ALGORITHM
        or not isinstance(payload.get("entries"), dict)
    ):
        return {}
    with _MANIFEST_LOCK:
        _MANIFEST_CACHE = (path, mtime_ns, payload)
    return payload


def invalidate_full_graph_manifest_cache() -> None:
    global _MANIFEST_CACHE

    with _MANIFEST_LOCK:
        _MANIFEST_CACHE = None
        _CHUNK_DIGEST_CACHE.clear()


def _manifest_entry(selection_key: str) -> dict[str, Any]:
    entry = _manifest().get("entries", {}).get(selection_key)
    if not isinstance(entry, dict):
        raise FullGraphUnavailableError(
            "The exact precomputed full graph is not available for this pack selection."
        )
    pack_ids = normalize_graph_pack_ids(entry.get("packIds") or [])
    if (
        str(entry.get("selectionKey") or "") != selection_key
        or full_graph_selection_key(pack_ids) != selection_key
    ):
        raise FullGraphUnavailableError("The full-graph manifest selection key is invalid.")
    chunks = entry.get("chunks")
    if not isinstance(chunks, list):
        raise FullGraphUnavailableError("The full-graph manifest has no valid chunk list.")
    return entry


def _validated_entry(
    selection_key: str,
    pack_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    entry = _manifest_entry(selection_key)
    pack_ids = normalize_graph_pack_ids(entry.get("packIds") or [])
    expected_signature = full_graph_pack_signature(pack_ids, pack_summaries)
    if str(entry.get("packSignature") or "") != expected_signature:
        raise FullGraphUnavailableError(
            "The precomputed full graph is stale for the current ontology pack generation."
        )
    return entry


def load_full_graph_entry(
    pack_ids: list[str],
    *,
    pack_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_ids = normalize_graph_pack_ids(pack_ids)
    selection_key = full_graph_selection_key(normalized_ids)
    entry = _validated_entry(selection_key, pack_summaries)
    if normalize_graph_pack_ids(entry.get("packIds") or []) != normalized_ids:
        raise FullGraphUnavailableError(
            "The precomputed full graph does not match the requested pack order."
        )
    return entry


def resolve_full_graph_chunk(
    selection_key: str,
    filename: str,
    *,
    pack_summaries: list[dict[str, Any]],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    entry = _validated_entry(selection_key, pack_summaries)
    return _resolve_full_graph_chunk(entry, filename)


def resolve_full_graph_chunk_from_manifest(
    selection_key: str,
    filename: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Resolve an immutable chunk after a capability authorizes its selection."""

    entry = _manifest_entry(selection_key)
    return _resolve_full_graph_chunk(entry, filename)


def _resolve_full_graph_chunk(
    entry: dict[str, Any],
    filename: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    safe_name = Path(str(filename or "")).name
    if not safe_name or safe_name != filename:
        raise FullGraphChunkNotFoundError(filename)
    descriptor = next(
        (
            item
            for item in entry.get("chunks") or []
            if isinstance(item, dict) and str(item.get("file") or "") == safe_name
        ),
        None,
    )
    if descriptor is None:
        raise FullGraphChunkNotFoundError(filename)
    digest = str(descriptor.get("sha256") or "")
    if not digest or safe_name != f"{digest}.json.gz":
        raise FullGraphChunkNotFoundError(filename)
    path = full_graph_dir() / safe_name
    if not path.is_file():
        raise FullGraphChunkNotFoundError(filename)
    try:
        stat = path.stat()
    except OSError as exc:
        raise FullGraphChunkNotFoundError(filename) from exc
    size = stat.st_size
    if size != int(descriptor.get("compressedBytes") or -1):
        raise FullGraphChunkNotFoundError(filename)
    with _MANIFEST_LOCK:
        cached_digest = _CHUNK_DIGEST_CACHE.get(path)
    if (
        cached_digest is not None
        and cached_digest[0] == stat.st_mtime_ns
        and cached_digest[1] == stat.st_size
    ):
        actual_digest = cached_digest[2]
    else:
        try:
            actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise FullGraphChunkNotFoundError(filename) from exc
        with _MANIFEST_LOCK:
            _CHUNK_DIGEST_CACHE[path] = (
                stat.st_mtime_ns,
                stat.st_size,
                actual_digest,
            )
    if actual_digest != digest:
        raise FullGraphChunkNotFoundError(filename)
    return path, descriptor, entry


def _stable_fraction(value: str, offset: int) -> float:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    integer = int.from_bytes(digest[offset : offset + 8], "big", signed=False)
    return integer / float((1 << 64) - 1)


def _node_position(
    pack_id: str,
    original_id: str,
    ordinal: int,
    *,
    pack_layout_index: int,
    pack_layout_count: int,
) -> tuple[float, float]:
    # The registry-wide sorted pack order is collision-free and identical for
    # every selection in one generation, so authored pack chunks remain reusable.
    columns = max(1, math.ceil(math.sqrt(max(1, pack_layout_count))))
    row, column = divmod(pack_layout_index, columns)
    center_x = (column - (columns - 1) / 2.0) * 12_000.0
    center_y = (row - (columns - 1) / 2.0) * 12_000.0
    angle = ordinal * math.pi * (3.0 - math.sqrt(5.0))
    radius = 20.0 + 13.0 * math.sqrt(ordinal + 1)
    jitter = (_stable_fraction(f"node:{pack_id}:{original_id}:{ordinal}", 0) - 0.5) * 4.0
    return (
        round(center_x + math.cos(angle) * (radius + jitter), 3),
        round(center_y + math.sin(angle) * (radius + jitter), 3),
    )


def _placeholder_position(placeholder_id: str) -> tuple[float, float]:
    angle = _stable_fraction(f"placeholder-angle:{placeholder_id}", 0) * math.tau
    radius = (
        250_000.0
        + _stable_fraction(f"placeholder-radius:{placeholder_id}", 8)
        * 10_000.0
    )
    return round(math.cos(angle) * radius, 3), round(math.sin(angle) * radius, 3)


def _compact_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _deterministic_gzip(payload: bytes) -> bytes:
    return gzip.compress(payload, compresslevel=6, mtime=0)


class _RecordChunkAccumulator:
    """Stream multiple pack iterators into one bounded, reusable chunk series."""

    def __init__(
        self,
        store: ContentAddressedChunkStore,
        kind: str,
    ) -> None:
        self.store = store
        self.kind = kind
        self.descriptors: list[dict[str, Any]] = []
        self.buffered: list[dict[str, Any]] = []
        self.buffered_bytes = 0
        self.finished = False

    def add_records(self, records: Iterable[dict[str, Any]]) -> None:
        if self.finished:
            raise RuntimeError("Cannot append to a finished full-graph chunk group.")
        for record in records:
            record_bytes = len(_compact_json_bytes(record)) + 1
            if (
                self.buffered
                and self.buffered_bytes + record_bytes
                > self.store.target_uncompressed_bytes
            ):
                self.descriptors.extend(
                    self.store._store_records(self.kind, self.buffered)
                )
                self.buffered = []
                self.buffered_bytes = 0
            self.buffered.append(record)
            self.buffered_bytes += record_bytes

    def finish(self) -> list[dict[str, Any]]:
        if not self.finished:
            if self.buffered:
                self.descriptors.extend(
                    self.store._store_records(self.kind, self.buffered)
                )
            self.buffered = []
            self.buffered_bytes = 0
            self.finished = True
        return list(self.descriptors)


class ContentAddressedChunkStore:
    def __init__(
        self,
        directory: Path,
        *,
        max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_CHUNK_BYTES,
        target_uncompressed_bytes: int = DEFAULT_TARGET_UNCOMPRESSED_CHUNK_BYTES,
    ) -> None:
        self.directory = directory
        self.max_compressed_bytes = max(256, int(max_compressed_bytes))
        self.target_uncompressed_bytes = max(128, int(target_uncompressed_bytes))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.unique_chunks: dict[str, dict[str, Any]] = {}

    def add_records(
        self,
        kind: str,
        records: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        accumulator = self.record_accumulator(kind)
        accumulator.add_records(records)
        return accumulator.finish()

    def record_accumulator(self, kind: str) -> _RecordChunkAccumulator:
        if kind not in {"nodes", "edges"}:
            raise ValueError(f"Unsupported full-graph chunk kind: {kind}")
        return _RecordChunkAccumulator(self, kind)

    def _store_records(
        self,
        kind: str,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        field = kind
        payload = {
            "version": FULL_GRAPH_VERSION,
            "kind": kind,
            field: records,
        }
        encoded = _compact_json_bytes(payload)
        compressed = _deterministic_gzip(encoded)
        if len(compressed) > self.max_compressed_bytes:
            if len(records) <= 1:
                raise RuntimeError(
                    "A single full-graph record exceeds the compressed chunk limit: "
                    f"{len(compressed)} > {self.max_compressed_bytes} bytes."
                )
            midpoint = len(records) // 2
            return [
                *self._store_records(kind, records[:midpoint]),
                *self._store_records(kind, records[midpoint:]),
            ]

        digest = hashlib.sha256(compressed).hexdigest()
        filename = f"{digest}.json.gz"
        target = self.directory / filename
        if target.exists():
            if target.read_bytes() != compressed:
                raise RuntimeError(f"Content-address collision for full-graph chunk {digest}.")
        else:
            temporary = target.with_suffix(f"{target.suffix}.tmp")
            temporary.write_bytes(compressed)
            os.replace(temporary, target)
        descriptor = {
            "file": filename,
            "kind": kind,
            "count": len(records),
            "compressedBytes": len(compressed),
            "uncompressedBytes": len(encoded),
            "sha256": digest,
        }
        self.unique_chunks.setdefault(digest, descriptor)
        return [descriptor]


def _placeholder_key(
    endpoint: Any,
    edge: dict[str, Any],
    role: str,
    edge_pack_id: str,
    edge_id: str,
) -> tuple[str, str, str]:
    endpoint_id = edge_endpoint_id(endpoint).strip()
    pack_hint = _edge_endpoint_pack_hint(endpoint, edge, role).strip()
    if endpoint_id:
        identity = json.dumps(
            {"endpointId": endpoint_id, "packHint": pack_hint},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    else:
        identity = json.dumps(
            {
                "edgeId": edge_id,
                "edgePackId": edge_pack_id,
                "packHint": pack_hint,
                "role": role,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"__external__::{digest}", endpoint_id, pack_hint


def _edge_identifier(
    raw_edge: dict[str, Any],
    *,
    pack_id: str,
    source_id: str,
    target_id: str,
    relation: str,
    edge_index: int,
    occurrences: dict[str, int],
) -> tuple[str, bool]:
    fallback = _edge(source_id, target_id, relation, pack_id, raw_edge)["id"]
    authored_id = str(raw_edge.get("id") or fallback)
    base_id = f"{pack_id}::{authored_id}"
    occurrence = occurrences.get(base_id, 0)
    occurrences[base_id] = occurrence + 1
    if occurrence == 0:
        return base_id, False
    return f"{base_id}::duplicate:{occurrence}:{edge_index}", True


def build_full_graph_selection(
    pack_ids: list[str],
    *,
    pack_summaries: list[dict[str, Any]],
    chunk_store: ContentAddressedChunkStore,
    pack_group_keys: Mapping[str, str] | None = None,
    project_names: Iterable[str] = (),
    registry_generation: str | None = None,
    find_pack_fn: Callable[[str], PackFile] = find_pack,
) -> dict[str, Any]:
    active_pack_ids = normalize_graph_pack_ids(pack_ids)
    registry_pack_ids = sorted(
        {
            str(summary.get("id") or "").strip()
            for summary in pack_summaries
            if isinstance(summary, dict) and summary.get("id")
        }
    )
    pack_layout_order = {
        pack_id: index
        for index, pack_id in enumerate(registry_pack_ids)
    }
    sources: list[tuple[str, int, PackFile, dict[str, Any], bool, bool]] = []
    for pack_order, requested_id in enumerate(active_pack_ids):
        pack = find_pack_fn(requested_id)
        summary = summarize_pack(pack)
        summary_id = str(summary.get("id") or requested_id)
        if summary_id != requested_id:
            raise RuntimeError(
                f"Requested full-graph pack {requested_id!r} resolved to {summary_id!r}."
            )
        has_nodes, has_edges = _multi_pack_payload_flags(pack)
        sources.append((summary_id, pack_order, pack, summary, has_nodes, has_edges))

    candidates_by_original: dict[str, list[_NodeCandidate]] = defaultdict(list)
    candidates_by_merged: dict[str, _NodeCandidate] = {}
    node_chunks: list[dict[str, Any]] = []
    node_accumulators: dict[str, _RecordChunkAccumulator] = {}
    authored_nodes = 0
    duplicate_authored_nodes = 0

    for pack_id, pack_order, pack, _summary, has_nodes, _has_edges in sources:
        if not has_nodes:
            continue
        group_key = str(
            (pack_group_keys or {}).get(pack_id)
            or f"pack:{pack_id}"
        )
        accumulator = node_accumulators.get(group_key)
        if accumulator is None:
            accumulator = chunk_store.record_accumulator("nodes")
            node_accumulators[group_key] = accumulator
        id_occurrences: dict[str, int] = {}

        def node_records() -> Iterable[dict[str, Any]]:
            nonlocal authored_nodes, duplicate_authored_nodes
            for ordinal, (obj, _payload) in enumerate(
                _iter_multi_pack_nodes(pack, 2_147_483_647)
            ):
                original_id = _raw_node_id(obj)
                if not original_id:
                    original_id = f"__anonymous_node__:{ordinal}"
                canonical_id = f"{pack_id}::{original_id}"
                occurrence = id_occurrences.get(canonical_id, 0)
                id_occurrences[canonical_id] = occurrence + 1
                merged_id = (
                    canonical_id
                    if occurrence == 0
                    else f"{canonical_id}::duplicate:{occurrence}"
                )
                if occurrence:
                    duplicate_authored_nodes += 1
                candidate = _NodeCandidate(
                    pack_id=pack_id,
                    pack_order=pack_order,
                    original_id=original_id,
                    merged_id=merged_id,
                    occurrence=occurrence,
                )
                candidates_by_merged[merged_id] = candidate
                if occurrence == 0:
                    candidates_by_merged[canonical_id] = candidate
                candidates_by_original[original_id].append(candidate)
                rendered = _node(original_id, obj, pack_id)
                x, y = _node_position(
                    pack_id,
                    original_id,
                    ordinal,
                    pack_layout_index=pack_layout_order.get(
                        pack_id,
                        len(pack_layout_order),
                    ),
                    pack_layout_count=max(
                        len(pack_layout_order),
                        len(active_pack_ids),
                    ),
                )
                authored_nodes += 1
                yield {
                    "id": merged_id,
                    "originalId": original_id,
                    "occurrence": occurrence,
                    "label": str(rendered.get("label") or original_id),
                    "type": str(rendered.get("type") or "Element"),
                    "packId": pack_id,
                    "x": x,
                    "y": y,
                    "size": float(rendered.get("size") or 5),
                }

        accumulator.add_records(node_records())

    for accumulator in node_accumulators.values():
        node_chunks.extend(accumulator.finish())

    placeholders: dict[str, dict[str, Any]] = {}
    placeholder_endpoint_references = 0
    unresolved_edges = 0
    unresolved_examples: list[dict[str, Any]] = []
    ambiguous_endpoint_keys: set[tuple[str, str]] = set()
    ambiguous_resolution_count = 0
    authored_edges = 0
    duplicate_authored_edges = 0
    edge_chunks: list[dict[str, Any]] = []
    edge_accumulators: dict[str, _RecordChunkAccumulator] = {}

    def resolve_endpoint(
        endpoint: Any,
        edge: dict[str, Any],
        role: str,
        edge_pack_id: str,
    ) -> tuple[_NodeCandidate | None, bool]:
        nonlocal ambiguous_resolution_count
        endpoint_id = edge_endpoint_id(endpoint).strip()
        if endpoint_id:
            exact = candidates_by_merged.get(endpoint_id)
            if exact is not None:
                return exact, False
        candidates = candidates_by_original.get(endpoint_id, ())
        if not candidates:
            return None, True
        hint = _edge_endpoint_pack_hint(endpoint, edge, role)
        if hint:
            hinted = [candidate for candidate in candidates if candidate.pack_id == hint]
            if hinted:
                return hinted[0], False
        local = [
            candidate for candidate in candidates if candidate.pack_id == edge_pack_id
        ]
        if local:
            return local[0], False
        if len(candidates) == 1:
            return candidates[0], False
        selected = min(
            candidates,
            key=lambda candidate: (
                candidate.pack_order,
                candidate.merged_id,
                candidate.occurrence,
            ),
        )
        ambiguous_resolution_count += 1
        ambiguous_endpoint_keys.add((edge_pack_id, endpoint_id))
        return selected, False

    for pack_id, _pack_order, pack, _summary, _has_nodes, has_edges in sources:
        if not has_edges:
            continue
        group_key = str(
            (pack_group_keys or {}).get(pack_id)
            or f"pack:{pack_id}"
        )
        accumulator = edge_accumulators.get(group_key)
        if accumulator is None:
            accumulator = chunk_store.record_accumulator("edges")
            edge_accumulators[group_key] = accumulator
        edge_id_occurrences: dict[str, int] = {}

        def edge_records() -> Iterable[dict[str, Any]]:
            nonlocal authored_edges
            nonlocal duplicate_authored_edges
            nonlocal placeholder_endpoint_references
            nonlocal unresolved_edges
            for edge_index, raw_edge in enumerate(
                _iter_multi_pack_edges(pack, 2_147_483_647)
            ):
                source_endpoint = raw_edge.get(
                    "source",
                    raw_edge.get("from", ""),
                )
                target_endpoint = raw_edge.get(
                    "target",
                    raw_edge.get("to", ""),
                )
                source_original = edge_endpoint_id(source_endpoint).strip()
                target_original = edge_endpoint_id(target_endpoint).strip()
                relation = str(raw_edge.get("relation") or "related_to")
                edge_id, duplicate = _edge_identifier(
                    raw_edge,
                    pack_id=pack_id,
                    source_id=source_original,
                    target_id=target_original,
                    relation=relation,
                    edge_index=edge_index,
                    occurrences=edge_id_occurrences,
                )
                if duplicate:
                    duplicate_authored_edges += 1
                source, source_missing = resolve_endpoint(
                    source_endpoint,
                    raw_edge,
                    "source",
                    pack_id,
                )
                target, target_missing = resolve_endpoint(
                    target_endpoint,
                    raw_edge,
                    "target",
                    pack_id,
                )
                edge_used_placeholder = source_missing or target_missing
                if edge_used_placeholder:
                    unresolved_edges += 1

                endpoints: dict[str, _NodeCandidate] = {}
                for role, endpoint, candidate, is_missing in (
                    ("source", source_endpoint, source, source_missing),
                    ("target", target_endpoint, target, target_missing),
                ):
                    if not is_missing and candidate is not None:
                        endpoints[role] = candidate
                        continue
                    placeholder_endpoint_references += 1
                    placeholder_id, endpoint_id, pack_hint = _placeholder_key(
                        endpoint,
                        raw_edge,
                        role,
                        pack_id,
                        edge_id,
                    )
                    if placeholder_id not in placeholders:
                        x, y = _placeholder_position(placeholder_id)
                        placeholders[placeholder_id] = {
                            "id": placeholder_id,
                            "label": endpoint_id or f"Missing {role} endpoint",
                            "type": "미해결참조",
                            "packId": pack_hint or "__external__",
                            "x": x,
                            "y": y,
                            "size": 4.0,
                            "placeholder": True,
                        }
                    endpoints[role] = _NodeCandidate(
                        pack_id=pack_hint or "__external__",
                        pack_order=len(active_pack_ids),
                        original_id=endpoint_id,
                        merged_id=placeholder_id,
                        occurrence=0,
                    )
                    if len(unresolved_examples) < 20:
                        unresolved_examples.append(
                            {
                                "edgeId": edge_id,
                                "edgePackId": pack_id,
                                "role": role,
                                "endpointId": endpoint_id,
                                "packHint": pack_hint,
                                "placeholderId": placeholder_id,
                            }
                        )

                authored_edges += 1
                yield {
                    "id": edge_id,
                    "source": endpoints["source"].merged_id,
                    "target": endpoints["target"].merged_id,
                    "relation": relation,
                    "packId": pack_id,
                }

        accumulator.add_records(edge_records())

    for accumulator in edge_accumulators.values():
        edge_chunks.extend(accumulator.finish())

    placeholder_chunks = chunk_store.add_records(
        "nodes",
        placeholders.values(),
    )
    chunks = [*node_chunks, *placeholder_chunks, *edge_chunks]
    stats = {
        "authoredNodes": authored_nodes,
        "placeholderNodes": len(placeholders),
        "totalNodes": authored_nodes + len(placeholders),
        "authoredEdges": authored_edges,
        "totalEdges": authored_edges,
        "resolvedEdges": authored_edges - unresolved_edges,
        "unresolvedEdges": unresolved_edges,
        "placeholderEndpointReferences": placeholder_endpoint_references,
    }
    return {
        "selectionKey": full_graph_selection_key(active_pack_ids),
        "packIds": active_pack_ids,
        "packCount": len(active_pack_ids),
        "packSignature": full_graph_pack_signature(
            active_pack_ids,
            pack_summaries,
            registry_generation=registry_generation,
        ),
        "projectNames": sorted(
            {
                str(project_name).strip()
                for project_name in project_names
                if str(project_name).strip()
            }
        ),
        "layout": FULL_GRAPH_LAYOUT,
        "chunkCount": len(chunks),
        "stats": stats,
        "diagnostics": {
            "source": "zip-authoritative",
            "sqliteSourceSkippedReason": (
                "The SQLite graph index uses (pack_id,id) primary keys and does not "
                "preserve duplicate authored rows or source order."
            ),
            "duplicateAuthoredNodes": duplicate_authored_nodes,
            "duplicateAuthoredEdges": duplicate_authored_edges,
            "ambiguousEndpointCount": len(ambiguous_endpoint_keys),
            "ambiguousResolutionCount": ambiguous_resolution_count,
            "unresolvedEdges": unresolved_edges,
            "placeholderNodes": len(placeholders),
            "placeholderEndpointReferences": placeholder_endpoint_references,
            "recordGroups": len(
                {
                    str(
                        (pack_group_keys or {}).get(pack_id)
                        or f"pack:{pack_id}"
                    )
                    for pack_id in active_pack_ids
                }
            ),
            "unresolvedExamples": unresolved_examples,
        },
        "chunks": chunks,
    }


def write_full_graph_manifest(
    output_dir: Path,
    *,
    entries: dict[str, dict[str, Any]],
    chunk_store: ContentAddressedChunkStore,
    generated_at: str | None,
    source_registry: str,
    max_combinations: int,
    max_chunks_per_selection: int = DEFAULT_MAX_CHUNKS_PER_SELECTION,
    max_total_compressed_bytes: int,
) -> dict[str, Any]:
    largest_selection_chunks = max(
        (
            len(entry.get("chunks") or [])
            for entry in entries.values()
            if isinstance(entry, dict)
        ),
        default=0,
    )
    if largest_selection_chunks > max_chunks_per_selection:
        raise RuntimeError(
            "Full-graph artifact request-count budget exceeded: "
            f"{largest_selection_chunks} > {max_chunks_per_selection} chunks "
            "for one selection."
        )
    unique_compressed_bytes = sum(
        int(descriptor.get("compressedBytes") or 0)
        for descriptor in chunk_store.unique_chunks.values()
    )
    if unique_compressed_bytes > max_total_compressed_bytes:
        raise RuntimeError(
            "Full-graph artifact compressed budget exceeded: "
            f"{unique_compressed_bytes} > {max_total_compressed_bytes} bytes."
        )
    manifest = {
        "version": FULL_GRAPH_VERSION,
        "algorithm": FULL_GRAPH_ALGORITHM,
        "layout": FULL_GRAPH_LAYOUT,
        "generatedAt": generated_at,
        "sourceRegistry": source_registry,
        "chunkLimits": {
            "maxCompressedBytes": chunk_store.max_compressed_bytes,
            "targetUncompressedBytes": chunk_store.target_uncompressed_bytes,
        },
        "budgets": {
            "maxCombinations": max_combinations,
            "maxChunksPerSelection": max_chunks_per_selection,
            "largestSelectionChunks": largest_selection_chunks,
            "maxTotalCompressedBytes": max_total_compressed_bytes,
            "uniqueCompressedBytes": unique_compressed_bytes,
            "uniqueChunks": len(chunk_store.unique_chunks),
        },
        "entries": entries,
    }
    target = output_dir / "manifest.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return manifest


def replace_full_graph_directory(staging_dir: Path, output_dir: Path) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    backup_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}-backup-",
            dir=output_dir.parent,
        )
    )
    backup_dir.rmdir()
    previous_moved = False
    try:
        if output_dir.exists():
            os.replace(output_dir, backup_dir)
            previous_moved = True
        os.replace(staging_dir, output_dir)
    except BaseException:
        if output_dir.exists():
            for child in output_dir.iterdir():
                if child.is_file():
                    child.unlink()
            output_dir.rmdir()
        if previous_moved and backup_dir.exists():
            os.replace(backup_dir, output_dir)
        raise
    else:
        if backup_dir.exists():
            for child in backup_dir.iterdir():
                if child.is_file():
                    child.unlink()
            backup_dir.rmdir()


def full_graph_node_detail(
    pack_id: str,
    original_id: str,
    *,
    occurrence: int = 0,
) -> dict[str, Any] | None:
    requested_occurrence = max(0, int(occurrence))
    try:
        pack = find_pack(pack_id)
    except (FileNotFoundError, RuntimeError):
        pack = None
    if pack is not None:
        matched_occurrence = 0
        for obj, _payload in _iter_multi_pack_nodes(pack, 2_147_483_647):
            if _raw_node_id(obj) != original_id:
                continue
            if matched_occurrence != requested_occurrence:
                matched_occurrence += 1
                continue
            rendered = _node(original_id, obj, pack_id)
            detail_id = f"{pack_id}::{original_id}"
            if requested_occurrence:
                detail_id = (
                    f"{detail_id}::duplicate:{requested_occurrence}"
                )
            return {
                "id": detail_id,
                "originalId": original_id,
                "occurrence": requested_occurrence,
                "packId": pack_id,
                "label": str(rendered.get("label") or original_id),
                "type": str(rendered.get("type") or "Element"),
                "properties": rendered.get("properties")
                if isinstance(rendered.get("properties"), dict)
                else {},
                "source": "pack-zip",
            }

    # The SQLite index intentionally de-duplicates (pack_id,id), so it is only
    # a valid fallback for the canonical occurrence when its pack ZIP is absent.
    if requested_occurrence == 0 and DB_PATH.is_file():
        try:
            uri = f"file:{DB_PATH.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    """
                    SELECT id, label, type, properties_json
                    FROM nodes
                    WHERE pack_id = ? AND id = ?
                    LIMIT 1
                    """,
                    (pack_id, original_id),
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                try:
                    properties = json.loads(row["properties_json"] or "{}")
                except json.JSONDecodeError:
                    properties = {}
                return {
                    "id": f"{pack_id}::{original_id}",
                    "originalId": original_id,
                    "occurrence": 0,
                    "packId": pack_id,
                    "label": str(row["label"] or original_id),
                    "type": str(row["type"] or "Element"),
                    "properties": properties,
                    "source": "sqlite-index",
                }
        except sqlite3.Error:
            pass
    return None
