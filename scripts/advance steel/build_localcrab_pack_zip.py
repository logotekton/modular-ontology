from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORK_ROOT = Path(__file__).resolve().parents[1]
PACK_ID = "advance_steel_samcheok_bldg_b_localcrab_ontology_pack"
PACK_SLUG = "advance-steel-samcheok-bldg-b-localcrab-ontology-pack"
PACK_TITLE = "Advance Steel Samcheok Building B LocalCrab Ontology Pack"
PACK_DIR = WORK_ROOT / PACK_SLUG
ZIP_PATH = WORK_ROOT / f"{PACK_SLUG}.zip"
SAFE_PACK_SLUG = "advance-steel-samcheok-bldg-b-localcrab-data-pack"
SAFE_PACK_DIR = WORK_ROOT / SAFE_PACK_SLUG
SAFE_ZIP_PATH = WORK_ROOT / f"{SAFE_PACK_SLUG}.zip"
SOURCE_XML = WORK_ROOT / "source" / "original.xml"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def safe_name(value: str | None) -> str:
    if not value:
        return "unmarked"
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in value)


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join("" if item is None else str(item) for item in row) + " |")
    return "\n".join(lines)


def reset_pack_dir() -> None:
    if PACK_DIR.exists():
        shutil.rmtree(PACK_DIR)
    PACK_DIR.mkdir(parents=True)


def reset_safe_pack_dir() -> None:
    if SAFE_PACK_DIR.exists():
        shutil.rmtree(SAFE_PACK_DIR)
    SAFE_PACK_DIR.mkdir(parents=True)


def write_zip() -> Path:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACK_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(PACK_DIR).as_posix())
    return ZIP_PATH


def write_safe_zip() -> Path:
    if SAFE_ZIP_PATH.exists():
        SAFE_ZIP_PATH.unlink()
    with zipfile.ZipFile(SAFE_ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SAFE_PACK_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(SAFE_PACK_DIR).as_posix())
    return SAFE_ZIP_PATH


def module_doc(row: dict[str, Any]) -> str:
    module_id = row["module_id"]
    mark_counts = row.get("assembly_mark_counts", {})
    instance_rows = [
        [
            item.get("assembly_mark") or "unmarked",
            item.get("assembly_role"),
            item.get("main_part"),
            item.get("single_part_count"),
            round(item.get("total_weight_kg") or 0, 3),
        ]
        for item in row.get("assembly_instances", [])
    ]
    return "\n".join(
        [
            f"# Module {module_id}",
            "",
            "XML-derived LocalCrab evidence for one Advance Steel module. Assembly instances are preserved in exported order and repeated marks are not collapsed except in explicit count fields.",
            "",
            "## Summary",
            "",
            f"- module_id: {module_id}",
            f"- module_type: {row.get('module_type')}",
            f"- assembly_count: {row.get('assembly_count')}",
            f"- single_part_count: {row.get('single_part_count')}",
            f"- main_single_part_count: {row.get('main_single_part_count')}",
            f"- attached_single_part_count: {row.get('attached_single_part_count')}",
            f"- total_weight_kg: {round(row.get('total_weight_kg') or 0, 3)}",
            f"- total_length_m: {round(row.get('total_length_m') or 0, 3)}",
            f"- assembly_mark_counts: {compact_json(mark_counts)}",
            f"- assembly_mark_sequence: {compact_json(row.get('assembly_mark_sequence', []))}",
            "",
            "## Assembly Mark Counts",
            "",
            markdown_table(["assembly_mark", "count"], [[mark or "unmarked", count] for mark, count in mark_counts.items()]),
            "",
            "## Assembly Instances",
            "",
            markdown_table(["assembly_mark", "assembly_role", "main_part", "single_part_count", "total_weight_kg"], instance_rows),
            "",
        ]
    )


def assembly_mark_doc(row: dict[str, Any]) -> str:
    mark = row.get("assembly_mark") or "unmarked"
    composition_rows = [
        [
            "main" if item.get("is_main_part") else "attached",
            item.get("role"),
            item.get("single_part_mark"),
            item.get("name"),
            item.get("part_category"),
            item.get("material"),
            item.get("section"),
            item.get("single_part_count"),
            round(item.get("weight_kg") or 0, 3),
        ]
        for item in row.get("single_part_composition", [])
    ]
    instance_rows = [
        [
            item.get("module_id"),
            item.get("module_type"),
            item.get("assembly_role"),
            item.get("main_part_name"),
            item.get("single_part_count"),
            item.get("attached_single_part_count"),
            round(item.get("total_weight_kg") or 0, 3),
        ]
        for item in row.get("assembly_instances", [])
    ]
    return "\n".join(
        [
            f"# Assembly Mark {mark}",
            "",
            "XML-derived LocalCrab evidence for one assembly mark. Counts preserve repeated assembly instances across modules.",
            "",
            "## Summary",
            "",
            f"- assembly_mark: {mark}",
            f"- assembly_count: {row.get('assembly_count')}",
            f"- module_count: {row.get('module_count')}",
            f"- single_part_count: {row.get('single_part_count')}",
            f"- attached_single_part_count: {row.get('attached_single_part_count')}",
            f"- total_weight_kg: {round(row.get('total_weight_kg') or 0, 3)}",
            f"- module_counts: {compact_json(row.get('module_counts', {}))}",
            f"- assembly_role_counts: {compact_json(row.get('assembly_role_counts', {}))}",
            f"- main_part_counts: {compact_json(row.get('main_part_counts', {}))}",
            "",
            "## Single Part Composition",
            "",
            markdown_table(
                ["part_kind", "role", "single_part_mark", "name", "part_category", "material", "section", "single_part_count", "weight_kg"],
                composition_rows,
            ),
            "",
            "## Assembly Instances",
            "",
            markdown_table(
                ["module_id", "module_type", "assembly_role", "main_part_name", "single_part_count", "attached_single_part_count", "total_weight_kg"],
                instance_rows,
            ),
            "",
        ]
    )


def graph_node(node_id: str, label: str, node_type: str, layer: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "label": label, "node_type": node_type, "layer": layer, "properties": properties}


def graph_edge(source: str, relation: str, target: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"source": source, "relation": relation, "target": target, "properties": {"original_relation": relation, **(properties or {})}}


def pick(row: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: row.get(key) for key in keys if key in row and row.get(key) is not None}


def build_graph() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    module_types = read_jsonl(WORK_ROOT / "jsonl" / "module_types.jsonl")
    modules = read_jsonl(WORK_ROOT / "jsonl" / "modules.jsonl")
    assemblies = read_jsonl(WORK_ROOT / "jsonl" / "assemblies.jsonl")
    single_parts = read_jsonl(WORK_ROOT / "jsonl" / "single_parts.jsonl")
    materials = read_jsonl(WORK_ROOT / "jsonl" / "materials.jsonl")
    sections = read_jsonl(WORK_ROOT / "jsonl" / "sections.jsonl")

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_node(row: dict[str, Any], label: str, node_type: str, layer: str, properties: dict[str, Any]) -> None:
        nodes[row["id"]] = graph_node(row["id"], label, node_type, layer, properties)

    def add_edge(source: str | None, relation: str, target: str | None, properties: dict[str, Any] | None = None) -> None:
        if not source or not target:
            return
        edge = graph_edge(source, relation, target, properties)
        edges[(source, relation, target)] = edge

    for row in module_types:
        add_node(row, row.get("module_type") or row["id"], "Entity", "concept", pick(row, ["module_type", "module_count", "assembly_count", "single_part_count", "total_weight_kg"]))
    for row in modules:
        add_node(row, row.get("module_id") or row["id"], "Entity", "concept", pick(row, ["module_id", "module_type", "assembly_count", "single_part_count", "main_single_part_count", "attached_single_part_count", "total_weight_kg", "total_length_m", "by_assembly_role", "by_single_part_category"]))
        add_edge(f"as:module_type:{row.get('module_type')}", "has_module", row["id"])
    for row in assemblies:
        label = row.get("assembly_mark") or row["id"]
        add_node(row, label, "Entity", "concept", pick(row, ["assembly_mark", "assembly_role", "module_id", "module_type", "main_part", "single_part_count", "main_single_part_count", "attached_single_part_count", "total_weight_kg", "main_part_weight_kg", "attached_part_weight_kg", "total_length_m", "by_single_part_role", "by_part_category"]))
        add_edge(f"as:module:{row.get('module_id')}", "has_assembly", row["id"])
        add_edge(row["id"], "has_main_part", row.get("main_single_part_id"))
    for row in single_parts:
        add_node(row, row.get("name") or row["id"], "Entity", "concept", pick(row, ["source_object_id", "source_class", "part_category", "name", "mark", "single_part_mark", "role", "module_type", "module_id", "assembly_id", "is_main_part", "material_name", "section_name", "profile_group", "length_m", "weight_kg"]))
        add_edge(row.get("assembly_id"), "has_single_part", row["id"])
    for row in materials:
        add_node(row, row.get("material_name") or row.get("name") or row["id"], "Entity", "concept", pick(row, ["material_name", "name"]))
    for row in sections:
        add_node(row, row.get("section_name") or row.get("name") or row["id"], "Entity", "concept", pick(row, ["section_name", "name", "standard", "profile_group"]))

    node_ids = set(nodes)
    clean_edges = [edge for key, edge in edges.items() if key[0] in node_ids and key[2] in node_ids]
    return list(nodes.values()), clean_edges


def build_documents() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    model = read_json(WORK_ROOT / "summary" / "model_summary.json")
    fasteners = read_json(WORK_ROOT / "summary" / "fastener_summary.json")
    modules = read_jsonl(WORK_ROOT / "user_views" / "module_user_view.jsonl")
    assembly_marks = read_jsonl(WORK_ROOT / "user_views" / "assembly_mark_summary_view.jsonl")

    module_rows = [
        [
            row["module_id"],
            row.get("module_type"),
            row.get("assembly_count"),
            row.get("single_part_count"),
            round(row.get("total_weight_kg") or 0, 3),
        ]
        for row in modules
    ]
    model_doc = (
        "# Samcheok Building B Model Summary\n\n"
        "XML-derived model-level evidence for LocalCrab comparison queries.\n\n"
        f"- module_type_count: {model.get('module_type_count')}\n"
        f"- module_count: {model.get('module_count')}\n"
        f"- assembly_count: {model.get('assembly_count')}\n"
        f"- single_part_count: {model.get('single_part_count')}\n"
        f"- total_weight_kg: {round(model.get('total_weight_kg') or 0, 3)}\n"
        f"- bolt_quantity: {fasteners.get('bolt_total_quantity_from_patterns')}\n"
        f"- anchor_quantity: {fasteners.get('anchor_total_quantity')}\n\n"
        "## Module Index\n\n"
        + markdown_table(["module_id", "module_type", "assembly_count", "single_part_count", "total_weight_kg"], module_rows)
        + "\n"
    )

    doc_specs: list[tuple[str, str, str, str]] = [("doc:model-summary", "Samcheok Building B Model Summary", "model_summary", model_doc)]
    for row in modules:
        doc_specs.append((f"doc:module:{safe_name(row['module_id'])}", f"Module {row['module_id']}", "module", module_doc(row)))
    for row in assembly_marks:
        mark = row.get("assembly_mark") or "unmarked"
        doc_specs.append((f"doc:assembly-mark:{safe_name(mark)}", f"Assembly Mark {mark}", "assembly_mark", assembly_mark_doc(row)))

    documents: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    document_index: list[dict[str, Any]] = []
    chunk_index: list[dict[str, Any]] = []
    for doc_id, title, role, text in doc_specs:
        source_url = f"local://advance-steel-samcheok-bldg-b/{doc_id}"
        documents.append(
            {
                "id": doc_id,
                "document_id": doc_id,
                "title": title,
                "source_url": source_url,
                "format": "markdown",
                "provider": "Advance Steel XML parser",
                "role": role,
                "content": text,
            }
        )
        chunk_id = f"chunk:{digest(doc_id)}"
        chunks.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "document_id": doc_id,
                "doc_id": doc_id,
                "parent_document_id": doc_id,
                "source_url": source_url,
                "title": title,
                "content": text,
                "text": text,
                "evidence_ref": f"{doc_id}#xml-derived",
                "claimVerificationStatus": "derived_from_source_xml",
                "metadata": {
                    "pack_id": PACK_ID,
                    "document_id": doc_id,
                    "role": role,
                    "source": "Advance Steel XML parser",
                },
            }
        )
        document_index.append(
            {
                "document_id": doc_id,
                "title": title,
                "source_url": source_url,
                "provider": "Advance Steel XML parser",
                "role": role,
                "chunk_count": 1,
            }
        )
        chunk_index.append(
            {
                "chunk_id": chunk_id,
                "parent_document_id": doc_id,
                "source_url": source_url,
                "evidence_ref": f"{doc_id}#xml-derived",
                "claimVerificationStatus": "derived_from_source_xml",
                "digest": digest(text),
                "summary": title,
            }
        )

    return documents, chunks, document_index, chunk_index


def build_pack() -> Path:
    reset_pack_dir()

    created_at = datetime.now(timezone.utc).isoformat()
    model = read_json(WORK_ROOT / "summary" / "model_summary.json")
    fasteners = read_json(WORK_ROOT / "summary" / "fastener_summary.json")
    nodes, edges = build_graph()
    documents, chunks, document_index, chunk_index = build_documents()

    write_jsonl(PACK_DIR / "cloud" / "documents.jsonl", documents)
    write_jsonl(PACK_DIR / "cloud" / "chunks.jsonl", chunks)
    write_jsonl(PACK_DIR / "graph" / "nodes.jsonl", nodes)
    write_jsonl(PACK_DIR / "graph" / "edges.jsonl", edges)

    evidence_index = [
        {
            "evidence_id": f"evidence:{row['chunk_id']}",
            "document_id": row["parent_document_id"],
            "chunk_id": row["chunk_id"],
            "source_url": row["source_url"],
            "basis": "Advance Steel XML source/original.xml parsed into module, assembly, single-part, fastener, material, and section records.",
        }
        for row in chunk_index
    ]
    write_jsonl(PACK_DIR / "00_index" / "document_index.jsonl", document_index)
    write_jsonl(PACK_DIR / "00_index" / "chunk_index.jsonl", chunk_index)
    write_jsonl(PACK_DIR / "00_index" / "evidence_index.jsonl", evidence_index)

    source_xml_size = SOURCE_XML.stat().st_size if SOURCE_XML.exists() else None
    index = {
        "pack_id": PACK_ID,
        "pack_title": PACK_TITLE,
        "format": "opencrab-cloud-pack-v1",
        "created_at": created_at,
        "entrypoints": {
            "documents": "cloud/documents.jsonl",
            "chunks": "cloud/chunks.jsonl",
            "nodes": "graph/nodes.jsonl",
            "edges": "graph/edges.jsonl",
        },
        "counts": {
            "documents": len(documents),
            "chunks": len(chunks),
            "evidence": len(evidence_index),
            "nodes": len(nodes),
            "edges": len(edges),
            "module_count": model.get("module_count"),
            "assembly_count": model.get("assembly_count"),
            "single_part_count": model.get("single_part_count"),
        },
        "validation_status": "built_pending_external_validation",
        "source_policy": {
            "source_xml_local_path": str(SOURCE_XML),
            "source_xml_size_bytes": source_xml_size,
            "source_xml_embedded_in_zip": False,
            "reason": "The original XML is retained locally as evidence but is larger than the pack skill's 5 MB per-file ZIP limit.",
        },
    }
    write_json(PACK_DIR / "00_index" / "index.json", index)

    write_json(
        PACK_DIR / "manifest.json",
        {
            "format": "opencrab-cloud-pack-v1",
            "pack_id": PACK_ID,
            "title": PACK_TITLE,
            "version": "1.0.0",
            "created_at": created_at,
            "language": "ko",
            "source": "Autodesk Advance Steel XML export parsed into LocalCrab/OpenCrab ontology artifacts",
            "documents": "cloud/documents.jsonl",
            "chunks": "cloud/chunks.jsonl",
            "graph_nodes": "graph/nodes.jsonl",
            "graph_edges": "graph/edges.jsonl",
        },
    )
    write_json(PACK_DIR / "pack.json", index)
    write_jsonl(
        PACK_DIR / "01_sources" / "source_records.jsonl",
        [
            {
                "source_id": "source:advance-steel-original-xml",
                "title": "Advance Steel Samcheok Building B original XML",
                "format": "xml",
                "local_path": str(SOURCE_XML),
                "size_bytes": source_xml_size,
                "embedded_in_zip": False,
            }
        ],
    )
    write_text(
        PACK_DIR / "01_sources" / "source_registry.md",
        "# Source Registry\n\n"
        f"- Original XML local path: `{SOURCE_XML}`\n"
        f"- Original XML size: {source_xml_size} bytes\n"
        "- ZIP embedding: false, because the source XML exceeds the skill file-size gate.\n"
        "- Evidence policy: all documents, chunks, graph nodes, and graph edges are derived from this XML through the local parser outputs.\n",
    )
    write_json(
        PACK_DIR / "02_schema" / "advance_steel_localcrab.schema.json",
        {
            "node_types": ["Entity"],
            "spaces": ["concept", "resource", "claim"],
            "domain_entities": ["ModuleType", "Module", "Assembly", "SinglePart", "Material", "Section"],
            "relations": ["has_module", "has_assembly", "has_single_part", "has_main_part"],
        },
    )
    write_json(
        PACK_DIR / "03_graph" / "graph_summary.json",
        {"nodes": len(nodes), "edges": len(edges), "edge_relations": sorted({edge["relation"] for edge in edges})},
    )
    write_json(
        PACK_DIR / "03_graph" / "neo4j_cypher_bundle.json",
        {
            "import_cypher": [
                "UNWIND $nodes AS row MERGE (n:Entity {id: row.id}) SET n += row.properties, n.label = row.label, n.node_type = row.node_type, n.layer = row.layer",
                "UNWIND $edges AS row MATCH (a {id: row.source}) MATCH (b {id: row.target}) MERGE (a)-[r:RELATED {relation: row.relation}]->(b) SET r += row.properties",
            ],
            "verify_cypher": [
                "MATCH (n) WHERE n.id STARTS WITH 'as:' RETURN count(n) AS advance_steel_nodes",
                "MATCH ()-[r]->() WHERE exists(r.original_relation) RETURN r.original_relation AS relation, count(*) AS count ORDER BY count DESC",
            ],
            "sample_queries": [
                "1-03-A 모듈의 무게와 어셈블리 목록",
                "G20 어셈블리 마크의 전체 개수와 single part 구성",
                "볼트와 앙카 개수",
            ],
        },
    )
    write_jsonl(PACK_DIR / "05_hypotheses" / "none.jsonl", [])

    description = (
        "이 팩은 삼척 군관사 B동 Advance Steel XML에서 파싱한 모듈, 어셈블리, 싱글파트, 단면, 재질, 볼트/앙카 집계를 LocalCrab/OpenCrab Cloud Pack 구조로 정리한다. "
        "원본 XML은 로컬 증거 파일로 보존하고, ZIP에는 XML에서 파생된 문서 청크와 그래프 JSONL만 넣어 파일 크기 제한을 지킨다. "
        "모듈별 중량, 어셈블리 마크별 반복 개수, G20 같은 특정 부재 구성, 볼트/앙카 수량 비교 질의 성능을 SaaS OpenCrab 팩과 나란히 테스트하는 데 쓰기 좋다."
    )
    write_text(PACK_DIR / "06_reports" / "pack_description.md", description + "\n")
    write_json(
        PACK_DIR / "06_reports" / "build_summary.json",
        {
            "pack_id": PACK_ID,
            "created_at": created_at,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "document_count": len(documents),
            "chunk_count": len(chunks),
            "total_weight_kg": model.get("total_weight_kg"),
            "bolt_quantity": fasteners.get("bolt_total_quantity_from_patterns"),
            "anchor_quantity": fasteners.get("anchor_total_quantity"),
        },
    )
    write_text(
        PACK_DIR / "06_reports" / "validation_report.md",
        "# Validation Report\n\nBuilt pending ZIP validation. Run `ontology-pack-builder/scripts/validate_pack_zip.py` after ZIP creation.\n",
    )
    write_text(
        PACK_DIR / "07_examples" / "sample_queries.md",
        "# Sample Queries\n\n"
        "- 삼척 간부숙소 모듈 리스트\n"
        "- 1-03-A 무게\n"
        "- 1-03-A를 구성하는 어셈블리 목록\n"
        "- G20이 전체 모델에서 몇 개 들어가는지, 그리고 G20을 구성하는 SINGLE PART는 무엇인지 알려줘\n"
        "- 볼트와 앙카의 개수\n",
    )
    write_json(
        PACK_DIR / "07_examples" / "sample_outputs.json",
        {"note": "Use LocalCrab retrieval to generate live answers from cloud/chunks.jsonl and graph/*.jsonl."},
    )
    write_json(
        PACK_DIR / "opencrab" / "promotion_package_opencrab_compatible.json",
        {
            "pack_id": PACK_ID,
            "format": "opencrab-cloud-pack-v1",
            "nodes_ref": "graph/nodes.jsonl",
            "edges_ref": "graph/edges.jsonl",
            "node_count": len(nodes),
            "edge_count": len(edges),
            "note": "Full graph payload is stored once under graph/ to avoid duplicate oversized ZIP entries.",
        },
    )
    write_json(PACK_DIR / "opencrab" / "dry_run_report.json", {"status": "not_run", "reason": "LocalCrab dry-run MCP/apply was not invoked by this builder."})
    write_text(
        PACK_DIR / "README.md",
        f"# {PACK_TITLE}\n\n{description}\n\n"
        "## Ingest Entrypoints\n\n"
        "- `manifest.json`\n"
        "- `cloud/documents.jsonl`\n"
        "- `cloud/chunks.jsonl`\n"
        "- `graph/nodes.jsonl`\n"
        "- `graph/edges.jsonl`\n",
    )

    return write_zip()


def build_safe_data_pack() -> Path:
    reset_safe_pack_dir()
    created_at = datetime.now(timezone.utc).isoformat()
    model = read_json(WORK_ROOT / "summary" / "model_summary.json")
    fasteners = read_json(WORK_ROOT / "summary" / "fastener_summary.json")
    documents, chunks, document_index, chunk_index = build_documents()

    description = (
        "이 팩은 삼척 군관사 B동 Advance Steel XML에서 파생한 모듈별 문서와 어셈블리 마크별 문서를 Data ZIP 방식으로 담은 LocalCrab 검색 비교용 패키지다. "
        "그래프 import 단계에서 서버가 실패하지 않도록 Cloud Pack manifest와 graph payload를 제외하고, 사람이 읽을 수 있는 Markdown과 JSONL 청크만 포함한다. "
        "모듈 리스트, 모듈 중량, 어셈블리 목록, G20 구성, 볼트/앙카 수량 같은 질문의 문서 검색 품질을 OpenCrab 그래프 팩과 비교하는 데 사용한다."
    )

    write_json(
        SAFE_PACK_DIR / "pack.json",
        {
            "pack_id": SAFE_PACK_SLUG,
            "title": "Advance Steel Samcheok Building B LocalCrab Data Pack",
            "format": "opencrab-data-zip-readable-documents",
            "created_at": created_at,
            "document_count": len(documents),
            "chunk_count": len(chunks),
            "module_count": model.get("module_count"),
            "assembly_count": model.get("assembly_count"),
            "single_part_count": model.get("single_part_count"),
            "bolt_quantity": fasteners.get("bolt_total_quantity_from_patterns"),
            "anchor_quantity": fasteners.get("anchor_total_quantity"),
            "source_xml_local_path": str(SOURCE_XML),
        },
    )
    write_text(SAFE_PACK_DIR / "README.md", f"# Advance Steel Samcheok Building B LocalCrab Data Pack\n\n{description}\n")
    write_jsonl(SAFE_PACK_DIR / "documents.jsonl", documents)
    write_jsonl(SAFE_PACK_DIR / "chunks.jsonl", chunks)
    write_jsonl(SAFE_PACK_DIR / "00_index" / "document_index.jsonl", document_index)
    write_jsonl(SAFE_PACK_DIR / "00_index" / "chunk_index.jsonl", chunk_index)

    for doc in documents:
        role = safe_name(doc.get("role"))
        name = safe_name(str(doc["title"]).replace(" ", "_"))
        write_text(SAFE_PACK_DIR / "documents" / role / f"{name}.md", doc["content"])

    write_text(
        SAFE_PACK_DIR / "sample_queries.md",
        "# Sample Queries\n\n"
        "- 삼척 간부숙소 모듈 리스트\n"
        "- 1-03-A 무게\n"
        "- 1-03-A를 구성하는 어셈블리 목록\n"
        "- G20이 전체 모델에서 몇 개 들어가는지, 그리고 G20을 구성하는 SINGLE PART는 무엇인지 알려줘\n"
        "- 볼트와 앙카의 개수\n",
    )
    write_text(SAFE_PACK_DIR / "pack_description.md", description + "\n")
    return write_safe_zip()


def main() -> None:
    path = build_pack()
    safe_path = build_safe_data_pack()
    print(json.dumps({"zip": str(path), "pack_folder": str(PACK_DIR), "safe_zip": str(safe_path), "safe_pack_folder": str(SAFE_PACK_DIR)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
