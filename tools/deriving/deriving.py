from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RAW_FILES = [
    "elements.jsonl",
    "materials.jsonl",
    "quantities.jsonl",
    "sheets.jsonl",
    "sheet_pdfs.jsonl",
    "sheet_dxfs.jsonl",
    "view_dxfs.jsonl",
    "drawing_entities.jsonl",
    "views.jsonl",
    "annotations.jsonl",
    "schedules.jsonl",
    "schedule_cells.jsonl",
    "graph_edges.jsonl",
]

DRAWING_CONTEXT_FILES = [
    "drawing_stage45_element_representation_index.jsonl",
    "drawing_stage45_element_representation_links.jsonl",
    "drawing_stage45_remaining_revit_elements.jsonl",
    "drawing_stage45_semantic_only_revit_elements.jsonl",
    "drawing_stage45_element_topology_groups.jsonl",
    "drawing_stage45_element_topology_group_members.jsonl",
]

RAW_FILES.extend(DRAWING_CONTEXT_FILES)


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


KEEP = "keep"
AUXILIARY = "auxiliary"
EXCLUDE = "exclude_candidate"
UNKNOWN = "unknown"


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


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compact_hash(value: Any) -> str:
    return stable_hash(value)[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def first_present(*values: Any) -> Any:
    for value in values:
        if value is None or value == "":
            continue
        return value
    return None


def drop_empty(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [], {})}


def source_ref(source_file: str, row: dict[str, Any]) -> dict[str, Any]:
    element = row.get("element") if isinstance(row.get("element"), dict) else {}
    sheet = row.get("sheet") if isinstance(row.get("sheet"), dict) else {}
    view = row.get("view") if isinstance(row.get("view"), dict) else {}
    drawing_ref = row.get("drawing_ref") if isinstance(row.get("drawing_ref"), dict) else {}
    source_id = (
        row.get("node_id")
        or row.get("index_key")
        or row.get("link_key")
        or row.get("remaining_unmatched_key")
        or row.get("semantic_only_key")
        or row.get("group_key")
        or row.get("member_key")
        or row.get("unique_id")
        or row.get("element_id")
        or row.get("material_id")
        or row.get("schedule_id")
        or row.get("sheet_number")
        or row.get("entity_key")
        or row.get("record_key")
        or row.get("handle")
        or compact_hash(row)
    )
    ref = {
        "source_file": source_file,
        "source_id": source_id,
        "source_node_id": row.get("node_id"),
        "raw_record_hash": stable_hash(row),
    }
    record_type = str(row.get("record_type") or "")
    is_element_ref = bool(
        source_file in {"elements.jsonl", "quantities.jsonl"}
        or record_type in {"bim_object", "quantity_fact"}
        or row.get("source_element_id") is not None
        or row.get("element_id") is not None
        or element.get("element_id") is not None
        or row.get("element_unique_id") is not None
        or element.get("unique_id") is not None
    )
    is_sheet_ref = bool(
        source_file == "sheets.jsonl"
        or record_type == "drawing_document"
        or row.get("sheet_id") is not None
        or row.get("source_sheet_id") is not None
        or row.get("sheet_number") is not None
        or sheet.get("sheet_number") is not None
    )
    is_view_ref = bool(
        source_file == "views.jsonl"
        or row.get("view_id") is not None
        or row.get("view_name") is not None
        or view.get("element_id") is not None
    )
    is_schedule_ref = bool(
        source_file in {"schedules.jsonl", "schedule_cells.jsonl"}
        or record_type == "schedule_row"
        or row.get("schedule_id") is not None
        or row.get("source_schedule_id") is not None
        or row.get("schedule_name") is not None
    )
    sheet_row_unique_id = row.get("unique_id") if source_file == "sheets.jsonl" or record_type == "drawing_document" else None
    view_row_unique_id = row.get("unique_id") if source_file == "views.jsonl" else None
    schedule_row_unique_id = (
        row.get("unique_id") if source_file in {"schedules.jsonl", "schedule_cells.jsonl"} or record_type == "schedule_row" else None
    )
    ref.update(
        drop_empty(
            {
                "record_type": row.get("record_type"),
                "element_id": (
                    first_present(row.get("source_element_id"), row.get("element_id"), element.get("element_id"))
                    if is_element_ref
                    else None
                ),
                "unique_id": (
                    first_present(row.get("unique_id"), row.get("element_unique_id"), element.get("unique_id"))
                    if is_element_ref
                    else None
                ),
                "ifc_guid": first_present(row.get("ifc_guid"), element.get("ifc_guid")) if is_element_ref else None,
                "sheet_id": (
                    first_present(row.get("source_sheet_id"), row.get("sheet_id"), sheet.get("element_id"))
                    if is_sheet_ref
                    else None
                ),
                "sheet_number": first_present(row.get("sheet_number"), sheet.get("sheet_number")) if is_sheet_ref else None,
                "sheet_name": (
                    first_present(row.get("sheet_name"), sheet.get("name"), sheet.get("sheet_name"))
                    if is_sheet_ref
                    else None
                ),
                "sheet_unique_id": (
                    first_present(row.get("sheet_unique_id"), sheet.get("unique_id"), sheet_row_unique_id)
                    if is_sheet_ref
                    else None
                ),
                "view_id": first_present(row.get("view_id"), view.get("element_id")) if is_view_ref else None,
                "view_name": first_present(row.get("view_name"), view.get("name")) if is_view_ref else None,
                "view_unique_id": (
                    first_present(row.get("view_unique_id"), view.get("unique_id"), view_row_unique_id)
                    if is_view_ref
                    else None
                ),
                "schedule_id": (
                    first_present(row.get("schedule_id"), row.get("source_schedule_id")) if is_schedule_ref else None
                ),
                "schedule_unique_id": (
                    first_present(row.get("schedule_unique_id"), schedule_row_unique_id) if is_schedule_ref else None
                ),
                "schedule_name": first_present(row.get("schedule_name"), row.get("name")) if is_schedule_ref else None,
                "section": row.get("section") if is_schedule_ref else None,
                "row": row.get("row") if is_schedule_ref else None,
                "column": row.get("column") if is_schedule_ref else None,
                "page": first_present(row.get("page"), row.get("page_number")),
                "dxf_file_name": first_present(row.get("dxf_file_name"), drawing_ref.get("dxf_file_name")),
                "entity_key": row.get("entity_key"),
                "source_entity_key": row.get("source_entity_key"),
                "handle": row.get("handle"),
                "entity_type": first_present(row.get("entity_type"), drawing_ref.get("entity_type")),
            }
        )
    )
    return ref


def source_key(source_file: str, row: dict[str, Any]) -> str:
    ref = source_ref(source_file, row)
    return f"{source_file}:{ref['source_id']}"


def strip_hash_prefix(name: str | None) -> str:
    if not name:
        return ""
    return str(name).lstrip("#").strip()


def category_name(element: dict[str, Any]) -> str:
    category = element.get("category")
    if isinstance(category, dict):
        return str(category.get("name") or "")
    return str(category or "")


def type_name(element: dict[str, Any]) -> str:
    type_value = element.get("type")
    if isinstance(type_value, dict):
        return str(type_value.get("name") or type_value.get("family_name") or "")
    return ""


def is_dwg_category(name: str) -> bool:
    return name.lower().endswith(".dwg")


def classify_element(element: dict[str, Any]) -> dict[str, Any]:
    category = category_name(element)
    klass = str(element.get("class") or "")
    type_text = type_name(element)
    combined = f"{category} {element.get('name') or ''} {type_text} {klass}".lower()

    exclude_tokens = [
        "sketch",
        "스케치",
        "detail",
        "상세 항목",
        "insulation lines",
        "단열재 배팅선",
        "material",
        "재료",
        "재질",
        "asset",
        "legend",
        "범례",
        "sun path",
        "태양",
        "camera",
        "카메라",
        "project information",
        "프로젝트 정보",
        "topography",
        "toposolid",
        "contour",
        "등고선",
        "base point",
        "기준점",
        "internal origin",
        "내부 원점",
        "raster",
        "래스터",
        "rvt link",
        "rvt 링크",
        "cad link",
        "dwg",
        "curtain grid",
        "커튼 루프 그리드",
        "grid 배치",
        "난간의 난간 경로 확장 선",
    ]
    core_tokens = [
        "wall",
        "벽",
        "floor",
        "바닥",
        "door",
        "문",
        "window",
        "창",
        "ceiling",
        "천장",
        "roof",
        "지붕",
        "structural framing",
        "구조 프레임",
        "structural column",
        "구조 기둥",
        "structural foundation",
        "구조 기초",
        "room",
        "룸",
        "space",
    ]
    auxiliary_tokens = [
        "curtain panel",
        "커튼월 패널",
        "wall sweep",
        "벽 스윕",
        "generic model",
        "일반 모델",
        "furniture",
        "가구",
        "railing",
        "난간",
        "baluster",
        "난간동자",
        "stair",
        "계단",
        "gutter",
        "거터",
        "fascia",
        "처마돌림",
        "pipe segment",
        "배관 세그먼트",
        "hvac",
    ]

    if is_dwg_category(category) or any(token in combined for token in exclude_tokens):
        return {
            "decision": EXCLUDE,
            "ai_category": "reference_or_internal_object",
            "importance": "exclude_candidate",
            "confidence": 0.86,
            "reason": "Category/name indicates sketch, internal, reference, material, view, or CAD-style support data.",
        }
    if any(token in combined for token in core_tokens):
        return {
            "decision": KEEP,
            "ai_category": "core_bim_object",
            "importance": "high",
            "confidence": 0.82,
            "reason": "Category/name indicates a primary building or space object.",
        }
    if any(token in combined for token in auxiliary_tokens):
        return {
            "decision": AUXILIARY,
            "ai_category": "auxiliary_bim_object",
            "importance": "medium",
            "confidence": 0.72,
            "reason": "Category/name indicates a secondary BIM object that can support core object interpretation.",
        }
    if "Autodesk.Revit.DB" in klass:
        return {
            "decision": AUXILIARY,
            "ai_category": "unclassified_bim_object",
            "importance": "medium",
            "confidence": 0.5,
            "reason": "Revit DB object without enough project-specific signals; keep for AI review.",
        }
    return {
        "decision": UNKNOWN,
        "ai_category": "unknown_element",
        "importance": "low",
        "confidence": 0.35,
        "reason": "No strong bootstrap classification signal.",
    }


def classify_material(material: dict[str, Any]) -> dict[str, Any]:
    name = strip_hash_prefix(str(material.get("name") or ""))
    category = str(material.get("material_category") or "")
    combined = f"{name} {category}".lower()
    groups = [
        ("glass", ["glass", "유리"]),
        ("metal", ["metal", "steel", "al", "철", "스틸", "알루미늄", "금속", "데크", "루버", "강판", "함석", "srt"]),
        ("concrete_mortar_masonry", ["concrete", "mortar", "masonry", "콘크리트", "몰탈", "모르타르", "시멘트", "벽돌"]),
        ("insulation_acoustic", ["insulation", "단열", "보온", "그라스울", "우레탄", "흡음", "암면"]),
        ("waterproofing_coating_sealant", ["waterproof", "paint", "방수", "도막", "페인트", "에폭시", "코킹", "tpo"]),
        ("board_panel_sheet", ["board", "panel", "보드", "패널", "합판", "mdf", "석고"]),
        ("tile_stone_floor_finish", ["tile", "stone", "타일", "석재", "대리석", "화강석", "테라조", "강마루"]),
        ("plastic_product", ["plastic", "pvc", "abs", "합성수지", "시스템욕실", "가구"]),
    ]
    if name.lower() in {"hidden", "default wall", "default"} or "hidden" in combined:
        return {
            "material_group": "default_or_hidden",
            "importance": "exclude_candidate",
            "confidence": 0.9,
            "reason": "Material appears to be hidden/default support data.",
        }
    for group, tokens in groups:
        if any(token in combined for token in tokens):
            return {
                "material_group": group,
                "importance": "high",
                "confidence": 0.74,
                "reason": "Material name/category matched a construction material signal.",
            }
    return {
        "material_group": "unclassified_material",
        "importance": "medium",
        "confidence": 0.42,
        "reason": "Material has no strong bootstrap group signal.",
    }


def classify_quantity(quantity: dict[str, Any]) -> dict[str, Any]:
    record_type = str(quantity.get("record_type") or "")
    parameter = quantity.get("parameter") if isinstance(quantity.get("parameter"), dict) else {}
    name = str(parameter.get("name") or "")
    text = name.lower()
    if record_type == "element_material_quantity":
        return {
            "quantity_category": "material_area_volume",
            "importance": "high",
            "confidence": 0.95,
            "reason": "Element-material quantity carries area/volume takeoff facts.",
        }
    groups = [
        ("linear_dimension", ["길이", "length", "폭", "width", "높이", "height", "offset", "간격띄우기"]),
        ("area_perimeter", ["면적", "area", "둘레", "perimeter"]),
        ("volume", ["체적", "volume"]),
        ("structural_member_length", ["절단 길이", "cut length"]),
        ("opening_dimension", ["씰", "헤드", "opening", "문틀"]),
        ("room_hvac_load", ["냉방", "난방", "기류", "실외 공기", "supply air"]),
        ("stair_railing_dimension", ["계단", "챌판", "난간"]),
        ("insulation_detail_dimension", ["단열"]),
        ("view_reference_dimension", ["눈 높이", "대상 높이", "무한한 높이"]),
    ]
    for group, tokens in groups:
        if any(token in text for token in tokens):
            importance = "low" if group == "view_reference_dimension" else "medium"
            if group in {"area_perimeter", "volume", "structural_member_length", "opening_dimension"}:
                importance = "high"
            return {
                "quantity_category": group,
                "importance": importance,
                "confidence": 0.7,
                "reason": "Parameter name matched a quantity semantic group.",
            }
    return {
        "quantity_category": "unclassified_parameter_quantity",
        "importance": "medium",
        "confidence": 0.35,
        "reason": "Parameter quantity needs AI review.",
    }


def classify_sheet(sheet: dict[str, Any]) -> dict[str, Any]:
    number = str(sheet.get("sheet_number") or "")
    name = str(sheet.get("name") or "")
    text = f"{number} {name}".lower()
    if re.search(r"(평면|floor plan|입면|elevation|단면|section)", text):
        return {
            "drawing_category": "core_drawing_sheet",
            "importance": "high",
            "confidence": 0.82,
            "reason": "Sheet name/number indicates plan, elevation, or section drawing.",
        }
    if re.search(r"(표지|목록|개요|cover|index|list|summary)", text):
        return {
            "drawing_category": "document_admin_sheet",
            "importance": "medium",
            "confidence": 0.76,
            "reason": "Sheet appears to be cover, list, or project summary.",
        }
    if re.search(r"(상세|detail|일람|schedule|마감|창호|벽체|패턴|계획|방수|방화|단열|흡음)", text):
        return {
            "drawing_category": "supporting_drawing_sheet",
            "importance": "medium",
            "confidence": 0.73,
            "reason": "Sheet appears to hold supporting detail, schedule, finish, or planning evidence.",
        }
    return {
        "drawing_category": "unclassified_drawing_sheet",
        "importance": "medium",
        "confidence": 0.38,
        "reason": "Sheet requires project-specific AI classification.",
    }


def classify_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    name = str(schedule.get("name") or "")
    fields = schedule.get("fields") if isinstance(schedule.get("fields"), list) else []
    field_text = " ".join(str(field.get("name") or "") + " " + str(field.get("column_heading") or "") for field in fields)
    text = f"{name} {field_text}".lower()
    if "도면" in text or "sheet" in text:
        category = "drawing_schedule"
        importance = "high" if "뷰" in text or "view" in text else "medium"
    elif "마감" in text or "finish" in text or "style schedule" in text:
        category = "finish_schedule"
        importance = "high"
    elif "floor" in text or "바닥" in text:
        category = "floor_quantity_schedule"
        importance = "high"
    elif "material takeoff" in text or "재료" in text:
        category = "material_takeoff_schedule"
        importance = "medium"
    else:
        category = "unclassified_schedule"
        importance = "medium"
    return {
        "schedule_category": category,
        "importance": importance,
        "confidence": 0.68 if category != "unclassified_schedule" else 0.36,
        "reason": "Schedule name and fields matched a table semantic group." if category != "unclassified_schedule" else "Schedule needs AI review.",
    }


def classify_relation(edge: dict[str, Any]) -> dict[str, Any]:
    relation = str(edge.get("relation") or "")
    groups = {
        "model_semantic_edge": {"ELEMENT_IN_CATEGORY", "ELEMENT_HAS_TYPE", "ELEMENT_HAS_MATERIAL"},
        "view_context_edge": {"VIEW_REFERENCES_ELEMENT"},
        "document_layout_edge": {"SHEET_HAS_TITLEBLOCK", "SHEET_CONTAINS_VIEW", "SHEET_CONTAINS_VIEWPORT", "VIEWPORT_PLACES_VIEW"},
        "schedule_table_edge": {"SCHEDULE_HAS_CELL"},
        "annotation_context_edge": {"ANNOTATION_OWNED_BY_VIEW"},
        "drawing_representation_edge": {
            "ELEMENT_HAS_DRAWING_REPRESENTATION_INDEX",
            "ELEMENT_REPRESENTED_BY_DRAWING",
            "ELEMENT_HAS_DRAWING_QA_STATUS",
            "ELEMENT_HAS_SEMANTIC_ONLY_DRAWING_STATUS",
            "DRAWING_EVIDENCE_ON_SHEET",
            "DRAWING_EVIDENCE_IN_VIEW",
            "TOPOLOGY_GROUP_HAS_SAMPLE_ELEMENT",
            "TOPOLOGY_GROUP_HAS_MEMBER_ELEMENT",
            "TOPOLOGY_GROUP_ON_SHEET",
            "TOPOLOGY_GROUP_IN_VIEW",
        },
    }
    for group, relations in groups.items():
        if relation in relations:
            importance = "high"
            confidence = 0.9
            reason = "Relation is central for object/material/type/document traversal."
            if relation == "VIEW_REFERENCES_ELEMENT":
                importance = "medium"
                confidence = 0.62
                reason = "View-element references can be numerous and may be capped; use with coverage metadata."
            if relation in {"SHEET_HAS_TITLEBLOCK", "SHEET_CONTAINS_VIEWPORT"}:
                importance = "low"
                reason = "Relation is useful for layout context but not usually a primary user query target."
            if relation == "ANNOTATION_OWNED_BY_VIEW":
                confidence = 0.55
                reason = "Annotation ownership is useful but may be incomplete in current raw extraction."
            return {
                "relationship_category": group,
                "importance": importance,
                "confidence": confidence,
                "reason": reason,
            }
    return {
        "relationship_category": "unclassified_relationship",
        "importance": "medium",
        "confidence": 0.35,
        "reason": "Relation requires AI review.",
    }


def schedule_cell_role(cell: dict[str, Any]) -> str:
    row = as_int(cell.get("row")) or 0
    column = as_int(cell.get("column")) or 0
    text = str(cell.get("text") or "")
    schedule_name = str(cell.get("schedule_name") or "")
    if row == 0 and column == 0 and text == schedule_name:
        return "schedule_title"
    if row <= 1:
        return "column_header"
    return "data_value"


def group_schedule_rows(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        schedule_id = str(cell.get("schedule_id") or cell.get("schedule_name") or "unknown")
        row_index = as_int(cell.get("row")) or 0
        grouped[(schedule_id, row_index)].append(cell)

    rows: list[dict[str, Any]] = []
    for (schedule_id, row_index), row_cells in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        ordered = sorted(row_cells, key=lambda cell: as_int(cell.get("column")) or 0)
        schedule_name = str(ordered[0].get("schedule_name") or "")
        roles = Counter(schedule_cell_role(cell) for cell in ordered)
        if roles.get("schedule_title"):
            row_role = "schedule_title"
        elif row_index <= 1 or roles.get("column_header"):
            row_role = "column_header"
        else:
            row_role = "data_row"
        values = [
            {
                "column": as_int(cell.get("column")),
                "text": cell.get("text"),
                "cell_node_id": cell.get("node_id"),
            }
            for cell in ordered
        ]
        row = {
            "record_type": "schedule_row",
            "row_id": f"schedule:{schedule_id}:row:{row_index}",
            "schedule_id": schedule_id,
            "schedule_name": schedule_name,
            "row": row_index,
            "row_role": row_role,
            "text": " | ".join(str(cell.get("text") or "") for cell in ordered),
            "values": values,
            "cell_count": len(values),
            "source_refs": [source_ref("schedule_cells.jsonl", cell) for cell in ordered],
        }
        rows.append(row)
    return rows


@dataclass
class RawBundle:
    raw_dir: Path
    manifest: dict[str, Any]
    files: dict[str, list[dict[str, Any]]]


def load_raw_bundle(raw_dir: Path) -> RawBundle:
    files = {file_name: read_jsonl(raw_dir / file_name) for file_name in RAW_FILES}
    return RawBundle(raw_dir=raw_dir, manifest=read_json(raw_dir / "manifest.json"), files=files)


def build_profile(bundle: RawBundle) -> dict[str, Any]:
    elements = bundle.files["elements.jsonl"]
    materials = bundle.files["materials.jsonl"]
    quantities = bundle.files["quantities.jsonl"]
    sheets = bundle.files["sheets.jsonl"]
    schedules = bundle.files["schedules.jsonl"]
    cells = bundle.files["schedule_cells.jsonl"]
    edges = bundle.files["graph_edges.jsonl"]
    annotations = bundle.files["annotations.jsonl"]
    views = bundle.files["views.jsonl"]
    sheet_pdfs = bundle.files["sheet_pdfs.jsonl"]
    sheet_dxfs = bundle.files["sheet_dxfs.jsonl"]
    view_dxfs = bundle.files["view_dxfs.jsonl"]

    return {
        "record_type": "project_profile",
        "generated_at": now_iso(),
        "raw_dir": str(bundle.raw_dir),
        "document": bundle.manifest.get("document", {}),
        "raw_counts": {name: len(rows) for name, rows in bundle.files.items()},
        "element_categories": Counter(category_name(row) for row in elements),
        "element_worksets": Counter(
            str((row.get("workset") if isinstance(row.get("workset"), dict) else {}).get("name") or row.get("workset_id") or "<none>")
            for row in elements
        ),
        "material_categories": Counter(str(row.get("material_category") or "<empty>") for row in materials),
        "quantity_record_types": Counter(str(row.get("record_type") or "") for row in quantities),
        "sheet_prefixes": Counter(str(row.get("sheet_number") or "")[:4] for row in sheets),
        "schedule_names": [row.get("name") for row in schedules],
        "schedule_cell_counts": Counter(str(row.get("schedule_name") or "") for row in cells),
        "edge_relations": Counter(str(row.get("relation") or "") for row in edges),
        "annotation_kinds": Counter(str(row.get("annotation_kind") or "") for row in annotations),
        "view_types": Counter(str(row.get("view_type") or "") for row in views),
        "sheet_pdf_statuses": Counter(str(row.get("status") or "") for row in sheet_pdfs),
        "sheet_dxf_statuses": Counter(str(row.get("status") or "") for row in sheet_dxfs),
        "view_dxf_statuses": Counter(str(row.get("status") or "") for row in view_dxfs),
        "drawing_context_counts": {name: len(bundle.files.get(name, [])) for name in DRAWING_CONTEXT_FILES},
        "drawing_stage45_representation_statuses": Counter(
            str(row.get("status") or "")
            for row in bundle.files.get("drawing_stage45_element_representation_links.jsonl", [])
        ),
        "drawing_stage45_remaining_priorities": Counter(
            str(row.get("review_priority") or "")
            for row in bundle.files.get("drawing_stage45_remaining_revit_elements.jsonl", [])
        ),
    }


def json_ready(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(value.most_common())
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def make_decision(source_file: str, row: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_type": "classification_decision",
        "source_file": source_file,
        "source_id": source_ref(source_file, row)["source_id"],
        "source_node_id": row.get("node_id"),
        "raw_record_hash": stable_hash(row),
        "decision": classification.get("decision", KEEP),
        "ai_category": classification.get("ai_category")
        or classification.get("material_group")
        or classification.get("quantity_category")
        or classification.get("drawing_category")
        or classification.get("schedule_category")
        or classification.get("relationship_category"),
        "importance": classification.get("importance", "medium"),
        "confidence": classification.get("confidence", 0.5),
        "reason": classification.get("reason", "Bootstrap classification."),
        "method": "deriving_bootstrap_v0",
        "review_required": float(classification.get("confidence", 0.0)) < 0.65,
    }


def coverage_row(source_file: str, row: dict[str, Any], derived_refs: list[str], decision: dict[str, Any]) -> dict[str, Any]:
    ref = source_ref(source_file, row)
    return {
        "record_type": "source_coverage",
        "source_file": source_file,
        "source_id": ref["source_id"],
        "source_node_id": ref.get("source_node_id"),
        "raw_record_hash": ref["raw_record_hash"],
        "coverage_status": "mapped" if derived_refs else "classified_only",
        "derived_refs": derived_refs,
        "decision": decision.get("decision"),
        "importance": decision.get("importance"),
        "confidence": decision.get("confidence"),
    }


def node_id_from_element(element: dict[str, Any]) -> str:
    return str(element.get("node_id") or f"revit:element:{element.get('element_id')}")


def workset_from_element(element: dict[str, Any]) -> dict[str, Any]:
    workset = element.get("workset") if isinstance(element.get("workset"), dict) else {}
    workset_id = workset.get("id", element.get("workset_id"))
    result: dict[str, Any] = {
        "id": workset_id,
        "name": workset.get("name"),
        "kind": workset.get("kind"),
        "is_open": workset.get("is_open"),
        "is_editable": workset.get("is_editable"),
        "is_visible_by_default": workset.get("is_visible_by_default"),
    }
    return {key: value for key, value in result.items() if value is not None}


def parameter_value(
    parameters: Any,
    *,
    built_in: str | None = None,
    names: set[str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(parameters, list):
        return None
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        if built_in and parameter.get("built_in_parameter") == built_in:
            return parameter
        if names and str(parameter.get("name") or "") in names:
            return parameter
    return None


def split_family_and_type(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    for delimiter in (":", "："):
        if delimiter in text:
            family, type_text = text.split(delimiter, 1)
            return family.strip() or None, type_text.strip() or None
    return text, None


def family_from_element(element: dict[str, Any]) -> dict[str, Any]:
    type_value = element.get("type") if isinstance(element.get("type"), dict) else {}
    type_name_value = type_value.get("name") or element.get("name")

    type_family_param = parameter_value(
        element.get("type_parameters"),
        built_in="SYMBOL_FAMILY_NAME_PARAM",
        names={"패밀리 이름", "Family Name"},
    )
    instance_family_param = parameter_value(
        element.get("parameters"),
        built_in="ELEM_FAMILY_PARAM",
        names={"패밀리", "Family"},
    )
    family_and_type_param = parameter_value(
        element.get("parameters"),
        built_in="ELEM_FAMILY_AND_TYPE_PARAM",
        names={"패밀리 및 유형", "Family and Type"},
    )

    family_and_type = str(family_and_type_param.get("display") or "") if family_and_type_param else ""
    parsed_family, parsed_type = split_family_and_type(family_and_type)

    family_name = (
        (type_family_param or {}).get("display")
        or (type_family_param or {}).get("raw")
        or (instance_family_param or {}).get("display")
        or parsed_family
    )
    family_source = None
    if (type_family_param or {}).get("display") or (type_family_param or {}).get("raw"):
        family_source = "type_parameter.SYMBOL_FAMILY_NAME_PARAM"
    elif (instance_family_param or {}).get("display"):
        family_source = "instance_parameter.ELEM_FAMILY_PARAM"
    elif parsed_family:
        family_source = "instance_parameter.ELEM_FAMILY_AND_TYPE_PARAM"
    elif type_name_value:
        family_source = "fallback.type_name"

    family_name = str(family_name or type_name_value or "").strip() or None
    family_ref_id = (instance_family_param or {}).get("raw")
    klass = str(element.get("class") or "")
    system_family = bool(family_name) and "FamilyInstance" not in klass

    return {
        "family_name": family_name,
        "family_and_type": family_and_type or None,
        "family_ref_id": family_ref_id,
        "family_source": family_source,
        "type_id": type_value.get("element_id"),
        "type_name": type_name_value,
        "type_node_id": type_value.get("node_id"),
        "system_family": system_family,
    }


def parameter_measure_kind(parameter: dict[str, Any]) -> str | None:
    data_type_id = str(parameter.get("data_type_id") or "").lower()
    unit_type_id = str(parameter.get("unit_type_id") or "").lower()
    built_in = str(parameter.get("built_in_parameter") or "")
    name = str(parameter.get("name") or "").lower()
    if "area" in data_type_id or "square" in unit_type_id or built_in == "HOST_AREA_COMPUTED" or "면적" in name:
        return "area"
    if "volume" in data_type_id or "cubic" in unit_type_id or built_in == "HOST_VOLUME_COMPUTED" or "체적" in name:
        return "volume"
    if "length" in data_type_id or "millimeters" in unit_type_id or "길이" in name or "높이" in name:
        return "length"
    return None


def quantity_measurement_info(quantity: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    record_type = str(quantity.get("record_type") or "")
    element = quantity.get("element") if isinstance(quantity.get("element"), dict) else {}
    material = quantity.get("material") if isinstance(quantity.get("material"), dict) else {}
    parameter = quantity.get("parameter") if isinstance(quantity.get("parameter"), dict) else {}
    element_id = element.get("element_id")
    material_id = material.get("material_id")
    parameter_name = str(parameter.get("name") or "")
    built_in = str(parameter.get("built_in_parameter") or "")

    if record_type == "element_material_quantity":
        if isinstance(quantity.get("area_m2"), (int, float)):
            measure_kind = "area"
        elif isinstance(quantity.get("volume_m3"), (int, float)):
            measure_kind = "volume"
        else:
            measure_kind = "material_quantity"
        scope = "material"
        role = f"material_{measure_kind}"
        is_primary = False
        dedup_key = f"material:{element_id}:{material_id}:{measure_kind}"
        precedence = 60
    else:
        measure_kind = parameter_measure_kind(parameter) or classification.get("quantity_category")
        scope = "element"
        if built_in == "HOST_AREA_COMPUTED" or parameter_name == "면적":
            role = "primary_element_area"
            is_primary = True
            precedence = 100
            dedup_key = f"element:{element_id}:area"
        elif built_in == "HOST_VOLUME_COMPUTED" or parameter_name == "체적":
            role = "primary_element_volume"
            is_primary = True
            precedence = 100
            dedup_key = f"element:{element_id}:volume"
        elif measure_kind == "length" and parameter_name in {"길이", "Length"}:
            role = "primary_element_length"
            is_primary = True
            precedence = 90
            dedup_key = f"element:{element_id}:length"
        else:
            role = "element_parameter"
            is_primary = False
            precedence = 40
            dedup_key = f"element:{element_id}:parameter:{parameter.get('parameter_id') or parameter_name}"

    return {
        "measure_kind": measure_kind,
        "aggregation_scope": scope,
        "quantity_role": role,
        "is_primary_measure": is_primary,
        "dedup_group_key": dedup_key,
        "dedup_precedence": precedence,
        "dedup_policy": "Prefer is_primary_measure=true for element-level totals; use aggregation_scope=material for material takeoff totals.",
    }


def drawing_export_evidence(source_file: str, row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source_stem = source_file.removesuffix(".jsonl")
    status = str(row.get("status") or "unknown")
    is_exported = status == "exported"

    if source_file == "sheet_pdfs.jsonl":
        evidence_type = "sheet_pdf"
        file_path = row.get("pdf_path")
        file_name = row.get("pdf_file_name")
        text = f"Sheet PDF {row.get('sheet_number')} {row.get('sheet_name')} status={status} file={file_name}"
    elif source_file == "sheet_dxfs.jsonl":
        evidence_type = "sheet_dxf"
        file_path = row.get("dxf_path")
        file_name = row.get("dxf_file_name")
        text = f"Sheet DXF {row.get('sheet_number')} {row.get('sheet_name')} status={status} file={file_name}"
    else:
        evidence_type = "view_dxf"
        file_path = row.get("dxf_path")
        file_name = row.get("dxf_file_name")
        text = f"View DXF {row.get('view_name')} ({row.get('view_type')}) status={status} file={file_name}"

    classification = {
        "ai_category": evidence_type,
        "importance": "high" if is_exported else "low",
        "confidence": 0.9 if is_exported else 0.35,
        "reason": f"{source_file} links exported drawing files to Revit sheet/view evidence.",
    }
    evidence = {
        "record_type": "drawing_evidence",
        "derived_id": f"drawing_evidence:{source_stem}:{row.get('node_id') or compact_hash(row)}",
        "evidence_type": evidence_type,
        "source": source_stem,
        "text": text,
        "status": status,
        "file_path": file_path,
        "file_name": file_name,
        "file_size_bytes": row.get("file_size_bytes"),
        "sheet_id": row.get("sheet_id"),
        "sheet_node_id": row.get("sheet_node_id"),
        "sheet_number": row.get("sheet_number"),
        "sheet_name": row.get("sheet_name"),
        "view_id": row.get("view_id"),
        "view_node_id": row.get("view_node_id"),
        "view_name": row.get("view_name"),
        "view_type": row.get("view_type"),
        "sheet_refs": row.get("sheet_refs"),
        "export_options": row.get("export_options"),
        "error": row.get("error"),
        "confidence": classification["confidence"],
        "source_refs": [source_ref(source_file, row)],
    }
    return evidence, classification


def point_xy(point: Any) -> list[float] | None:
    if not isinstance(point, dict):
        return None
    try:
        return [float(point["x"]), float(point["y"])]
    except (KeyError, TypeError, ValueError):
        return None


def bbox_xyxy(bbox: Any) -> list[float] | None:
    if isinstance(bbox, list) and len(bbox) >= 4:
        try:
            return [float(value) for value in bbox[:4]]
        except (TypeError, ValueError):
            return None
    if not isinstance(bbox, dict):
        return None
    minimum = bbox.get("min") if isinstance(bbox.get("min"), dict) else {}
    maximum = bbox.get("max") if isinstance(bbox.get("max"), dict) else {}
    try:
        return [float(minimum["x"]), float(minimum["y"]), float(maximum["x"]), float(maximum["y"])]
    except (KeyError, TypeError, ValueError):
        return None


def drawing_entity_text(row: dict[str, Any]) -> str:
    geometry = as_dict(row.get("geometry"))
    entity_type = str(row.get("entity_type") or "").upper()
    if entity_type == "DIMENSION" and geometry.get("measurement") is not None:
        return str(geometry.get("measurement"))
    for field in ("decoded_text", "plain_text", "text", "measurement_text"):
        value = geometry.get(field)
        if value is not None:
            return str(value)
    for field in ("text", "plain_text", "mtext", "dimension_text"):
        value = row.get(field)
        if value is not None:
            return str(value)
    return ""


def drawing_entity_points(row: dict[str, Any]) -> dict[str, Any]:
    geometry = as_dict(row.get("geometry"))
    start = point_xy(geometry.get("first_point") or geometry.get("start_point") or geometry.get("start"))
    end = point_xy(geometry.get("second_point") or geometry.get("end_point") or geometry.get("end"))
    insert = point_xy(geometry.get("insert_point") or geometry.get("insertion_point") or geometry.get("text_middle_point") or geometry.get("definition_point"))
    return {
        "bbox": bbox_xyxy(row.get("bbox") or row.get("bounding_box")),
        "start_point": start,
        "end_point": end,
        "insert": insert,
    }


def drawing_entity_evidence(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    context = as_dict(row.get("source_context"))
    geometry = as_dict(row.get("geometry"))
    entity_type = str(row.get("entity_type") or row.get("record_type") or "UNKNOWN").upper()
    source_entity_key = row.get("entity_key") or row.get("record_key")
    dxf_file_name = row.get("dxf_file_name")
    if not dxf_file_name and row.get("dxf_path"):
        dxf_file_name = Path(str(row.get("dxf_path"))).name
    text_value = drawing_entity_text(row)
    points = drawing_entity_points(row)
    classification = {
        "ai_category": "dxf_entity",
        "importance": "medium" if entity_type in {"TEXT", "MTEXT", "DIMENSION"} else "low",
        "confidence": 0.84 if entity_type in {"TEXT", "MTEXT", "DIMENSION"} else 0.7,
        "reason": "DXF parser entity row preserves sheet/view drawing primitive geometry for drawing QA.",
    }
    evidence = {
        "record_type": "drawing_evidence",
        "derived_id": f"drawing_evidence:drawing_entities:{source_entity_key or compact_hash(row)}",
        "evidence_type": "dxf_entity",
        "source": "drawing_entities",
        "source_stage": "dxf_parser",
        "source_entity_key": source_entity_key,
        "text": (
            f"DXF {entity_type} sheet={context.get('sheet_number')} view={context.get('view_name')} "
            f"file={dxf_file_name} layer={row.get('layer')} text={text_value}"
        ).strip(),
        "raw_text": text_value,
        "entity_type": entity_type,
        "layer": row.get("layer"),
        "handle": row.get("handle"),
        "space": row.get("space"),
        "block_name": row.get("block_name"),
        "block_path": row.get("block_path"),
        "source_kind": row.get("source_kind") or context.get("source_kind"),
        "source_file": dxf_file_name,
        "dxf_file_name": dxf_file_name,
        "dxf_path": row.get("dxf_path"),
        "dxf_file_hash": row.get("dxf_file_hash"),
        "sheet_id": context.get("sheet_id"),
        "sheet_number": context.get("sheet_number"),
        "sheet_name": context.get("sheet_name"),
        "view_id": context.get("view_id"),
        "view_name": context.get("view_name"),
        "view_type": context.get("view_type"),
        "bbox": points["bbox"],
        "start_point": points["start_point"],
        "end_point": points["end_point"],
        "insert": points["insert"],
        "value_mm": geometry.get("measurement") if entity_type == "DIMENSION" else None,
        "unit": "mm" if entity_type == "DIMENSION" and geometry.get("measurement") is not None else None,
        "geometry_kind": geometry.get("kind"),
        "drawing_ref": {
            "entity_type": entity_type,
            "layer": row.get("layer"),
            "dxf_file_name": dxf_file_name,
            "bbox": points["bbox"],
        },
        "importance": classification["importance"],
        "confidence": classification["confidence"],
        "source_refs": [source_ref("drawing_entities.jsonl", row)],
    }
    return evidence, classification


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def stage_source_id(row: dict[str, Any]) -> str:
    return str(
        row.get("index_key")
        or row.get("link_key")
        or row.get("remaining_unmatched_key")
        or row.get("semantic_only_key")
        or row.get("group_key")
        or row.get("member_key")
        or compact_hash(row)
    )


def stage_element(row: dict[str, Any]) -> dict[str, Any]:
    element = as_dict(row.get("element"))
    if element:
        return element
    member = as_dict(row.get("member"))
    element = as_dict(member.get("element"))
    if element:
        return element
    return as_dict(row.get("sample_element"))


def stage_sheet(row: dict[str, Any]) -> dict[str, Any]:
    sheet = as_dict(row.get("sheet"))
    if sheet:
        return sheet
    hierarchy = as_dict(row.get("hierarchy"))
    sheet = as_dict(hierarchy.get("sheet"))
    if sheet:
        return sheet
    sheets = row.get("sheets") if isinstance(row.get("sheets"), list) else []
    return as_dict(sheets[0]) if sheets else {}


def stage_view(row: dict[str, Any]) -> dict[str, Any]:
    view = as_dict(row.get("view"))
    if view:
        return view
    hierarchy = as_dict(row.get("hierarchy"))
    view = as_dict(hierarchy.get("view"))
    if view:
        return view
    views = row.get("views") if isinstance(row.get("views"), list) else []
    return as_dict(views[0]) if views else {}


def stage_type_name(element: dict[str, Any]) -> str | None:
    element_type = as_dict(element.get("type"))
    value = element_type.get("name") or element.get("type_name")
    return str(value) if value is not None else None


def stage_category_name(element: dict[str, Any], fallback: dict[str, Any] | None = None) -> str | None:
    value = element.get("category_name")
    if value is not None:
        return str(value)
    category = as_dict(element.get("category"))
    value = category.get("name")
    if value is not None:
        return str(value)
    fallback_category = as_dict((fallback or {}).get("category"))
    value = fallback_category.get("name")
    return str(value) if value is not None else None


def stage_category_id(element: dict[str, Any], fallback: dict[str, Any] | None = None) -> Any:
    value = element.get("category_id")
    if value is not None:
        return value
    category = as_dict(element.get("category"))
    if category.get("id") is not None:
        return category.get("id")
    fallback_category = as_dict((fallback or {}).get("category"))
    return fallback_category.get("id")


def stage_workset(element: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    workset = as_dict(element.get("workset"))
    if workset:
        return workset
    fallback_dict = as_dict(fallback)
    if fallback_dict.get("id") is not None or fallback_dict.get("name") is not None:
        return fallback_dict
    return as_dict(fallback_dict.get("workset"))


def element_ref_id(element: dict[str, Any]) -> str | None:
    if element.get("node_id"):
        return str(element.get("node_id"))
    if element.get("element_id") is not None:
        return f"revit:element:{element.get('element_id')}"
    return None


def sheet_ref_id(sheet: dict[str, Any]) -> str | None:
    if sheet.get("node_id"):
        return str(sheet.get("node_id"))
    if sheet.get("element_id") is not None:
        return f"revit:sheet:{sheet.get('element_id')}"
    if sheet.get("sheet_number"):
        return f"revit:sheet_number:{sheet.get('sheet_number')}"
    return None


def view_ref_id(view: dict[str, Any]) -> str | None:
    if view.get("node_id"):
        return str(view.get("node_id"))
    if view.get("element_id") is not None:
        return f"revit:view:{view.get('element_id')}"
    if view.get("name"):
        return f"revit:view_name:{view.get('name')}"
    return None


def drawing_context_evidence_id(source_file: str, row: dict[str, Any]) -> str:
    return f"drawing_evidence:{source_file.removesuffix('.jsonl')}:{stage_source_id(row)}"


def drawing_context_classification(source_file: str, row: dict[str, Any]) -> dict[str, Any]:
    if source_file == "drawing_stage45_element_representation_links.jsonl":
        status = str(row.get("status") or "")
        confidence = float(row.get("confidence") or 0.0)
        return {
            "ai_category": "drawing_element_representation_link",
            "importance": "high" if confidence >= 0.75 else "medium",
            "confidence": max(0.45, min(0.95, confidence)),
            "reason": f"Stage 4.5 link records an element-to-DXF/PDF drawing representation with status={status}.",
        }
    if source_file == "drawing_stage45_element_representation_index.jsonl":
        return {
            "ai_category": "drawing_element_representation_index",
            "importance": "high",
            "confidence": 0.9,
            "reason": "Stage 4.5 element index summarizes all drawing representations for one Revit element.",
        }
    if source_file == "drawing_stage45_remaining_revit_elements.jsonl":
        priority = str(row.get("review_priority") or "")
        return {
            "ai_category": "drawing_unmatched_revit_element",
            "importance": "high" if priority.startswith("high") else "medium",
            "confidence": 0.82,
            "reason": "Stage 4.5 QA record preserves an element still lacking drawing evidence.",
        }
    if source_file == "drawing_stage45_semantic_only_revit_elements.jsonl":
        return {
            "ai_category": "drawing_semantic_only_revit_element",
            "importance": "medium",
            "confidence": 0.86,
            "reason": "Stage 4.5 intentionally keeps this Revit item as semantic/modeling data instead of a drawing-match target.",
        }
    if source_file == "drawing_stage45_element_topology_groups.jsonl":
        quality = as_dict(row.get("quality"))
        priority = str(quality.get("review_priority") or as_dict(row.get("hierarchy")).get("review_priority") or "")
        return {
            "ai_category": "drawing_element_topology_group",
            "importance": "high" if priority.startswith("high") else "medium",
            "confidence": 0.82,
            "reason": "Stage 4.5 topology group preserves workset/category/type/view hierarchy for drawing QA and later matching.",
        }
    return {
        "ai_category": "drawing_element_topology_group_member",
        "importance": "medium",
        "confidence": 0.78,
        "reason": "Stage 4.5 topology group member links a Revit element into a drawing/topology QA group.",
    }


def drawing_context_evidence(source_file: str, row: dict[str, Any]) -> dict[str, Any] | None:
    if source_file == "drawing_stage45_element_topology_group_members.jsonl":
        return None

    source_stem = source_file.removesuffix(".jsonl")
    element = stage_element(row)
    hierarchy = as_dict(row.get("hierarchy"))
    sheet = stage_sheet(row)
    view = stage_view(row)
    workset = stage_workset(element, hierarchy)
    category = stage_category_name(element, hierarchy)
    category_id = stage_category_id(element, hierarchy)
    element_id = element.get("element_id")
    element_name = element.get("name")
    type_name_value = stage_type_name(element) or as_dict(hierarchy.get("type")).get("name")
    sheet_number = sheet.get("sheet_number")
    view_name = view.get("name")
    source_id = stage_source_id(row)
    evidence_id = drawing_context_evidence_id(source_file, row)
    classification = drawing_context_classification(source_file, row)

    if source_file == "drawing_stage45_element_representation_index.jsonl":
        evidence_type = "drawing_element_representation_index"
        text = (
            f"Stage 4.5 representation index element_id={element_id} category={category} "
            f"name={element_name} type={type_name_value} workset={workset.get('name')} "
            f"representation_count={row.get('representation_count')} best_status={row.get('best_status')} "
            f"best_confidence={row.get('best_confidence')} sheets={sheet_number} views={view_name}"
        )
        extra = {
            "representation_count": row.get("representation_count"),
            "representation_counts": row.get("representation_counts"),
            "best_status": row.get("best_status"),
            "best_confidence": row.get("best_confidence"),
            "drawing_refs_truncated": row.get("drawing_refs_truncated"),
        }
    elif source_file == "drawing_stage45_element_representation_links.jsonl":
        drawing_ref = as_dict(row.get("drawing_ref"))
        evidence_type = "drawing_element_representation_link"
        text = (
            f"Stage 4.5 drawing link element_id={element_id} category={category} "
            f"name={element_name} type={type_name_value} workset={workset.get('name')} "
            f"sheet={sheet_number} view={view_name} representation_type={row.get('representation_type')} "
            f"status={row.get('status')} confidence={row.get('confidence')} "
            f"layer={drawing_ref.get('layer')} fragment={drawing_ref.get('fragment_key')}"
        )
        extra = {
            "source_stage": row.get("source_stage"),
            "representation_type": row.get("representation_type"),
            "status": row.get("status"),
            "confidence": row.get("confidence"),
            "placement": row.get("placement"),
            "drawing_ref": {
                "fragment_key": drawing_ref.get("fragment_key"),
                "fragment_type": drawing_ref.get("fragment_type"),
                "layer": drawing_ref.get("layer"),
                "dxf_file_name": drawing_ref.get("dxf_file_name"),
                "target_block_name": drawing_ref.get("target_block_name"),
                "member_count": drawing_ref.get("member_count"),
                "entity_type_counts": drawing_ref.get("entity_type_counts"),
                "view_model_bbox": drawing_ref.get("view_model_bbox"),
                "sheet_model_bbox": drawing_ref.get("sheet_model_bbox"),
                "sheet_paper_bbox": drawing_ref.get("sheet_paper_bbox"),
            },
        }
    elif source_file == "drawing_stage45_remaining_revit_elements.jsonl":
        evidence_type = "drawing_unmatched_revit_element"
        text = (
            f"Stage 4.5 remaining unmatched Revit element_id={element_id} category={category} "
            f"name={element_name} type={type_name_value} workset={workset.get('name')} "
            f"sheet={sheet_number} view={view_name} review_priority={row.get('review_priority')} "
            f"status={row.get('status')}"
        )
        extra = {
            "status": row.get("status"),
            "review_priority": row.get("review_priority"),
            "reason": row.get("reason"),
            "source_unmatched_key": row.get("source_unmatched_key"),
        }
    elif source_file == "drawing_stage45_semantic_only_revit_elements.jsonl":
        evidence_type = "drawing_semantic_only_revit_element"
        text = (
            f"Stage 4.5 semantic-only Revit element_id={element_id} category={category} "
            f"name={element_name} type={type_name_value} workset={workset.get('name')} "
            f"status={row.get('status')} reason={row.get('reason')}"
        )
        extra = {
            "status": row.get("status"),
            "reason": row.get("reason"),
            "source_remaining_unmatched_key": row.get("source_remaining_unmatched_key"),
            "policy": row.get("policy"),
        }
    else:
        counts = as_dict(row.get("counts"))
        quality = as_dict(row.get("quality"))
        evidence_type = "drawing_element_topology_group"
        text = (
            f"Stage 4.5 topology group kind={row.get('group_kind')} workset={workset.get('name')} "
            f"category={category} type={type_name_value} sheet={sheet_number} view={view_name} "
            f"element_count={counts.get('element_count')} member_count={counts.get('member_count')} "
            f"best_status={quality.get('best_status')} review_priority={quality.get('review_priority')}"
        )
        extra = {
            "group_kind": row.get("group_kind"),
            "hierarchy": hierarchy,
            "counts": counts,
            "quality": quality,
            "geometry": row.get("geometry"),
            "samples": row.get("samples"),
        }

    return {
        "record_type": "drawing_evidence",
        "derived_id": evidence_id,
        "evidence_type": evidence_type,
        "source": source_stem,
        "source_stage45_id": source_id,
        "text": text,
        "element_id": element_id,
        "element_unique_id": element.get("unique_id"),
        "element_node_id": element.get("node_id"),
        "element_name": element_name,
        "category_id": category_id,
        "category_name": category,
        "type_id": as_dict(element.get("type")).get("element_id") or as_dict(hierarchy.get("type")).get("element_id"),
        "type_name": type_name_value,
        "workset_id": workset.get("id"),
        "workset_name": workset.get("name"),
        "workset": workset,
        "sheet_id": sheet.get("element_id"),
        "sheet_number": sheet_number,
        "sheet_name": sheet.get("name") or sheet.get("sheet_name"),
        "view_id": view.get("element_id"),
        "view_name": view_name,
        "view_type": view.get("view_type"),
        "importance": classification["importance"],
        "confidence": classification["confidence"],
        **extra,
        "source_refs": [source_ref(source_file, row)],
    }


def drawing_context_relationship(
    source_file: str,
    row: dict[str, Any],
    *,
    relation: str,
    from_id: str,
    to_id: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    edge = {
        "relation": relation,
        "from": from_id,
        "to": to_id,
        "source": source_file.removesuffix(".jsonl"),
    }
    classification = classify_relation(edge)
    derived_id = f"relationship:{source_file.removesuffix('.jsonl')}:{compact_hash({'relation': relation, 'from': from_id, 'to': to_id, 'source': stage_source_id(row)})}"
    return {
        "record_type": "relationship",
        "derived_id": derived_id,
        **edge,
        "properties": properties or {},
        "relationship_category": classification["relationship_category"],
        "importance": classification["importance"],
        "confidence": classification["confidence"],
        "source_refs": [source_ref(source_file, row)],
    }


def drawing_context_relationships(source_file: str, row: dict[str, Any], evidence_id: str | None) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    element = stage_element(row)
    sheet = stage_sheet(row)
    view = stage_view(row)
    element_ref = element_ref_id(element)
    sheet_ref = sheet_ref_id(sheet)
    view_ref = view_ref_id(view)

    if evidence_id and element_ref:
        if source_file == "drawing_stage45_element_representation_index.jsonl":
            relation = "ELEMENT_HAS_DRAWING_REPRESENTATION_INDEX"
        elif source_file == "drawing_stage45_element_representation_links.jsonl":
            relation = "ELEMENT_REPRESENTED_BY_DRAWING"
        elif source_file == "drawing_stage45_remaining_revit_elements.jsonl":
            relation = "ELEMENT_HAS_DRAWING_QA_STATUS"
        elif source_file == "drawing_stage45_semantic_only_revit_elements.jsonl":
            relation = "ELEMENT_HAS_SEMANTIC_ONLY_DRAWING_STATUS"
        else:
            relation = ""
        if relation:
            relationships.append(
                drawing_context_relationship(
                    source_file,
                    row,
                    relation=relation,
                    from_id=element_ref,
                    to_id=evidence_id,
                    properties={
                        "element_id": element.get("element_id"),
                        "status": row.get("status") or row.get("best_status"),
                        "confidence": row.get("confidence") or row.get("best_confidence"),
                        "representation_type": row.get("representation_type"),
                    },
                )
            )

    if evidence_id and sheet_ref:
        relationships.append(
            drawing_context_relationship(
                source_file,
                row,
                relation="DRAWING_EVIDENCE_ON_SHEET",
                from_id=evidence_id,
                to_id=sheet_ref,
                properties={"sheet_number": sheet.get("sheet_number"), "sheet_id": sheet.get("element_id")},
            )
        )
    if evidence_id and view_ref:
        relationships.append(
            drawing_context_relationship(
                source_file,
                row,
                relation="DRAWING_EVIDENCE_IN_VIEW",
                from_id=evidence_id,
                to_id=view_ref,
                properties={"view_id": view.get("element_id"), "view_name": view.get("name")},
            )
        )

    if source_file == "drawing_stage45_element_topology_groups.jsonl" and evidence_id:
        samples = as_dict(row.get("samples"))
        for sample in samples.get("elements") or []:
            sample_ref = element_ref_id(as_dict(sample))
            if sample_ref:
                relationships.append(
                    drawing_context_relationship(
                        source_file,
                        row,
                        relation="TOPOLOGY_GROUP_HAS_SAMPLE_ELEMENT",
                        from_id=evidence_id,
                        to_id=sample_ref,
                        properties={"group_key": row.get("group_key"), "sample_element_id": as_dict(sample).get("element_id")},
                    )
                )
        if sheet_ref:
            relationships.append(
                drawing_context_relationship(
                    source_file,
                    row,
                    relation="TOPOLOGY_GROUP_ON_SHEET",
                    from_id=evidence_id,
                    to_id=sheet_ref,
                    properties={"group_key": row.get("group_key"), "sheet_number": sheet.get("sheet_number")},
                )
            )
        if view_ref:
            relationships.append(
                drawing_context_relationship(
                    source_file,
                    row,
                    relation="TOPOLOGY_GROUP_IN_VIEW",
                    from_id=evidence_id,
                    to_id=view_ref,
                    properties={"group_key": row.get("group_key"), "view_id": view.get("element_id")},
                )
            )

    if source_file == "drawing_stage45_element_topology_group_members.jsonl":
        group_key = row.get("group_key")
        member_element = stage_element(row)
        member_ref = element_ref_id(member_element)
        if group_key and member_ref:
            group_evidence_id = f"drawing_evidence:drawing_stage45_element_topology_groups:{group_key}"
            relationships.append(
                drawing_context_relationship(
                    source_file,
                    row,
                    relation="TOPOLOGY_GROUP_HAS_MEMBER_ELEMENT",
                    from_id=group_evidence_id,
                    to_id=member_ref,
                    properties={"group_key": group_key, "element_id": member_element.get("element_id")},
                )
            )

    return relationships


def build_derived(bundle: RawBundle) -> dict[str, list[dict[str, Any]]]:
    outputs: dict[str, list[dict[str, Any]]] = {file_name: [] for file_name in DERIVED_FILES}
    coverage_refs: dict[str, list[str]] = defaultdict(list)
    decisions: dict[str, dict[str, Any]] = {}

    elements_by_id: dict[str, dict[str, Any]] = {}
    for element in bundle.files["elements.jsonl"]:
        element_id = str(element.get("element_id") or "")
        if element_id:
            elements_by_id[element_id] = element
        classification = classify_element(element)
        decision = make_decision("elements.jsonl", element, classification)
        decisions[source_key("elements.jsonl", element)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        if classification["decision"] != EXCLUDE:
            derived_id = f"bim_object:{element.get('element_id') or compact_hash(element)}"
            workset = workset_from_element(element)
            family = family_from_element(element)
            record = {
                "record_type": "bim_object",
                "derived_id": derived_id,
                "source_element_id": element.get("element_id"),
                "source_node_id": element.get("node_id"),
                "name": element.get("name"),
                "category": category_name(element),
                "class": element.get("class"),
                "family": family,
                "family_name": family.get("family_name"),
                "family_and_type": family.get("family_and_type"),
                "system_family": family.get("system_family"),
                "type": element.get("type"),
                "type_id": family.get("type_id"),
                "type_name": family.get("type_name"),
                "level_id": element.get("level_id"),
                "workset_id": element.get("workset_id"),
                "workset_name": workset.get("name"),
                "workset": workset,
                "ifc_guid": element.get("ifc_guid"),
                "ai_category": classification["ai_category"],
                "importance": classification["importance"],
                "confidence": classification["confidence"],
                "source_refs": [source_ref("elements.jsonl", element)],
            }
            outputs["bim_objects.jsonl"].append(record)
            coverage_refs[source_key("elements.jsonl", element)].append(derived_id)

    materials_by_id: dict[str, dict[str, Any]] = {}
    for material in bundle.files["materials.jsonl"]:
        material_id = str(material.get("material_id") or "")
        if material_id:
            materials_by_id[material_id] = material
        classification = classify_material(material)
        decision = make_decision("materials.jsonl", material, classification)
        decisions[source_key("materials.jsonl", material)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        derived_id = f"material_fact:{material.get('material_id') or compact_hash(material)}"
        record = {
            "record_type": "material_fact",
            "derived_id": derived_id,
            "source_material_id": material.get("material_id"),
            "source_node_id": material.get("node_id"),
            "name": material.get("name"),
            "normalized_name": strip_hash_prefix(str(material.get("name") or "")),
            "material_class": material.get("material_class"),
            "material_category": material.get("material_category"),
            "material_group": classification["material_group"],
            "importance": classification["importance"],
            "confidence": classification["confidence"],
            "source_refs": [source_ref("materials.jsonl", material)],
        }
        outputs["material_facts.jsonl"].append(record)
        coverage_refs[source_key("materials.jsonl", material)].append(derived_id)

    for quantity in bundle.files["quantities.jsonl"]:
        classification = classify_quantity(quantity)
        decision = make_decision("quantities.jsonl", quantity, classification)
        decisions[source_key("quantities.jsonl", quantity)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        element = quantity.get("element") if isinstance(quantity.get("element"), dict) else {}
        source_element = elements_by_id.get(str(element.get("element_id") or ""))
        workset = workset_from_element(source_element) if source_element else {}
        family = family_from_element(source_element) if source_element else {}
        parameter = quantity.get("parameter") if isinstance(quantity.get("parameter"), dict) else {}
        material = quantity.get("material") if isinstance(quantity.get("material"), dict) else {}
        measurement = quantity_measurement_info(quantity, classification)
        derived_id = f"quantity_fact:{compact_hash(quantity)}"
        record = {
            "record_type": "quantity_fact",
            "derived_id": derived_id,
            "source_record_type": quantity.get("record_type"),
            "element": element,
            "element_category": category_name(source_element) if source_element else None,
            "element_class": source_element.get("class") if source_element else None,
            "family": family,
            "family_name": family.get("family_name"),
            "family_and_type": family.get("family_and_type"),
            "system_family": family.get("system_family"),
            "type_id": family.get("type_id"),
            "type_name": family.get("type_name"),
            "material": material,
            "parameter": parameter,
            "area_m2": quantity.get("area_m2"),
            "volume_m3": quantity.get("volume_m3"),
            "quantity_category": classification["quantity_category"],
            "measure_kind": measurement["measure_kind"],
            "aggregation_scope": measurement["aggregation_scope"],
            "quantity_role": measurement["quantity_role"],
            "is_primary_measure": measurement["is_primary_measure"],
            "dedup_group_key": measurement["dedup_group_key"],
            "dedup_precedence": measurement["dedup_precedence"],
            "dedup_policy": measurement["dedup_policy"],
            "workset_id": workset.get("id"),
            "workset_name": workset.get("name"),
            "workset": workset,
            "importance": classification["importance"],
            "confidence": classification["confidence"],
            "source_refs": [source_ref("quantities.jsonl", quantity)],
        }
        outputs["quantity_facts.jsonl"].append(record)
        coverage_refs[source_key("quantities.jsonl", quantity)].append(derived_id)

    for sheet in bundle.files["sheets.jsonl"]:
        classification = classify_sheet(sheet)
        decision = make_decision("sheets.jsonl", sheet, classification)
        decisions[source_key("sheets.jsonl", sheet)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        derived_id = f"drawing_document:{sheet.get('element_id') or sheet.get('sheet_number') or compact_hash(sheet)}"
        record = {
            "record_type": "drawing_document",
            "derived_id": derived_id,
            "source_sheet_id": sheet.get("element_id"),
            "source_node_id": sheet.get("node_id"),
            "sheet_number": sheet.get("sheet_number"),
            "sheet_name": sheet.get("name"),
            "is_placeholder": sheet.get("is_placeholder"),
            "placed_view_ids": sheet.get("placed_view_ids", []),
            "viewport_ids": sheet.get("viewport_ids", []),
            "drawing_category": classification["drawing_category"],
            "importance": classification["importance"],
            "confidence": classification["confidence"],
            "source_refs": [source_ref("sheets.jsonl", sheet)],
        }
        outputs["drawing_documents.jsonl"].append(record)
        coverage_refs[source_key("sheets.jsonl", sheet)].append(derived_id)

    for source_file in ("sheet_pdfs.jsonl", "sheet_dxfs.jsonl", "view_dxfs.jsonl"):
        for export_row in bundle.files[source_file]:
            evidence, classification = drawing_export_evidence(source_file, export_row)
            decision = make_decision(source_file, export_row, classification)
            decisions[source_key(source_file, export_row)] = decision
            outputs["classification_decisions.jsonl"].append(decision)
            outputs["drawing_evidence.jsonl"].append(evidence)
            coverage_refs[source_key(source_file, export_row)].append(evidence["derived_id"])

    for entity_row in bundle.files["drawing_entities.jsonl"]:
        evidence, classification = drawing_entity_evidence(entity_row)
        decision = make_decision("drawing_entities.jsonl", entity_row, classification)
        decisions[source_key("drawing_entities.jsonl", entity_row)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        outputs["drawing_evidence.jsonl"].append(evidence)
        coverage_refs[source_key("drawing_entities.jsonl", entity_row)].append(evidence["derived_id"])

    schedule_classifications: dict[str, dict[str, Any]] = {}
    for schedule in bundle.files["schedules.jsonl"]:
        classification = classify_schedule(schedule)
        schedule_classifications[str(schedule.get("element_id") or schedule.get("name") or "")] = classification
        decision = make_decision("schedules.jsonl", schedule, classification)
        decisions[source_key("schedules.jsonl", schedule)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        derived_id = f"schedule_table:{schedule.get('element_id') or compact_hash(schedule)}"
        record = {
            "record_type": "schedule_table",
            "derived_id": derived_id,
            "source_schedule_id": schedule.get("element_id"),
            "source_node_id": schedule.get("node_id"),
            "name": schedule.get("name"),
            "view_type": schedule.get("view_type"),
            "is_key_schedule": schedule.get("is_key_schedule"),
            "category_id": schedule.get("category_id"),
            "fields": schedule.get("fields", []),
            "filters": schedule.get("filters", []),
            "sort_group_fields": schedule.get("sort_group_fields", []),
            "schedule_category": classification["schedule_category"],
            "importance": classification["importance"],
            "confidence": classification["confidence"],
            "source_refs": [source_ref("schedules.jsonl", schedule)],
        }
        outputs["schedule_tables.jsonl"].append(record)
        coverage_refs[source_key("schedules.jsonl", schedule)].append(derived_id)

    schedule_rows = group_schedule_rows(bundle.files["schedule_cells.jsonl"])
    for row in schedule_rows:
        outputs["schedule_rows.jsonl"].append(row)
        evidence_id = f"drawing_evidence:{row['row_id']}"
        evidence = {
            "record_type": "drawing_evidence",
            "derived_id": evidence_id,
            "evidence_type": row["row_role"],
            "source": "schedule_cells",
            "text": row["text"],
            "schedule_id": row["schedule_id"],
            "schedule_name": row["schedule_name"],
            "row": row["row"],
            "confidence": 0.88,
            "source_refs": row["source_refs"],
        }
        outputs["drawing_evidence.jsonl"].append(evidence)
        for ref in row["source_refs"]:
            coverage_refs[f"{ref['source_file']}:{ref['source_id']}"].append(row["row_id"])
            coverage_refs[f"{ref['source_file']}:{ref['source_id']}"].append(evidence_id)

    for cell in bundle.files["schedule_cells.jsonl"]:
        classification = {
            "ai_category": "schedule_cell",
            "importance": "medium",
            "confidence": 0.82,
            "reason": "Schedule cell is preserved through derived schedule_rows and drawing_evidence.",
        }
        decision = make_decision("schedule_cells.jsonl", cell, classification)
        decisions[source_key("schedule_cells.jsonl", cell)] = decision
        outputs["classification_decisions.jsonl"].append(decision)

    views_by_id: dict[str, dict[str, Any]] = {}
    for view in bundle.files["views.jsonl"]:
        if view.get("element_id") is not None:
            views_by_id[str(view.get("element_id"))] = view
        classification = {
            "ai_category": "view_context",
            "importance": "high" if view.get("is_placed_on_sheet") else "medium",
            "confidence": 0.82,
            "reason": "View context supports sheet-object traversal and drawing evidence.",
        }
        decision = make_decision("views.jsonl", view, classification)
        decisions[source_key("views.jsonl", view)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        evidence_id = f"drawing_evidence:view:{view.get('element_id') or compact_hash(view)}"
        evidence = {
            "record_type": "drawing_evidence",
            "derived_id": evidence_id,
            "evidence_type": "view_context",
            "source": "views",
            "text": f"{view.get('name')} ({view.get('view_type')}, scale {view.get('scale')})",
            "view_id": view.get("element_id"),
            "view_name": view.get("name"),
            "view_type": view.get("view_type"),
            "is_placed_on_sheet": view.get("is_placed_on_sheet"),
            "confidence": classification["confidence"],
            "source_refs": [source_ref("views.jsonl", view)],
        }
        outputs["drawing_evidence.jsonl"].append(evidence)
        coverage_refs[source_key("views.jsonl", view)].append(evidence_id)

    for annotation in bundle.files["annotations.jsonl"]:
        kind = str(annotation.get("annotation_kind") or "annotation")
        classification = {
            "ai_category": f"annotation_{kind}",
            "importance": "medium",
            "confidence": 0.62 if not annotation.get("owner_view_id") or annotation.get("owner_view_id") == -1 else 0.78,
            "reason": "Annotation text/dimension/tag can provide drawing evidence; owner view may be incomplete.",
        }
        decision = make_decision("annotations.jsonl", annotation, classification)
        decisions[source_key("annotations.jsonl", annotation)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        evidence_id = f"drawing_evidence:annotation:{annotation.get('element_id') or compact_hash(annotation)}"
        evidence = {
            "record_type": "drawing_evidence",
            "derived_id": evidence_id,
            "evidence_type": f"annotation_{kind}",
            "source": "annotations",
            "text": annotation.get("text"),
            "annotation_id": annotation.get("element_id"),
            "owner_view_id": annotation.get("owner_view_id"),
            "category": annotation.get("category"),
            "confidence": classification["confidence"],
            "source_refs": [source_ref("annotations.jsonl", annotation)],
        }
        outputs["drawing_evidence.jsonl"].append(evidence)
        coverage_refs[source_key("annotations.jsonl", annotation)].append(evidence_id)

    for source_file in DRAWING_CONTEXT_FILES:
        for context_row in bundle.files.get(source_file, []):
            classification = drawing_context_classification(source_file, context_row)
            decision = make_decision(source_file, context_row, classification)
            decisions[source_key(source_file, context_row)] = decision
            outputs["classification_decisions.jsonl"].append(decision)

            evidence = drawing_context_evidence(source_file, context_row)
            evidence_id = evidence["derived_id"] if evidence else None
            if evidence:
                outputs["drawing_evidence.jsonl"].append(evidence)
                coverage_refs[source_key(source_file, context_row)].append(evidence["derived_id"])

            for relationship in drawing_context_relationships(source_file, context_row, evidence_id):
                outputs["relationships.jsonl"].append(relationship)
                coverage_refs[source_key(source_file, context_row)].append(relationship["derived_id"])

    for edge in bundle.files["graph_edges.jsonl"]:
        classification = classify_relation(edge)
        decision = make_decision("graph_edges.jsonl", edge, classification)
        decisions[source_key("graph_edges.jsonl", edge)] = decision
        outputs["classification_decisions.jsonl"].append(decision)
        derived_id = f"relationship:{compact_hash(edge)}"
        record = {
            "record_type": "relationship",
            "derived_id": derived_id,
            "relation": edge.get("relation"),
            "from": edge.get("from"),
            "to": edge.get("to"),
            "source": edge.get("source"),
            "relationship_category": classification["relationship_category"],
            "importance": classification["importance"],
            "confidence": classification["confidence"],
            "source_refs": [source_ref("graph_edges.jsonl", edge)],
        }
        outputs["relationships.jsonl"].append(record)
        coverage_refs[source_key("graph_edges.jsonl", edge)].append(derived_id)

    for source_file, rows in bundle.files.items():
        for row in rows:
            key = source_key(source_file, row)
            decision = decisions.get(
                key,
                {
                    "decision": UNKNOWN,
                    "importance": "low",
                    "confidence": 0.0,
                },
            )
            outputs["source_coverage.jsonl"].append(coverage_row(source_file, row, coverage_refs.get(key, []), decision))

    return outputs


def build(raw_dir: Path, out_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    bundle = load_raw_bundle(raw_dir)
    profile = build_profile(bundle)
    profile_ready = json_ready(profile)
    if dry_run:
        return {
            "status": "dry_run",
            "raw_dir": str(raw_dir),
            "profile": profile_ready,
        }

    outputs = build_derived(bundle)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {file_name: write_jsonl(out_dir / file_name, rows) for file_name, rows in outputs.items()}
    write_json(out_dir / "project_profile.json", profile_ready)
    manifest = {
        "format": "bimgraph-ai-deriving-derived-jsonl",
        "schema_version": "0.1.0",
        "generated_at": now_iso(),
        "raw_dir": str(raw_dir),
        "tool": "tools/deriving/deriving.py",
        "method": "deriving_bootstrap_v0",
        "raw_counts": {name: len(rows) for name, rows in bundle.files.items()},
        "derived_counts": counts,
        "files": {file_name: file_name for file_name in DERIVED_FILES},
        "notes": [
            "Raw files remain the lossless source of truth.",
            "Bootstrap classifications are project-profile hints and should be replaceable by BIMGraph AI judgment.",
            "Every raw record is represented in source_coverage.jsonl.",
        ],
    }
    write_json(out_dir / "manifest.json", manifest)
    return {
        "status": "ok",
        "raw_dir": str(raw_dir),
        "out_dir": str(out_dir),
        "manifest": manifest,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build BIMGraph AI deriving derived JSONL from a Revit raw export.")
    parser.add_argument("--raw-dir", required=True, type=Path, help="Revit raw JSONL export directory.")
    parser.add_argument("--out-dir", type=Path, help="Output directory for derived JSONL. Defaults to RAW_DIR/derived.")
    parser.add_argument("--dry-run", action="store_true", help="Only print the detected project profile.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw_dir = args.raw_dir
    out_dir = args.out_dir or raw_dir / "derived"
    result = build(raw_dir=raw_dir, out_dir=out_dir, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
