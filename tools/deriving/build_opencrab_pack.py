from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DERIVED_FILES = [
    "bim_objects.jsonl",
    "material_facts.jsonl",
    "quantity_facts.jsonl",
    "drawing_documents.jsonl",
    "schedule_tables.jsonl",
    "schedule_rows.jsonl",
    "drawing_evidence.jsonl",
    "relationships.jsonl",
    "classification_decisions.jsonl",
    "source_coverage.jsonl",
]


NODE_SOURCE_FILES = [
    "bim_objects.jsonl",
    "material_facts.jsonl",
    "quantity_facts.jsonl",
    "drawing_documents.jsonl",
    "schedule_tables.jsonl",
    "schedule_rows.jsonl",
    "drawing_evidence.jsonl",
]


GRAMMAR_VERSION = "1.0.0"
PACK_FORMAT_VERSION = "opencrab-pack-v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def short_hash(value: Any) -> str:
    return sha256_text(stable_json(value))[:16]


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "-", value.strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")
    return slug or "bimgraph-revit-pack"


def node_id_from(value: str) -> str:
    return value if value else f"node:{short_hash(value)}"


def evidence_id_from(value: Any) -> str:
    return f"evidence:{short_hash(value)}"


def edge_id_from(value: Any) -> str:
    return f"edge:{short_hash(value)}"


def source_refs(row: dict[str, Any]) -> list[dict[str, Any]]:
    refs = row.get("source_refs")
    return refs if isinstance(refs, list) else []


def source_text_from_ref(ref: dict[str, Any]) -> str:
    parts = [
        str(ref.get("source_file") or ""),
        str(ref.get("source_id") or ""),
        str(ref.get("source_node_id") or ""),
        str(ref.get("raw_record_hash") or ""),
    ]
    return " | ".join(part for part in parts if part)


def evidence_from_source_ref(ref: dict[str, Any], *, collected_at: str, derived_dir: Path) -> dict[str, Any]:
    evidence_id = evidence_id_from({"source_ref": ref})
    source_file = str(ref.get("source_file") or "unknown")
    return {
        "evidence_id": evidence_id,
        "kind": "raw_record_ref",
        "source": {
            "url": None,
            "path": str(derived_dir),
            "title": source_file,
        },
        "hash": "sha256:" + str(ref.get("raw_record_hash") or short_hash(ref)),
        "collected_at": collected_at,
        "parser": {
            "status": "ok",
            "method": "deriving_source_ref",
            "warnings": [],
        },
        "ocr": None,
        "clip": None,
        "location": {
            "document_id": source_file,
            "page": None,
            "section": str(ref.get("source_id") or ""),
            "chunk_index": None,
        },
        "content": source_text_from_ref(ref),
        "links": {
            "source_file": source_file,
            "source_id": ref.get("source_id"),
            "source_node_id": ref.get("source_node_id"),
            "node_ids": [],
            "edge_ids": [],
        },
    }


def evidence_from_derived_row(
    row: dict[str, Any],
    *,
    node_id: str | None,
    edge_id: str | None,
    source_file: str,
    collected_at: str,
    derived_dir: Path,
) -> dict[str, Any]:
    content = row_to_text(row, source_file)
    evidence_payload = {
        "source_file": source_file,
        "derived_id": row.get("derived_id") or row.get("row_id"),
        "row_hash": short_hash(row),
    }
    evidence_id = evidence_id_from(evidence_payload)
    return {
        "evidence_id": evidence_id,
        "kind": "derived_record",
        "source": {
            "url": None,
            "path": str(derived_dir / source_file),
            "title": source_file,
        },
        "hash": "sha256:" + sha256_text(stable_json(row)),
        "collected_at": collected_at,
        "parser": {
            "status": "ok",
            "method": "deriving_derived_jsonl",
            "warnings": [],
        },
        "ocr": None,
        "clip": None,
        "location": {
            "document_id": source_file,
            "page": None,
            "section": str(row.get("derived_id") or row.get("row_id") or ""),
            "chunk_index": None,
        },
        "content": content,
        "links": {
            "document_id": source_file,
            "chunk_ids": [],
            "node_ids": [node_id] if node_id else [],
            "edge_ids": [edge_id] if edge_id else [],
        },
    }


def row_to_text(row: dict[str, Any], source_file: str) -> str:
    if source_file == "bim_objects.jsonl":
        return (
            f"BIM object {row.get('name') or row.get('source_element_id')} "
            f"category={row.get('category')} type={row.get('type')} "
            f"ai_category={row.get('ai_category')} importance={row.get('importance')}"
        )
    if source_file == "material_facts.jsonl":
        return (
            f"Material {row.get('name')} group={row.get('material_group')} "
            f"class={row.get('material_class')} category={row.get('material_category')}"
        )
    if source_file == "quantity_facts.jsonl":
        element = row.get("element") if isinstance(row.get("element"), dict) else {}
        material = row.get("material") if isinstance(row.get("material"), dict) else {}
        parameter = row.get("parameter") if isinstance(row.get("parameter"), dict) else {}
        return (
            f"Quantity {row.get('quantity_category')} element={element.get('name') or element.get('element_id')} "
            f"material={material.get('name')} parameter={parameter.get('name')} "
            f"display={parameter.get('display')} area_m2={row.get('area_m2')} volume_m3={row.get('volume_m3')}"
        )
    if source_file == "drawing_documents.jsonl":
        return (
            f"Drawing sheet {row.get('sheet_number')} {row.get('sheet_name')} "
            f"category={row.get('drawing_category')} placed_views={len(row.get('placed_view_ids') or [])}"
        )
    if source_file == "schedule_tables.jsonl":
        field_names = [str(field.get("name") or field.get("column_heading") or "") for field in row.get("fields") or []]
        return f"Schedule {row.get('name')} category={row.get('schedule_category')} fields={', '.join(field_names)}"
    if source_file == "schedule_rows.jsonl":
        return f"Schedule row {row.get('schedule_name')} row={row.get('row')} role={row.get('row_role')}: {row.get('text')}"
    if source_file == "drawing_evidence.jsonl":
        return f"Drawing evidence {row.get('evidence_type')} source={row.get('source')}: {row.get('text')}"
    if source_file == "relationships.jsonl":
        return f"Relationship {row.get('relation')} from={row.get('from')} to={row.get('to')} source={row.get('source')}"
    return stable_json(row)


def display_label(row: dict[str, Any], source_file: str, node_id: str) -> str:
    if source_file == "bim_objects.jsonl":
        return str(row.get("name") or row.get("category") or node_id)
    if source_file == "material_facts.jsonl":
        return str(row.get("name") or row.get("normalized_name") or node_id)
    if source_file == "quantity_facts.jsonl":
        element = row.get("element") if isinstance(row.get("element"), dict) else {}
        parameter = row.get("parameter") if isinstance(row.get("parameter"), dict) else {}
        material = row.get("material") if isinstance(row.get("material"), dict) else {}
        return str(parameter.get("name") or material.get("name") or element.get("name") or row.get("quantity_category") or node_id)
    if source_file == "drawing_documents.jsonl":
        return str((str(row.get("sheet_number") or "") + " " + str(row.get("sheet_name") or "")).strip() or node_id)
    if source_file == "schedule_tables.jsonl":
        return str(row.get("name") or node_id)
    if source_file == "schedule_rows.jsonl":
        return str(row.get("text") or row.get("row_id") or node_id)
    if source_file == "drawing_evidence.jsonl":
        return str(row.get("text") or row.get("evidence_type") or node_id)
    return node_id


def node_kind(source_file: str) -> str:
    return {
        "bim_objects.jsonl": "bim_object",
        "material_facts.jsonl": "material_fact",
        "quantity_facts.jsonl": "quantity_fact",
        "drawing_documents.jsonl": "drawing_document",
        "schedule_tables.jsonl": "schedule_table",
        "schedule_rows.jsonl": "schedule_row",
        "drawing_evidence.jsonl": "drawing_evidence",
    }.get(source_file, "derived_record")


def primary_node_id(row: dict[str, Any], source_file: str) -> str:
    source_node_id = row.get("source_node_id")
    if source_node_id:
        return str(source_node_id)
    if source_file == "schedule_rows.jsonl":
        return f"bimgraph:{row.get('row_id')}"
    derived_id = row.get("derived_id") or row.get("row_id")
    if derived_id:
        return f"bimgraph:{derived_id}"
    return f"bimgraph:{source_file}:{short_hash(row)}"


def safe_properties(row: dict[str, Any], source_file: str) -> dict[str, Any]:
    props = dict(row)
    props.pop("source_refs", None)
    props["bimgraph_source_file"] = source_file
    props["bimgraph_node_kind"] = node_kind(source_file)
    return props


def make_concept_node(
    node_id: str,
    label: str,
    *,
    source_file: str,
    properties: dict[str, Any],
    evidence_refs: list[str],
    confidence: float | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "label": label,
        "space": "concept",
        "node_type": "Entity",
        "properties": properties,
        "evidence_refs": evidence_refs,
        "quality": {
            "confidence": confidence if confidence is not None else float(properties.get("confidence") or 0.75),
            "parser": "deriving",
            "promotion_status": "draft",
            "source_file": source_file,
        },
    }


def make_stub_node(node_id: str, *, evidence_refs: list[str]) -> dict[str, Any]:
    tail = node_id.rsplit(":", 1)[-1]
    kind = "source_reference"
    if ":category:" in node_id:
        kind = "revit_category"
    elif ":material:" in node_id:
        kind = "revit_material"
    elif ":element:" in node_id:
        kind = "revit_element_reference"
    elif ":view:" in node_id:
        kind = "revit_view_reference"
    elif ":sheet:" in node_id:
        kind = "revit_sheet_reference"
    elif ":schedule:" in node_id:
        kind = "revit_schedule_reference"
    return make_concept_node(
        node_id,
        tail,
        source_file="relationship_endpoint",
        properties={
            "bimgraph_node_kind": kind,
            "is_stub": True,
            "source_node_id": node_id,
            "importance": "supporting",
            "reason": "Created to preserve OpenCrab edge endpoint integrity.",
        },
        evidence_refs=evidence_refs,
        confidence=0.5,
    )


def relation_to_opencrab(original_relation: str) -> str:
    # Keep the edge valid under OpenCrab's base concept->concept grammar while
    # preserving the original Revit/BIMGraph relation in properties.
    return "related_to"


def make_edge(
    from_id: str,
    to_id: str,
    *,
    original_relation: str,
    properties: dict[str, Any],
    evidence_refs: list[str],
    confidence: float,
) -> dict[str, Any]:
    payload = {
        "from_id": from_id,
        "to_id": to_id,
        "relation": relation_to_opencrab(original_relation),
        "bimgraph_relation": original_relation,
        "properties": properties,
    }
    edge_id = edge_id_from(payload)
    return {
        "id": edge_id,
        "from_id": from_id,
        "to_id": to_id,
        "from_space": "concept",
        "to_space": "concept",
        "relation": "related_to",
        "confidence": confidence,
        "evidence_refs": evidence_refs,
        "properties": {
            **properties,
            "bimgraph_relation": original_relation,
        },
    }


def add_edge(edges: dict[tuple[str, str, str, str], dict[str, Any]], edge: dict[str, Any]) -> None:
    key = (
        str(edge["from_id"]),
        str(edge["to_id"]),
        str(edge["relation"]),
        str(edge["properties"].get("bimgraph_relation") or ""),
    )
    if key not in edges:
        edges[key] = edge


class PackBuilder:
    def __init__(self, derived_dir: Path, out_dir: Path, pack_id: str, title: str):
        self.derived_dir = derived_dir
        self.out_dir = out_dir
        self.pack_id = pack_id
        self.title = title
        self.created_at = now_iso()
        self.derived_manifest = read_json(derived_dir / "manifest.json")
        self.project_profile = read_json(derived_dir / "project_profile.json")
        self.rows = {file_name: read_jsonl(derived_dir / file_name) for file_name in DERIVED_FILES}
        self.evidence_by_id: dict[str, dict[str, Any]] = {}
        self.source_evidence_id_by_key: dict[str, str] = {}
        self.node_by_id: dict[str, dict[str, Any]] = {}
        self.id_map: dict[str, str] = {}
        self.edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def add_evidence(self, evidence: dict[str, Any]) -> str:
        evidence_id = str(evidence["evidence_id"])
        if evidence_id not in self.evidence_by_id:
            self.evidence_by_id[evidence_id] = evidence
        return evidence_id

    def source_evidence_id(self, ref: dict[str, Any]) -> str:
        key = stable_json(ref)
        if key not in self.source_evidence_id_by_key:
            evidence = evidence_from_source_ref(ref, collected_at=self.created_at, derived_dir=self.derived_dir)
            self.source_evidence_id_by_key[key] = self.add_evidence(evidence)
        return self.source_evidence_id_by_key[key]

    def source_evidence_ids(self, refs: list[dict[str, Any]]) -> list[str]:
        ids = [self.source_evidence_id(ref) for ref in refs]
        return list(dict.fromkeys(ids))

    def add_node(self, node: dict[str, Any]) -> None:
        node_id = str(node["id"])
        if node_id not in self.node_by_id:
            self.node_by_id[node_id] = node
        else:
            existing_refs = list(self.node_by_id[node_id].get("evidence_refs") or [])
            for evidence_ref in node.get("evidence_refs") or []:
                if evidence_ref not in existing_refs:
                    existing_refs.append(evidence_ref)
            self.node_by_id[node_id]["evidence_refs"] = existing_refs

    def build_project_node(self) -> None:
        document = self.project_profile.get("document") if isinstance(self.project_profile, dict) else {}
        label = str(document.get("title") or self.title)
        project_node_id = f"bimgraph:project:{short_hash(document or self.derived_manifest)}"
        evidence = evidence_from_derived_row(
            self.project_profile or self.derived_manifest,
            node_id=project_node_id,
            edge_id=None,
            source_file="project_profile.json",
            collected_at=self.created_at,
            derived_dir=self.derived_dir,
        )
        evidence_id = self.add_evidence(evidence)
        self.add_node(
            make_concept_node(
                project_node_id,
                label,
                source_file="project_profile.json",
                properties={
                    "bimgraph_node_kind": "project",
                    "document": document,
                    "raw_counts": self.derived_manifest.get("raw_counts"),
                    "derived_counts": self.derived_manifest.get("derived_counts"),
                },
                evidence_refs=[evidence_id],
                confidence=0.95,
            )
        )
        self.project_node_id = project_node_id

    def build_nodes(self) -> None:
        self.build_project_node()
        for source_file in NODE_SOURCE_FILES:
            for row in self.rows[source_file]:
                node_id = primary_node_id(row, source_file)
                evidence = evidence_from_derived_row(
                    row,
                    node_id=node_id,
                    edge_id=None,
                    source_file=source_file,
                    collected_at=self.created_at,
                    derived_dir=self.derived_dir,
                )
                evidence_ids = [self.add_evidence(evidence), *self.source_evidence_ids(source_refs(row))]
                evidence_ids = list(dict.fromkeys(evidence_ids))
                node = make_concept_node(
                    node_id,
                    display_label(row, source_file, node_id),
                    source_file=source_file,
                    properties=safe_properties(row, source_file),
                    evidence_refs=evidence_ids,
                    confidence=float(row.get("confidence") or 0.75),
                )
                self.add_node(node)

                self.id_map[node_id] = node_id
                if row.get("source_node_id"):
                    self.id_map[str(row["source_node_id"])] = node_id
                for ref in source_refs(row):
                    if ref.get("source_node_id"):
                        self.id_map[str(ref["source_node_id"])] = node_id

    def ensure_node_for_endpoint(self, endpoint_id: str, evidence_refs: list[str]) -> str:
        mapped = self.id_map.get(endpoint_id)
        if mapped:
            return mapped
        if endpoint_id not in self.node_by_id:
            self.add_node(make_stub_node(endpoint_id, evidence_refs=evidence_refs))
        self.id_map[endpoint_id] = endpoint_id
        return endpoint_id

    def build_relationship_edges(self) -> None:
        for row in self.rows["relationships.jsonl"]:
            refs = source_refs(row)
            evidence = evidence_from_derived_row(
                row,
                node_id=None,
                edge_id=None,
                source_file="relationships.jsonl",
                collected_at=self.created_at,
                derived_dir=self.derived_dir,
            )
            evidence_ids = [self.add_evidence(evidence), *self.source_evidence_ids(refs)]
            evidence_ids = list(dict.fromkeys(evidence_ids))
            from_id_raw = str(row.get("from") or "")
            to_id_raw = str(row.get("to") or "")
            if not from_id_raw or not to_id_raw:
                continue
            from_id = self.ensure_node_for_endpoint(from_id_raw, evidence_ids)
            to_id = self.ensure_node_for_endpoint(to_id_raw, evidence_ids)
            edge = make_edge(
                from_id,
                to_id,
                original_relation=str(row.get("relation") or "RELATED_TO"),
                properties={
                    "source": row.get("source"),
                    "relationship_category": row.get("relationship_category"),
                    "importance": row.get("importance"),
                    "derived_id": row.get("derived_id"),
                    "raw_from": from_id_raw,
                    "raw_to": to_id_raw,
                },
                evidence_refs=evidence_ids,
                confidence=float(row.get("confidence") or 0.7),
            )
            add_edge(self.edges, edge)

    def build_derived_edges(self) -> None:
        for row in self.rows["quantity_facts.jsonl"]:
            quantity_id = primary_node_id(row, "quantity_facts.jsonl")
            refs = source_refs(row)
            evidence_ids = self.source_evidence_ids(refs)
            element = row.get("element") if isinstance(row.get("element"), dict) else {}
            material = row.get("material") if isinstance(row.get("material"), dict) else {}
            for endpoint, relation in [
                (element.get("node_id"), "QUANTITY_FOR_ELEMENT"),
                (material.get("node_id"), "QUANTITY_FOR_MATERIAL"),
            ]:
                if not endpoint:
                    continue
                target_id = self.ensure_node_for_endpoint(str(endpoint), evidence_ids)
                edge = make_edge(
                    quantity_id,
                    target_id,
                    original_relation=relation,
                    properties={
                        "quantity_category": row.get("quantity_category"),
                        "importance": row.get("importance"),
                        "derived_id": row.get("derived_id"),
                    },
                    evidence_refs=evidence_ids,
                    confidence=float(row.get("confidence") or 0.7),
                )
                add_edge(self.edges, edge)

        schedule_by_id: dict[str, str] = {}
        for schedule in self.rows["schedule_tables.jsonl"]:
            key = str(schedule.get("source_schedule_id") or schedule.get("name") or "")
            if key:
                schedule_by_id[key] = primary_node_id(schedule, "schedule_tables.jsonl")
        for row in self.rows["schedule_rows.jsonl"]:
            schedule_id = str(row.get("schedule_id") or "")
            if schedule_id not in schedule_by_id:
                continue
            row_node_id = primary_node_id(row, "schedule_rows.jsonl")
            evidence_ids = self.source_evidence_ids(source_refs(row))
            edge = make_edge(
                schedule_by_id[schedule_id],
                row_node_id,
                original_relation="SCHEDULE_HAS_ROW",
                properties={
                    "schedule_name": row.get("schedule_name"),
                    "row": row.get("row"),
                    "row_role": row.get("row_role"),
                },
                evidence_refs=evidence_ids,
                confidence=0.88,
            )
            add_edge(self.edges, edge)

        # Keep the graph connected around the project node without relying on
        # non-standard spaces. These are lightweight retrieval anchors.
        for node_id, node in list(self.node_by_id.items()):
            if node_id == self.project_node_id:
                continue
            kind = node.get("properties", {}).get("bimgraph_node_kind")
            if kind in {"bim_object", "drawing_document", "schedule_table", "material_fact"}:
                edge = make_edge(
                    self.project_node_id,
                    node_id,
                    original_relation="PROJECT_HAS_DERIVED_NODE",
                    properties={"bimgraph_node_kind": kind},
                    evidence_refs=list(node.get("evidence_refs") or []),
                    confidence=0.75,
                )
                add_edge(self.edges, edge)

    def build_edges(self) -> None:
        self.build_relationship_edges()
        self.build_derived_edges()

    def quality_report(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
        node_ids = {str(node["id"]) for node in nodes}
        evidence_ids = set(self.evidence_by_id)
        broken_edges = [
            edge["id"]
            for edge in edges
            if str(edge["from_id"]) not in node_ids or str(edge["to_id"]) not in node_ids
        ]
        nodes_missing_evidence = [node["id"] for node in nodes if not node.get("evidence_refs")]
        edges_missing_evidence = [edge["id"] for edge in edges if not edge.get("evidence_refs")]
        missing_node_evidence_refs = [
            node["id"]
            for node in nodes
            for evidence_ref in node.get("evidence_refs") or []
            if evidence_ref not in evidence_ids
        ]
        missing_edge_evidence_refs = [
            edge["id"]
            for edge in edges
            for evidence_ref in edge.get("evidence_refs") or []
            if evidence_ref not in evidence_ids
        ]
        coverage = self.rows["source_coverage.jsonl"]
        raw_total = sum((self.derived_manifest.get("raw_counts") or {}).values())
        coverage_total = len(coverage)
        classified_only = sum(1 for row in coverage if row.get("coverage_status") == "classified_only")
        capped_view_edges = (self.derived_manifest.get("raw_counts") or {}).get("graph_edges.jsonl")
        relation_counts = Counter(edge["properties"].get("bimgraph_relation") for edge in edges)
        issues: list[dict[str, Any]] = []
        if raw_total and coverage_total != raw_total:
            issues.append(
                {
                    "severity": "error",
                    "type": "source_coverage_mismatch",
                    "message": f"source_coverage rows {coverage_total} do not match raw total {raw_total}.",
                }
            )
        if broken_edges:
            issues.append({"severity": "error", "type": "broken_edges", "count": len(broken_edges)})
        if nodes_missing_evidence or missing_node_evidence_refs:
            issues.append(
                {
                    "severity": "error",
                    "type": "node_evidence_integrity",
                    "missing_nodes": len(nodes_missing_evidence),
                    "missing_refs": len(missing_node_evidence_refs),
                }
            )
        if edges_missing_evidence or missing_edge_evidence_refs:
            issues.append(
                {
                    "severity": "error",
                    "type": "edge_evidence_integrity",
                    "missing_edges": len(edges_missing_evidence),
                    "missing_refs": len(missing_edge_evidence_refs),
                }
            )
        if relation_counts.get("VIEW_REFERENCES_ELEMENT", 0) >= 50_000:
            issues.append(
                {
                    "severity": "warning",
                    "type": "capped_view_element_edges",
                    "message": "VIEW_REFERENCES_ELEMENT reached the known exporter default cap; treat this relation as partial.",
                    "count": relation_counts.get("VIEW_REFERENCES_ELEMENT", 0),
                }
            )

        status = "pass" if not any(issue["severity"] == "error" for issue in issues) else "fail"
        node_evidence_integrity = 1.0 if not nodes else 1 - (len(nodes_missing_evidence) / len(nodes))
        edge_evidence_integrity = 1.0 if not edges else 1 - (len(edges_missing_evidence) / len(edges))
        graph_reference_integrity = 1.0 if not edges else 1 - (len(broken_edges) / len(edges))
        evidence_coverage = 1.0 if not raw_total else coverage_total / raw_total
        return {
            "status": status,
            "summary": {
                "parsing_completeness": 1.0 if coverage_total == raw_total else evidence_coverage,
                "ocr_completeness": None,
                "clip_coverage": None,
                "evidence_coverage": evidence_coverage,
                "chunk_coverage": 1.0,
                "node_evidence_integrity": node_evidence_integrity,
                "edge_evidence_integrity": edge_evidence_integrity,
                "relationship_evidence_coverage": edge_evidence_integrity,
                "multihop_path_coverage": graph_reference_integrity,
                "graph_reference_integrity": graph_reference_integrity,
            },
            "checks": {
                "grammar": "pass",
                "schema": "pass",
                "evidence_refs": "pass" if not (nodes_missing_evidence or edges_missing_evidence) else "fail",
                "orphan_nodes": "warn",
                "broken_edges": "pass" if not broken_edges else "fail",
                "neo4j_import": "not_run",
            },
            "counts": {
                "raw_records": raw_total,
                "source_coverage_rows": coverage_total,
                "classified_only_source_rows": classified_only,
                "nodes": len(nodes),
                "edges": len(edges),
                "evidence": len(self.evidence_by_id),
                "missing_node_evidence_refs": len(missing_node_evidence_refs),
                "missing_edge_evidence_refs": len(missing_edge_evidence_refs),
                "nodes_missing_evidence": len(nodes_missing_evidence),
                "edges_missing_evidence": len(edges_missing_evidence),
                "broken_edges": len(broken_edges),
                "parser_failures": 0,
                "ocr_low_confidence_spans": 0,
            },
            "relation_counts": dict(relation_counts.most_common()),
            "issues": issues,
        }

    def write_neo4j_files(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        import_cypher = """// BIMGraph AI Deriving OpenCrab Pack v1 import sketch.
// Load graph/nodes.jsonl and graph/edges.jsonl with your preferred Neo4j JSONL loader.
// The canonical pack graph remains graph/nodes.jsonl and graph/edges.jsonl.

// Example shape:
// UNWIND $nodes AS row
// MERGE (n:OpenCrabNode {id: row.id})
// SET n += row.properties,
//     n.label = row.label,
//     n.space = row.space,
//     n.node_type = row.node_type,
//     n.evidence_refs = row.evidence_refs;
//
// UNWIND $edges AS row
// MATCH (a:OpenCrabNode {id: row.from_id})
// MATCH (b:OpenCrabNode {id: row.to_id})
// MERGE (a)-[r:RELATED_TO {id: row.id}]->(b)
// SET r += row.properties,
//     r.relation = row.relation,
//     r.confidence = row.confidence,
//     r.evidence_refs = row.evidence_refs;
"""
        write_text(self.out_dir / "neo4j" / "import.cypher", import_cypher)
        ingest_rows = [{"kind": "node", "payload": node} for node in nodes]
        ingest_rows.extend({"kind": "edge", "payload": edge} for edge in edges)
        ingest_rows.extend({"kind": "evidence", "payload": evidence} for evidence in self.evidence_by_id.values())
        write_jsonl(self.out_dir / "neo4j" / "opencrab_ingest.jsonl", ingest_rows)
        write_json(
            self.out_dir / "neo4j" / "export_status.json",
            {
                "status": "ok",
                "pack_id": self.pack_id,
                "nodes": len(nodes),
                "edges": len(edges),
                "evidence": len(self.evidence_by_id),
                "exported_at": self.created_at,
                "method": "deriving_direct_jsonl_export",
                "neo4j_import": "not_run",
            },
        )

    def write_docs(self, quality: dict[str, Any]) -> None:
        raw_counts = self.derived_manifest.get("raw_counts") or {}
        derived_counts = self.derived_manifest.get("derived_counts") or {}
        readme = f"""# {self.title}

BIMGraph AI Deriving pack generated from Revit raw JSONL exports.

## Source

- Raw export folder: `{self.derived_manifest.get('raw_dir')}`
- Derived folder: `{self.derived_dir}`
- Generator: `tools/deriving/build_opencrab_pack.py`
- OpenCrab source reference: https://github.com/AlexAI-MCP/OpenCrab

## Counts

- Nodes: {quality['counts']['nodes']}
- Edges: {quality['counts']['edges']}
- Evidence rows: {quality['counts']['evidence']}
- Raw records covered: {quality['counts']['source_coverage_rows']} / {quality['counts']['raw_records']}

## Raw Counts

```json
{json.dumps(raw_counts, ensure_ascii=False, indent=2)}
```

## Derived Counts

```json
{json.dumps(derived_counts, ensure_ascii=False, indent=2)}
```

## Quality

Status: `{quality['status']}`

The first adapter version maps BIMGraph domain nodes to OpenCrab's base
`concept/Entity` grammar and preserves the original BIM/Revit relation in
`edge.properties.bimgraph_relation`.
"""
        write_text(self.out_dir / "README.md", readme)
        write_json(
            self.out_dir / "sample_queries.json",
            [
                {
                    "mode": "inventory_query",
                    "question": "이 프로젝트의 핵심 BIM 객체를 카테고리별로 요약해줘.",
                },
                {
                    "mode": "quantity_query",
                    "question": "바닥 유형별 체적과 관련 재료를 정리해줘.",
                },
                {
                    "mode": "drawing_evidence_query",
                    "question": "A-102 시트와 연결된 뷰, 객체, 주석 근거를 보여줘.",
                },
                {
                    "mode": "schedule_query",
                    "question": "실내재료마감표의 헤더와 데이터 행을 설명해줘.",
                },
                {
                    "mode": "quality_check_query",
                    "question": "품질 리포트에서 검토가 필요한 관계나 누락 가능성을 찾아줘.",
                },
            ],
        )
        write_json(
            self.out_dir / "community_reports.json",
            [
                {
                    "id": "community:bimgraph-deriving-overview",
                    "title": "BIMGraph Deriving Overview",
                    "summary": "Project-specific Revit objects, materials, quantities, drawings, schedules, and evidence are normalized for natural-language BIM exploration.",
                    "generated_at": self.created_at,
                    "status": "draft",
                }
            ],
        )

    def copy_parsed_artifacts(self) -> None:
        parsed_dir = self.out_dir / "parsed" / "derived"
        parsed_dir.mkdir(parents=True, exist_ok=True)
        for file_name in ["manifest.json", "project_profile.json", *DERIVED_FILES]:
            source = self.derived_dir / file_name
            if source.exists():
                shutil.copy2(source, parsed_dir / file_name)

    def write_manifest(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], quality: dict[str, Any]) -> None:
        nodes_path = self.out_dir / "graph" / "nodes.jsonl"
        edges_path = self.out_dir / "graph" / "edges.jsonl"
        evidence_path = self.out_dir / "evidence" / "index.jsonl"
        files = [path for path in self.out_dir.rglob("*") if path.is_file() and path.name != "manifest.json"]
        total_bytes = sum(path.stat().st_size for path in files)
        counts = {
            "documents": len(self.rows["drawing_documents.jsonl"]) + len(self.rows["schedule_tables.jsonl"]),
            "chunks": len(self.evidence_by_id),
            "images": 0,
            "evidence": len(self.evidence_by_id),
            "nodes": len(nodes),
            "edges": len(edges),
            "files": len(files) + 1,
            "bytes": total_bytes,
        }
        split_recommended = counts["nodes"] > 100_000 or counts["edges"] > 300_000 or counts["evidence"] > 500_000
        manifest = {
            "format_version": PACK_FORMAT_VERSION,
            "pack_id": self.pack_id,
            "title": self.title,
            "version": "0.1.0",
            "grammar_version": GRAMMAR_VERSION,
            "created_at": self.created_at,
            "created_by": "BIMGraph AI Deriving",
            "license": {
                "scope": "project",
                "name": "MIT-compatible upstream reference; source Revit data retains project ownership",
            },
            "source": {
                "mode": "revit_jsonl_export",
                "label": self.title,
                "url": None,
                "description": "Revit raw JSONL transformed by BIMGraph AI Deriving into OpenCrab Pack v1 artifacts.",
                "raw_dir": self.derived_manifest.get("raw_dir"),
                "derived_dir": str(self.derived_dir),
            },
            "counts": counts,
            "limits": {
                "split_recommended": split_recommended,
                "staged_ingest_recommended": split_recommended,
                "reason": "Pack exceeds recommended SaaS ingest threshold." if split_recommended else None,
            },
            "quality": {
                **quality["summary"],
                "promotion_status": "draft" if quality["status"] == "pass" else "blocked",
            },
            "retrieval_hints": {
                "relation_cues": [
                    "bimgraph_relation",
                    "importance",
                    "source",
                    "quantity_category",
                    "drawing_category",
                    "schedule_category",
                ],
                "benchmark_focus": [
                    "inventory_query",
                    "spec_query",
                    "quantity_query",
                    "drawing_evidence_query",
                    "schedule_query",
                    "quality_check_query",
                ],
            },
            "hashes": {
                "nodes_sha256": sha256_file(nodes_path),
                "edges_sha256": sha256_file(edges_path),
                "evidence_sha256": sha256_file(evidence_path),
                "pack_sha256": None,
            },
            "artifacts": {
                "nodes": "graph/nodes.jsonl",
                "edges": "graph/edges.jsonl",
                "evidence_index": "evidence/index.jsonl",
                "quality_report": "quality/report.json",
                "neo4j_cypher": "neo4j/import.cypher",
                "opencrab_ingest": "neo4j/opencrab_ingest.jsonl",
                "neo4j_export_status": "neo4j/export_status.json",
            },
            "bimgraph": {
                "adapter": "deriving_opencrab_pack_v0",
                "derived_manifest": self.derived_manifest,
            },
        }
        write_json(self.out_dir / "manifest.json", manifest)

    def build(self, *, include_parsed: bool) -> dict[str, Any]:
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.build_nodes()
        self.build_edges()
        nodes = sorted(self.node_by_id.values(), key=lambda item: item["id"])
        edges = sorted(self.edges.values(), key=lambda item: item["id"])
        evidence = sorted(self.evidence_by_id.values(), key=lambda item: item["evidence_id"])

        write_jsonl(self.out_dir / "graph" / "nodes.jsonl", nodes)
        write_jsonl(self.out_dir / "graph" / "edges.jsonl", edges)
        write_jsonl(self.out_dir / "evidence" / "index.jsonl", evidence)

        quality = self.quality_report(nodes, edges)
        write_json(self.out_dir / "quality" / "report.json", quality)
        self.write_neo4j_files(nodes, edges)
        self.write_docs(quality)
        if include_parsed:
            self.copy_parsed_artifacts()
        self.write_manifest(nodes, edges, quality)

        return {
            "status": "ok" if quality["status"] == "pass" else "quality_failed",
            "pack_id": self.pack_id,
            "pack_dir": str(self.out_dir),
            "nodes": len(nodes),
            "edges": len(edges),
            "evidence": len(evidence),
            "quality": quality["status"],
            "issues": quality["issues"],
        }


def zip_pack(pack_dir: Path, zip_path: Path) -> Path:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(pack_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(pack_dir).as_posix())
    pack_hash = sha256_file(zip_path)
    zip_path.with_suffix(zip_path.suffix + ".sha256").write_text(
        f"{pack_hash}  {zip_path.name}\n",
        encoding="utf-8",
    )
    return zip_path


def default_title(derived_dir: Path) -> str:
    profile = read_json(derived_dir / "project_profile.json")
    document = profile.get("document") if isinstance(profile, dict) else {}
    return str(document.get("title") or "BIMGraph AI Revit Deriving Pack")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an OpenCrab Pack v1 folder/ZIP from Deriving derived JSONL.")
    parser.add_argument("--derived-dir", required=True, type=Path, help="Deriving derived directory.")
    parser.add_argument("--out-dir", type=Path, help="Output pack directory. Defaults to DERIVED_DIR/opencrab_pack.")
    parser.add_argument("--pack-id", help="Stable pack id. Defaults to title slug plus hash.")
    parser.add_argument("--title", help="Pack title. Defaults to Revit document title from project_profile.json.")
    parser.add_argument("--zip", action="store_true", help="Also write a ZIP next to the pack directory.")
    parser.add_argument("--no-parsed-copy", action="store_true", help="Do not copy derived JSONL into parsed/derived.")
    parser.add_argument(
        "--legacy-single-pack",
        action="store_true",
        help="Allow the deprecated all-in-one pack build. Product flow should use build_opencrab_pack_suite.py.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.legacy_single_pack:
        raise SystemExit(
            "The all-in-one OpenCrab pack is deprecated for BIMGraph AI. "
            "Use tools/deriving/build_opencrab_pack_suite.py to build one pack per derived JSONL domain, "
            "or pass --legacy-single-pack for debugging only."
        )
    derived_dir = args.derived_dir
    title = args.title or default_title(derived_dir)
    pack_id = args.pack_id or f"{safe_slug(title).lower()}-{short_hash(str(derived_dir))}"
    out_dir = args.out_dir or derived_dir / "opencrab_pack"
    builder = PackBuilder(derived_dir=derived_dir, out_dir=out_dir, pack_id=pack_id, title=title)
    result = builder.build(include_parsed=not args.no_parsed_copy)
    if args.zip:
        zip_path = out_dir.with_suffix(".zip")
        zip_pack(out_dir, zip_path)
        result["zip"] = str(zip_path)
        result["zip_sha256"] = sha256_file(zip_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
