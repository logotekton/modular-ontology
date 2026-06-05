from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import PACKS_DIR, ROOT, USE_STRUCTURED_DATA_DIR

UPLOAD_DIR = PACKS_DIR


TYPE_COLORS = {
    "Document": "#2563eb",
    "Chunk": "#94a3b8",
    "Module": "#0f766e",
    "ModuleType": "#14b8a6",
    "Assembly": "#f59e0b",
    "SinglePart": "#ef4444",
    "Material": "#64748b",
    "Section": "#8b5cf6",
    "Category": "#22c55e",
    "Element": "#0891b2",
    "Evidence": "#84cc16",
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
    search_dirs = [PACKS_DIR] if USE_STRUCTURED_DATA_DIR else [root, PACKS_DIR, root / "data" / "packs"]
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*.zip"):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                packs.append(PackFile(resolved))
    return sorted(packs, key=lambda item: item.path.name.lower())


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
        "color": TYPE_COLORS.get(kind, "#64748b"),
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


def summarize_pack(pack: PackFile) -> dict[str, Any]:
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
        node_count = counts.get("nodes") or _count_lines(zf, graph_nodes_path) or derived_node_count
        edge_count = counts.get("edges") or _count_lines(zf, graph_edges_path) or _count_lines(zf, "backdata/jsonl/edges.jsonl")
        document_count = counts.get("documents") or len([name for name in names if name.startswith("documents/") and name.endswith(".md")])

        source = "Revit IFC" if "revit" in pack.id.lower() else "Advance Steel" if "advance" in pack.id.lower() else "Ontology"
        validation = build_summary.get("validation_status") or "READY"

        return {
            "id": manifest.get("pack_id") or pack.id,
            "filename": pack.path.name,
            "title": manifest.get("title") or pack.id.replace("-", " ").title(),
            "format": manifest.get("format", "ontology-pack"),
            "description": manifest.get("description") or _extract_readme(zf),
            "source": source,
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


def list_packs() -> list[dict[str, Any]]:
    return [summarize_pack(pack) for pack in discover_pack_files()]


def find_pack(pack_id: str) -> PackFile:
    for pack in discover_pack_files():
        summary_id = pack.id
        try:
            summary_id = summarize_pack(pack)["id"]
        except Exception:
            pass
        if pack.id == pack_id or pack.path.name == pack_id or summary_id == pack_id:
            return pack
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
    pack = find_pack(pack_id)
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
    pack = find_pack(pack_id)
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


def search_packs(query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    normalized = query.strip().lower()
    packs = list_packs()
    if not normalized:
        return packs[:limit]
    return [
        pack
        for pack in packs
        if normalized in pack["id"].lower()
        or normalized in pack["title"].lower()
        or normalized in pack["source"].lower()
        or normalized in str(pack.get("description", "")).lower()
    ][:limit]


def list_sources(pack_id: str | None = None) -> dict[str, Any]:
    packs = [summarize_pack(find_pack(pack_id))] if pack_id else list_packs()
    return {
        "count": len(packs),
        "sources": [
            {
                "pack_id": pack["id"],
                "title": pack["title"],
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
    pack = find_pack(pack_id)
    return build_graph_from_pack(pack, max_nodes=max_nodes, max_edges=max_edges)


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
    normalized = query.strip().lower()
    if not normalized:
        return {"pack_id": pack_id, "count": 0, "nodes": []}
    graph = build_graph(pack_id, max_nodes=5000, max_edges=0)
    matches = [
        node
        for node in graph["nodes"]
        if normalized in str(node.get("id", "")).lower()
        or normalized in str(node.get("label", "")).lower()
        or normalized in json.dumps(node.get("properties", {}), ensure_ascii=False).lower()
    ]
    matches.sort(key=lambda node: (str(node.get("type")) != "Module", len(str(node.get("label", "")))))
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


def edge_endpoint_id(endpoint: str | dict[str, Any]) -> str:
    return endpoint if isinstance(endpoint, str) else str(endpoint.get("id", ""))


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


def list_projects() -> list[dict[str, Any]]:
    packs = list_packs()
    from .project_store import list_projects as list_stored_projects

    return list_stored_projects(packs)


def search_pack(pack_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
    pack = find_pack(pack_id)
    normalized = query.strip().lower()
    if not normalized:
        return []
    results: list[dict[str, Any]] = []
    with zipfile.ZipFile(pack.path) as zf:
        for info in zf.infolist():
            if len(results) >= limit:
                break
            if not (info.filename.startswith("documents/") and info.filename.endswith(".md")):
                continue
            text = zf.read(info.filename).decode("utf-8-sig", errors="replace")
            lower = text.lower()
            hit = lower.find(normalized)
            if hit < 0:
                continue
            start = max(0, hit - 120)
            end = min(len(text), hit + 260)
            results.append(
                {
                    "path": info.filename,
                    "title": Path(info.filename).stem,
                    "snippet": re.sub(r"\s+", " ", text[start:end]).strip(),
                    "score": 1.0,
                }
            )
    return results


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
