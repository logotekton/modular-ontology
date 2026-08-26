"""Build deterministic graph previews for every large UI group combination.

The React graph explorer toggles folder groups atomically.  Precomputing those
exact selections keeps the full two-pass/cross-pack graph semantics while
avoiding a cold Google Drive ZIP fan-out during a live demo.
"""

from __future__ import annotations

import argparse
import gzip
import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from modular_ontology.graph_preview import (  # noqa: E402
    GRAPH_PREVIEW_ALGORITHM,
    GRAPH_PREVIEW_MAX_EDGES,
    GRAPH_PREVIEW_MAX_NODES,
    GRAPH_PREVIEW_VERSION,
    graph_pack_signature,
    graph_selection_key,
)
from modular_ontology.pack_index import build_multi_pack_graph  # noqa: E402


DEFAULT_MAX_COMBINATIONS = 128
DEFAULT_MAX_COMPRESSED_BYTES = 32 * 1024 * 1024


def _display_groups(project: dict[str, Any], packs_by_id: dict[str, dict[str, Any]]) -> list[list[str]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for raw_pack_id in project.get("packIds") or []:
        pack_id = str(raw_pack_id or "").strip()
        if not pack_id:
            continue
        pack = packs_by_id.get(pack_id, {})
        scope = str(pack.get("driveScope") or ("_Common" if pack.get("commonScoped") else "")).strip()
        category = str(pack.get("driveCategory") or pack.get("commonCategory") or "").strip()
        key = (scope or "drive", category) if category else (scope or "pack", pack_id)
        groups.setdefault(key, []).append(pack_id)
    return list(groups.values())


def _large_ui_selections(
    projects: list[dict[str, Any]],
    packs_by_id: dict[str, dict[str, Any]],
    *,
    threshold: int,
    max_combinations: int | None = None,
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
        if max_combinations is not None:
            possible_group_selections = (1 << len(groups)) - 1
            if possible_group_selections > max_combinations:
                raise RuntimeError(
                    f"Refusing to enumerate {possible_group_selections} graph group selections "
                    f"for project {project.get('id') or project.get('name') or 'Project'}; "
                    f"hard limit is {max_combinations}."
                )
        for size in range(1, len(groups) + 1):
            for selected_groups in itertools.combinations(groups, size):
                selected_pack_ids = {
                    pack_id
                    for group in selected_groups
                    for pack_id in group
                }
                pack_ids = tuple(
                    pack_id
                    for pack_id in project_pack_ids
                    if pack_id in selected_pack_ids
                )
                if len(pack_ids) <= threshold:
                    continue
                if (
                    pack_ids not in selections
                    and max_combinations is not None
                    and len(selections) >= max_combinations
                ):
                    raise RuntimeError(
                        "Graph preview combination hard limit exceeded while enumerating: "
                        f"more than {max_combinations} unique selections."
                    )
                selections.setdefault(pack_ids, set()).add(str(project.get("name") or project.get("id") or "Project"))
    return selections


def _replace_preview_directory(staging_dir: Path, output_dir: Path) -> None:
    """Replace one complete generation and remove every prior orphan."""

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
            shutil.rmtree(output_dir, ignore_errors=True)
        if previous_moved and backup_dir.exists():
            os.replace(backup_dir, output_dir)
        raise
    else:
        if backup_dir.exists():
            shutil.rmtree(backup_dir)


def build_graph_previews(
    *,
    registry_path: Path,
    output_dir: Path,
    threshold: int = 16,
    max_combinations: int = DEFAULT_MAX_COMBINATIONS,
    max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
) -> dict[str, Any]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    packs = [pack for pack in registry.get("packs") or [] if isinstance(pack, dict) and pack.get("id")]
    projects = [project for project in registry.get("projects") or [] if isinstance(project, dict)]
    packs_by_id = {str(pack["id"]): pack for pack in packs}
    combination_limit = max(0, int(max_combinations))
    compressed_limit = max(0, int(max_compressed_bytes))
    selections = _large_ui_selections(
        projects,
        packs_by_id,
        threshold=threshold,
        max_combinations=combination_limit,
    )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}-build-",
            dir=output_dir.parent,
        )
    )
    entries: dict[str, dict[str, Any]] = {}
    started_at = time.perf_counter()
    compressed_total = 0
    try:
        for index, (pack_ids_tuple, project_names) in enumerate(selections.items(), start=1):
            pack_ids = list(pack_ids_tuple)
            key = graph_selection_key(pack_ids, GRAPH_PREVIEW_MAX_NODES, GRAPH_PREVIEW_MAX_EDGES)
            filename = f"{key}.json.gz"
            item_started_at = time.perf_counter()
            payload = build_multi_pack_graph(
                pack_ids,
                title=sorted(project_names)[0],
                project=None,
                max_nodes=GRAPH_PREVIEW_MAX_NODES,
                max_edges=GRAPH_PREVIEW_MAX_EDGES,
            )
            target = staging_dir / filename
            temp = target.with_suffix(f"{target.suffix}.tmp")
            with temp.open("wb") as raw_stream:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    compresslevel=6,
                    fileobj=raw_stream,
                    mtime=0,
                ) as compressed_stream:
                    with io.TextIOWrapper(compressed_stream, encoding="utf-8") as stream:
                        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            temp.replace(target)
            compressed_bytes = target.stat().st_size
            compressed_total += compressed_bytes
            if compressed_total > compressed_limit:
                raise RuntimeError(
                    "Graph preview compressed payload budget exceeded: "
                    f"{compressed_total} > {compressed_limit} bytes."
                )
            entries[key] = {
                "file": filename,
                "packIds": pack_ids,
                "packCount": len(pack_ids),
                "packSignature": graph_pack_signature(
                    pack_ids,
                    packs,
                    registry_generation=str(registry.get("generatedAt") or registry.get("generation") or ""),
                ),
                "projectNames": sorted(project_names),
                "compressedBytes": compressed_bytes,
                "visibleNodes": len(payload.get("nodes") or []),
                "visibleEdges": len(payload.get("edges") or []),
            }
            print(
                f"[{index}/{len(selections)}] {len(pack_ids):3d} packs -> "
                f"{entries[key]['visibleNodes']} nodes / {entries[key]['visibleEdges']} edges / "
                f"{compressed_bytes / 1024:.1f} KiB in {time.perf_counter() - item_started_at:.2f}s"
            )

        manifest = {
            "version": GRAPH_PREVIEW_VERSION,
            "algorithm": GRAPH_PREVIEW_ALGORITHM,
            "generatedAt": registry.get("generatedAt"),
            "sourceRegistry": {
                "path": registry_path.name,
                "generatedAt": registry.get("generatedAt"),
            },
            "maxNodes": GRAPH_PREVIEW_MAX_NODES,
            "maxEdges": GRAPH_PREVIEW_MAX_EDGES,
            "selectionThreshold": threshold,
            "budgets": {
                "maxCombinations": combination_limit,
                "maxCompressedBytes": compressed_limit,
                "compressedBytes": compressed_total,
            },
            "entries": entries,
        }
        (staging_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _replace_preview_directory(staging_dir, output_dir)
    except BaseException:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    print(
        f"Built {len(entries)} previews in {time.perf_counter() - started_at:.2f}s; "
        f"compressed total {compressed_total / 1024 / 1024:.2f} MiB"
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
        default=REPOSITORY / "snapshots" / "graph-previews",
    )
    parser.add_argument("--threshold", type=int, default=16)
    parser.add_argument("--max-combinations", type=int, default=DEFAULT_MAX_COMBINATIONS)
    parser.add_argument("--max-compressed-bytes", type=int, default=DEFAULT_MAX_COMPRESSED_BYTES)
    args = parser.parse_args()
    build_graph_previews(
        registry_path=args.registry.resolve(),
        output_dir=args.output.resolve(),
        threshold=max(0, args.threshold),
        max_combinations=max(0, args.max_combinations),
        max_compressed_bytes=max(0, args.max_compressed_bytes),
    )


if __name__ == "__main__":
    main()
