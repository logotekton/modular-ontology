"""Build deterministic, full-count, content-addressed project graph artifacts.

Unlike the legacy UI preview, v3 does not sample nodes or edges. Heavy authored
properties stay in the pack/SQLite detail source while compact topology and
precomputed coordinates are emitted as reusable immutable gzip chunks.
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from modular_ontology.full_graph import (  # noqa: E402
    DEFAULT_FULL_GRAPH_DIR,
    DEFAULT_MAX_CHUNKS_PER_SELECTION,
    DEFAULT_MAX_COMPRESSED_CHUNK_BYTES,
    DEFAULT_TARGET_UNCOMPRESSED_CHUNK_BYTES,
    ContentAddressedChunkStore,
    build_full_graph_selection,
    replace_full_graph_directory,
    write_full_graph_manifest,
)
from modular_ontology.pack_index import PackFile, find_pack  # noqa: E402


DEFAULT_MAX_COMBINATIONS = 128
DEFAULT_MAX_TOTAL_COMPRESSED_BYTES = 256 * 1024 * 1024


def _display_group_key(
    pack_id: str,
    pack: dict[str, Any],
) -> tuple[str, str]:
    direct_scope = str(pack.get("driveScope") or "").strip()
    direct_category = str(
        pack.get("projectCategory")
        or pack.get("commonCategory")
        or pack.get("driveCategory")
        or ""
    ).strip()
    scope = ""
    category = ""
    if direct_scope and direct_category:
        scope = direct_scope
        category = direct_category
    else:
        filename_parts = str(pack.get("filename") or "").split("__")
        if (
            len(filename_parts) >= 3
            and filename_parts[0].strip()
            and filename_parts[1].strip()
        ):
            scope = filename_parts[0].strip()
            category = filename_parts[1].strip()
    return (
        (scope or "drive", category)
        if category
        else (scope or "pack", pack_id)
    )


def _record_group_key(
    pack_id: str,
    pack: dict[str, Any],
) -> str:
    return json.dumps(
        _display_group_key(pack_id, pack),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _display_groups(
    project: dict[str, Any],
    packs_by_id: dict[str, dict[str, Any]],
) -> list[list[str]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for raw_pack_id in project.get("packIds") or []:
        pack_id = str(raw_pack_id or "").strip()
        if not pack_id:
            continue
        pack = packs_by_id.get(pack_id, {})
        key = _display_group_key(pack_id, pack)
        groups.setdefault(key, []).append(pack_id)
    return list(groups.values())


def project_ui_selections(
    projects: list[dict[str, Any]],
    packs_by_id: dict[str, dict[str, Any]],
    *,
    min_pack_count: int = 1,
    max_combinations: int = DEFAULT_MAX_COMBINATIONS,
) -> dict[tuple[str, ...], set[str]]:
    selections: dict[tuple[str, ...], set[str]] = {}
    for project in projects:
        project_pack_ids = list(
            dict.fromkeys(
                str(raw_pack_id).strip()
                for raw_pack_id in project.get("packIds") or []
                if str(raw_pack_id).strip()
            )
        )
        groups = _display_groups(project, packs_by_id)
        for size in range(1, len(groups) + 1):
            for selected_groups in itertools.combinations(groups, size):
                selected_ids = {
                    pack_id
                    for group in selected_groups
                    for pack_id in group
                }
                pack_ids = tuple(
                    pack_id
                    for pack_id in project_pack_ids
                    if pack_id in selected_ids
                )
                if len(pack_ids) < max(1, int(min_pack_count)):
                    continue
                selections.setdefault(pack_ids, set()).add(
                    str(project.get("name") or project.get("id") or "Project")
                )
                if len(selections) > max_combinations:
                    raise RuntimeError(
                        "Full-graph selection hard limit exceeded: "
                        f"{len(selections)} > {max_combinations}."
                    )
    return selections


def build_full_graph_artifacts(
    *,
    registry_path: Path,
    output_dir: Path,
    min_pack_count: int = 1,
    max_combinations: int = DEFAULT_MAX_COMBINATIONS,
    max_chunks_per_selection: int = DEFAULT_MAX_CHUNKS_PER_SELECTION,
    max_total_compressed_bytes: int = DEFAULT_MAX_TOTAL_COMPRESSED_BYTES,
    max_compressed_chunk_bytes: int = DEFAULT_MAX_COMPRESSED_CHUNK_BYTES,
    target_uncompressed_chunk_bytes: int = (
        DEFAULT_TARGET_UNCOMPRESSED_CHUNK_BYTES
    ),
    find_pack_fn: Callable[[str], PackFile] = find_pack,
) -> dict[str, Any]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    packs = [
        pack
        for pack in registry.get("packs") or []
        if isinstance(pack, dict) and pack.get("id")
    ]
    projects = [
        project
        for project in registry.get("projects") or []
        if isinstance(project, dict)
    ]
    packs_by_id = {str(pack["id"]): pack for pack in packs}
    pack_group_keys = {
        pack_id: _record_group_key(pack_id, pack)
        for pack_id, pack in packs_by_id.items()
    }
    selections = project_ui_selections(
        projects,
        packs_by_id,
        min_pack_count=min_pack_count,
        max_combinations=max_combinations,
    )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}-build-",
            dir=output_dir.parent,
        )
    )
    chunk_store = ContentAddressedChunkStore(
        staging_dir,
        max_compressed_bytes=max_compressed_chunk_bytes,
        target_uncompressed_bytes=target_uncompressed_chunk_bytes,
    )
    entries: dict[str, dict[str, Any]] = {}
    started_at = time.perf_counter()
    try:
        for index, (pack_ids_tuple, project_names) in enumerate(
            selections.items(),
            start=1,
        ):
            item_started_at = time.perf_counter()
            entry = build_full_graph_selection(
                list(pack_ids_tuple),
                pack_summaries=packs,
                chunk_store=chunk_store,
                pack_group_keys=pack_group_keys,
                project_names=project_names,
                registry_generation=str(
                    registry.get("generatedAt")
                    or registry.get("generation")
                    or ""
                ),
                find_pack_fn=find_pack_fn,
            )
            if len(entry["chunks"]) > max_chunks_per_selection:
                raise RuntimeError(
                    "Full-graph artifact request-count budget exceeded: "
                    f"{len(entry['chunks'])} > "
                    f"{max_chunks_per_selection} chunks for "
                    f"{len(pack_ids_tuple)} packs."
                )
            entries[str(entry["selectionKey"])] = entry
            stats = entry["stats"]
            print(
                f"[{index}/{len(selections)}] {len(pack_ids_tuple):3d} packs -> "
                f"{stats['totalNodes']} nodes / {stats['totalEdges']} edges / "
                f"{len(entry['chunks'])} chunks in "
                f"{time.perf_counter() - item_started_at:.2f}s"
            )

        manifest = write_full_graph_manifest(
            staging_dir,
            entries=entries,
            chunk_store=chunk_store,
            generated_at=str(
                registry.get("generatedAt")
                or registry.get("generation")
                or ""
            ),
            source_registry=registry_path.name,
            max_combinations=max_combinations,
            max_chunks_per_selection=max_chunks_per_selection,
            max_total_compressed_bytes=max_total_compressed_bytes,
        )
        replace_full_graph_directory(staging_dir, output_dir)
    except BaseException:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    print(
        f"Built {len(entries)} full graph selections in "
        f"{time.perf_counter() - started_at:.2f}s; "
        f"{manifest['budgets']['uniqueChunks']} unique chunks / "
        f"{manifest['budgets']['uniqueCompressedBytes'] / 1024 / 1024:.2f} MiB"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=REPOSITORY / "data" / "01_Database" / "pack_registry.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_FULL_GRAPH_DIR,
    )
    parser.add_argument("--min-pack-count", type=int, default=1)
    parser.add_argument(
        "--max-combinations",
        type=int,
        default=DEFAULT_MAX_COMBINATIONS,
    )
    parser.add_argument(
        "--max-chunks-per-selection",
        type=int,
        default=DEFAULT_MAX_CHUNKS_PER_SELECTION,
    )
    parser.add_argument(
        "--max-total-compressed-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_COMPRESSED_BYTES,
    )
    parser.add_argument(
        "--max-compressed-chunk-bytes",
        type=int,
        default=DEFAULT_MAX_COMPRESSED_CHUNK_BYTES,
    )
    parser.add_argument(
        "--target-uncompressed-chunk-bytes",
        type=int,
        default=DEFAULT_TARGET_UNCOMPRESSED_CHUNK_BYTES,
    )
    args = parser.parse_args()
    build_full_graph_artifacts(
        registry_path=args.registry.resolve(),
        output_dir=args.output.resolve(),
        min_pack_count=max(1, args.min_pack_count),
        max_combinations=max(1, args.max_combinations),
        max_chunks_per_selection=max(
            1,
            args.max_chunks_per_selection,
        ),
        max_total_compressed_bytes=max(
            1,
            args.max_total_compressed_bytes,
        ),
        max_compressed_chunk_bytes=max(
            256,
            args.max_compressed_chunk_bytes,
        ),
        target_uncompressed_chunk_bytes=max(
            128,
            args.target_uncompressed_chunk_bytes,
        ),
    )


if __name__ == "__main__":
    main()
