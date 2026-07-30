from __future__ import annotations

import gzip
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from modular_ontology import graph_preview, pack_index
from scripts import build_graph_previews as preview_builder


def _summaries() -> list[dict]:
    return [
        {
            "id": "pack-a",
            "sizeBytes": 100,
            "counts": {"nodes": 2, "edges": 1, "documents": 1},
            "drive": {
                "fileId": "file-a",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "sizeBytes": 100,
                "md5Checksum": "md5-a",
            },
        },
        {
            "id": "pack-b",
            "sizeBytes": 200,
            "counts": {"nodes": 3, "edges": 2, "documents": 1},
            "drive": {
                "fileId": "file-b",
                "modifiedTime": "2026-01-02T00:00:00Z",
                "sizeBytes": 200,
                "md5Checksum": "md5-b",
            },
        },
    ]


def _many_summaries(count: int) -> list[dict]:
    return [
        {
            "id": f"pack-{index:02d}",
            "sizeBytes": 100 + index,
            "counts": {"nodes": 2, "edges": 1, "documents": 1},
            "drive": {
                "fileId": f"file-{index:02d}",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "sizeBytes": 100 + index,
                "md5Checksum": f"md5-{index:02d}",
            },
        }
        for index in range(count)
    ]


def _write_preview(
    root: Path,
    summaries: list[dict],
    pack_ids: list[str] | None = None,
) -> None:
    pack_ids = pack_ids or ["pack-a", "pack-b"]
    key = graph_preview.graph_selection_key(pack_ids, 20_000, 50_000)
    payload = {
        "pack": {"id": "project", "title": "Build title"},
        "project": None,
        "packs": summaries,
        "activePackIds": pack_ids,
        "nodes": [{"id": f"{pack_id}::node"} for pack_id in pack_ids],
        "edges": [],
        "stats": {"visibleNodes": 2, "visibleEdges": 0},
        "diagnostics": {"mode": "zip-two-pass"},
    }
    root.mkdir(parents=True)
    with gzip.open(root / f"{key}.json.gz", "wt", encoding="utf-8") as stream:
        json.dump(payload, stream)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "version": graph_preview.GRAPH_PREVIEW_VERSION,
                "algorithm": graph_preview.GRAPH_PREVIEW_ALGORITHM,
                "entries": {
                    key: {
                        "file": f"{key}.json.gz",
                        "packSignature": graph_preview.graph_pack_signature(pack_ids, summaries),
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_preview_key_uses_effective_limits_but_preserves_pack_order() -> None:
    high = graph_preview.graph_selection_key(["pack-b", "pack-a", "pack-a"], 20_000, 50_000)
    applied = graph_preview.graph_selection_key(["pack-b", "pack-a"], 1_000, 2_000)
    reversed_order = graph_preview.graph_selection_key(["pack-a", "pack-b"], 1_000, 2_000)

    assert high == applied
    assert high != reversed_order


def test_preview_loads_exact_signature_and_adapts_project_metadata(monkeypatch, tmp_path: Path) -> None:
    summaries = _summaries()
    preview_dir = tmp_path / "previews"
    _write_preview(preview_dir, summaries)
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", str(preview_dir))
    graph_preview.invalidate_graph_cache()
    builds: list[list[str]] = []

    result = graph_preview.get_or_build_project_graph(
        ["pack-a", "pack-b"],
        pack_summaries=summaries,
        title="Runtime Project",
        project={"id": "runtime", "name": "Runtime Project"},
        max_nodes=20_000,
        max_edges=50_000,
        builder=lambda pack_ids, **_kwargs: builds.append(pack_ids) or {},
    )

    assert builds == []
    assert result["activePackIds"] == ["pack-a", "pack-b"]
    assert result["pack"]["id"] == "runtime"
    assert result["pack"]["title"] == "Runtime Project"
    assert result["diagnostics"]["graphCache"] == "preview"


def test_preview_signature_survives_lazy_direct_pack_cache(monkeypatch, tmp_path: Path) -> None:
    registry_summaries = _summaries()
    direct_summaries = [
        {
            "id": summary["id"],
            "title": f"Direct {summary['id']}",
            "sizeBytes": summary["sizeBytes"],
            "counts": dict(summary["counts"]),
        }
        for summary in registry_summaries
    ]
    merged_summaries = pack_index._merge_pack_summaries(
        registry_summaries,
        direct_summaries,
    )
    preview_dir = tmp_path / "previews"
    _write_preview(preview_dir, registry_summaries)
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", str(preview_dir))
    graph_preview.invalidate_graph_cache()

    payload = graph_preview.load_graph_preview(
        ["pack-a", "pack-b"],
        pack_summaries=merged_summaries,
        max_nodes=20_000,
        max_edges=50_000,
    )

    assert payload is not None
    assert payload["activePackIds"] == ["pack-a", "pack-b"]
    assert graph_preview.graph_pack_signature(
        ["pack-a", "pack-b"],
        merged_summaries,
    ) == graph_preview.graph_pack_signature(
        ["pack-a", "pack-b"],
        registry_summaries,
    )


def test_stale_preview_falls_back_once_then_hits_memory_cache(monkeypatch, tmp_path: Path) -> None:
    summaries = _summaries()
    preview_dir = tmp_path / "previews"
    _write_preview(preview_dir, summaries)
    stale_summaries = [*summaries]
    stale_summaries[0] = {
        **stale_summaries[0],
        "drive": {**stale_summaries[0]["drive"], "modifiedTime": "2026-02-01T00:00:00Z"},
    }
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", str(preview_dir))
    graph_preview.invalidate_graph_cache()
    builds: list[list[str]] = []

    def build(pack_ids, **_kwargs):
        builds.append(pack_ids)
        return {
            "pack": {"id": "project", "title": "Built"},
            "activePackIds": pack_ids,
            "nodes": [],
            "edges": [],
            "diagnostics": {},
        }

    first = graph_preview.get_or_build_project_graph(
        ["pack-a", "pack-b"],
        pack_summaries=stale_summaries,
        title="Runtime",
        project={"id": "runtime"},
        max_nodes=1_000,
        max_edges=2_000,
        builder=build,
    )
    second = graph_preview.get_or_build_project_graph(
        ["pack-a", "pack-b"],
        pack_summaries=stale_summaries,
        title="Runtime",
        project={"id": "runtime"},
        max_nodes=20_000,
        max_edges=50_000,
        builder=build,
    )

    assert builds == [["pack-a", "pack-b"]]
    assert first["diagnostics"]["graphCache"] == "built"
    assert second["diagnostics"]["graphCache"] == "memory"


def test_pack_signature_tracks_file_identity_checksum_and_registry_generation() -> None:
    summaries = _summaries()
    base = graph_preview.graph_pack_signature(
        ["pack-a", "pack-b"],
        summaries,
        registry_generation="generation-a",
    )
    changed_file = json.loads(json.dumps(summaries))
    changed_file[0]["drive"]["fileId"] = "replacement-file"
    changed_checksum = json.loads(json.dumps(summaries))
    changed_checksum[0]["drive"]["md5Checksum"] = "replacement-checksum"

    assert base != graph_preview.graph_pack_signature(
        ["pack-a", "pack-b"],
        changed_file,
        registry_generation="generation-a",
    )
    assert base != graph_preview.graph_pack_signature(
        ["pack-a", "pack-b"],
        changed_checksum,
        registry_generation="generation-a",
    )
    assert base != graph_preview.graph_pack_signature(
        ["pack-a", "pack-b"],
        summaries,
        registry_generation="generation-b",
    )


def test_cached_payload_overlays_each_authorized_project_without_metadata_leak(
    monkeypatch,
    tmp_path: Path,
) -> None:
    summaries = _summaries()
    preview_dir = tmp_path / "previews"
    _write_preview(preview_dir, summaries)
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", str(preview_dir))
    graph_preview.invalidate_graph_cache()

    first = graph_preview.get_or_build_project_graph(
        ["pack-a", "pack-b"],
        pack_summaries=summaries,
        title="Project A",
        project={"id": "project-a", "name": "Project A", "company": "Company A"},
        max_nodes=20_000,
        max_edges=50_000,
        builder=lambda *_args, **_kwargs: pytest.fail("preview should be used"),
    )
    second = graph_preview.get_or_build_project_graph(
        ["pack-a", "pack-b"],
        pack_summaries=summaries,
        title="Project B",
        project={"id": "project-b", "name": "Project B", "company": "Company B"},
        max_nodes=20_000,
        max_edges=50_000,
        builder=lambda *_args, **_kwargs: pytest.fail("memory cache should be used"),
    )

    assert first["project"]["id"] == "project-a"
    assert first["pack"]["id"] == "project-a"
    assert first["pack"]["title"] == "Project A"
    assert second["project"]["id"] == "project-b"
    assert second["pack"]["id"] == "project-b"
    assert second["pack"]["title"] == "Project B"
    assert first["project"]["company"] == "Company A"
    assert second["project"]["company"] == "Company B"
    assert first["diagnostics"]["graphCache"] == "preview"
    assert second["diagnostics"]["graphCache"] == "memory"


def test_large_stale_preview_fails_fast_without_live_build(monkeypatch, tmp_path: Path) -> None:
    summaries = _many_summaries(17)
    pack_ids = [summary["id"] for summary in summaries]
    preview_dir = tmp_path / "previews"
    _write_preview(preview_dir, summaries, pack_ids)
    stale_summaries = json.loads(json.dumps(summaries))
    stale_summaries[0]["drive"]["md5Checksum"] = "changed"
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", str(preview_dir))
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_LIVE_BUILD_MAX_PACKS", "16")
    graph_preview.invalidate_graph_cache()
    builds: list[list[str]] = []

    with pytest.raises(
        graph_preview.GraphPreviewUnavailableError,
        match="precomputed graph preview is required",
    ):
        graph_preview.get_or_build_project_graph(
            pack_ids,
            pack_summaries=stale_summaries,
            title="Large Project",
            project={"id": "large-project"},
            max_nodes=20_000,
            max_edges=50_000,
            builder=lambda ids, **_kwargs: builds.append(ids) or {},
        )

    assert builds == []


def test_inflight_followers_share_the_leader_result(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_INFLIGHT_WAIT_SECONDS", "2")
    graph_preview.invalidate_graph_cache()
    summaries = _summaries()
    started = threading.Event()
    release = threading.Event()
    build_count = 0
    count_lock = threading.Lock()

    def build(pack_ids, **_kwargs):
        nonlocal build_count
        with count_lock:
            build_count += 1
        started.set()
        assert release.wait(timeout=2)
        return {
            "pack": {"id": "project", "title": "Built"},
            "project": None,
            "activePackIds": pack_ids,
            "nodes": [],
            "edges": [],
            "diagnostics": {},
        }

    def load():
        return graph_preview.get_or_build_project_graph(
            ["pack-a", "pack-b"],
            pack_summaries=summaries,
            title="Runtime",
            project={"id": "runtime"},
            max_nodes=1_000,
            max_edges=2_000,
            builder=build,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(load)
        assert started.wait(timeout=1)
        follower = executor.submit(load)
        time.sleep(0.05)
        release.set()
        leader_result = leader.result(timeout=2)
        follower_result = follower.result(timeout=2)

    assert build_count == 1
    assert leader_result["diagnostics"]["graphCache"] == "built"
    assert follower_result["diagnostics"]["graphCache"] == "inflight"


def test_inflight_followers_share_failure_without_rebuilding(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_INFLIGHT_WAIT_SECONDS", "2")
    graph_preview.invalidate_graph_cache()
    summaries = _summaries()
    started = threading.Event()
    release = threading.Event()
    build_count = 0
    count_lock = threading.Lock()

    def build(_pack_ids, **_kwargs):
        nonlocal build_count
        with count_lock:
            build_count += 1
        started.set()
        assert release.wait(timeout=2)
        raise ValueError("leader failed")

    def load():
        return graph_preview.get_or_build_project_graph(
            ["pack-a", "pack-b"],
            pack_summaries=summaries,
            title="Runtime",
            project={"id": "runtime"},
            max_nodes=1_000,
            max_edges=2_000,
            builder=build,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(load)
        assert started.wait(timeout=1)
        follower = executor.submit(load)
        time.sleep(0.05)
        release.set()
        with pytest.raises(ValueError, match="leader failed"):
            leader.result(timeout=2)
        with pytest.raises(graph_preview.GraphBuildFailedError, match="did not repeat"):
            follower.result(timeout=2)

    assert build_count == 1


def test_inflight_timeout_fails_fast_without_starting_a_second_build(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_PREVIEW_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("MODULAR_ONTOLOGY_GRAPH_INFLIGHT_WAIT_SECONDS", "0")
    graph_preview.invalidate_graph_cache()
    summaries = _summaries()
    started = threading.Event()
    release = threading.Event()
    build_count = 0

    def build(pack_ids, **_kwargs):
        nonlocal build_count
        build_count += 1
        started.set()
        assert release.wait(timeout=2)
        return {
            "pack": {"id": "project", "title": "Built"},
            "project": None,
            "activePackIds": pack_ids,
            "nodes": [],
            "edges": [],
            "diagnostics": {},
        }

    def load():
        return graph_preview.get_or_build_project_graph(
            ["pack-a", "pack-b"],
            pack_summaries=summaries,
            title="Runtime",
            project={"id": "runtime"},
            max_nodes=1_000,
            max_edges=2_000,
            builder=build,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        leader = executor.submit(load)
        assert started.wait(timeout=1)
        with pytest.raises(graph_preview.GraphBuildInProgressError, match="still being built"):
            load()
        assert build_count == 1
        release.set()
        leader.result(timeout=2)


def _write_builder_registry(path: Path) -> dict:
    registry = {
        "generatedAt": "generation-1",
        "packs": [
            {
                "id": "pack-b",
                "driveScope": "_Common",
                "driveCategory": "Category B",
                "sizeBytes": 10,
                "counts": {"nodes": 1, "edges": 0, "documents": 0},
                "drive": {"fileId": "file-b", "md5Checksum": "md5-b"},
            },
            {
                "id": "pack-a",
                "driveScope": "_Common",
                "driveCategory": "Category A",
                "sizeBytes": 10,
                "counts": {"nodes": 1, "edges": 0, "documents": 0},
                "drive": {"fileId": "file-a", "md5Checksum": "md5-a"},
            },
        ],
        "projects": [
            {
                "id": "project",
                "name": "Project",
                "packIds": ["pack-b", "pack-a"],
            }
        ],
    }
    path.write_text(json.dumps(registry), encoding="utf-8")
    return registry


def _fake_built_graph(pack_ids: list[str], **_kwargs) -> dict:
    return {
        "pack": {"id": "project", "title": "Project"},
        "project": None,
        "activePackIds": list(pack_ids),
        "nodes": [{"id": f"{pack_id}::node"} for pack_id in pack_ids],
        "edges": [],
        "diagnostics": {},
    }


def test_preview_builder_preserves_project_order_and_atomically_prunes_orphans(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    _write_builder_registry(registry_path)
    output_dir = tmp_path / "previews"
    output_dir.mkdir()
    (output_dir / "orphan.json.gz").write_bytes(b"stale")
    (output_dir / "manifest.json").write_text('{"old":true}', encoding="utf-8")
    monkeypatch.setattr(preview_builder, "build_multi_pack_graph", _fake_built_graph)

    manifest = preview_builder.build_graph_previews(
        registry_path=registry_path,
        output_dir=output_dir,
        threshold=0,
        max_combinations=3,
        max_compressed_bytes=1_000_000,
    )

    assert len(manifest["entries"]) == 3
    assert not (output_dir / "orphan.json.gz").exists()
    assert {path.name for path in output_dir.iterdir()} == {
        "manifest.json",
        *(entry["file"] for entry in manifest["entries"].values()),
    }
    combined_key = graph_preview.graph_selection_key(
        ["pack-b", "pack-a"],
        graph_preview.GRAPH_PREVIEW_MAX_NODES,
        graph_preview.GRAPH_PREVIEW_MAX_EDGES,
    )
    assert manifest["entries"][combined_key]["packIds"] == ["pack-b", "pack-a"]
    with gzip.open(output_dir / manifest["entries"][combined_key]["file"], "rt", encoding="utf-8") as stream:
        assert json.load(stream)["activePackIds"] == ["pack-b", "pack-a"]


def test_preview_builder_budget_failures_leave_previous_generation_untouched(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.json"
    _write_builder_registry(registry_path)
    output_dir = tmp_path / "previews"
    output_dir.mkdir()
    old_manifest = '{"old":true}'
    (output_dir / "manifest.json").write_text(old_manifest, encoding="utf-8")
    monkeypatch.setattr(preview_builder, "build_multi_pack_graph", _fake_built_graph)

    with pytest.raises(RuntimeError, match="hard limit is 2"):
        preview_builder.build_graph_previews(
            registry_path=registry_path,
            output_dir=output_dir,
            threshold=0,
            max_combinations=2,
            max_compressed_bytes=1_000_000,
        )
    assert (output_dir / "manifest.json").read_text(encoding="utf-8") == old_manifest

    with pytest.raises(RuntimeError, match="compressed payload budget exceeded"):
        preview_builder.build_graph_previews(
            registry_path=registry_path,
            output_dir=output_dir,
            threshold=0,
            max_combinations=3,
            max_compressed_bytes=1,
        )
    assert (output_dir / "manifest.json").read_text(encoding="utf-8") == old_manifest
    assert not list(tmp_path.glob(".previews-build-*"))


def test_bundled_preview_generation_is_complete_and_self_consistent() -> None:
    preview_dir = graph_preview.DEFAULT_GRAPH_PREVIEW_DIR
    manifest = json.loads((preview_dir / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["entries"]
    registry = json.loads(
        (preview_builder.REPOSITORY / "data" / "01_Database" / "pack_registry.json").read_text(
            encoding="utf-8"
        )
    )
    packs = [pack for pack in registry["packs"] if isinstance(pack, dict) and pack.get("id")]
    projects = [project for project in registry["projects"] if isinstance(project, dict)]
    expected_selections = preview_builder._large_ui_selections(
        projects,
        {str(pack["id"]): pack for pack in packs},
        threshold=manifest["selectionThreshold"],
        max_combinations=manifest["budgets"]["maxCombinations"],
    )
    expected_keys = {
        graph_preview.graph_selection_key(
            list(pack_ids),
            manifest["maxNodes"],
            manifest["maxEdges"],
        )
        for pack_ids in expected_selections
    }

    assert manifest["version"] == graph_preview.GRAPH_PREVIEW_VERSION
    assert manifest["algorithm"] == graph_preview.GRAPH_PREVIEW_ALGORITHM
    assert set(entries) == expected_keys
    assert {path.name for path in preview_dir.glob("*.json.gz")} == {
        entry["file"] for entry in entries.values()
    }

    for key, entry in entries.items():
        assert key == graph_preview.graph_selection_key(
            entry["packIds"],
            manifest["maxNodes"],
            manifest["maxEdges"],
        )
        payload_path = preview_dir / entry["file"]
        assert payload_path.stat().st_size == entry["compressedBytes"]
        with gzip.open(payload_path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        assert payload["activePackIds"] == entry["packIds"]
        assert len(payload["nodes"]) == entry["visibleNodes"]
        assert len(payload["edges"]) == entry["visibleEdges"]
        assert entry["packSignature"] == graph_preview.graph_pack_signature(
            entry["packIds"],
            packs,
            registry_generation=str(registry.get("generatedAt") or registry.get("generation") or ""),
        )
