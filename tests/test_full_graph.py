from __future__ import annotations

import gzip
import hashlib
import json
import math
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from modular_ontology import app, full_graph
from modular_ontology.pack_index import PackFile
from scripts import build_full_graph_artifacts as artifact_builder


def _summary(pack_id: str, nodes: int, edges: int) -> dict:
    return {
        "id": pack_id,
        "sizeBytes": 100,
        "counts": {
            "nodes": nodes,
            "edges": edges,
            "documents": 0,
        },
        "drive": {
            "fileId": f"file-{pack_id}",
            "modifiedTime": "2026-01-01T00:00:00Z",
            "sizeBytes": 100,
            "md5Checksum": f"md5-{pack_id}",
        },
    }


def _write_pack(
    root: Path,
    pack_id: str,
    *,
    nodes: list[dict],
    edges: list[dict],
) -> PackFile:
    path = root / f"{pack_id}.zip"
    manifest = {
        "pack_id": pack_id,
        "title": pack_id,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "documents": 0,
        },
        "entrypoints": {
            "nodes": "graph/nodes.jsonl",
            "edges": "graph/edges.jsonl",
        },
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(
            "graph/nodes.jsonl",
            "".join(
                f"{json.dumps(node, ensure_ascii=False)}\n"
                for node in nodes
            ),
        )
        archive.writestr(
            "graph/edges.jsonl",
            "".join(
                f"{json.dumps(edge, ensure_ascii=False)}\n"
                for edge in edges
            ),
        )
    return PackFile(path)


def _fixture_selection(tmp_path: Path):
    pack_a = _write_pack(
        tmp_path,
        "pack-a",
        nodes=[
            {"id": "a1", "label": "A1", "type": "Element"},
            {"id": "a1", "label": "A1 duplicate", "type": "Element"},
            {"id": "a2", "label": "A2", "type": "Element"},
        ],
        edges=[
            {
                "id": "edge-1",
                "source": "a1",
                "target": "b1",
                "relation": "cross_pack",
            },
            {
                "id": "edge-2",
                "source": "a2",
                "target": "missing-target",
                "relation": "missing",
            },
            {
                "id": "edge-2",
                "source": "a1",
                "target": "b1",
                "relation": "duplicate_id",
            },
            {
                "source": "",
                "target": "a2",
                "relation": "empty_source",
            },
        ],
    )
    pack_b = _write_pack(
        tmp_path,
        "pack-b",
        nodes=[
            {"id": "b1", "label": "B1", "type": "Category"},
        ],
        edges=[],
    )
    summaries = [
        _summary("pack-a", 3, 4),
        _summary("pack-b", 1, 0),
    ]
    packs = {"pack-a": pack_a, "pack-b": pack_b}
    store = full_graph.ContentAddressedChunkStore(
        tmp_path / "artifacts",
        max_compressed_bytes=2_000,
        target_uncompressed_bytes=300,
    )
    entry = full_graph.build_full_graph_selection(
        ["pack-a", "pack-b"],
        pack_summaries=summaries,
        chunk_store=store,
        project_names={"Fixture Project"},
        find_pack_fn=packs.__getitem__,
    )
    return entry, summaries, store


def _chunk_records(root: Path, entry: dict, kind: str) -> list[dict]:
    records: list[dict] = []
    for descriptor in entry["chunks"]:
        if descriptor["kind"] != kind:
            continue
        with gzip.open(root / descriptor["file"], "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        assert payload["version"] == full_graph.FULL_GRAPH_VERSION
        assert payload["kind"] == kind
        records.extend(payload[kind])
    return records


def test_full_graph_preserves_authored_rows_and_materializes_placeholders(
    tmp_path: Path,
) -> None:
    entry, _summaries, store = _fixture_selection(tmp_path)
    nodes = _chunk_records(store.directory, entry, "nodes")
    edges = _chunk_records(store.directory, entry, "edges")

    assert entry["stats"] == {
        "authoredNodes": 4,
        "placeholderNodes": 2,
        "totalNodes": 6,
        "authoredEdges": 4,
        "totalEdges": 4,
        "resolvedEdges": 2,
        "unresolvedEdges": 2,
        "placeholderEndpointReferences": 2,
    }
    assert len(nodes) == 6
    assert len(edges) == 4
    assert entry["diagnostics"]["duplicateAuthoredNodes"] == 1
    assert entry["diagnostics"]["duplicateAuthoredEdges"] == 1
    assert len({node["id"] for node in nodes}) == len(nodes)
    assert len({edge["id"] for edge in edges}) == len(edges)

    node_ids = {node["id"] for node in nodes}
    assert all(edge["source"] in node_ids for edge in edges)
    assert all(edge["target"] in node_ids for edge in edges)
    placeholders = [node for node in nodes if node.get("placeholder")]
    assert len(placeholders) == 2
    assert {node["type"] for node in placeholders} == {"미해결참조"}
    assert all(
        math.isfinite(float(node["x"]))
        and math.isfinite(float(node["y"]))
        for node in nodes
    )
    assert all(
        descriptor["compressedBytes"] < store.max_compressed_bytes
        for descriptor in entry["chunks"]
    )


def test_full_graph_chunks_are_deterministic_and_content_addressed(
    tmp_path: Path,
) -> None:
    first, summaries, store = _fixture_selection(tmp_path)
    packs = {
        pack_id: PackFile(tmp_path / f"{pack_id}.zip")
        for pack_id in ("pack-a", "pack-b")
    }
    second = full_graph.build_full_graph_selection(
        ["pack-a", "pack-b"],
        pack_summaries=summaries,
        chunk_store=store,
        project_names={"Fixture Project"},
        find_pack_fn=packs.__getitem__,
    )

    assert first == second
    assert len(list(store.directory.glob("*.json.gz"))) == len(
        store.unique_chunks
    )
    assert full_graph.full_graph_selection_key(
        ["pack-a", "pack-b"],
    ) != full_graph.full_graph_selection_key(["pack-b", "pack-a"])


def test_full_graph_signature_is_stable_across_registry_and_runtime_shapes() -> None:
    registry_summary = {
        **_summary("pack-a", 3, 4),
        "drive": {
            "fileId": "drive-file-a",
            "modifiedTime": "2026-07-30T00:00:00Z",
            "sizeBytes": 100,
            "md5Checksum": "registry-only-checksum",
        },
    }
    runtime_summary = {
        "id": "pack-a",
        "sizeBytes": 100,
        "counts": {
            "nodes": 3,
            "edges": 4,
            "documents": 0,
        },
    }

    registry_signature = full_graph.full_graph_pack_signature(
        ["pack-a"],
        [registry_summary],
        registry_generation="generation-1",
    )
    runtime_signature = full_graph.full_graph_pack_signature(
        ["pack-a"],
        [runtime_summary],
        registry_generation="generation-1",
    )

    assert runtime_signature == registry_signature
    assert full_graph.full_graph_pack_signature(
        ["pack-a"],
        [{**runtime_summary, "sizeBytes": 101}],
        registry_generation="generation-1",
    ) != registry_signature
    assert full_graph.full_graph_pack_signature(
        ["pack-a"],
        [runtime_summary],
        registry_generation="generation-2",
    ) != registry_signature


def test_full_graph_capability_is_scoped_short_lived_and_tamper_evident(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MODULAR_ONTOLOGY_SESSION_SECRET", "test-secret")
    capability = full_graph.issue_full_graph_capability(
        "project-a",
        "selection-a",
        now=1_000,
        ttl_seconds=60,
    )

    assert capability
    assert full_graph.validate_full_graph_capability(
        capability,
        "project-a",
        "selection-a",
        now=1_059,
    )
    assert not full_graph.validate_full_graph_capability(
        capability,
        "project-b",
        "selection-a",
        now=1_059,
    )
    assert not full_graph.validate_full_graph_capability(
        capability,
        "project-a",
        "selection-a",
        now=1_060,
    )
    assert not full_graph.validate_full_graph_capability(
        f"{capability[:-1]}{'a' if capability[-1] != 'a' else 'b'}",
        "project-a",
        "selection-a",
        now=1_059,
    )


def test_full_graph_capability_is_not_issued_without_auth_secret(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MODULAR_ONTOLOGY_SESSION_SECRET", raising=False)
    monkeypatch.delenv("MODULAR_ONTOLOGY_AUTH_SECRET", raising=False)

    assert (
        full_graph.issue_full_graph_capability(
            "project-a",
            "selection-a",
        )
        is None
    )


def test_chunk_store_splits_below_compressed_limit(tmp_path: Path) -> None:
    store = full_graph.ContentAddressedChunkStore(
        tmp_path,
        max_compressed_bytes=512,
        target_uncompressed_bytes=100_000,
    )
    descriptors = store.add_records(
        "nodes",
        (
            {
                "id": (
                    f"node-{index}-"
                    f"{hashlib.sha256(f'id-{index}'.encode()).hexdigest()}"
                ),
                "label": hashlib.sha256(
                    f"label-{index}".encode(),
                ).hexdigest(),
                "type": "Element",
                "packId": "pack-a",
                "x": float(index),
                "y": float(index),
                "size": 5.0,
            }
            for index in range(100)
        ),
    )

    assert len(descriptors) > 1
    assert all(
        descriptor["compressedBytes"] <= 512
        for descriptor in descriptors
    )
    assert sum(descriptor["count"] for descriptor in descriptors) == 100


def test_common_104_pack_group_aggregates_to_a_few_chunks(
    tmp_path: Path,
) -> None:
    packs: dict[str, PackFile] = {}
    summaries: list[dict] = []
    pack_ids: list[str] = []
    for index in range(104):
        pack_id = f"common-{index:03d}"
        node_id = f"node-{index:03d}"
        packs[pack_id] = _write_pack(
            tmp_path,
            pack_id,
            nodes=[{"id": node_id, "label": node_id}],
            edges=[
                {
                    "id": f"edge-{index:03d}",
                    "source": node_id,
                    "target": node_id,
                    "relation": "self",
                }
            ],
        )
        pack_ids.append(pack_id)
        summaries.append(_summary(pack_id, 1, 1))

    store = full_graph.ContentAddressedChunkStore(
        tmp_path / "artifacts",
        max_compressed_bytes=100_000,
        target_uncompressed_bytes=1_000_000,
    )
    entry = full_graph.build_full_graph_selection(
        pack_ids,
        pack_summaries=summaries,
        chunk_store=store,
        pack_group_keys={
            pack_id: '["_Common","specification"]'
            for pack_id in pack_ids
        },
        find_pack_fn=packs.__getitem__,
    )

    assert entry["stats"]["totalNodes"] == 104
    assert entry["stats"]["totalEdges"] == 104
    assert entry["diagnostics"]["recordGroups"] == 1
    assert entry["chunkCount"] == len(entry["chunks"]) == 2
    assert {chunk["kind"] for chunk in entry["chunks"]} == {
        "nodes",
        "edges",
    }


def test_manifest_enforces_per_selection_chunk_budget(
    tmp_path: Path,
) -> None:
    entry, _summaries, store = _fixture_selection(tmp_path)
    entry_chunks = len(entry["chunks"])

    with pytest.raises(RuntimeError, match="request-count budget"):
        full_graph.write_full_graph_manifest(
            store.directory,
            entries={entry["selectionKey"]: entry},
            chunk_store=store,
            generated_at="",
            source_registry="fixture.json",
            max_combinations=10,
            max_chunks_per_selection=entry_chunks - 1,
            max_total_compressed_bytes=1_000_000,
        )

    manifest = full_graph.write_full_graph_manifest(
        store.directory,
        entries={entry["selectionKey"]: entry},
        chunk_store=store,
        generated_at="",
        source_registry="fixture.json",
        max_combinations=10,
        max_chunks_per_selection=entry_chunks,
        max_total_compressed_bytes=1_000_000,
    )

    assert manifest["budgets"]["maxChunksPerSelection"] == entry_chunks
    assert manifest["budgets"]["largestSelectionChunks"] == entry_chunks


def test_manifest_loader_rejects_same_size_chunk_tampering(
    monkeypatch,
    tmp_path: Path,
) -> None:
    entry, summaries, store = _fixture_selection(tmp_path)
    full_graph.write_full_graph_manifest(
        store.directory,
        entries={entry["selectionKey"]: entry},
        chunk_store=store,
        generated_at="",
        source_registry="fixture.json",
        max_combinations=10,
        max_total_compressed_bytes=1_000_000,
    )
    monkeypatch.setenv(
        "MODULAR_ONTOLOGY_FULL_GRAPH_DIR",
        str(store.directory),
    )
    full_graph.invalidate_full_graph_manifest_cache()

    loaded = full_graph.load_full_graph_entry(
        ["pack-a", "pack-b"],
        pack_summaries=summaries,
    )
    descriptor = loaded["chunks"][0]
    path, _descriptor, _entry = full_graph.resolve_full_graph_chunk(
        entry["selectionKey"],
        descriptor["file"],
        pack_summaries=summaries,
    )
    original = path.read_bytes()
    tampered = bytes([original[0] ^ 0x01]) + original[1:]
    path.write_bytes(tampered)

    with pytest.raises(full_graph.FullGraphChunkNotFoundError):
        full_graph.resolve_full_graph_chunk(
            entry["selectionKey"],
            descriptor["file"],
            pack_summaries=summaries,
        )


def test_builder_grouping_matches_ui_precedence_and_filename_fallback() -> None:
    packs = {
        "project": {
            "id": "project",
            "filename": "scope__filename-category__project.zip",
            "driveScope": "scope",
            "projectCategory": "project-category",
            "commonCategory": "common-category",
            "driveCategory": "drive-category",
        },
        "project-peer": {
            "id": "project-peer",
            "filename": "scope__other__project-peer.zip",
            "driveScope": "scope",
            "projectCategory": "project-category",
        },
        "fallback": {
            "id": "fallback",
            "filename": "scope__filename-category__fallback.zip",
            "projectCategory": "ignored-without-scope",
        },
        "single": {
            "id": "single",
            "filename": "single.zip",
        },
    }
    project = {
        "id": "p",
        "name": "P",
        "packIds": [
            "project",
            "project-peer",
            "fallback",
            "single",
        ],
    }

    groups = artifact_builder._display_groups(project, packs)

    assert groups == [
        ["project", "project-peer"],
        ["fallback"],
        ["single"],
    ]
    selections = artifact_builder.project_ui_selections(
        [project],
        packs,
        min_pack_count=1,
        max_combinations=8,
    )
    assert len(selections) == 7


def test_small_artifact_build_is_deterministic_and_reuses_shared_chunks(
    tmp_path: Path,
) -> None:
    pack_shared = _write_pack(
        tmp_path,
        "shared",
        nodes=[{"id": "shared-node", "label": "Shared"}],
        edges=[],
    )
    pack_project = _write_pack(
        tmp_path,
        "project",
        nodes=[{"id": "project-node", "label": "Project"}],
        edges=[
            {
                "id": "link",
                "source": "project-node",
                "target": "shared-node",
                "relation": "uses",
            }
        ],
    )
    packs = {
        "shared": pack_shared,
        "project": pack_project,
    }
    registry = {
        "generatedAt": "2026-01-01T00:00:00Z",
        "packs": [
            {
                **_summary("shared", 1, 0),
                "filename": "_Common__common__shared.zip",
                "driveScope": "_Common",
                "commonCategory": "common",
                "commonScoped": True,
            },
            {
                **_summary("project", 1, 1),
                "filename": "project-a__model__project.zip",
                "driveScope": "project-a",
                "projectCategory": "model",
            },
        ],
        "projects": [
            {
                "id": "project-a",
                "name": "Project A",
                "packIds": ["shared", "project"],
            }
        ],
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(registry),
        encoding="utf-8",
    )
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first = artifact_builder.build_full_graph_artifacts(
        registry_path=registry_path,
        output_dir=first_dir,
        max_combinations=8,
        max_total_compressed_bytes=1_000_000,
        max_compressed_chunk_bytes=2_000,
        target_uncompressed_chunk_bytes=300,
        find_pack_fn=packs.__getitem__,
    )
    second = artifact_builder.build_full_graph_artifacts(
        registry_path=registry_path,
        output_dir=second_dir,
        max_combinations=8,
        max_total_compressed_bytes=1_000_000,
        max_compressed_chunk_bytes=2_000,
        target_uncompressed_chunk_bytes=300,
        find_pack_fn=packs.__getitem__,
    )

    assert first == second
    first_files = {
        path.name: path.read_bytes()
        for path in first_dir.iterdir()
        if path.is_file()
    }
    second_files = {
        path.name: path.read_bytes()
        for path in second_dir.iterdir()
        if path.is_file()
    }
    assert first_files == second_files
    assert len(first["entries"]) == 3
    referenced_chunks = sum(
        len(entry["chunks"])
        for entry in first["entries"].values()
    )
    assert referenced_chunks > first["budgets"]["uniqueChunks"]
    for entry in first["entries"].values():
        node_ids = {
            node["id"]
            for node in _chunk_records(first_dir, entry, "nodes")
        }
        for edge in _chunk_records(first_dir, entry, "edges"):
            assert edge["source"] in node_ids
            assert edge["target"] in node_ids


def test_full_graph_manifest_authorizes_before_loading_artifact(
    monkeypatch,
) -> None:
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(
        app,
        "current_user",
        lambda _authorization: {"email": "viewer@example.com"},
    )

    def deny(_project_id, _user):
        raise app.HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(app, "ensure_project_access", deny)
    monkeypatch.setattr(
        app,
        "load_full_graph_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("artifact must not load before project access")
        ),
    )

    with pytest.raises(app.HTTPException) as exc_info:
        app.project_full_graph_manifest(
            "private",
            pack_ids="pack-a",
            authorization="Bearer viewer",
        )

    assert exc_info.value.status_code == 403


def test_full_graph_manifest_contract_and_canonical_pack_order(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MODULAR_ONTOLOGY_SESSION_SECRET", raising=False)
    monkeypatch.delenv("MODULAR_ONTOLOGY_AUTH_SECRET", raising=False)
    project = {
        "id": "project-a",
        "name": "Project A",
        "packIds": ["pack-a", "pack-b"],
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(
        app,
        "current_user",
        lambda _authorization: {"email": "admin@example.com"},
    )
    monkeypatch.setattr(
        app,
        "ensure_project_access",
        lambda _project_id, _user: project,
    )
    monkeypatch.setattr(
        app,
        "list_packs",
        lambda: [_summary("pack-a", 1, 1), _summary("pack-b", 1, 1)],
    )

    def load(pack_ids, **_kwargs):
        captured["packIds"] = pack_ids
        return {
            "selectionKey": "a" * 64,
            "stats": {
                "authoredNodes": 2,
                "placeholderNodes": 1,
                "totalNodes": 3,
                "authoredEdges": 2,
                "totalEdges": 2,
                "resolvedEdges": 1,
                "unresolvedEdges": 1,
                "placeholderEndpointReferences": 1,
            },
            "diagnostics": {},
            "chunks": [
                {
                    "file": f"{'b' * 64}.json.gz",
                    "kind": "nodes",
                    "count": 3,
                    "compressedBytes": 100,
                    "uncompressedBytes": 300,
                    "sha256": "b" * 64,
                }
            ],
        }

    monkeypatch.setattr(app, "load_full_graph_entry", load)

    payload = app.project_full_graph_manifest(
        "project-a",
        pack_ids="pack-b,pack-a",
        authorization="Bearer admin",
    )

    assert captured["packIds"] == ["pack-a", "pack-b"]
    assert payload["version"] == 3
    assert payload["totalNodes"] == 3
    assert payload["totalEdges"] == 2
    assert payload["chunks"][0]["url"].endswith(
        f"/{'b' * 64}.json.gz"
    )
    assert urlsplit(payload["chunks"][0]["url"]).query == ""


def test_full_graph_manifest_adds_valid_scoped_capability(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MODULAR_ONTOLOGY_SESSION_SECRET", "test-secret")
    project = {
        "id": "project-a",
        "name": "Project A",
        "packIds": ["pack-a"],
    }
    selection_key = "a" * 64
    filename = f"{'b' * 64}.json.gz"
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(
        app,
        "current_user",
        lambda _authorization: {"email": "admin@example.com"},
    )
    monkeypatch.setattr(
        app,
        "ensure_project_access",
        lambda _project_id, _user: project,
    )
    monkeypatch.setattr(
        app,
        "list_packs",
        lambda: [_summary("pack-a", 1, 1)],
    )
    monkeypatch.setattr(
        app,
        "load_full_graph_entry",
        lambda *_args, **_kwargs: {
            "selectionKey": selection_key,
            "stats": {
                "totalNodes": 1,
                "totalEdges": 1,
            },
            "chunks": [
                {
                    "file": filename,
                    "kind": "nodes",
                    "count": 1,
                    "compressedBytes": 100,
                    "sha256": "b" * 64,
                }
            ],
        },
    )

    payload = app.project_full_graph_manifest(
        "project-a",
        pack_ids="pack-a",
        authorization="Bearer admin",
    )

    chunk_url = urlsplit(payload["chunks"][0]["url"])
    graph_access = parse_qs(chunk_url.query)["graph_access"][0]
    assert chunk_url.path.endswith(f"/{filename}")
    assert full_graph.validate_full_graph_capability(
        graph_access,
        "project-a",
        selection_key,
    )


def test_full_graph_chunk_capability_skips_runtime_registry_and_user_auth(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MODULAR_ONTOLOGY_SESSION_SECRET", "test-secret")
    selection_key = "a" * 64
    path = tmp_path / f"{'b' * 64}.json.gz"
    path.write_bytes(gzip.compress(b'{"nodes":[]}'))
    descriptor = {
        "file": path.name,
        "kind": "nodes",
        "count": 0,
        "compressedBytes": path.stat().st_size,
        "sha256": "b" * 64,
    }
    entry = {
        "selectionKey": selection_key,
        "packIds": ["pack-a"],
        "chunks": [descriptor],
    }
    capability = full_graph.issue_full_graph_capability(
        "project-a",
        selection_key,
    )
    assert capability

    def unexpected(*_args, **_kwargs):
        raise AssertionError("capability path must not hydrate runtime metadata")

    monkeypatch.setattr(app, "ensure_runtime_registry", unexpected)
    monkeypatch.setattr(app, "current_user", unexpected)
    monkeypatch.setattr(app, "ensure_project_access", unexpected)
    monkeypatch.setattr(app, "list_packs", unexpected)
    monkeypatch.setattr(app, "resolve_full_graph_chunk", unexpected)
    monkeypatch.setattr(
        app,
        "resolve_full_graph_chunk_from_manifest",
        lambda *_args, **_kwargs: (path, descriptor, entry),
    )

    response = app.project_full_graph_chunk(
        "project-a",
        selection_key,
        path.name,
        graph_access=capability,
        authorization="Bearer legacy-session-is-still-sent-by-the-client",
    )

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"].endswith("immutable")


def test_full_graph_chunk_rejects_invalid_capabilities_without_drive_sync(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MODULAR_ONTOLOGY_SESSION_SECRET", "test-secret")
    selection_key = "a" * 64
    valid = full_graph.issue_full_graph_capability(
        "project-a",
        selection_key,
    )
    expired = full_graph.issue_full_graph_capability(
        "project-a",
        selection_key,
        now=1,
        ttl_seconds=1,
    )
    assert valid
    assert expired
    tampered = f"{valid[:-1]}{'a' if valid[-1] != 'a' else 'b'}"
    cases = [
        ("project-b", valid),
        ("project-a", expired),
        ("project-a", tampered),
    ]

    def unexpected(*_args, **_kwargs):
        raise AssertionError("invalid anonymous capability must fail before Drive sync")

    monkeypatch.setattr(app, "ensure_runtime_registry", unexpected)
    for project_id, capability in cases:
        with pytest.raises(app.HTTPException) as exc_info:
            app.project_full_graph_chunk(
                project_id,
                selection_key,
                f"{'b' * 64}.json.gz",
                graph_access=capability,
                authorization="Bearer valid-looking-legacy-session",
            )
        assert exc_info.value.status_code == 401


def test_full_graph_chunk_uses_private_immutable_gzip_and_etag(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"{'b' * 64}.json.gz"
    path.write_bytes(gzip.compress(b'{"nodes":[]}'))
    descriptor = {
        "file": path.name,
        "kind": "nodes",
        "count": 0,
        "compressedBytes": path.stat().st_size,
        "sha256": "b" * 64,
    }
    entry = {
        "packIds": ["pack-a"],
        "chunks": [descriptor],
    }
    project = {
        "id": "project-a",
        "packIds": ["pack-a"],
    }
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(
        app,
        "current_user",
        lambda _authorization: {"email": "admin@example.com"},
    )
    monkeypatch.setattr(
        app,
        "ensure_project_access",
        lambda _project_id, _user: project,
    )
    monkeypatch.setattr(app, "list_packs", lambda: [])
    monkeypatch.setattr(
        app,
        "resolve_full_graph_chunk",
        lambda *_args, **_kwargs: (path, descriptor, entry),
    )

    response = app.project_full_graph_chunk(
        "project-a",
        "a" * 64,
        path.name,
        authorization="Bearer admin",
    )
    cached = app.project_full_graph_chunk(
        "project-a",
        "a" * 64,
        path.name,
        authorization="Bearer admin",
        if_none_match=f'"{"b" * 64}"',
    )

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"].endswith("immutable")
    assert response.headers["etag"] == f'"{"b" * 64}"'
    assert response.headers["vary"] == "Authorization"
    assert cached.status_code == 304


def test_full_graph_chunk_rejects_cross_project_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"{'b' * 64}.json.gz"
    path.write_bytes(gzip.compress(b'{"nodes":[]}'))
    descriptor = {
        "file": path.name,
        "kind": "nodes",
        "count": 0,
        "compressedBytes": path.stat().st_size,
        "sha256": "b" * 64,
    }
    entry = {
        "packIds": ["shared", "other-project-pack"],
        "chunks": [descriptor],
    }
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(
        app,
        "current_user",
        lambda _authorization: {"email": "viewer@example.com"},
    )
    monkeypatch.setattr(
        app,
        "ensure_project_access",
        lambda _project_id, _user: {
            "id": "project-a",
            "packIds": ["shared", "project-a-pack"],
        },
    )
    monkeypatch.setattr(app, "list_packs", lambda: [])
    monkeypatch.setattr(
        app,
        "resolve_full_graph_chunk",
        lambda *_args, **_kwargs: (path, descriptor, entry),
    )

    with pytest.raises(app.HTTPException) as exc_info:
        app.project_full_graph_chunk(
            "project-a",
            "a" * 64,
            path.name,
            authorization="Bearer viewer",
        )

    assert exc_info.value.status_code == 403


def test_node_detail_preserves_original_ids_with_namespace_delimiters(
    monkeypatch,
) -> None:
    project = {
        "id": "project-a",
        "packIds": ["pack-a"],
    }
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(app, "ensure_runtime_registry", lambda: None)
    monkeypatch.setattr(
        app,
        "current_user",
        lambda _authorization: {"email": "admin@example.com"},
    )
    monkeypatch.setattr(
        app,
        "ensure_project_access",
        lambda _project_id, _user: project,
    )
    monkeypatch.setattr(
        app,
        "full_graph_node_detail",
        lambda pack_id, original_id, occurrence=0: captured.append(
            (pack_id, original_id, occurrence)
        )
        or {"id": f"{pack_id}::{original_id}"},
    )

    app.project_full_graph_node_detail(
        "project-a",
        "pack-a::original::with::colons::duplicate:2",
        original_id="original::with::colons",
        occurrence=2,
        authorization="Bearer admin",
    )

    assert captured == [("pack-a", "original::with::colons", 2)]

    app.project_full_graph_node_detail(
        "project-a",
        "pack-a::literal::duplicate:1",
        authorization="Bearer admin",
    )

    assert captured[-1] == ("pack-a", "literal::duplicate:1", 0)


def test_duplicate_node_detail_reads_exact_authored_occurrence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    pack = _write_pack(
        tmp_path,
        "pack-a",
        nodes=[
            {
                "id": "same",
                "label": "First",
                "properties": {"version": 1},
            },
            {
                "id": "same",
                "label": "Second",
                "properties": {"version": 2},
            },
        ],
        edges=[],
    )
    monkeypatch.setattr(
        full_graph,
        "find_pack",
        lambda _pack_id: pack,
    )

    first = full_graph.full_graph_node_detail(
        "pack-a",
        "same",
        occurrence=0,
    )
    second = full_graph.full_graph_node_detail(
        "pack-a",
        "same",
        occurrence=1,
    )

    assert first is not None
    assert second is not None
    assert first["label"] == "First"
    assert first["properties"] == {"version": 1}
    assert second["label"] == "Second"
    assert second["properties"] == {"version": 2}
    assert second["id"] == "pack-a::same::duplicate:1"
