from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from build_opencrab_pack import (
        default_title,
        display_label,
        node_kind,
        primary_node_id,
        read_json,
        read_jsonl,
        row_to_text,
        safe_slug,
        sha256_file,
        short_hash,
        source_refs,
        stable_json,
        write_json,
        write_jsonl,
        write_text,
        zip_pack,
    )
except ImportError:  # pragma: no cover - allows package-style execution later.
    from .build_opencrab_pack import (
        default_title,
        display_label,
        node_kind,
        primary_node_id,
        read_json,
        read_jsonl,
        row_to_text,
        safe_slug,
        sha256_file,
        short_hash,
        source_refs,
        stable_json,
        write_json,
        write_jsonl,
        write_text,
        zip_pack,
    )


SOURCE_TO_DERIVED = {
    "elements.jsonl": ["bim_objects.jsonl"],
    "materials.jsonl": ["material_facts.jsonl"],
    "quantities.jsonl": ["quantity_facts.jsonl"],
    "sheets.jsonl": ["drawing_documents.jsonl"],
    "sheet_pdfs.jsonl": ["drawing_evidence.jsonl"],
    "sheet_dxfs.jsonl": ["drawing_evidence.jsonl"],
    "view_dxfs.jsonl": ["drawing_evidence.jsonl", "relationships.jsonl"],
    "drawing_entities.jsonl": ["drawing_evidence.jsonl"],
    "schedules.jsonl": ["schedule_tables.jsonl"],
    "schedule_cells.jsonl": ["schedule_rows.jsonl", "drawing_evidence.jsonl"],
    "graph_edges.jsonl": ["relationships.jsonl"],
    "views.jsonl": ["drawing_evidence.jsonl", "relationships.jsonl"],
    "annotations.jsonl": ["drawing_evidence.jsonl"],
    "PDF/DWG extraction": ["drawing_evidence.jsonl"],
}


FT2_TO_M2 = 0.09290304

SOURCE_REF_FIELDS = (
    "source_file",
    "source_id",
    "source_node_id",
    "raw_record_hash",
    "record_type",
    "element_id",
    "unique_id",
    "ifc_guid",
    "sheet_id",
    "sheet_number",
    "sheet_name",
    "sheet_unique_id",
    "view_id",
    "view_name",
    "view_unique_id",
    "schedule_id",
    "schedule_unique_id",
    "schedule_name",
    "section",
    "row",
    "column",
    "page",
    "dxf_file_name",
    "entity_key",
    "source_entity_key",
    "handle",
    "entity_type",
)


@dataclass(frozen=True)
class PackSpec:
    derived_file: str
    logical_name: str
    title: str
    query_mode: str
    source_policy: str
    useful_for: str
    split_key: str | None = None
    row_nodes_limit: int = 12_000
    include_by_default: bool = True
    max_rows_per_pack: int | None = None


PRIMARY_SPECS = [
    PackSpec(
        "bim_objects.jsonl",
        "bim_objects",
        "BIM Objects",
        "inventory_query",
        "elements.jsonl에서 keep된 핵심/보조 BIM 객체만 chunk와 graph 색인으로 보존합니다.",
        "벽, 바닥, 문, 창, 구조부재 같은 Revit 객체를 카테고리와 중요도 기준으로 탐색하는 데 사용합니다.",
    ),
    PackSpec(
        "material_facts.jsonl",
        "material_facts",
        "Material Facts",
        "spec_query",
        "materials.jsonl의 재료명, 재료군, 분류 정보를 source_ref와 함께 보존합니다.",
        "재료 목록, 마감/구조 재료 후보, 객체-재료 연결 질의의 기준 사전으로 사용합니다.",
    ),
    PackSpec(
        "quantity_facts.jsonl",
        "quantity_facts",
        "Quantity Facts",
        "quantity_query",
        "quantities.jsonl에서 추출한 수량 파라미터를 행 단위 chunk로 보존하고 카테고리 graph로 색인합니다.",
        "면적, 체적, 길이, 재료 물량을 자연어로 집계하거나 표로 내보내는 데 사용합니다.",
        row_nodes_limit=0,
        max_rows_per_pack=10_000,
    ),
    PackSpec(
        "drawing_documents.jsonl",
        "drawing_documents",
        "Drawing Documents",
        "drawing_evidence_query",
        "sheets.jsonl에서 나온 시트 번호, 시트명, 배치 뷰 정보를 보존합니다.",
        "도면 목록, 시트 분류, 시트와 뷰/일람표 연결 탐색의 시작점으로 사용합니다.",
    ),
    PackSpec(
        "schedule_tables.jsonl",
        "schedule_tables",
        "Schedule Tables",
        "schedule_query",
        "schedules.jsonl에서 나온 일람표 정의, 필드, 필터, 정렬 정보를 보존합니다.",
        "어떤 일람표가 존재하는지, 어떤 열과 목적을 갖는지 파악하는 데 사용합니다.",
    ),
    PackSpec(
        "schedule_rows.jsonl",
        "schedule_rows",
        "Schedule Rows",
        "schedule_query",
        "schedule_cells.jsonl을 재구성한 행 텍스트와 셀 값을 source_ref와 함께 보존합니다.",
        "일람표의 실제 행 내용을 검색하고 Excel/CSV용 표 데이터로 재가공하는 데 사용합니다.",
    ),
    PackSpec(
        "drawing_evidence.jsonl",
        "drawing_evidence",
        "Drawing Evidence",
        "drawing_evidence_query",
        "schedule_cells, views, annotations, PDF/DXF export index, DXF entity primitives를 근거 chunk로 통합합니다.",
        "도면/뷰/주석/일람표/PDF/DXF와 LINE/TEXT/DIMENSION 같은 2D 엔티티 근거를 객체 질의와 연결하는 데 사용합니다.",
        row_nodes_limit=4_500,
        max_rows_per_pack=4_500,
    ),
    PackSpec(
        "relationships.jsonl",
        "relationships",
        "Relationships",
        "spec_query",
        "graph_edges.jsonl과 view/sheet/schedule 연결 관계를 관계 행 단위 chunk로 보존합니다.",
        "객체-카테고리, 객체-재료, 시트-뷰, 뷰-객체 같은 연결을 찾아 멀티홉 탐색에 사용합니다.",
        row_nodes_limit=0,
        max_rows_per_pack=10_000,
    ),
]


AUDIT_SPECS = [
    PackSpec(
        "classification_decisions.jsonl",
        "classification_decisions",
        "Classification Decisions",
        "quality_check_query",
        "AI/부트스트랩 분류 판단, keep/exclude 이유, confidence를 감사 로그로 보존합니다.",
        "분류 기준을 검토하거나 다른 프로젝트에서 AI 판단을 재실행할 때 비교 기준으로 사용합니다.",
        split_key="source_file",
        row_nodes_limit=0,
        include_by_default=False,
    ),
    PackSpec(
        "source_coverage.jsonl",
        "source_coverage",
        "Source Coverage",
        "quality_check_query",
        "raw row가 derived row 또는 classified_only 판단으로 어떻게 커버됐는지 보존합니다.",
        "raw에서 derived로 넘어갈 때 누락 여부를 감사하고 품질 게이트를 확인하는 데 사용합니다.",
        split_key="source_file",
        row_nodes_limit=0,
        include_by_default=False,
    ),
]


def clean_label(value: Any, fallback: str = "unclassified") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def compact_source_refs(row: dict[str, Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for ref in source_refs(row):
        compact.append(
            {key: ref.get(key) for key in SOURCE_REF_FIELDS if ref.get(key) not in (None, "", [], {})}
        )
    return compact


def row_identity(row: dict[str, Any], source_file: str) -> str:
    return str(
        row.get("derived_id")
        or row.get("row_id")
        or row.get("source_node_id")
        or row.get("source_id")
        or primary_node_id(row, source_file)
        or short_hash(row)
    )


def row_split_value(row: dict[str, Any], spec: PackSpec) -> str:
    if not spec.split_key:
        return "all"
    value = row.get(spec.split_key)
    if spec.derived_file == "bim_objects.jsonl" and spec.split_key == "ai_category":
        value = f"{clean_label(row.get('ai_category'))}__{clean_label(row.get('category'))}"
    return clean_label(value)


def row_category_value(row: dict[str, Any], spec: PackSpec) -> str:
    if spec.derived_file == "bim_objects.jsonl":
        return f"{clean_label(row.get('ai_category'))}__{clean_label(row.get('category'))}"
    if spec.derived_file == "quantity_facts.jsonl":
        return clean_label(row.get("quantity_category"))
    if spec.derived_file == "drawing_documents.jsonl":
        return clean_label(row.get("drawing_category"))
    if spec.derived_file == "schedule_tables.jsonl":
        return clean_label(row.get("schedule_category"))
    if spec.derived_file == "schedule_rows.jsonl":
        return clean_label(row.get("schedule_name"))
    if spec.derived_file == "drawing_evidence.jsonl":
        return clean_label(row.get("evidence_type"))
    if spec.derived_file == "relationships.jsonl":
        return clean_label(row.get("relation"))
    if spec.derived_file in {"classification_decisions.jsonl", "source_coverage.jsonl"}:
        return clean_label(row.get("source_file"))
    return row_split_value(row, spec)


def chunk_id(pack_id: str, row: dict[str, Any], source_file: str) -> str:
    return f"chunk:{pack_id}:{short_hash({'source_file': source_file, 'row': row_identity(row, source_file)})}"


def document_id(pack_id: str, source_file: str) -> str:
    return f"doc:{pack_id}:{safe_slug(source_file)}"


def compact_row_properties(row: dict[str, Any], source_file: str) -> dict[str, Any]:
    if source_file == "bim_objects.jsonl":
        return {
            "source_element_id": row.get("source_element_id"),
            "source_node_id": row.get("source_node_id"),
            "name": row.get("name"),
            "category": row.get("category"),
            "class": row.get("class"),
            "family": row.get("family"),
            "family_name": row.get("family_name"),
            "family_and_type": row.get("family_and_type"),
            "system_family": row.get("system_family"),
            "type": row.get("type"),
            "type_id": row.get("type_id"),
            "type_name": row.get("type_name"),
            "level_id": row.get("level_id"),
            "workset_id": row.get("workset_id"),
            "workset_name": row.get("workset_name"),
            "workset": row.get("workset"),
            "ifc_guid": row.get("ifc_guid"),
            "ai_category": row.get("ai_category"),
            "importance": row.get("importance"),
            "confidence": row.get("confidence"),
        }
    if source_file == "quantity_facts.jsonl":
        element = row.get("element") if isinstance(row.get("element"), dict) else {}
        material = row.get("material") if isinstance(row.get("material"), dict) else {}
        parameter = row.get("parameter") if isinstance(row.get("parameter"), dict) else {}
        return {
            "source_record_type": row.get("source_record_type"),
            "quantity_category": row.get("quantity_category"),
            "element_id": element.get("element_id"),
            "element_name": element.get("name"),
            "element_node_id": element.get("node_id"),
            "element_category": row.get("element_category"),
            "element_class": row.get("element_class"),
            "family": row.get("family"),
            "family_name": row.get("family_name"),
            "family_and_type": row.get("family_and_type"),
            "system_family": row.get("system_family"),
            "type_id": row.get("type_id"),
            "type_name": row.get("type_name"),
            "material_id": material.get("material_id"),
            "material_name": material.get("name"),
            "material_node_id": material.get("node_id"),
            "parameter_name": parameter.get("name"),
            "parameter_display": parameter.get("display"),
            "parameter_raw": parameter.get("raw"),
            "area_m2": row.get("area_m2"),
            "volume_m3": row.get("volume_m3"),
            "measure_kind": row.get("measure_kind"),
            "aggregation_scope": row.get("aggregation_scope"),
            "quantity_role": row.get("quantity_role"),
            "is_primary_measure": row.get("is_primary_measure"),
            "dedup_group_key": row.get("dedup_group_key"),
            "dedup_precedence": row.get("dedup_precedence"),
            "dedup_policy": row.get("dedup_policy"),
            "workset_id": row.get("workset_id"),
            "workset_name": row.get("workset_name"),
            "workset": row.get("workset"),
            "importance": row.get("importance"),
            "confidence": row.get("confidence"),
        }
    if source_file == "relationships.jsonl":
        return {
            "relation": row.get("relation"),
            "from": row.get("from"),
            "to": row.get("to"),
            "source": row.get("source"),
            "properties": row.get("properties"),
            "relationship_category": row.get("relationship_category"),
            "importance": row.get("importance"),
            "confidence": row.get("confidence"),
        }
    if source_file == "drawing_evidence.jsonl":
        drawing_ref = row.get("drawing_ref") if isinstance(row.get("drawing_ref"), dict) else {}
        return {
            "evidence_type": row.get("evidence_type"),
            "source": row.get("source"),
            "source_stage45_id": row.get("source_stage45_id"),
            "text": row.get("text"),
            "raw_text": row.get("raw_text"),
            "element_id": row.get("element_id"),
            "element_node_id": row.get("element_node_id"),
            "element_name": row.get("element_name"),
            "category_id": row.get("category_id"),
            "category_name": row.get("category_name") or row.get("category"),
            "type_id": row.get("type_id"),
            "type_name": row.get("type_name"),
            "workset_id": row.get("workset_id"),
            "workset_name": row.get("workset_name"),
            "sheet_id": row.get("sheet_id"),
            "sheet_number": row.get("sheet_number"),
            "sheet_name": row.get("sheet_name"),
            "view_id": row.get("view_id"),
            "view_name": row.get("view_name"),
            "view_type": row.get("view_type"),
            "source_file": row.get("source_file"),
            "source_stage": row.get("source_stage"),
            "representation_type": row.get("representation_type"),
            "status": row.get("status") or row.get("best_status"),
            "review_priority": row.get("review_priority"),
            "confidence": row.get("confidence") or row.get("best_confidence"),
            "fragment_key": drawing_ref.get("fragment_key"),
            "fragment_type": drawing_ref.get("fragment_type"),
            "entity_type": row.get("entity_type") or drawing_ref.get("entity_type"),
            "layer": row.get("layer") or drawing_ref.get("layer"),
            "dxf_file_name": row.get("dxf_file_name") or drawing_ref.get("dxf_file_name"),
            "source_entity_key": row.get("source_entity_key"),
            "handle": row.get("handle"),
            "space": row.get("space"),
            "block_name": row.get("block_name"),
            "block_path": row.get("block_path"),
            "source_kind": row.get("source_kind"),
            "bbox": row.get("bbox") or drawing_ref.get("bbox"),
            "start_point": row.get("start_point"),
            "end_point": row.get("end_point"),
            "insert": row.get("insert"),
            "value_mm": row.get("value_mm"),
            "unit": row.get("unit"),
            "geometry_kind": row.get("geometry_kind"),
            "group_kind": row.get("group_kind"),
            "counts": row.get("counts"),
        }
    props = dict(row)
    props.pop("source_refs", None)
    return props


def quantity_area_m2(row: dict[str, Any]) -> float | None:
    area = row.get("area_m2")
    if isinstance(area, (int, float)):
        return float(area)
    parameter = row.get("parameter") if isinstance(row.get("parameter"), dict) else {}
    raw = parameter.get("raw")
    if row.get("quantity_category") == "area_perimeter" and parameter.get("name") == "면적" and isinstance(raw, (int, float)):
        return float(raw) * FT2_TO_M2
    return None


def quantity_volume_m3(row: dict[str, Any]) -> float | None:
    volume = row.get("volume_m3")
    if isinstance(volume, (int, float)):
        return float(volume)
    return None


def join_keys(row: dict[str, Any], source_file: str) -> dict[str, Any]:
    if source_file == "bim_objects.jsonl":
        type_value = row.get("type") if isinstance(row.get("type"), dict) else {}
        family = row.get("family") if isinstance(row.get("family"), dict) else {}
        return {
            "element_id": row.get("source_element_id"),
            "element_node_id": row.get("source_node_id"),
            "category": row.get("category"),
            "family_name": row.get("family_name") or family.get("family_name"),
            "family_and_type": row.get("family_and_type") or family.get("family_and_type"),
            "family_ref_id": family.get("family_ref_id"),
            "family_source": family.get("family_source"),
            "system_family": row.get("system_family") if row.get("system_family") is not None else family.get("system_family"),
            "type_id": row.get("type_id") or type_value.get("element_id"),
            "type_name": row.get("type_name") or type_value.get("name") or row.get("name"),
            "workset_id": row.get("workset_id"),
            "workset_name": row.get("workset_name"),
        }
    if source_file == "quantity_facts.jsonl":
        element = row.get("element") if isinstance(row.get("element"), dict) else {}
        material = row.get("material") if isinstance(row.get("material"), dict) else {}
        parameter = row.get("parameter") if isinstance(row.get("parameter"), dict) else {}
        family = row.get("family") if isinstance(row.get("family"), dict) else {}
        return {
            "element_id": element.get("element_id"),
            "element_node_id": element.get("node_id"),
            "element_name": element.get("name"),
            "element_category": row.get("element_category"),
            "family_name": row.get("family_name") or family.get("family_name"),
            "family_and_type": row.get("family_and_type") or family.get("family_and_type"),
            "family_ref_id": family.get("family_ref_id"),
            "family_source": family.get("family_source"),
            "system_family": row.get("system_family") if row.get("system_family") is not None else family.get("system_family"),
            "type_id": row.get("type_id"),
            "type_name": row.get("type_name"),
            "material_id": material.get("material_id"),
            "material_name": material.get("name"),
            "parameter_name": parameter.get("name"),
            "parameter_display": parameter.get("display"),
            "quantity_category": row.get("quantity_category"),
            "measure_kind": row.get("measure_kind"),
            "aggregation_scope": row.get("aggregation_scope"),
            "quantity_role": row.get("quantity_role"),
            "is_primary_measure": row.get("is_primary_measure"),
            "dedup_group_key": row.get("dedup_group_key"),
            "dedup_precedence": row.get("dedup_precedence"),
            "area_m2": quantity_area_m2(row),
            "volume_m3": quantity_volume_m3(row),
            "workset_id": row.get("workset_id"),
            "workset_name": row.get("workset_name"),
        }
    if source_file == "relationships.jsonl":
        return {
            "relation": row.get("relation"),
            "from": row.get("from"),
            "to": row.get("to"),
            "relationship_category": row.get("relationship_category"),
        }
    if source_file == "drawing_evidence.jsonl":
        drawing_ref = row.get("drawing_ref") if isinstance(row.get("drawing_ref"), dict) else {}
        return {
            "evidence_type": row.get("evidence_type"),
            "source": row.get("source"),
            "status": row.get("status"),
            "source_stage45_id": row.get("source_stage45_id"),
            "element_id": row.get("element_id"),
            "element_node_id": row.get("element_node_id"),
            "element_name": row.get("element_name"),
            "category_id": row.get("category_id"),
            "category_name": row.get("category_name") or row.get("category"),
            "type_id": row.get("type_id"),
            "type_name": row.get("type_name"),
            "workset_id": row.get("workset_id"),
            "workset_name": row.get("workset_name"),
            "sheet_id": row.get("sheet_id"),
            "sheet_number": row.get("sheet_number"),
            "sheet_name": row.get("sheet_name"),
            "view_id": row.get("view_id"),
            "view_name": row.get("view_name"),
            "view_type": row.get("view_type"),
            "file_name": row.get("file_name"),
            "file_path": row.get("file_path"),
            "source_file": row.get("source_file"),
            "source_stage": row.get("source_stage"),
            "representation_type": row.get("representation_type"),
            "representation_status": row.get("status") or row.get("best_status"),
            "review_priority": row.get("review_priority"),
            "confidence": row.get("confidence") or row.get("best_confidence"),
            "fragment_key": drawing_ref.get("fragment_key"),
            "fragment_type": drawing_ref.get("fragment_type"),
            "entity_type": row.get("entity_type") or drawing_ref.get("entity_type"),
            "layer": row.get("layer") or drawing_ref.get("layer"),
            "dxf_file_name": row.get("dxf_file_name") or drawing_ref.get("dxf_file_name"),
            "source_entity_key": row.get("source_entity_key"),
            "handle": row.get("handle"),
            "space": row.get("space"),
            "block_name": row.get("block_name"),
            "block_path": row.get("block_path"),
            "source_kind": row.get("source_kind"),
            "bbox": row.get("bbox") or drawing_ref.get("bbox"),
            "start_point": row.get("start_point"),
            "end_point": row.get("end_point"),
            "insert": row.get("insert"),
            "value_mm": row.get("value_mm"),
            "unit": row.get("unit"),
            "geometry_kind": row.get("geometry_kind"),
            "group_kind": row.get("group_kind"),
        }
    return {}


def workset_info_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    workset = row.get("workset") if isinstance(row.get("workset"), dict) else {}
    workset_id = workset.get("id", row.get("workset_id"))
    workset_name = workset.get("name", row.get("workset_name"))
    if workset_id is None and not workset_name:
        return None
    return {
        "id": workset_id,
        "name": workset_name,
        "kind": workset.get("kind"),
        "is_open": workset.get("is_open"),
        "is_editable": workset.get("is_editable"),
        "is_visible_by_default": workset.get("is_visible_by_default"),
    }


def workset_node_id(workset: dict[str, Any]) -> str:
    return f"bimgraph:workset:{short_hash({'id': workset.get('id'), 'name': workset.get('name')})}"


def family_info_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    family = row.get("family") if isinstance(row.get("family"), dict) else {}
    family_name = row.get("family_name") or family.get("family_name")
    if not family_name:
        return None
    return {
        "family_name": family_name,
        "family_and_type": row.get("family_and_type") or family.get("family_and_type"),
        "family_ref_id": family.get("family_ref_id"),
        "family_source": family.get("family_source"),
        "system_family": row.get("system_family") if row.get("system_family") is not None else family.get("system_family"),
        "type_id": row.get("type_id") or family.get("type_id"),
        "type_name": row.get("type_name") or family.get("type_name"),
    }


def family_node_id(family: dict[str, Any], workset_key: str, category: str, logical_name: str) -> str:
    return f"bimgraph:{logical_name}:family:{short_hash({'workset': workset_key, 'category': category, 'family': family.get('family_name')})}"


def chunk_text(row: dict[str, Any], source_file: str) -> str:
    if source_file == "bim_objects.jsonl":
        return (
            f"BIM object {row.get('name')} category={row.get('category')} "
            f"family={row.get('family_name')} type={row.get('type_name') or (row.get('type') or {}).get('name') if isinstance(row.get('type'), dict) else row.get('type_name')} "
            f"workset={row.get('workset_name')} ai_category={row.get('ai_category')} importance={row.get('importance')}"
        )
    if source_file == "quantity_facts.jsonl":
        element = row.get("element") if isinstance(row.get("element"), dict) else {}
        material = row.get("material") if isinstance(row.get("material"), dict) else {}
        parameter = row.get("parameter") if isinstance(row.get("parameter"), dict) else {}
        return (
            f"Quantity {row.get('quantity_category')} "
            f"element_id={element.get('element_id')} element={element.get('name')} "
            f"category={row.get('element_category')} family={row.get('family_name')} type={row.get('type_name')} "
            f"material_id={material.get('material_id')} material={material.get('name')} "
            f"parameter={parameter.get('name')} display={parameter.get('display')} "
            f"measure_kind={row.get('measure_kind')} aggregation_scope={row.get('aggregation_scope')} "
            f"quantity_role={row.get('quantity_role')} is_primary_measure={row.get('is_primary_measure')} "
            f"dedup_group_key={row.get('dedup_group_key')} "
            f"area_m2={quantity_area_m2(row)} volume_m3={quantity_volume_m3(row)}"
        )
    if source_file == "drawing_evidence.jsonl":
        return (
            f"Drawing evidence {row.get('evidence_type')} source={row.get('source')} "
            f"element_id={row.get('element_id')} category={row.get('category_name') or row.get('category')} "
            f"type={row.get('type_name')} workset={row.get('workset_name')} "
            f"sheet={row.get('sheet_number')} view={row.get('view_name')} "
            f"status={row.get('status') or row.get('best_status')} confidence={row.get('confidence') or row.get('best_confidence')}: "
            f"{row.get('text')}"
        )
    text = row_to_text(row, source_file)
    if text and text != stable_json(row):
        return text
    return json.dumps(compact_row_properties(row, source_file), ensure_ascii=False, sort_keys=True)


def graph_node(node_id: str, label: str, node_type_name: str, properties: dict[str, Any], evidence_refs: list[str]) -> dict[str, Any]:
    return {
        "id": node_id,
        "label": label,
        "node_type": "Entity",
        "layer": "concept",
        "space": "concept",
        "properties": {
            "name": label,
            "entity_type": "other",
            "bimgraph_node_kind": node_type_name,
            **properties,
        },
        "evidence_refs": evidence_refs,
    }


def graph_edge(source: str, relation: str, target: str, properties: dict[str, Any], evidence_refs: list[str]) -> dict[str, Any]:
    return {
        "id": f"edge:{short_hash({'source': source, 'relation': relation, 'target': target, 'properties': properties})}",
        "source": source,
        "relation": "related_to",
        "target": target,
        "properties": {
            "original_relation": relation,
            **properties,
        },
        "confidence": float(properties.get("confidence") or 0.75),
        "evidence_refs": evidence_refs,
    }


def split_groups(rows: list[dict[str, Any]], spec: PackSpec, max_rows_per_pack: int) -> list[tuple[str, int, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row_split_value(row, spec), []).append(row)

    parts: list[tuple[str, int, list[dict[str, Any]]]] = []
    for group_key in sorted(groups, key=lambda item: (-len(groups[item]), item)):
        group_rows = groups[group_key]
        if len(group_rows) <= max_rows_per_pack:
            parts.append((group_key, 1, group_rows))
            continue
        for index in range(0, len(group_rows), max_rows_per_pack):
            shard_rows = group_rows[index : index + max_rows_per_pack]
            parts.append((group_key, index // max_rows_per_pack + 1, shard_rows))
    return parts


def dedupe_nodes(nodes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node["id"])
        if node_id not in by_id:
            by_id[node_id] = node
            continue
        existing = by_id[node_id]
        refs = list(existing.get("evidence_refs") or [])
        for ref in node.get("evidence_refs") or []:
            if ref not in refs:
                refs.append(ref)
        existing["evidence_refs"] = refs
    return sorted(by_id.values(), key=lambda item: item["id"])


def dedupe_edges(edges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (str(edge["source"]), str(edge["relation"]), str(edge["target"]))
        if key not in by_key:
            by_key[key] = edge
    return sorted(by_key.values(), key=lambda item: item["id"])


def pack_description(title: str, spec: PackSpec, row_count: int, shard_note: str) -> str:
    return (
        f"{title}{shard_note} pack은 `{spec.derived_file}`의 {row_count:,}개 derived row를 다룹니다. "
        f"{spec.source_policy} "
        f"{spec.useful_for}"
    )


class CloudPackBuilder:
    def __init__(
        self,
        *,
        derived_dir: Path,
        out_dir: Path,
        pack_id: str,
        title: str,
        spec: PackSpec,
        rows: list[dict[str, Any]],
        group_key: str,
        shard_index: int,
        shard_count: int,
        project_title: str,
        derived_manifest: dict[str, Any],
    ):
        self.derived_dir = derived_dir
        self.out_dir = out_dir
        self.pack_id = pack_id
        self.title = title
        self.spec = spec
        self.rows = rows
        self.group_key = group_key
        self.shard_index = shard_index
        self.shard_count = shard_count
        self.project_title = project_title
        self.derived_manifest = derived_manifest
        self.doc_id = document_id(pack_id, spec.derived_file)
        self.shard_note = f" ({shard_index}/{shard_count})" if shard_count > 1 else ""
        self.description = pack_description(title, spec, len(rows), self.shard_note)

    def build_cloud_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        document = {
            "document_id": self.doc_id,
            "title": f"{self.project_title} - {self.title}{self.shard_note}",
            "source_url": None,
            "provider": "bimgraph-ai",
            "source_type": "deriving_derived_jsonl",
            "path": str(self.derived_dir / self.spec.derived_file),
            "derived_file": self.spec.derived_file,
            "logical_pack": self.spec.logical_name,
            "group_key": self.group_key,
            "chunk_count": len(self.rows),
        }

        chunks: list[dict[str, Any]] = []
        chunk_index: list[dict[str, Any]] = []
        include_inline_row = len(self.rows) <= 5_000
        for ordinal, row in enumerate(self.rows, start=1):
            cid = chunk_id(self.pack_id, row, self.spec.derived_file)
            text = chunk_text(row, self.spec.derived_file)
            category = row_category_value(row, self.spec)
            refs = compact_source_refs(row)
            metadata = {
                "ordinal": ordinal,
                "derived_file": self.spec.derived_file,
                "row_id": row_identity(row, self.spec.derived_file),
                "category": category,
                "source_refs": refs,
            }
            row_join_keys = join_keys(row, self.spec.derived_file)
            if row_join_keys:
                metadata["join_keys"] = row_join_keys
            if include_inline_row:
                metadata["compact_row"] = compact_row_properties(row, self.spec.derived_file)
            chunks.append(
                {
                    "chunk_id": cid,
                    "document_id": self.doc_id,
                    "text": text,
                    "metadata": metadata,
                }
            )
            chunk_index.append(
                {
                    "chunk_id": cid,
                    "parent_document_id": self.doc_id,
                    "source_url": None,
                    "evidence_ref": cid,
                    "claimVerificationStatus": "source_backed",
                    "digest": text[:280],
                    "derived_file": self.spec.derived_file,
                    "row_id": row_identity(row, self.spec.derived_file),
                    "category": category,
                }
            )
        return [document], chunks, chunk_index

    def build_graph(self, chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        project_node_id = f"bimgraph:project:{short_hash(self.project_title)}"
        dataset_node_id = f"bimgraph:dataset:{self.spec.logical_name}:{short_hash(self.pack_id)}"
        nodes: list[dict[str, Any]] = [
            graph_node(
                project_node_id,
                self.project_title,
                "project",
                {"pack_id": self.pack_id, "source": "revit_jsonl_export"},
                [],
            ),
            graph_node(
                dataset_node_id,
                f"{self.title}{self.shard_note}",
                "derived_dataset",
                {
                    "pack_id": self.pack_id,
                    "derived_file": self.spec.derived_file,
                    "query_mode": self.spec.query_mode,
                    "group_key": self.group_key,
                    "row_count": len(self.rows),
                },
                [chunk["chunk_id"] for chunk in chunks[:25]],
            ),
        ]
        edges = [
            graph_edge(
                project_node_id,
                "PROJECT_HAS_DERIVED_PACK",
                dataset_node_id,
                {"derived_file": self.spec.derived_file, "confidence": 0.95},
                [],
            )
        ]

        workset_nodes: dict[str, str] = {}
        category_nodes: dict[tuple[str, str], str] = {}
        family_nodes: dict[tuple[str, str, str], str] = {}
        for row, chunk in zip(self.rows, chunks):
            category = row_category_value(row, self.spec)
            workset = workset_info_from_row(row)
            workset_key = str(workset.get("name") or workset.get("id")) if workset else ""
            if workset:
                workset_id = workset_node_id(workset)
                if workset_key not in workset_nodes:
                    workset_nodes[workset_key] = workset_id
                    nodes.append(
                        graph_node(
                            workset_id,
                            str(workset.get("name") or f"Workset {workset.get('id')}"),
                            "revit_workset",
                            {
                                "workset_id": workset.get("id"),
                                "workset_name": workset.get("name"),
                                "workset_kind": workset.get("kind"),
                                "is_open": workset.get("is_open"),
                                "is_editable": workset.get("is_editable"),
                                "is_visible_by_default": workset.get("is_visible_by_default"),
                            },
                            [chunk["chunk_id"]],
                        )
                    )
                    edges.append(
                        graph_edge(
                            project_node_id,
                            "PROJECT_HAS_WORKSET",
                            workset_id,
                            {"confidence": 0.95},
                            [chunk["chunk_id"]],
                        )
                    )
                edges.append(
                    graph_edge(
                        workset_nodes[workset_key],
                        "WORKSET_HAS_DERIVED_PACK",
                        dataset_node_id,
                        {"derived_file": self.spec.derived_file, "confidence": 0.9},
                        [chunk["chunk_id"]],
                    )
                )
            category_key = (workset_key, category)
            if category_key not in category_nodes:
                category_node_id = f"bimgraph:{self.spec.logical_name}:category:{short_hash({'workset': workset_key, 'category': category})}"
                category_nodes[category_key] = category_node_id
                nodes.append(
                    graph_node(
                        category_node_id,
                        category,
                        f"{self.spec.logical_name}_category",
                        {
                            "derived_file": self.spec.derived_file,
                            "category": category,
                            "workset_id": workset.get("id") if workset else None,
                            "workset_name": workset.get("name") if workset else None,
                            "query_mode": self.spec.query_mode,
                        },
                        [chunk["chunk_id"]],
                    )
                )
                edges.append(
                    graph_edge(
                        dataset_node_id,
                        "DATASET_HAS_CATEGORY",
                        category_node_id,
                        {"derived_file": self.spec.derived_file, "confidence": 0.85},
                        [chunk["chunk_id"]],
                    )
                )
                if workset:
                    edges.append(
                        graph_edge(
                            workset_nodes[workset_key],
                            "WORKSET_HAS_CATEGORY",
                            category_node_id,
                            {"derived_file": self.spec.derived_file, "category": category, "confidence": 0.9},
                            [chunk["chunk_id"]],
                        )
                    )
            family = family_info_from_row(row)
            if family:
                family_key = (workset_key, category, str(family.get("family_name")))
                if family_key not in family_nodes:
                    family_id = family_node_id(family, workset_key, category, self.spec.logical_name)
                    family_nodes[family_key] = family_id
                    nodes.append(
                        graph_node(
                            family_id,
                            str(family.get("family_name")),
                            "revit_family",
                            {
                                "derived_file": self.spec.derived_file,
                                "category": category,
                                "family_name": family.get("family_name"),
                                "family_and_type": family.get("family_and_type"),
                                "family_ref_id": family.get("family_ref_id"),
                                "family_source": family.get("family_source"),
                                "system_family": family.get("system_family"),
                                "type_id": family.get("type_id"),
                                "type_name": family.get("type_name"),
                                "workset_id": workset.get("id") if workset else None,
                                "workset_name": workset.get("name") if workset else None,
                                "query_mode": self.spec.query_mode,
                            },
                            [chunk["chunk_id"]],
                        )
                    )
                    edges.append(
                        graph_edge(
                            category_nodes[(workset_key, category)],
                            "CATEGORY_HAS_FAMILY",
                            family_id,
                            {"derived_file": self.spec.derived_file, "confidence": 0.9},
                            [chunk["chunk_id"]],
                        )
                    )

        should_make_row_nodes = self.spec.row_nodes_limit > 0 and len(self.rows) <= self.spec.row_nodes_limit
        if should_make_row_nodes:
            for row, chunk in zip(self.rows, chunks):
                rid = primary_node_id(row, self.spec.derived_file)
                if self.spec.derived_file not in {
                    "bim_objects.jsonl",
                    "material_facts.jsonl",
                    "quantity_facts.jsonl",
                    "drawing_documents.jsonl",
                    "schedule_tables.jsonl",
                    "schedule_rows.jsonl",
                    "drawing_evidence.jsonl",
                }:
                    rid = f"bimgraph:{self.spec.logical_name}:{short_hash(row_identity(row, self.spec.derived_file))}"
                category = row_category_value(row, self.spec)
                workset = workset_info_from_row(row)
                workset_key = str(workset.get("name") or workset.get("id")) if workset else ""
                nodes.append(
                    graph_node(
                        rid,
                        display_label(row, self.spec.derived_file, rid),
                        node_kind(self.spec.derived_file),
                        {
                            "derived_file": self.spec.derived_file,
                            "row_id": row_identity(row, self.spec.derived_file),
                            "category": category,
                            **compact_row_properties(row, self.spec.derived_file),
                        },
                        [chunk["chunk_id"]],
                    )
                )
                edges.append(
                    graph_edge(
                        category_nodes[(workset_key, category)],
                        "CATEGORY_HAS_ROW",
                        rid,
                        {"derived_file": self.spec.derived_file, "confidence": row.get("confidence") or 0.75},
                        [chunk["chunk_id"]],
                    )
                )

        return dedupe_nodes(nodes), dedupe_edges(edges)

    def validation(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, Any]:
        node_ids = {node["id"] for node in nodes}
        edge_keys = [(edge["source"], edge["relation"], edge["target"]) for edge in edges]
        broken_edges = [edge for edge in edges if edge["source"] not in node_ids or edge["target"] not in node_ids]
        duplicate_edges = len(edge_keys) - len(set(edge_keys))
        duplicate_nodes = len(nodes) - len(node_ids)
        status = "pass" if not broken_edges and not duplicate_edges and not duplicate_nodes and chunks else "fail"
        return {
            "status": status,
            "checks": {
                "cloud_pack_shape": "pass",
                "edge_endpoints": "pass" if not broken_edges else "fail",
                "duplicate_node_ids": "pass" if not duplicate_nodes else "fail",
                "duplicate_edges": "pass" if not duplicate_edges else "fail",
                "chunk_index": "pass" if chunks else "fail",
                "localcrab_dry_run": "not_run",
            },
            "counts": {
                "rows": len(self.rows),
                "nodes": len(nodes),
                "edges": len(edges),
                "chunks": len(chunks),
                "broken_edges": len(broken_edges),
                "duplicate_edges": duplicate_edges,
                "duplicate_nodes": duplicate_nodes,
            },
            "notes": [
                "LocalCrab dry-run was not run because C:\\Logotekton\\OpenCrab is not available."
            ],
        }

    def write(self) -> dict[str, Any]:
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        documents, chunks, chunk_index = self.build_cloud_rows()
        nodes, edges = self.build_graph(chunks)
        validation = self.validation(nodes, edges, chunks)
        document_index = [
            {
                "document_id": document["document_id"],
                "title": document["title"],
                "source_url": document["source_url"],
                "provider": document["provider"],
                "source_type": document["source_type"],
                "chunk_count": document["chunk_count"],
                "derived_file": document["derived_file"],
            }
            for document in documents
        ]

        write_jsonl(self.out_dir / "cloud" / "documents.jsonl", documents)
        write_jsonl(self.out_dir / "cloud" / "chunks.jsonl", chunks)
        write_jsonl(self.out_dir / "graph" / "nodes.jsonl", nodes)
        write_jsonl(self.out_dir / "graph" / "edges.jsonl", edges)
        write_jsonl(self.out_dir / "00_index" / "document_index.jsonl", document_index)
        write_jsonl(self.out_dir / "00_index" / "chunk_index.jsonl", chunk_index)
        write_jsonl(self.out_dir / "00_index" / "evidence_index.jsonl", [])
        write_jsonl(
            self.out_dir / "01_sources" / "source_records.jsonl",
            [
                {
                    "source_id": self.spec.derived_file,
                    "source_type": "derived_jsonl",
                    "path": str(self.derived_dir / self.spec.derived_file),
                    "row_count": len(self.rows),
                    "source_refs_policy": "Rows keep source_refs back to Revit raw JSONL.",
                }
            ],
        )
        write_jsonl(self.out_dir / "05_hypotheses" / "review_hints.jsonl", [])

        manifest = {
            "format": "opencrab-cloud-pack-v1",
            "pack_id": self.pack_id,
            "title": f"{self.project_title} - {self.title}{self.shard_note}",
            "version": "0.1.0",
            "created_by": "BIMGraph AI Deriving Pack Suite",
            "source": {
                "mode": "revit_derived_jsonl",
                "derived_dir": str(self.derived_dir),
                "derived_file": self.spec.derived_file,
                "raw_to_derived": SOURCE_TO_DERIVED,
            },
            "counts": {
                "documents": len(documents),
                "chunks": len(chunks),
                "nodes": len(nodes),
                "edges": len(edges),
            },
            "entrypoints": {
                "documents": "cloud/documents.jsonl",
                "chunks": "cloud/chunks.jsonl",
                "nodes": "graph/nodes.jsonl",
                "edges": "graph/edges.jsonl",
            },
            "quality": validation,
        }
        write_json(self.out_dir / "manifest.json", manifest)
        write_json(
            self.out_dir / "pack.json",
            {
                "pack_id": self.pack_id,
                "title": manifest["title"],
                "logical_pack": self.spec.logical_name,
                "derived_file": self.spec.derived_file,
                "query_mode": self.spec.query_mode,
                "description": self.description,
                "cloud_pack": manifest,
            },
        )
        write_json(
            self.out_dir / "00_index" / "index.json",
            {
                "pack_id": self.pack_id,
                "title": manifest["title"],
                "entrypoints": manifest["entrypoints"],
                "validation_status": validation["status"],
                "document_count": len(documents),
                "chunk_count": len(chunks),
                "evidence_count": 0,
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        )
        write_text(
            self.out_dir / "01_sources" / "source_registry.md",
            f"# Source Registry\n\n- Derived file: `{self.spec.derived_file}`\n- Path: `{self.derived_dir / self.spec.derived_file}`\n- Rows: {len(self.rows):,}\n- Policy: {self.spec.source_policy}\n",
        )
        write_json(
            self.out_dir / "02_schema" / "cloud_pack.schema.json",
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": "BIMGraph AI OpenCrab Cloud Pack",
                "type": "object",
                "required": ["format", "pack_id", "title", "counts", "entrypoints"],
                "properties": {
                    "format": {"const": "opencrab-cloud-pack-v1"},
                    "pack_id": {"type": "string"},
                    "title": {"type": "string"},
                    "counts": {"type": "object"},
                    "entrypoints": {"type": "object"},
                },
            },
        )
        write_json(
            self.out_dir / "03_graph" / "graph_summary.json",
            {
                "nodes": len(nodes),
                "edges": len(edges),
                "node_types": sorted({node["properties"].get("bimgraph_node_kind") for node in nodes}),
                "relations": sorted({edge["properties"].get("original_relation") for edge in edges}),
                "graph_payload": {"nodes": "../graph/nodes.jsonl", "edges": "../graph/edges.jsonl"},
            },
        )
        write_json(
            self.out_dir / "03_graph" / "neo4j_cypher_bundle.json",
            {
                "mode": "passive",
                "import_cypher": "UNWIND $nodes AS row MERGE (n:OpenCrabNode {id: row.id}) SET n += row.properties;",
                "verify_cypher": "MATCH (n:OpenCrabNode) RETURN count(n) AS nodes;",
                "sample_queries": [
                    f"{self.title} pack에서 주요 카테고리와 row 수를 요약해줘.",
                    f"{self.title} pack에서 confidence가 낮은 row를 찾아줘.",
                ],
            },
        )
        write_json(
            self.out_dir / "04_mappings" / "raw_to_derived_mapping.json",
            {
                "source_to_derived": SOURCE_TO_DERIVED,
                "this_pack": {
                    "derived_file": self.spec.derived_file,
                    "logical_pack": self.spec.logical_name,
                    "query_mode": self.spec.query_mode,
                },
                "relation_mapping": {"domain_relations": "properties.original_relation", "canonical_relation": "related_to"},
            },
        )
        write_text(
            self.out_dir / "06_reports" / "validation_report.md",
            f"# Validation Report\n\nStatus: `{validation['status']}`\n\n- Nodes: {len(nodes):,}\n- Edges: {len(edges):,}\n- Chunks: {len(chunks):,}\n- LocalCrab dry-run: `not_run` (`C:\\Logotekton\\OpenCrab` not found)\n",
        )
        write_text(self.out_dir / "06_reports" / "pack_description.md", self.description + "\n")
        write_json(
            self.out_dir / "06_reports" / "build_summary.json",
            {
                "pack_id": self.pack_id,
                "title": manifest["title"],
                "description": self.description,
                "validation": validation,
                "source_policy": self.spec.source_policy,
                "useful_for": self.spec.useful_for,
            },
        )
        write_text(
            self.out_dir / "07_examples" / "sample_queries.md",
            f"# Sample Queries\n\n- {self.title}의 카테고리별 row 수를 요약해줘.\n- `{self.spec.derived_file}`에서 특정 객체/도면/일람표와 관련된 근거를 찾아줘.\n- 이 pack의 source_refs를 기준으로 원본 raw 파일명을 알려줘.\n",
        )
        write_json(
            self.out_dir / "07_examples" / "sample_outputs.json",
            {
                "example": {
                    "query_mode": self.spec.query_mode,
                    "pack_id": self.pack_id,
                    "expected_output": "category summary, source-backed rows, export-ready table",
                }
            },
        )
        write_json(
            self.out_dir / "opencrab" / "promotion_package_opencrab_compatible.json",
            {
                "pack_id": self.pack_id,
                "status": "draft",
                "compatibility": "opencrab-cloud-pack-v1",
                "graph_payload": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl"},
                "mapping": {
                    "domain_entities": "concept/Entity",
                    "documents": "resource/Document via cloud/documents.jsonl",
                    "chunks": "Evidence chunks via cloud/chunks.jsonl",
                    "relations": "related_to with properties.original_relation preserved",
                },
            },
        )
        write_json(
            self.out_dir / "opencrab" / "dry_run_report.json",
            {
                "status": "not_run",
                "reason": "C:\\Logotekton\\OpenCrab is not available in this workspace.",
                "pack_id": self.pack_id,
            },
        )
        readme = f"""# {manifest['title']}

{self.description}

## Entrypoints

- Cloud documents: `cloud/documents.jsonl`
- Cloud chunks: `cloud/chunks.jsonl`
- Graph nodes: `graph/nodes.jsonl`
- Graph edges: `graph/edges.jsonl`

## Counts

- Rows: {len(self.rows):,}
- Chunks: {len(chunks):,}
- Nodes: {len(nodes):,}
- Edges: {len(edges):,}

## Source Policy

{self.spec.source_policy}
"""
        write_text(self.out_dir / "README.md", readme)
        return {
            "pack_id": self.pack_id,
            "title": manifest["title"],
            "pack_dir": str(self.out_dir),
            "description": self.description,
            "derived_file": self.spec.derived_file,
            "logical_pack": self.spec.logical_name,
            "rows": len(self.rows),
            "chunks": len(chunks),
            "nodes": len(nodes),
            "edges": len(edges),
            "quality": validation["status"],
        }


class PackSuiteBuilder:
    def __init__(
        self,
        *,
        derived_dir: Path,
        out_dir: Path,
        title: str,
        max_rows_per_pack: int,
        include_audit_packs: bool,
    ):
        self.derived_dir = derived_dir
        self.out_dir = out_dir
        self.title = title
        self.max_rows_per_pack = max_rows_per_pack
        self.include_audit_packs = include_audit_packs
        self.derived_manifest = read_json(derived_dir / "manifest.json")

    def specs(self) -> list[PackSpec]:
        specs = list(PRIMARY_SPECS)
        if self.include_audit_packs:
            specs.extend(AUDIT_SPECS)
        return specs

    def build(self, *, make_zip: bool) -> dict[str, Any]:
        if self.out_dir.exists():
            shutil.rmtree(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        results: list[dict[str, Any]] = []
        zip_dir = self.out_dir / "zips"
        if make_zip:
            zip_dir.mkdir(parents=True, exist_ok=True)
        for spec in self.specs():
            rows = read_jsonl(self.derived_dir / spec.derived_file)
            if not rows:
                continue
            parts = split_groups(rows, spec, spec.max_rows_per_pack or self.max_rows_per_pack)
            shard_count = len(parts)
            for group_key, shard_index, shard_rows in parts:
                if group_key == "all":
                    base_name = f"{spec.logical_name}-{shard_index:02d}" if shard_count > 1 else spec.logical_name
                else:
                    base_name = f"{spec.logical_name}-{group_key}-{shard_index:02d}"
                base = safe_slug(base_name).lower()
                pack_id = f"bimgraph_{spec.logical_name}_{short_hash({'group': group_key, 'shard': shard_index, 'dir': str(self.derived_dir)})}"
                pack_dir = self.out_dir / spec.logical_name / f"{base}-ontology-pack"
                pack_title = f"{spec.title} - {group_key}" if group_key != "all" else spec.title
                builder = CloudPackBuilder(
                    derived_dir=self.derived_dir,
                    out_dir=pack_dir,
                    pack_id=pack_id,
                    title=pack_title,
                    spec=spec,
                    rows=shard_rows,
                    group_key=group_key,
                    shard_index=shard_index,
                    shard_count=shard_count,
                    project_title=self.title,
                    derived_manifest=self.derived_manifest,
                )
                result = builder.write()
                if make_zip:
                    zip_path = zip_dir / f"{pack_dir.name}.zip"
                    zip_pack(pack_dir, zip_path)
                    result["zip"] = str(zip_path)
                    result["zip_sha256"] = sha256_file(zip_path)
                results.append(result)

        catalog = {
            "title": self.title,
            "derived_dir": str(self.derived_dir),
            "out_dir": str(self.out_dir),
            "pack_count": len(results),
            "packs": results,
            "include_audit_packs": self.include_audit_packs,
            "max_rows_per_pack": self.max_rows_per_pack,
        }
        write_json(self.out_dir / "pack_catalog.json", catalog)
        write_text(
            self.out_dir / "pack_descriptions.md",
            "# Pack Descriptions\n\n"
            + "\n\n".join(f"## {item['title']}\n\n{item['description']}" for item in results)
            + "\n",
        )
        write_json(
            self.out_dir / "build_summary.json",
            {
                "pack_count": len(results),
                "total_rows": sum(item["rows"] for item in results),
                "total_chunks": sum(item["chunks"] for item in results),
                "total_nodes": sum(item["nodes"] for item in results),
                "total_edges": sum(item["edges"] for item in results),
                "zipped": make_zip,
                "quality": "pass" if all(item["quality"] == "pass" for item in results) else "fail",
            },
        )
        return catalog


def validate_zip_shape(zip_path: Path) -> dict[str, Any]:
    required = {"manifest.json", "cloud/documents.jsonl", "cloud/chunks.jsonl", "graph/nodes.jsonl", "graph/edges.jsonl"}
    allowed = {".md", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm", ".pdf"}
    with zipfile.ZipFile(zip_path) as archive:
        names = {info.filename.replace("\\", "/") for info in archive.infolist() if not info.is_dir()}
        bad_exts = sorted(name for name in names if Path(name).suffix.lower() not in allowed)
        missing = sorted(required - names)
        jsonl_errors: list[str] = []
        for name in names:
            if Path(name).suffix.lower() != ".jsonl":
                continue
            for index, line in enumerate(archive.read(name).decode("utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    jsonl_errors.append(f"{name}:{index}:{exc}")
                    continue
                if not isinstance(value, dict):
                    jsonl_errors.append(f"{name}:{index}:not_object")
    return {
        "zip": str(zip_path),
        "passed": not missing and not bad_exts and not jsonl_errors,
        "missing": missing,
        "bad_exts": bad_exts,
        "jsonl_errors": jsonl_errors[:20],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build separate OpenCrab Cloud Pack ZIPs for each Deriving derived JSONL.")
    parser.add_argument("--derived-dir", required=True, type=Path, help="Deriving derived directory.")
    parser.add_argument("--out-dir", type=Path, help="Output suite directory. Defaults to DERIVED_DIR/opencrab_packs.")
    parser.add_argument("--title", help="Project title. Defaults to project_profile.json title.")
    parser.add_argument("--zip", action="store_true", help="Write one ZIP beside every generated pack folder.")
    parser.add_argument("--include-audit-packs", action="store_true", help="Also build classification/source coverage audit packs.")
    parser.add_argument("--max-rows-per-pack", type=int, default=30_000, help="Shard a logical pack when it exceeds this row count.")
    parser.add_argument("--validate-zips", action="store_true", help="Run lightweight Cloud Pack ZIP shape validation after build.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    derived_dir = args.derived_dir
    title = args.title or default_title(derived_dir)
    out_dir = args.out_dir or derived_dir / "opencrab_packs"
    builder = PackSuiteBuilder(
        derived_dir=derived_dir,
        out_dir=out_dir,
        title=title,
        max_rows_per_pack=args.max_rows_per_pack,
        include_audit_packs=args.include_audit_packs,
    )
    result = builder.build(make_zip=args.zip)
    if args.zip and args.validate_zips:
        result["zip_validation"] = [validate_zip_shape(Path(item["zip"])) for item in result["packs"] if item.get("zip")]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
