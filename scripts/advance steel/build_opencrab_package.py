from __future__ import annotations

import argparse
import json
import math
import shutil
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


WORK_ROOT = Path(__file__).resolve().parents[1]
SOURCE_XML = WORK_ROOT / "source" / "original.xml"
JSONL_DIR = WORK_ROOT / "jsonl"
SUMMARY_DIR = WORK_ROOT / "summary"
USER_VIEW_DIR = WORK_ROOT / "user_views"
MANIFEST_PATH = WORK_ROOT / "ingest_manifest.json"

SINGLE_PART_CLASSES = {
    "CGREXBeam": "Beam",
    "CGREXColumn": "Column",
    "CGREXPlate": "Plate",
    "CGREXFoldedPlate": "FoldedPlate",
}


def first_attr(elem: ET.Element, tag: str, attr: str) -> str | None:
    found = elem.find(f".//{tag}")
    if found is None:
        return None
    return found.attrib.get(attr)


def as_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def as_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def as_bool_long(value: str | None) -> bool | None:
    parsed = as_int(value)
    if parsed is None:
        return None
    return parsed != 0


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def node_id(prefix: str, raw_id: str | int | None) -> str | None:
    if raw_id is None:
        return None
    return f"as:{prefix}:{raw_id}"


def view_id(prefix: str, *parts: str | int | None) -> str:
    safe_parts = []
    for part in parts:
        value = str(part or "blank").strip()
        value = value.replace(":", "-").replace("/", "-").replace("\\", "-").replace(" ", "_")
        safe_parts.append(value)
    return f"view:{prefix}:{':'.join(safe_parts)}"


def section_profile(section_standard: str | None, section_name: str | None) -> dict[str, Any]:
    standard = section_standard or ""
    name = section_name or ""
    if standard == "Korea C-Channels":
        return {
            "profile_group": "C-Channel",
            "profile_name_ko": "씨찬넬",
            "profile_aliases_ko": ["씨찬넬", "씨잔넬"],
            "profile_query_terms": ["C-Channel", "씨찬넬", "씨잔넬"],
        }
    if standard == "Korea Channels":
        return {
            "profile_group": "Channel",
            "profile_name_ko": "찬넬",
            "profile_aliases_ko": ["찬넬", "잔넬", "ㄷ형강"],
            "profile_query_terms": ["Channel", "ㄷ형강", "찬넬", "잔넬"],
        }
    if standard == "Korea H-Beam" or name.startswith("H "):
        return {
            "profile_group": "H-Beam",
            "profile_name_ko": "H형강",
            "profile_aliases_ko": ["H형강", "에이치형강"],
            "profile_query_terms": ["H-Beam", "H형강", "에이치형강"],
        }
    if "Angle" in standard or name.startswith("L "):
        return {
            "profile_group": "Angle",
            "profile_name_ko": "앵글",
            "profile_aliases_ko": ["앵글", "ㄱ형강"],
            "profile_query_terms": ["Angle", "앵글", "ㄱ형강"],
        }
    if "Square" in standard or "Rectangular" in standard or name.startswith(("Q", "RHS", "SQ")):
        return {
            "profile_group": "Tube",
            "profile_name_ko": "각관",
            "profile_aliases_ko": ["각관", "사각관"],
            "profile_query_terms": ["Tube", "각관", "사각관", "Square Section", "Rectangular Section"],
        }
    if name.startswith("PL "):
        return {
            "profile_group": "Plate",
            "profile_name_ko": "플레이트",
            "profile_aliases_ko": ["플레이트", "철판", "판재"],
            "profile_query_terms": ["Plate", "플레이트", "철판", "판재"],
        }
    return {
        "profile_group": None,
        "profile_name_ko": None,
        "profile_aliases_ko": [],
        "profile_query_terms": [],
    }


def object_ref(elem: ET.Element, tag: str) -> str | None:
    ref = elem.find(f".//{tag}")
    if ref is None:
        return None
    found = ref.find(".//m_nID")
    if found is None:
        return None
    return found.attrib.get("long")


def object_refs_by_prefix(elem: ET.Element, prefix: str) -> list[str]:
    size = as_int(first_attr(elem, f"{prefix}_SIZE", "long")) or 0
    refs: list[str] = []
    for index in range(size):
        ref = elem.find(f".//{prefix}_{index}")
        if ref is None:
            continue
        found = ref.find(".//m_nID")
        value = found.attrib.get("long") if found is not None else None
        if value:
            refs.append(value)
    return refs


def point(elem: ET.Element, tag: str) -> dict[str, float] | None:
    found = elem if tag == "." else elem.find(f".//{tag}")
    if found is None:
        return None
    x = as_float(first_attr(found, "m_dfX", "double"))
    y = as_float(first_attr(found, "m_dfY", "double"))
    z = as_float(first_attr(found, "m_dfZ", "double"))
    if x is None or y is None or z is None:
        return None
    return {"x": x, "y": y, "z": z}


def external_reference_ids(elem: ET.Element) -> list[str]:
    size = as_int(first_attr(elem, "m_ExternalReferenceIDs_SIZE", "long")) or 0
    refs: list[str] = []
    for index in range(size):
        value = first_attr(elem, f"m_ExternalReferenceIDs_{index}", "string")
        if value:
            refs.append(value)
    return refs


def user_attributes(elem: ET.Element) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for index in range(10):
        value = first_attr(elem, f"m_UserAttributes_{index}", "string")
        attrs[f"{index + 1:02d}"] = value or ""
    return attrs


def iter_top_level_objects(xml_path: Path):
    stack: list[ET.Element] = []
    for event, elem in ET.iterparse(xml_path, events=("start", "end")):
        if event == "start":
            stack.append(elem)
            continue

        if elem.tag == "Object" and len(stack) == 2:
            yield elem
            elem.clear()

        stack.pop()


def sorted_counter(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"value": key, "count": count} for key, count in sorted(counter.items())]


def add_sum(target: dict[str, Any], part: dict[str, Any]) -> None:
    target["single_part_count"] += 1
    if part["is_main_part"]:
        target["main_single_part_count"] += 1
    else:
        target["attached_single_part_count"] += 1
    target["total_weight_kg"] += part["weight_kg"] or 0
    target["total_length_m"] += part["length_m"] or 0


def blank_sum() -> dict[str, Any]:
    return {
        "single_part_count": 0,
        "main_single_part_count": 0,
        "attached_single_part_count": 0,
        "total_weight_kg": 0.0,
        "total_length_m": 0.0,
    }


def nested_stat_add(bucket: dict[str, dict[str, Any]], key: str | None, part: dict[str, Any]) -> None:
    name = key or "(blank)"
    stat = bucket.setdefault(name, {"count": 0, "weight_kg": 0.0, "length_m": 0.0})
    stat["count"] += 1
    stat["weight_kg"] += part["weight_kg"] or 0
    stat["length_m"] += part["length_m"] or 0


def round_floats(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        if math.isfinite(value):
            return round(value, digits)
        return None
    if isinstance(value, dict):
        return {key: round_floats(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [round_floats(item, digits) for item in value]
    return value


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(round_floats(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(round_floats(row), ensure_ascii=False, separators=(",", ":")) + "\n")


def collect_reference_data(xml_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], Counter[str]]:
    materials: dict[str, dict[str, Any]] = {}
    sections: dict[str, dict[str, Any]] = {}
    class_counts: Counter[str] = Counter()

    for elem in iter_top_level_objects(xml_path):
        cls = elem.attrib.get("class", "")
        class_counts[cls] += 1
        source_id = first_attr(elem, "m_nID", "long")

        if cls == "CGREXMaterial" and source_id:
            material = {
                "id": node_id("material", source_id),
                "source_object_id": as_int(source_id),
                "name": clean(first_attr(elem, "m_strName", "string")),
                "material_type": as_int(first_attr(elem, "m_nType", "long")),
                "density_kg_m3": as_float(first_attr(elem, "m_dfDensity", "double")),
                "young_modulus_pa": as_float(first_attr(elem, "m_dfYoung", "double")),
                "fy_pa": as_float(first_attr(elem, "m_dfFy", "double")),
                "fu_pa": as_float(first_attr(elem, "m_dfFu", "double")),
                "used_by_single_part_count": 0,
                "used_by_module_count": 0,
                "total_weight_kg": 0.0,
            }
            materials[source_id] = material

        if cls == "CGREXSection" and source_id:
            geometry_points = []
            size = as_int(first_attr(elem, "m_tabGeometryPoints_SIZE", "long")) or 0
            for index in range(size):
                item = elem.find(f".//m_tabGeometryPoints_{index}")
                if item is None:
                    continue
                parsed = point(item, ".")
                if parsed:
                    geometry_points.append(parsed)

            section = {
                "id": node_id("section", source_id),
                "source_object_id": as_int(source_id),
                "section_name": clean(first_attr(elem, "m_strName", "string")),
                "standard": clean(first_attr(elem, "m_strStandard", "string")),
                "internal_name": clean(first_attr(elem, "m_strInternalName", "string")),
                "family": as_int(first_attr(elem, "m_Family", "long")),
                "section_class": clean(first_attr(elem, "m_strClass", "string")),
                "geometry_points": geometry_points,
                "used_by_single_part_count": 0,
                "used_by_module_count": 0,
                "total_weight_kg": 0.0,
            }
            sections[source_id] = section

    return materials, sections, class_counts


def single_part_record(elem: ET.Element, materials: dict[str, dict[str, Any]], sections: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    cls = elem.attrib.get("class", "")
    part_category = SINGLE_PART_CLASSES.get(cls)
    if not part_category:
        return None

    source_id = first_attr(elem, "m_nID", "long")
    if source_id is None:
        return None

    attrs = user_attributes(elem)
    module_id = clean(attrs.get("01"))
    module_type = clean(attrs.get("02"))
    material_source_id = object_ref(elem, "m_pMaterial")
    section_source_id = object_ref(elem, "m_pSectionStart") or object_ref(elem, "m_pSectionEnd")
    assembly_source_id = object_ref(elem, "m_pMainPartEx")
    assembly_key = assembly_source_id or f"orphan-{source_id}"
    material = materials.get(material_source_id or "")
    section = sections.get(section_source_id or "")
    section_name = section.get("section_name") if section else clean(first_attr(elem, "m_strName", "string"))
    section_standard = section.get("standard") if section else None
    profile = section_profile(section_standard, section_name)

    return {
        "id": node_id("single_part", source_id),
        "source_object_id": as_int(source_id),
        "source_class": cls,
        "part_category": part_category,
        "name": clean(first_attr(elem, "m_strName", "string")),
        "mark": clean(first_attr(elem, "m_strMark", "string")),
        "single_part_mark": clean(first_attr(elem, "m_strSinglePartMark", "string")),
        "role": clean(first_attr(elem, "m_strRole", "string")),
        "module_type": module_type,
        "module_id": module_id,
        "assembly_id": node_id("assembly", assembly_key),
        "source_main_part_id": as_int(assembly_source_id),
        "has_source_assembly": assembly_source_id is not None,
        "is_main_part": as_bool_long(first_attr(elem, "m_bIsMainPart", "long")) or False,
        "is_structural_part": as_bool_long(first_attr(elem, "m_bIsStructuralPart", "long")),
        "material_id": node_id("material", material_source_id),
        "material_name": material.get("name") if material else None,
        "section_id": node_id("section", section_source_id),
        "section_name": section_name,
        "section_standard": section_standard,
        **profile,
        "length_m": as_float(first_attr(elem, "m_Length", "double")),
        "width_m": as_float(first_attr(elem, "m_Width", "double")),
        "height_m": as_float(first_attr(elem, "m_Height", "double")),
        "volume_m3": as_float(first_attr(elem, "m_Volume", "double")),
        "painted_area_m2": as_float(first_attr(elem, "m_PaintedArea", "double")),
        "weight_kg": as_float(first_attr(elem, "m_ExactWeight", "double")) or as_float(first_attr(elem, "m_dfWeight", "double")) or 0.0,
        "cad_start": point(elem, "m_pCADStart"),
        "cad_end": point(elem, "m_pCADEnd"),
        "gravity_center": point(elem, "m_GravityCenter"),
        "normal": point(elem, "m_pNormal"),
        "external_reference_ids": external_reference_ids(elem),
        "joint_transfer_id": clean(first_attr(elem, "m_strJointTransferID", "string")),
        "user_attributes": attrs,
    }


def collect_single_parts(
    xml_path: Path,
    materials: dict[str, dict[str, Any]],
    sections: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    quality = {
        "raw_single_part_count": 0,
        "missing_module_id_count": 0,
        "missing_module_type_count": 0,
        "orphan_part_count": 0,
        "zero_weight_part_count": 0,
        "excluded_missing_hierarchy_count": 0,
    }

    for elem in iter_top_level_objects(xml_path):
        cls = elem.attrib.get("class", "")
        if cls not in SINGLE_PART_CLASSES:
            continue
        quality["raw_single_part_count"] += 1
        part = single_part_record(elem, materials, sections)
        if part is None:
            continue

        if not part["module_id"]:
            quality["missing_module_id_count"] += 1
        if not part["module_type"]:
            quality["missing_module_type_count"] += 1
        if not part["has_source_assembly"]:
            quality["orphan_part_count"] += 1
        if not part["weight_kg"]:
            quality["zero_weight_part_count"] += 1

        if not part["module_id"] or not part["module_type"]:
            quality["excluded_missing_hierarchy_count"] += 1
            continue

        parts.append(part)

    return parts, quality


def fastener_spec(elem: ET.Element, fastener_type: str) -> dict[str, Any]:
    diameter_m = as_float(first_attr(elem, "m_dfDiameter", "double"))
    length_m = as_float(first_attr(elem, "m_dfLength", "double"))
    diameter_mm = round(diameter_m * 1000) if diameter_m is not None else None
    length_mm = round(length_m * 1000) if length_m is not None else None
    standard = (
        clean(first_attr(elem, "m_strNorm", "string"))
        or clean(first_attr(elem, "m_strAnchorType", "string"))
        or "(blank)"
    )
    grade = clean(first_attr(elem, "m_strMaterial", "string"))
    set_name = clean(first_attr(elem, "m_strBoltSet", "string")) or clean(first_attr(elem, "m_strAnchorSet", "string"))
    return {
        "fastener_type": fastener_type,
        "description": f"M{diameter_mm}x{length_mm}" if diameter_mm and length_mm else None,
        "diameter_mm": diameter_mm,
        "length_mm": length_mm,
        "grade": grade,
        "standard": standard,
        "set_name": set_name,
    }


def collect_fasteners(xml_path: Path, parts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    part_by_source_id = {str(part["source_object_id"]): part for part in parts if part.get("source_object_id") is not None}
    patterns: list[dict[str, Any]] = []
    bolt_objects_by_spec: dict[tuple[Any, ...], dict[str, Any]] = {}
    bolt_patterns_by_spec: dict[tuple[Any, ...], dict[str, Any]] = {}
    anchor_patterns_by_spec: dict[tuple[Any, ...], dict[str, Any]] = {}
    quality = {
        "bolt_object_count": 0,
        "bolt_pattern_count": 0,
        "anchor_pattern_count": 0,
        "bolt_total_quantity_from_patterns": 0,
        "anchor_total_quantity": 0,
        "anchor_location_mismatch_count": 0,
        "missing_pattern_grid_count": 0,
    }

    def spec_key(spec: dict[str, Any]) -> tuple[Any, ...]:
        return (spec["description"], spec["grade"], spec["standard"], spec["diameter_mm"], spec["length_mm"])

    def add_spec(bucket: dict[tuple[Any, ...], dict[str, Any]], spec: dict[str, Any], quantity: int) -> None:
        key = spec_key(spec)
        row = bucket.setdefault(
            key,
            {
                "description": spec["description"],
                "grade": spec["grade"],
                "standard": spec["standard"],
                "diameter_mm": spec["diameter_mm"],
                "length_mm": spec["length_mm"],
                "quantity": 0,
            },
        )
        row["quantity"] += quantity

    def modules_from_refs(refs: list[str]) -> tuple[list[str], list[str]]:
        module_ids: set[str] = set()
        assembly_ids: set[str] = set()
        for ref in refs:
            part = part_by_source_id.get(str(ref))
            if not part:
                continue
            if part.get("module_id"):
                module_ids.add(part["module_id"])
            if part.get("assembly_id"):
                assembly_ids.add(part["assembly_id"])
        return sorted(module_ids), sorted(assembly_ids)

    for elem in iter_top_level_objects(xml_path):
        cls = elem.attrib.get("class", "")
        if cls == "CGREXBolt":
            quality["bolt_object_count"] += 1
            add_spec(bolt_objects_by_spec, fastener_spec(elem, "Bolt"), 1)
            continue
        if cls not in {"CGREXBoltPattern", "CGREXAnchorPattern"}:
            continue

        is_anchor = cls == "CGREXAnchorPattern"
        fastener_type = "Anchor" if is_anchor else "Bolt"
        if is_anchor:
            quality["anchor_pattern_count"] += 1
        else:
            quality["bolt_pattern_count"] += 1

        number_on_x = as_int(first_attr(elem, "m_nNumberOnX", "long"))
        number_on_y = as_int(first_attr(elem, "m_nNumberOnY", "long"))
        grid_quantity = (number_on_x or 0) * (number_on_y or 0)
        location_count = as_int(first_attr(elem, "m_tabLocations_SIZE", "long"))
        if number_on_x is None or number_on_y is None:
            quality["missing_pattern_grid_count"] += 1
        if is_anchor and location_count is not None and grid_quantity and location_count != grid_quantity:
            quality["anchor_location_mismatch_count"] += 1

        computed_quantity = location_count if is_anchor and location_count is not None else grid_quantity
        spec = fastener_spec(elem, fastener_type)
        connected_source_object_ids = object_refs_by_prefix(elem, "m_tabConnectedObjects")
        module_ids, assembly_ids = modules_from_refs(connected_source_object_ids)
        origin = point(elem, "m_pOrigin")

        pattern_index = quality["anchor_pattern_count"] if is_anchor else quality["bolt_pattern_count"]
        pattern_id = view_id("fastener_pattern", fastener_type.lower(), pattern_index)
        row = {
            "id": pattern_id,
            "source_class": cls,
            "fastener_type": fastener_type,
            **spec,
            "quantity": computed_quantity,
            "number_on_x": number_on_x,
            "number_on_y": number_on_y,
            "grid_quantity": grid_quantity,
            "location_count": location_count,
            "origin": origin,
            "connected_source_object_ids": [as_int(ref) for ref in connected_source_object_ids],
            "module_ids": module_ids,
            "assembly_ids": assembly_ids,
        }
        patterns.append(row)
        if is_anchor:
            quality["anchor_total_quantity"] += computed_quantity
            add_spec(anchor_patterns_by_spec, spec, computed_quantity)
        else:
            quality["bolt_total_quantity_from_patterns"] += computed_quantity
            add_spec(bolt_patterns_by_spec, spec, computed_quantity)

    summary = {
        **quality,
        "bolt_object_matches_pattern_quantity": quality["bolt_object_count"] == quality["bolt_total_quantity_from_patterns"],
        "bolt_objects_by_spec": sorted(bolt_objects_by_spec.values(), key=lambda row: (row["description"] or "", row["standard"] or "")),
        "bolt_patterns_by_spec": sorted(bolt_patterns_by_spec.values(), key=lambda row: (row["description"] or "", row["standard"] or "")),
        "anchor_patterns_by_spec": sorted(anchor_patterns_by_spec.values(), key=lambda row: (row["description"] or "", row["standard"] or "")),
    }
    return patterns, summary


def build_bbox(parts: list[dict[str, Any]]) -> dict[str, float | None]:
    coords: list[dict[str, float]] = []
    for part in parts:
        for key in ("cad_start", "cad_end", "gravity_center"):
            value = part.get(key)
            if value:
                coords.append(value)
    if not coords:
        return {"min_x": None, "max_x": None, "min_y": None, "max_y": None, "min_z": None, "max_z": None}
    return {
        "min_x": min(item["x"] for item in coords),
        "max_x": max(item["x"] for item in coords),
        "min_y": min(item["y"] for item in coords),
        "max_y": max(item["y"] for item in coords),
        "min_z": min(item["z"] for item in coords),
        "max_z": max(item["z"] for item in coords),
    }


def make_assembly_rows(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for part in parts:
        grouped[part["assembly_id"]].append(part)

    rows: list[dict[str, Any]] = []
    for assembly_id, assembly_parts in sorted(grouped.items()):
        main_parts = [part for part in assembly_parts if part["is_main_part"]]
        main_part = main_parts[0] if main_parts else assembly_parts[0]
        module_ids = sorted({part["module_id"] for part in assembly_parts if part["module_id"]})
        module_types = sorted({part["module_type"] for part in assembly_parts if part["module_type"]})

        role_stats: dict[str, dict[str, Any]] = {}
        category_stats: dict[str, dict[str, Any]] = {}
        attached_grouped: dict[tuple[str | None, str | None, str | None, str | None, str | None], dict[str, Any]] = {}
        for part in assembly_parts:
            nested_stat_add(role_stats, part["role"], part)
            nested_stat_add(category_stats, part["part_category"], part)
            if not part["is_main_part"]:
                key = (part["role"], part["single_part_mark"], part["name"], part["material_name"], part["section_name"])
                stat = attached_grouped.setdefault(
                    key,
                    {
                        "role": part["role"],
                        "single_part_mark": part["single_part_mark"],
                        "name": part["name"],
                        "part_category": part["part_category"],
                        "material": part["material_name"],
                        "section": part["section_name"],
                        "single_part_count": 0,
                        "weight_kg": 0.0,
                        "length_m": 0.0,
                        "single_parts": [],
                    },
                )
                stat["single_part_count"] += 1
                stat["weight_kg"] += part["weight_kg"] or 0
                stat["length_m"] += part["length_m"] or 0
                stat["single_parts"].append(part["id"])

        total_weight = sum(part["weight_kg"] or 0 for part in assembly_parts)
        main_weight = sum(part["weight_kg"] or 0 for part in main_parts)
        attached_weight = sum(part["weight_kg"] or 0 for part in assembly_parts if not part["is_main_part"])

        rows.append(
            {
                "id": assembly_id,
                "source_main_part_id": main_part.get("source_main_part_id"),
                "assembly_mark": main_part.get("mark"),
                "assembly_role": main_part.get("role"),
                "module_id": module_ids[0] if len(module_ids) == 1 else None,
                "module_ids": module_ids,
                "module_type": module_types[0] if len(module_types) == 1 else None,
                "module_types": module_types,
                "main_single_part_id": main_part["id"] if main_parts else None,
                "main_part": {
                    "single_part_id": main_part["id"],
                    "category": main_part["part_category"],
                    "name": main_part["name"],
                    "single_part_mark": main_part["single_part_mark"],
                    "material": main_part["material_name"],
                    "section": main_part["section_name"],
                    "weight_kg": main_part["weight_kg"],
                },
                "single_part_count": len(assembly_parts),
                "main_single_part_count": len(main_parts),
                "attached_single_part_count": len(assembly_parts) - len(main_parts),
                "total_weight_kg": total_weight,
                "main_part_weight_kg": main_weight,
                "attached_part_weight_kg": attached_weight,
                "total_length_m": sum(part["length_m"] or 0 for part in assembly_parts),
                "by_single_part_role": role_stats,
                "by_part_category": category_stats,
                "attached_parts_summary": sorted(
                    attached_grouped.values(),
                    key=lambda item: ((item["role"] or ""), (item["single_part_mark"] or ""), (item["name"] or "")),
                ),
                "single_parts": [part["id"] for part in sorted(assembly_parts, key=lambda item: item["source_object_id"] or 0)],
            }
        )
    return rows


def make_module_rows(parts: list[dict[str, Any]], assemblies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parts_by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    assemblies_by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for part in parts:
        parts_by_module[part["module_id"]].append(part)
    for assembly in assemblies:
        for module_id in assembly["module_ids"]:
            assemblies_by_module[module_id].append(assembly)

    rows: list[dict[str, Any]] = []
    for module_id, module_parts in sorted(parts_by_module.items()):
        module_types = sorted({part["module_type"] for part in module_parts if part["module_type"]})
        totals = blank_sum()
        by_category: dict[str, dict[str, Any]] = {}
        by_role: dict[str, dict[str, Any]] = {}
        by_material: dict[str, dict[str, Any]] = {}
        by_section: dict[str, dict[str, Any]] = {}
        for part in module_parts:
            add_sum(totals, part)
            nested_stat_add(by_category, part["part_category"], part)
            nested_stat_add(by_role, part["role"], part)
            nested_stat_add(by_material, part["material_name"], part)
            nested_stat_add(by_section, part["section_name"], part)

        module_assemblies = assemblies_by_module.get(module_id, [])
        by_assembly_role: dict[str, dict[str, Any]] = {}
        for assembly in module_assemblies:
            key = assembly.get("assembly_role") or "(blank)"
            stat = by_assembly_role.setdefault(
                key,
                {
                    "assembly_count": 0,
                    "single_part_count": 0,
                    "main_single_part_count": 0,
                    "attached_single_part_count": 0,
                    "weight_kg": 0.0,
                },
            )
            stat["assembly_count"] += 1
            stat["single_part_count"] += assembly["single_part_count"]
            stat["main_single_part_count"] += assembly["main_single_part_count"]
            stat["attached_single_part_count"] += assembly["attached_single_part_count"]
            stat["weight_kg"] += assembly["total_weight_kg"]

        rows.append(
            {
                "id": node_id("module", module_id),
                "module_id": module_id,
                "module_type": module_types[0] if len(module_types) == 1 else None,
                "module_types": module_types,
                "assembly_count": len({assembly["id"] for assembly in module_assemblies}),
                **totals,
                "by_assembly_role": by_assembly_role,
                "by_single_part_category": by_category,
                "by_single_part_role": by_role,
                "by_material": by_material,
                "by_section": by_section,
                "assemblies": [
                    {
                        "assembly_id": assembly["id"],
                        "assembly_mark": assembly["assembly_mark"],
                        "assembly_role": assembly["assembly_role"],
                        "main_part": assembly["main_part"]["name"],
                        "main_single_part_id": assembly["main_single_part_id"],
                        "single_part_count": assembly["single_part_count"],
                        "total_weight_kg": assembly["total_weight_kg"],
                    }
                    for assembly in sorted(module_assemblies, key=lambda item: item["id"])
                ],
            }
        )
    return rows


def make_module_type_rows(module_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    modules_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for module in module_rows:
        for module_type in module["module_types"]:
            modules_by_type[module_type].append(module)

    rows: list[dict[str, Any]] = []
    for module_type, modules in sorted(modules_by_type.items()):
        totals = blank_sum()
        by_category: dict[str, dict[str, Any]] = {}
        by_role: dict[str, dict[str, Any]] = {}
        by_section: dict[str, dict[str, Any]] = {}
        assembly_ids: set[str] = set()
        for module in modules:
            totals["single_part_count"] += module["single_part_count"]
            totals["main_single_part_count"] += module["main_single_part_count"]
            totals["attached_single_part_count"] += module["attached_single_part_count"]
            totals["total_weight_kg"] += module["total_weight_kg"]
            totals["total_length_m"] += module["total_length_m"]
            assembly_ids.update(item["assembly_id"] for item in module["assemblies"])
            merge_stat_dict(by_category, module["by_single_part_category"])
            merge_stat_dict(by_role, module["by_single_part_role"])
            merge_stat_dict(by_section, module["by_section"])

        rows.append(
            {
                "id": node_id("module_type", module_type),
                "module_type": module_type,
                "source_attribute": "USER ATTRIBUTE 02",
                "module_count": len(modules),
                "assembly_count": len(assembly_ids),
                **totals,
                "modules": [module["module_id"] for module in sorted(modules, key=lambda item: item["module_id"])],
                "by_part_category": by_category,
                "by_single_part_role": by_role,
                "by_section": by_section,
            }
        )
    return rows


def merge_stat_dict(target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]]) -> None:
    for key, value in source.items():
        stat = target.setdefault(key, {"count": 0, "weight_kg": 0.0, "length_m": 0.0})
        stat["count"] += value.get("count", 0)
        stat["weight_kg"] += value.get("weight_kg", 0)
        stat["length_m"] += value.get("length_m", 0)


def make_reference_summaries(parts: list[dict[str, Any]], field: str, id_field: str, name_field: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for part in parts:
        key = part.get(field) or "(blank)"
        grouped[key].append(part)

    rows: list[dict[str, Any]] = []
    for key, items in sorted(grouped.items()):
        rows.append(
            {
                id_field: key if key != "(blank)" else None,
                name_field: key if key != "(blank)" else None,
                "used_in_module_count": len({part["module_id"] for part in items if part["module_id"]}),
                "single_part_count": len(items),
                "total_weight_kg": sum(part["weight_kg"] or 0 for part in items),
                "modules": sorted({part["module_id"] for part in items if part["module_id"]}),
                "by_part_category": counter_to_count_weight(items, "part_category"),
            }
        )
    return rows


def query_aliases() -> dict[str, Any]:
    return {
        "description": "User-facing Korean and shape aliases for Advance Steel profile queries.",
        "aliases": {
            "C-Channel": ["C-Channel", "씨찬넬", "씨잔넬"],
            "Channel": ["Channel", "찬넬", "잔넬", "ㄷ형강"],
            "H-Beam": ["H-Beam", "H형강", "에이치형강"],
            "Angle": ["Angle", "앵글", "ㄱ형강"],
            "Tube": ["Tube", "각관", "사각관", "Square Section", "Rectangular Section"],
            "Plate": ["Plate", "플레이트", "철판", "판재"],
        },
        "field_mapping": {
            "module": "module_id",
            "assembly_mark": "assembly_mark",
            "profile": "profile_group",
            "material": "material_name",
            "weight": "total_weight_kg",
        },
    }


def make_fastener_user_view_rows(fastener_summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "id": "view:fastener:model:bolts",
            "user_view_type": "ModelFastenerUserView",
            "display_name": "전체 볼트",
            "fastener_type": "Bolt",
            "quantity": fastener_summary["bolt_total_quantity_from_patterns"],
            "pattern_count": fastener_summary["bolt_pattern_count"],
            "object_count": fastener_summary["bolt_object_count"],
            "by_spec": fastener_summary["bolt_patterns_by_spec"],
            "answer_summary": (
                f"전체 볼트는 {fastener_summary['bolt_total_quantity_from_patterns']}개입니다. "
                f"볼트 패턴은 {fastener_summary['bolt_pattern_count']}개이며, "
                f"실제 볼트 객체 수 {fastener_summary['bolt_object_count']}개와 일치합니다."
            ),
        },
        {
            "id": "view:fastener:model:anchors",
            "user_view_type": "ModelFastenerUserView",
            "display_name": "전체 앙카",
            "fastener_type": "Anchor",
            "quantity": fastener_summary["anchor_total_quantity"],
            "pattern_count": fastener_summary["anchor_pattern_count"],
            "object_count": None,
            "by_spec": fastener_summary["anchor_patterns_by_spec"],
            "answer_summary": (
                f"전체 앙카는 {fastener_summary['anchor_total_quantity']}개입니다. "
                f"앙카 패턴 {fastener_summary['anchor_pattern_count']}개의 위치 수량을 합산했습니다."
            ),
        },
    ]
    return rows


def make_user_view_rows(
    parts: list[dict[str, Any]],
    assembly_rows: list[dict[str, Any]],
    module_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    parts_by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for part in parts:
        parts_by_module[part["module_id"]].append(part)

    module_user_rows: list[dict[str, Any]] = []
    for module in module_rows:
        module_id = module["module_id"]
        assembly_instances = [
            {
                "assembly_mark": item.get("assembly_mark"),
                "assembly_role": item.get("assembly_role"),
                "main_part": item.get("main_part"),
                "single_part_count": item.get("single_part_count"),
                "total_weight_kg": item.get("total_weight_kg"),
                "backing_assembly_id": item.get("assembly_id"),
            }
            for item in sorted(module["assemblies"], key=lambda item: (item.get("assembly_mark") or "", item.get("assembly_id") or ""))
        ]
        assembly_mark_sequence = [item.get("assembly_mark") for item in assembly_instances]
        assembly_mark_counts = dict(sorted(Counter(mark for mark in assembly_mark_sequence if mark).items()))
        assembly_marks = sorted(assembly_mark_counts)
        module_user_rows.append(
            {
                "id": view_id("module", module_id),
                "user_view_type": "ModuleUserView",
                "display_name": f"{module_id} 모듈",
                "module_id": module_id,
                "module_type": module.get("module_type"),
                "assembly_count": module["assembly_count"],
                "single_part_count": module["single_part_count"],
                "main_single_part_count": module["main_single_part_count"],
                "attached_single_part_count": module["attached_single_part_count"],
                "total_weight_kg": module["total_weight_kg"],
                "total_length_m": module["total_length_m"],
                "assembly_marks": assembly_marks,
                "assembly_mark_count": len(assembly_marks),
                "assembly_mark_counts": assembly_mark_counts,
                "assembly_mark_sequence": assembly_mark_sequence,
                "assembly_instances": assembly_instances,
                "answer_summary": (
                    f"{module_id} 모듈은 {module.get('module_type') or '타입 미지정'}이며, "
                    f"어셈블리 {module['assembly_count']}개, 단품 {module['single_part_count']}개, "
                    f"총중량 {round(module['total_weight_kg'], 3)} kg입니다."
                ),
                "backing_module_id": module["id"],
            }
        )

    parts_by_assembly = {part["id"]: part for part in parts}
    assembly_user_rows: list[dict[str, Any]] = []
    for assembly in assembly_rows:
        module_id = assembly.get("module_id") or (assembly.get("module_ids") or ["multi"])[0]
        main_part = parts_by_assembly.get(assembly.get("main_single_part_id") or "")
        assembly_mark = assembly.get("assembly_mark")
        main_name = assembly["main_part"].get("name")
        attached_summary = [
            {
                "role": item.get("role"),
                "single_part_mark": item.get("single_part_mark"),
                "name": item.get("name"),
                "part_category": item.get("part_category"),
                "material": item.get("material"),
                "section": item.get("section"),
                "single_part_count": item.get("single_part_count"),
                "weight_kg": item.get("weight_kg"),
            }
            for item in assembly.get("attached_parts_summary", [])
        ]
        single_parts_summary = []
        if assembly["main_single_part_count"]:
            single_parts_summary.append(
                {
                    "is_main_part": True,
                    "role": assembly.get("assembly_role"),
                    "single_part_mark": assembly["main_part"].get("single_part_mark"),
                    "name": assembly["main_part"].get("name"),
                    "part_category": assembly["main_part"].get("category"),
                    "material": assembly["main_part"].get("material"),
                    "section": assembly["main_part"].get("section"),
                    "single_part_count": assembly["main_single_part_count"],
                    "weight_kg": assembly["main_part_weight_kg"],
                }
            )
        single_parts_summary.extend(
            {
                "is_main_part": False,
                **item,
            }
            for item in attached_summary
        )
        assembly_user_rows.append(
            {
                "id": view_id("assembly", module_id, assembly_mark or "unmarked", assembly.get("source_main_part_id") or assembly["id"]),
                "user_view_type": "AssemblyUserView",
                "display_name": f"{module_id} {assembly_mark or '마크 없음'} 어셈블리",
                "module_id": module_id,
                "module_type": assembly.get("module_type"),
                "assembly_mark": assembly_mark,
                "assembly_role": assembly.get("assembly_role"),
                "main_part_name": main_name,
                "main_part_category": assembly["main_part"].get("category"),
                "main_part_profile_group": main_part.get("profile_group") if main_part else None,
                "main_part_material": assembly["main_part"].get("material"),
                "single_part_count": assembly["single_part_count"],
                "attached_single_part_count": assembly["attached_single_part_count"],
                "total_weight_kg": assembly["total_weight_kg"],
                "main_part_weight_kg": assembly["main_part_weight_kg"],
                "attached_part_weight_kg": assembly["attached_part_weight_kg"],
                "attached_parts_summary": attached_summary,
                "single_parts_summary": single_parts_summary,
                "answer_summary": (
                    f"{module_id}의 {assembly_mark or '마크 없음'} 어셈블리는 "
                    f"{assembly.get('assembly_role') or '역할 미지정'}이며, 대표 부재는 "
                    f"{main_name or '이름 없음'}, 단품 {assembly['single_part_count']}개, "
                    f"총중량 {round(assembly['total_weight_kg'], 3)} kg입니다."
                ),
                "backing_assembly_id": assembly["id"],
                "backing_main_single_part_id": assembly.get("main_single_part_id"),
            }
        )

    assembly_mark_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in assembly_user_rows:
        assembly_mark_groups[row.get("assembly_mark") or "(unmarked)"].append(row)

    assembly_mark_summary_rows: list[dict[str, Any]] = []
    for assembly_mark, items in sorted(assembly_mark_groups.items()):
        module_counts = dict(sorted(Counter(item["module_id"] for item in items).items()))
        role_counts = dict(sorted(Counter(item.get("assembly_role") or "(blank)" for item in items).items()))
        main_part_counts = dict(sorted(Counter(item.get("main_part_name") or "(blank)" for item in items).items()))
        single_part_composition: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in items:
            for part in item.get("single_parts_summary", []):
                key = (
                    part.get("is_main_part"),
                    part.get("role"),
                    part.get("single_part_mark"),
                    part.get("name"),
                    part.get("part_category"),
                    part.get("material"),
                    part.get("section"),
                )
                stat = single_part_composition.setdefault(
                    key,
                    {
                        "is_main_part": part.get("is_main_part"),
                        "role": part.get("role"),
                        "single_part_mark": part.get("single_part_mark"),
                        "name": part.get("name"),
                        "part_category": part.get("part_category"),
                        "material": part.get("material"),
                        "section": part.get("section"),
                        "single_part_count": 0,
                        "weight_kg": 0.0,
                    },
                )
                stat["single_part_count"] += part.get("single_part_count") or 0
                stat["weight_kg"] += part.get("weight_kg") or 0.0

        visible_mark = None if assembly_mark == "(unmarked)" else assembly_mark
        assembly_mark_summary_rows.append(
            {
                "id": view_id("assembly_mark", assembly_mark),
                "user_view_type": "AssemblyMarkSummaryView",
                "display_name": f"{visible_mark or 'unmarked'} assembly mark",
                "assembly_mark": visible_mark,
                "assembly_count": len(items),
                "module_count": len(module_counts),
                "single_part_count": sum(item.get("single_part_count") or 0 for item in items),
                "attached_single_part_count": sum(item.get("attached_single_part_count") or 0 for item in items),
                "total_weight_kg": sum(item.get("total_weight_kg") or 0 for item in items),
                "module_counts": module_counts,
                "assembly_role_counts": role_counts,
                "main_part_counts": main_part_counts,
                "single_part_composition": sorted(
                    single_part_composition.values(),
                    key=lambda item: (
                        not bool(item.get("is_main_part")),
                        item.get("role") or "",
                        item.get("single_part_mark") or "",
                        item.get("name") or "",
                    ),
                ),
                "assembly_instances": [
                    {
                        "module_id": item.get("module_id"),
                        "module_type": item.get("module_type"),
                        "assembly_role": item.get("assembly_role"),
                        "main_part_name": item.get("main_part_name"),
                        "single_part_count": item.get("single_part_count"),
                        "attached_single_part_count": item.get("attached_single_part_count"),
                        "total_weight_kg": item.get("total_weight_kg"),
                        "single_parts_summary": item.get("single_parts_summary"),
                        "backing_assembly_id": item.get("backing_assembly_id"),
                    }
                    for item in sorted(items, key=lambda item: (item.get("module_id") or "", item.get("backing_assembly_id") or ""))
                ],
                "answer_summary": (
                    f"{visible_mark or 'unmarked'} assembly mark has {len(items)} assembly instances "
                    f"across {len(module_counts)} modules."
                ),
            }
        )

    profile_rows: list[dict[str, Any]] = []
    for module_id, module_parts in sorted(parts_by_module.items()):
        grouped: dict[str, dict[str, Any]] = {}
        for part in module_parts:
            profile_group = part.get("profile_group") or "(unclassified)"
            stat = grouped.setdefault(
                profile_group,
                {
                    "id": view_id("module_profile", module_id, profile_group),
                    "user_view_type": "ModuleProfileSummaryView",
                    "display_name": f"{module_id} {profile_group}",
                    "module_id": module_id,
                    "profile_group": profile_group,
                    "profile_name_ko": part.get("profile_name_ko"),
                    "profile_aliases_ko": part.get("profile_aliases_ko") or [],
                    "profile_query_terms": part.get("profile_query_terms") or [],
                    "single_part_count": 0,
                    "main_single_part_count": 0,
                    "attached_single_part_count": 0,
                    "total_weight_kg": 0.0,
                    "total_length_m": 0.0,
                    "by_section": {},
                    "by_role": {},
                },
            )
            add_sum(stat, part)
            nested_stat_add(stat["by_section"], part.get("section_name"), part)
            nested_stat_add(stat["by_role"], part.get("role"), part)
        for stat in grouped.values():
            stat["answer_summary"] = (
                f"{stat['module_id']} 모듈의 {stat['profile_name_ko'] or stat['profile_group']} 부재는 "
                f"{stat['single_part_count']}개, 총중량 {round(stat['total_weight_kg'], 3)} kg입니다."
            )
            profile_rows.append(stat)

    material_rows: list[dict[str, Any]] = []
    for module_id, module_parts in sorted(parts_by_module.items()):
        grouped: dict[str, dict[str, Any]] = {}
        for part in module_parts:
            material = part.get("material_name") or "(blank)"
            stat = grouped.setdefault(
                material,
                {
                    "id": view_id("module_material", module_id, material),
                    "user_view_type": "ModuleMaterialSummaryView",
                    "display_name": f"{module_id} {material}",
                    "module_id": module_id,
                    "material_name": None if material == "(blank)" else material,
                    "single_part_count": 0,
                    "main_single_part_count": 0,
                    "attached_single_part_count": 0,
                    "total_weight_kg": 0.0,
                    "total_length_m": 0.0,
                    "by_profile_group": {},
                    "by_section": {},
                },
            )
            add_sum(stat, part)
            nested_stat_add(stat["by_profile_group"], part.get("profile_group"), part)
            nested_stat_add(stat["by_section"], part.get("section_name"), part)
        for stat in grouped.values():
            stat["answer_summary"] = (
                f"{stat['module_id']} 모듈의 {stat['material_name'] or '재질 미지정'} 재질은 "
                f"{stat['single_part_count']}개, 총중량 {round(stat['total_weight_kg'], 3)} kg입니다."
            )
            material_rows.append(stat)

    return {
        "module_user_view": sorted(module_user_rows, key=lambda item: item["module_id"]),
        "assembly_user_view": sorted(
            assembly_user_rows,
            key=lambda item: (item["module_id"], item.get("assembly_mark") or "", item["id"]),
        ),
        "assembly_mark_summary_view": sorted(
            assembly_mark_summary_rows,
            key=lambda item: (item.get("assembly_mark") or "", item["id"]),
        ),
        "module_profile_summary_view": sorted(
            profile_rows,
            key=lambda item: (item["module_id"], item.get("profile_group") or ""),
        ),
        "module_material_summary_view": sorted(
            material_rows,
            key=lambda item: (item["module_id"], item.get("material_name") or ""),
        ),
    }


def counter_to_count_weight(parts: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for part in parts:
        nested_stat_add(stats, part.get(field), part)
    return stats


def make_edges(
    module_type_rows: list[dict[str, Any]],
    module_rows: list[dict[str, Any]],
    assembly_rows: list[dict[str, Any]],
    parts: list[dict[str, Any]],
) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_edge(from_id: str | None, relation: str, to_id: str | None) -> None:
        if not from_id or not to_id:
            return
        key = (from_id, relation, to_id)
        if key in seen:
            return
        seen.add(key)
        edges.append(
            {
                "id": f"as:edge:{len(edges) + 1}",
                "from": from_id,
                "relation": relation,
                "to": to_id,
            }
        )

    for module_type in module_type_rows:
        for module_id in module_type["modules"]:
            add_edge(module_type["id"], "has_module", node_id("module", module_id))

    for module in module_rows:
        for assembly in module["assemblies"]:
            add_edge(module["id"], "has_assembly", assembly["assembly_id"])

    for assembly in assembly_rows:
        for part_id in assembly["single_parts"]:
            add_edge(assembly["id"], "has_single_part", part_id)
            add_edge(part_id, "part_of_assembly", assembly["id"])
        add_edge(assembly["id"], "has_main_part", assembly["main_single_part_id"])

    for part in parts:
        add_edge(part["id"], "belongs_to_module", node_id("module", part["module_id"]))
        add_edge(part["id"], "has_material", part["material_id"])
        add_edge(part["id"], "has_section", part["section_id"])

    return edges


def update_reference_usage(
    materials: dict[str, dict[str, Any]],
    sections: dict[str, dict[str, Any]],
    parts: list[dict[str, Any]],
) -> None:
    material_modules: dict[str, set[str]] = defaultdict(set)
    section_modules: dict[str, set[str]] = defaultdict(set)
    for part in parts:
        material_id = part.get("material_id")
        section_id = part.get("section_id")
        module_id = part.get("module_id")
        if material_id:
            raw_id = material_id.rsplit(":", 1)[-1]
            if raw_id in materials:
                materials[raw_id]["used_by_single_part_count"] += 1
                materials[raw_id]["total_weight_kg"] += part["weight_kg"] or 0
                if module_id:
                    material_modules[raw_id].add(module_id)
        if section_id:
            raw_id = section_id.rsplit(":", 1)[-1]
            if raw_id in sections:
                sections[raw_id]["used_by_single_part_count"] += 1
                sections[raw_id]["total_weight_kg"] += part["weight_kg"] or 0
                if module_id:
                    section_modules[raw_id].add(module_id)

    for raw_id, modules in material_modules.items():
        materials[raw_id]["used_by_module_count"] = len(modules)
    for raw_id, modules in section_modules.items():
        sections[raw_id]["used_by_module_count"] = len(modules)


def build_package(xml_path: Path) -> dict[str, Any]:
    started = time.time()
    JSONL_DIR.mkdir(exist_ok=True)
    SUMMARY_DIR.mkdir(exist_ok=True)
    USER_VIEW_DIR.mkdir(exist_ok=True)
    (WORK_ROOT / "source").mkdir(exist_ok=True)

    if xml_path.resolve() != SOURCE_XML.resolve():
        shutil.copy2(xml_path, SOURCE_XML)
    else:
        xml_path = SOURCE_XML

    materials, sections, class_counts = collect_reference_data(xml_path)
    parts, quality = collect_single_parts(xml_path, materials, sections)
    fastener_patterns, fastener_summary = collect_fasteners(xml_path, parts)
    update_reference_usage(materials, sections, parts)

    assembly_rows = make_assembly_rows(parts)
    module_rows = make_module_rows(parts, assembly_rows)
    module_type_rows = make_module_type_rows(module_rows)
    user_view_rows = make_user_view_rows(parts, assembly_rows, module_rows)
    edges = make_edges(module_type_rows, module_rows, assembly_rows, parts)

    material_rows = sorted(materials.values(), key=lambda item: item["id"] or "")
    section_rows = sorted(sections.values(), key=lambda item: item["id"] or "")
    section_summary_rows = make_reference_summaries(parts, "section_name", "section_id", "section_name")
    material_summary_rows = make_reference_summaries(parts, "material_name", "material_id", "material_name")

    write_jsonl(JSONL_DIR / "single_parts.jsonl", sorted(parts, key=lambda item: item["source_object_id"] or 0))
    write_jsonl(JSONL_DIR / "assemblies.jsonl", assembly_rows)
    write_jsonl(JSONL_DIR / "modules.jsonl", module_rows)
    write_jsonl(JSONL_DIR / "module_types.jsonl", module_type_rows)
    write_jsonl(JSONL_DIR / "materials.jsonl", material_rows)
    write_jsonl(JSONL_DIR / "sections.jsonl", section_rows)
    write_jsonl(JSONL_DIR / "fastener_patterns.jsonl", fastener_patterns)
    write_jsonl(JSONL_DIR / "edges.jsonl", edges)

    write_jsonl(SUMMARY_DIR / "module_type_summary.jsonl", module_type_rows)
    write_jsonl(SUMMARY_DIR / "module_summary.jsonl", module_rows)
    write_jsonl(SUMMARY_DIR / "assembly_summary.jsonl", assembly_rows)
    write_jsonl(SUMMARY_DIR / "section_summary.jsonl", section_summary_rows)
    write_jsonl(SUMMARY_DIR / "material_summary.jsonl", material_summary_rows)
    write_json(SUMMARY_DIR / "fastener_summary.json", fastener_summary)

    write_jsonl(USER_VIEW_DIR / "module_user_view.jsonl", user_view_rows["module_user_view"])
    write_jsonl(USER_VIEW_DIR / "assembly_user_view.jsonl", user_view_rows["assembly_user_view"])
    write_jsonl(USER_VIEW_DIR / "assembly_mark_summary_view.jsonl", user_view_rows["assembly_mark_summary_view"])
    write_jsonl(USER_VIEW_DIR / "module_profile_summary_view.jsonl", user_view_rows["module_profile_summary_view"])
    write_jsonl(USER_VIEW_DIR / "module_material_summary_view.jsonl", user_view_rows["module_material_summary_view"])
    write_jsonl(USER_VIEW_DIR / "model_fastener_user_view.jsonl", make_fastener_user_view_rows(fastener_summary))
    write_json(USER_VIEW_DIR / "query_aliases.json", query_aliases())

    model_totals = blank_sum()
    by_category: dict[str, dict[str, Any]] = {}
    for part in parts:
        add_sum(model_totals, part)
        nested_stat_add(by_category, part["part_category"], part)

    model_summary = {
        "model_id": "advance_steel:samcheok_bldg_b",
        "source_file": str(SOURCE_XML),
        "source_size_bytes": SOURCE_XML.stat().st_size,
        "module_type_count": len(module_type_rows),
        "module_count": len(module_rows),
        "assembly_count": len(assembly_rows),
        **model_totals,
        "by_part_category": by_category,
        "bbox_m": build_bbox(parts),
        "top_level_class_counts": [{"class": key, "count": value} for key, value in sorted(class_counts.items())],
        "data_quality": quality,
        "fasteners": fastener_summary,
    }
    write_json(SUMMARY_DIR / "model_summary.json", model_summary)

    manifest = {
        "package_id": "advance_steel_opencrab_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_xml": str(SOURCE_XML),
        "hierarchy": ["ModuleType", "Module", "Assembly", "SinglePart"],
        "source_attribute_mapping": {
            "m_UserAttributes_1": "module_type",
            "m_UserAttributes_0": "module_id",
            "m_pMainPartEx.m_nID": "assembly_id",
        },
        "outputs": {
            "jsonl": sorted(path.name for path in JSONL_DIR.glob("*.jsonl")),
            "summary": sorted(path.name for path in SUMMARY_DIR.glob("*")),
            "user_views": sorted(path.name for path in USER_VIEW_DIR.glob("*")),
        },
        "counts": {
            "module_types": len(module_type_rows),
            "modules": len(module_rows),
            "assemblies": len(assembly_rows),
            "single_parts": len(parts),
            "materials": len(material_rows),
            "sections": len(section_rows),
            "edges": len(edges),
            "fastener_patterns": len(fastener_patterns),
        },
        "data_quality": quality,
        "elapsed_seconds": round(time.time() - started, 3),
    }
    write_json(MANIFEST_PATH, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build OpenCrab JSONL package from Advance Steel XML.")
    parser.add_argument("--xml", type=Path, default=SOURCE_XML, help="Advance Steel XML path.")
    args = parser.parse_args()
    manifest = build_package(args.xml)
    print(json.dumps(round_floats(manifest), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
