from __future__ import annotations

import json
import zipfile

import pytest

from modular_ontology import pack_index, store


@pytest.fixture
def evidence_pack(tmp_path, monkeypatch):
    path = tmp_path / "evidence.zip"
    chunk = {
        "chunk_id": " c1 ", "content": "Beam\n evidence", "title": "Detail",
        "document_id": "d1", "source_url": "https://example.com/detail",
        "source_ref": {"page": 2}, "compact_source_refs": [{"row": 0}],
        "metadata": {"material": "강재"},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"pack_id": "evidence"}))
        archive.writestr("documents/Beam.md", "\ufeffBeam\t evidence")
        archive.writestr("ignored.md", "Beam")
        archive.writestr("documents/ignored.txt", "Beam")
        archive.writestr("cloud/chunks.jsonl", "\ufeff" + "\n".join([
            "", "invalid json", "[]", "null", json.dumps(chunk, ensure_ascii=False),
            json.dumps({"id": "alias", "text": "Beam evidence", "heading": "Alias"}),
            json.dumps({"text": "Beam evidence", "metadata": []}),
        ]))
    pack = pack_index.PackFile(path)
    monkeypatch.setattr(pack_index, "find_pack", lambda pack_id: pack)
    return pack


def test_zip_search_preserves_order_aliases_and_source_fields(evidence_pack):
    results = pack_index.search_pack("evidence", "beam")
    assert [item["path"] for item in results] == [
        "documents/Beam.md", "cloud/chunks.jsonl#c1",
        "cloud/chunks.jsonl#alias", "cloud/chunks.jsonl",
    ]
    assert results[0] == {
        "path": "documents/Beam.md", "title": "Beam", "snippet": "Beam evidence", "score": 4.62,
    }
    assert results[1] == {
        "path": "cloud/chunks.jsonl#c1", "title": "Detail", "snippet": "Beam evidence",
        "score": 3.96, "source": "cloud-chunk", "chunkId": "c1", "documentId": "d1",
        "sourceUrl": "https://example.com/detail", "sourceRef": {"page": 2},
        "compactSourceRefs": [{"row": 0}], "metadata": {"material": "강재"},
    }
    assert results[2]["title"] == "Alias"
    assert results[3]["title"] == "Evidence chunk"
    assert results[3]["chunkId"] is None
    assert results[3]["metadata"] == {}


def test_zip_search_matches_metadata_without_body_hit(evidence_pack):
    results = pack_index.search_pack("evidence", "강재")
    assert len(results) == 1
    assert results[0]["chunkId"] == "c1"
    assert results[0]["snippet"] == "Beam evidence"


@pytest.mark.parametrize("query, limit, count", [("the", 8, 0), ("absent", 8, 0), ("beam", 0, 0), ("beam", 2, 2)])
def test_zip_search_limits_and_empty_matches(evidence_pack, query, limit, count):
    assert len(pack_index.search_pack("evidence", query, limit)) == count


def test_index_and_db_search_preserve_paths_and_fallback(evidence_pack, tmp_path, monkeypatch):
    db_path = tmp_path / "index.sqlite3"
    conn = store.connect(db_path)
    try:
        store.init_db(conn)
        assert store.index_pack(conn, evidence_pack)["documents"] == 4
        assert store.index_pack(conn, evidence_pack)["documents"] == 4
        rows = conn.execute("SELECT path, title, body FROM documents ORDER BY id").fetchall()
        assert [row["path"] for row in rows] == [
            "documents/Beam.md", "cloud/chunks.jsonl#c1", "cloud/chunks.jsonl#alias", "cloud/chunks.jsonl#4",
        ]
        assert rows[0]["body"] == "Beam\t evidence"
    finally:
        conn.close()
    monkeypatch.setattr(pack_index, "_db_connect", lambda: store.connect(db_path))
    monkeypatch.setattr(pack_index, "_db_pack_summary", lambda pack_id: {"id": "evidence"})

    def missing_pack(pack_id):
        raise FileNotFoundError(pack_id)

    monkeypatch.setattr(pack_index, "find_pack", missing_pack)
    fallback = pack_index.search_pack("evidence", "beam")
    indexed = store.search_documents("evidence", "beam", db_path=db_path)
    assert fallback == [{key: value for key, value in row.items() if key != "chunkId"} for row in indexed]
    assert len(fallback) == 4
    assert all(row["snippet"] == "Beam evidence" for row in fallback)
    assert indexed[-1]["chunkId"] == "4"


def test_search_snippet_preserves_context_window(evidence_pack):
    with zipfile.ZipFile(evidence_pack.path, "a") as archive:
        archive.writestr("documents/context.md", "x" * 150 + "BEAM" + "y" * 300)
    result = next(row for row in pack_index.search_pack("evidence", "beam") if row["title"] == "context")
    assert result["snippet"] == "x" * 120 + "BEAM" + "y" * 256
