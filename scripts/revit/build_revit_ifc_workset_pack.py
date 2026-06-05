from __future__ import annotations

import json
import math
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_IFC = ROOT / "여주 모듈러 간부숙소 신축공사_AR_mwhong2776P.ifc"
PACK_DIR = ROOT / "revit-yeoju-ar-ifc-workset-module-pack"
BACKDATA_DIR = PACK_DIR / "backdata"
JSONL_DIR = BACKDATA_DIR / "jsonl"
SUMMARY_DIR = BACKDATA_DIR / "summary"
USER_VIEWS_DIR = BACKDATA_DIR / "user_views"
DOCS_DIR = PACK_DIR / "documents"

EXCLUDED_PROPERTY_NAMES = {
    "Module Number",
    "Module Type",
    "Module Type(AR)",
}

MODULE_RE = re.compile(r"^[12]-\d{2}-[A-Z]+$")
ENTITY_RE = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]+)\((.*)\);")
IFC_X2_RE = re.compile(r"\\X2\\([0-9A-Fa-f]+)\\X0\\")
TYPE_THICKNESS_RE = re.compile(r"T=(\d+(?:\.\d+)?)")

ELEMENT_TYPES = {
    "IFCWALLSTANDARDCASE",
    "IFCWALL",
    "IFCDOOR",
    "IFCWINDOW",
    "IFCSLAB",
    "IFCBEAM",
    "IFCCOLUMN",
    "IFCPLATE",
    "IFCBUILDINGELEMENTPROXY",
    "IFCCOVERING",
    "IFCROOF",
    "IFCSTAIR",
    "IFCSTAIRFLIGHT",
    "IFCRAILING",
    "IFCFURNISHINGELEMENT",
    "IFCMEMBER",
    "IFCFLOWSEGMENT",
    "IFCFLOWFITTING",
    "IFCFLOWTERMINAL",
    "IFCDISTRIBUTIONELEMENT",
}

STYLE_TYPES = {
    "IFCDOORSTYLE",
    "IFCWINDOWSTYLE",
}

CATEGORY_BY_IFC_TYPE = {
    "IFCWALLSTANDARDCASE": "Wall",
    "IFCWALL": "Wall",
    "IFCDOOR": "Door",
    "IFCWINDOW": "Window",
    "IFCSLAB": "Slab",
    "IFCBEAM": "Beam",
    "IFCCOLUMN": "Column",
    "IFCPLATE": "Plate",
    "IFCBUILDINGELEMENTPROXY": "Proxy",
    "IFCCOVERING": "Covering",
    "IFCROOF": "Roof",
    "IFCSTAIR": "Stair",
    "IFCSTAIRFLIGHT": "Stair",
    "IFCRAILING": "Railing",
    "IFCFURNISHINGELEMENT": "Furnishing",
    "IFCMEMBER": "Member",
    "IFCFLOWSEGMENT": "MEP",
    "IFCFLOWFITTING": "MEP",
    "IFCFLOWTERMINAL": "MEP",
    "IFCDISTRIBUTIONELEMENT": "MEP",
}

DISPLAY_CATEGORY = {
    "Wall": "벽체",
    "Door": "문",
    "Window": "창",
    "Slab": "슬래브",
    "Beam": "보",
    "Column": "기둥",
    "Plate": "플레이트",
    "Proxy": "프록시",
    "Covering": "마감",
    "Roof": "지붕",
    "Stair": "계단",
    "Railing": "난간",
    "Furnishing": "가구",
    "Member": "부재",
    "MEP": "MEP",
}

DOCUMENT_CATEGORY_FILES = {
    "Wall": "walls.md",
    "Door": "doors.md",
    "Window": "windows.md",
    "Slab": "slabs.md",
    "Beam": "beams.md",
    "Column": "columns.md",
    "Plate": "plates.md",
    "Proxy": "proxies.md",
}


def ifc_decode(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return bytes.fromhex(match.group(1)).decode("utf-16-be")
        except Exception:
            return match.group(0)

    return IFC_X2_RE.sub(repl, value)


def split_args(body: str) -> list[str]:
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    in_string = False
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "'":
            buf.append(ch)
            if in_string and i + 1 < len(body) and body[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            in_string = not in_string
            i += 1
            continue
        if not in_string:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append("".join(buf).strip())
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    if buf:
        args.append("".join(buf).strip())
    return args


def unquote(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if value == "$":
        return None
    if value.startswith("'") and value.endswith("'"):
        return ifc_decode(value[1:-1].replace("''", "'"))
    return ifc_decode(value)


def scalar_value(arg: str) -> Any:
    arg = arg.strip()
    if arg == "$":
        return None
    wrapper = re.match(r"([A-Z0-9_]+)\((.*)\)$", arg)
    if wrapper:
        inner = wrapper.group(2).strip()
        return scalar_value(inner)
    if arg in {".T.", ".F."}:
        return arg == ".T."
    unquoted = unquote(arg)
    if unquoted is None:
        return None
    if re.fullmatch(r"-?\d+", unquoted):
        try:
            return int(unquoted)
        except ValueError:
            return unquoted
    if re.fullmatch(r"-?(\d+(\.\d*)?|\.\d+)([Ee][+-]?\d+)?", unquoted):
        try:
            return float(unquoted)
        except ValueError:
            return unquoted
    return unquoted


def ref_ids(arg: str) -> list[int]:
    return [int(x[1:]) for x in re.findall(r"#\d+", arg)]


def sanitize_filename(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", name)
    safe = re.sub(r"\s+", "_", safe).strip("._ ")
    return safe or "UNKNOWN"


def display_storey(storey: str | None) -> str:
    if not storey or storey == "__NO_STOREY__":
        return "층 미지정"
    return storey


def parse_family_type(object_type: str | None, name: str | None) -> tuple[str | None, str | None]:
    source = object_type or name or ""
    if not source:
        return None, None
    parts = source.split(":")
    if len(parts) >= 2:
        return parts[0] or None, parts[-1] or None
    return None, source


def module_kind(workset: str | None) -> str:
    if not workset:
        return "missing_workset"
    if MODULE_RE.match(workset):
        return "module"
    if workset.startswith("현장공사분-") or workset == "분리발주공사":
        return "site_work"
    return "non_module_workset"


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def fmt_num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return "-"
        return f"{value:,.{digits}f}".rstrip("0").rstrip(".")
    return str(value)


def table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(x).replace("\n", " ") for x in row) + " |")
    return "\n".join(lines)


def numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    return None


def element_dimensions(row: dict[str, Any]) -> dict[str, Any]:
    props = row.get("properties") or {}
    dimensions = dict((props.get("Dimensions(Type)") or {}))
    dimensions.update(props.get("Dimensions") or {})
    other = props.get("Other") or {}
    constraints = props.get("Constraints") or {}
    for key in ["Head Height", "Sill Height"]:
        if key in other:
            dimensions.setdefault(key, other[key])
        if key in constraints:
            dimensions.setdefault(key, constraints[key])
    for key, value in (row.get("quantities") or {}).items():
        dimensions.setdefault(key, value)
    type_name = row.get("type_name")
    if type_name and "Thickness" not in dimensions:
        match = TYPE_THICKNESS_RE.search(type_name)
        if match:
            dimensions["Thickness"] = float(match.group(1))
    return dimensions


def quantity_value(row: dict[str, Any], key: str) -> float | None:
    value = (row.get("quantities") or {}).get(key)
    numeric = numeric_value(value)
    if numeric is not None:
        return numeric
    return numeric_value(element_dimensions(row).get(key))


def clean_material_value(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text == "-":
        return None
    text = text.replace("#", "").replace(";", "").strip()
    return text or None


def material_values(row: dict[str, Any]) -> str:
    material_keys = {
        "material",
        "structural material",
        "finish",
        "riser material",
        "tread material",
        "재료",
    }
    values: list[str] = []
    for pset_name, props in (row.get("properties") or {}).items():
        pset_l = pset_name.lower()
        for key, value in (props or {}).items():
            key_l = key.lower()
            if (
                "materials and finishes" in pset_l
                or "material" in key_l
                or "finish" in key_l
                or key_l in material_keys
                or key == "재료"
            ):
                cleaned = clean_material_value(value)
                if cleaned and cleaned not in values:
                    values.append(cleaned)
    return ", ".join(values) if values else "-"


def is_concrete_related(row: dict[str, Any]) -> bool:
    texts = [
        row.get("type_name") or "",
        row.get("family") or "",
        row.get("object_type") or "",
        material_values(row),
    ]
    haystack = " ".join(texts).lower()
    return "콘크리트" in haystack or "concrete" in haystack


NOISY_DIMENSION_KEYS = {
        "Area",
        "Volume",
        "Elevation at Bottom",
        "Elevation at Top",
        "Elevation at Bottom Core",
        "Elevation at Top Core",
        "Weight",
        "Base Offset",
        "Top Offset",
        "Base Extension Distance",
        "Top Extension Distance",
        "Reference Level Elevation",
        "Offset from Host",
    }


DIMENSION_PRIORITY_BY_CATEGORY = {
    "Wall": ["Length", "Thickness", "Unconnected Height", "Height", "Area"],
    "Door": ["Width", "Height", "Head Height", "Sill Height", "문틀 두께", "문틀 두께 2", "문틀 폭", "문 패널 두께", "문 패널 폭", "하부 문틀", "A", "B", "H1", "Area"],
    "Window": ["Head Height", "Sill Height", "A", "B", "H1", "문틀 두께", "문짝 두께", "유리 두께", "유리 두께1", "유리 두께2", "Area"],
    "Slab": ["Area", "Thickness", "Perimeter", "Length", "Width"],
    "Beam": ["Length", "Cut Length", "Width", "Height"],
    "Column": ["Length", "Cut Length", "Width", "Height"],
    "Proxy": ["Length", "Width", "Height", "Thickness", "길이", "폭", "Area"],
    "Covering": ["Area", "Perimeter", "Thickness", "Length", "Width"],
    "Furnishing": ["Length", "Width", "Height", "Area"],
}

DIMENSION_PRIORITY_BY_CATEGORY["Window"] = ["Width", "Height", "Head Height", "Sill Height", "문틀 두께", "문짝 두께", "유리 두께", "유리 두께1", "유리 두께2", "문틀 폭", "A", "B", "H1", "Area"]


DIMENSION_LABELS_BY_CATEGORY = {
    "Door": {
        "Width": "폭",
        "Height": "높이",
        "Head Height": "상단 높이",
        "Sill Height": "하단 높이",
    },
    "Window": {
        "Width": "폭",
        "Height": "높이",
        "Head Height": "상단 높이",
        "Sill Height": "창대 높이",
    },
}


def dimension_label(category: str, key: str) -> str:
    return DIMENSION_LABELS_BY_CATEGORY.get(category, {}).get(key, key)


def dimension_values(row: dict[str, Any]) -> str:
    parts = []
    dimensions = element_dimensions(row)
    category = row.get("category") or ""
    priority = DIMENSION_PRIORITY_BY_CATEGORY.get(category, ["Length", "Width", "Height", "Thickness", "Area"])
    ordered_keys = priority + sorted(k for k in dimensions if k not in priority)
    for key in ordered_keys:
        if key in NOISY_DIMENSION_KEYS:
            continue
        value = dimensions.get(key)
        label = dimension_label(category, key)
        numeric = numeric_value(value)
        if numeric is not None:
            parts.append(f"{label}={fmt_num(numeric)}")
        elif value not in {None, "", "-"} and not isinstance(value, bool):
            parts.append(f"{label}={value}")
        if len(parts) >= 8:
            break
    return ", ".join(parts) if parts else "-"


def dependency_values(row: dict[str, Any]) -> str:
    dependency = row.get("dependency") or {}
    if dependency:
        parts = []
        host_id = dependency.get("host_revit_element_id")
        host = dependency.get("host")
        if host_id:
            parts.append(f"Host Id={host_id}")
        if dependency.get("host_category_label") or dependency.get("host_family") or dependency.get("host_type_name"):
            label = " / ".join(
                x
                for x in [
                    dependency.get("host_category_label"),
                    dependency.get("host_family"),
                    dependency.get("host_type_name"),
                ]
                if x
            )
            if label:
                parts.append(label)
        elif host:
            parts.append(f"Host={host}")
        return ", ".join(parts) if parts else "-"

    other = (row.get("properties") or {}).get("Other") or {}
    constraints = (row.get("properties") or {}).get("Constraints") or {}
    host_id = other.get("Host Id")
    host = constraints.get("Host")
    if host_id:
        return f"Host Id={host_id}"
    if host:
        return f"Host={host}"
    return "-"


def quantity_type_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        type_name = row.get("type_name") or "__NO_TYPE__"
        grouped[(row["category_label"], type_name)].append(row)

    output = []
    for (category_label, type_name), type_rows in grouped.items():
        length_sum = sum(quantity_value(row, "Length") or 0 for row in type_rows)
        area_sum = sum(quantity_value(row, "Area") or 0 for row in type_rows)
        concrete_rows = [row for row in type_rows if is_concrete_related(row)]
        volume_sum = sum(quantity_value(row, "Volume") or 0 for row in concrete_rows)
        sample_sizes = []
        sample_materials = []
        sample_dependencies = []
        for row in type_rows:
            values = dimension_values(row)
            if values != "-" and values not in sample_sizes:
                sample_sizes.append(values)
            materials = material_values(row)
            if materials != "-" and materials not in sample_materials:
                sample_materials.append(materials)
            dependencies = dependency_values(row)
            if dependencies != "-" and dependencies not in sample_dependencies:
                sample_dependencies.append(dependencies)
            if len(sample_sizes) >= 3:
                # Keep scanning until we also have representative material/host samples.
                if len(sample_materials) >= 3 and len(sample_dependencies) >= 3:
                    break
        output.append(
            [
                category_label,
                type_name,
                len(type_rows),
                fmt_num(length_sum) if length_sum else "-",
                fmt_num(area_sum) if area_sum else "-",
                fmt_num(volume_sum) if volume_sum else "-",
                "; ".join(sample_materials[:3]) if sample_materials else "-",
                "; ".join(sample_sizes[:3]) if sample_sizes else "-",
                "; ".join(sample_dependencies[:3]) if sample_dependencies else "-",
            ]
        )
    return sorted(output, key=lambda row: (-int(row[2]), str(row[0]), str(row[1])))


def quantity_type_rows_limited(rows: list[dict[str, Any]], limit: int = 20) -> list[list[Any]]:
    return quantity_type_rows(rows)[:limit]


def element_quantity_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    output = []
    for row in sorted(rows, key=lambda x: (x["category"], x.get("type_name") or "", x.get("revit_element_id") or "")):
        output.append(
            [
                row["category_label"],
                row.get("type_name") or "-",
                display_storey(row.get("storey")),
                row.get("revit_element_id") or "-",
                fmt_num(quantity_value(row, "Length")),
                fmt_num(quantity_value(row, "Area")),
                fmt_num(quantity_value(row, "Volume")) if is_concrete_related(row) else "-",
                material_values(row),
                dimension_values(row),
                dependency_values(row),
            ]
        )
    return output


def is_user_module(element: dict[str, Any]) -> bool:
    return element["module_kind"] in {"module", "site_work"}


def build() -> None:
    if not SOURCE_IFC.exists():
        raise FileNotFoundError(SOURCE_IFC)

    if PACK_DIR.exists():
        shutil.rmtree(PACK_DIR)
    for directory in [JSONL_DIR, SUMMARY_DIR, USER_VIEWS_DIR, DOCS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)

    text = SOURCE_IFC.read_text(encoding="utf-8", errors="ignore")
    entities: dict[int, tuple[str, str]] = {}
    type_counts: Counter[str] = Counter()
    for line in text.splitlines():
        match = ENTITY_RE.match(line)
        if not match:
            continue
        eid = int(match.group(1))
        typ = match.group(2)
        body = match.group(3)
        entities[eid] = (typ, body)
        type_counts[typ] += 1

    properties: dict[int, dict[str, Any]] = {}
    psets: dict[int, dict[str, Any]] = {}
    products: dict[int, dict[str, Any]] = {}
    styles: dict[int, dict[str, Any]] = {}
    storeys: dict[int, dict[str, Any]] = {}
    rel_defines: list[tuple[list[int], int]] = []
    rel_contains: list[tuple[list[int], int]] = []

    for eid, (typ, body) in entities.items():
        args = split_args(body)
        if typ == "IFCPROPERTYSINGLEVALUE" and len(args) >= 3:
            name = unquote(args[0])
            if name in EXCLUDED_PROPERTY_NAMES:
                continue
            properties[eid] = {"name": name, "value": scalar_value(args[2])}
        elif typ == "IFCPROPERTYSET" and len(args) >= 5:
            psets[eid] = {
                "id": f"ifc:property_set:{eid}",
                "ifc_id": eid,
                "ifc_guid": unquote(args[0]),
                "name": unquote(args[2]),
                "property_refs": ref_ids(args[4]),
            }
        elif typ == "IFCRELDEFINESBYPROPERTIES" and len(args) >= 6:
            related = ref_ids(args[4])
            relating = ref_ids(args[5])
            if relating:
                rel_defines.append((related, relating[0]))
        elif typ == "IFCRELCONTAINEDINSPATIALSTRUCTURE" and len(args) >= 6:
            related = ref_ids(args[4])
            relating = ref_ids(args[5])
            if relating:
                rel_contains.append((related, relating[0]))
        elif typ == "IFCBUILDINGSTOREY" and len(args) >= 10:
            storeys[eid] = {
                "id": f"ifc:storey:{eid}",
                "ifc_id": eid,
                "ifc_guid": unquote(args[0]),
                "name": unquote(args[2]),
                "long_name": unquote(args[7]),
                "elevation": scalar_value(args[9]),
            }
        elif typ in ELEMENT_TYPES:
            products[eid] = {
                "ifc_id": eid,
                "ifc_guid": unquote(args[0]) if len(args) > 0 else None,
                "ifc_type": typ,
                "name": unquote(args[2]) if len(args) > 2 else None,
                "object_type": unquote(args[4]) if len(args) > 4 else None,
                "revit_element_id": unquote(args[7]) if len(args) > 7 else None,
            }
        elif typ in STYLE_TYPES and len(args) >= 8:
            styles[eid] = {
                "ifc_id": eid,
                "ifc_guid": unquote(args[0]) if len(args) > 0 else None,
                "ifc_type": typ,
                "name": unquote(args[2]) if len(args) > 2 else None,
                "property_refs": ref_ids(args[5]) if len(args) > 5 else [],
                "revit_type_id": unquote(args[7]) if len(args) > 7 else None,
            }

    property_set_rows: list[dict[str, Any]] = []
    product_props: dict[int, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    rels_by_psid: dict[int, list[int]] = defaultdict(list)
    for related, rel_psid in rel_defines:
        for product_id in related:
            if product_id in products:
                rels_by_psid[rel_psid].append(product_id)
    for psid, ps in psets.items():
        props: dict[str, Any] = {}
        for pref in ps["property_refs"]:
            prop = properties.get(pref)
            if prop and prop["name"] not in EXCLUDED_PROPERTY_NAMES:
                props[prop["name"]] = prop["value"]
        property_set_rows.append(
            {
                "id": ps["id"],
                "ifc_id": psid,
                "ifc_guid": ps["ifc_guid"],
                "name": ps["name"],
                "properties": props,
            }
        )
        for product_id in rels_by_psid.get(psid, []):
            product_props[product_id][ps["name"]].update(props)

    style_props_by_type_id: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    for style in styles.values():
        type_id = style.get("revit_type_id")
        if not type_id:
            continue
        for psid in style.get("property_refs") or []:
            ps = psets.get(psid)
            if not ps:
                continue
            props: dict[str, Any] = {}
            for pref in ps["property_refs"]:
                prop = properties.get(pref)
                if prop and prop["name"] not in EXCLUDED_PROPERTY_NAMES:
                    props[prop["name"]] = prop["value"]
            if not props:
                continue
            name = ps["name"]
            if name == "Dimensions":
                name = "Dimensions(Type)"
            elif name in {"Other", "Construction", "Identity Data", "Analytical Properties"}:
                name = f"{name}(Type)"
            style_props_by_type_id[str(type_id)][name].update(props)

    product_to_storey: dict[int, dict[str, Any]] = {}
    for related, storey_id in rel_contains:
        storey = storeys.get(storey_id)
        for product_id in related:
            if product_id in products and storey:
                product_to_storey[product_id] = storey

    elements: list[dict[str, Any]] = []
    audit_counter: Counter[str] = Counter()
    workset_counter: Counter[str] = Counter()
    for eid, product in products.items():
        psets_for_product = {name: dict(values) for name, values in product_props.get(eid, {}).items()}
        type_id = (psets_for_product.get("Other") or {}).get("Type Id")
        if type_id is not None:
            for name, values in style_props_by_type_id.get(str(type_id), {}).items():
                psets_for_product.setdefault(name, {}).update(values)
        identity = psets_for_product.get("Identity Data", {})
        workset = identity.get("Workset")
        kind = module_kind(str(workset) if workset is not None else None)
        module_id = str(workset) if workset else "__UNASSIGNED__"
        family, type_name = parse_family_type(product.get("object_type"), product.get("name"))
        category = CATEGORY_BY_IFC_TYPE.get(product["ifc_type"], "Other")

        properties_out: dict[str, dict[str, Any]] = {}
        quantities: dict[str, Any] = {}
        for pset_name, kv in psets_for_product.items():
            clean = {k: v for k, v in kv.items() if k not in EXCLUDED_PROPERTY_NAMES}
            if not clean:
                continue
            properties_out[pset_name] = clean
            if pset_name in {"Dimensions", "Structural", "Pset_QuantityTakeOff"}:
                for key, value in clean.items():
                    lk = key.lower()
                    if lk in {"length", "area", "volume", "width", "height", "weight", "cut length"}:
                        quantities[key] = value

        storey = product_to_storey.get(eid)
        element = {
            "id": f"ifc:element:{eid}",
            "ifc_id": eid,
            "ifc_guid": product.get("ifc_guid"),
            "revit_element_id": product.get("revit_element_id"),
            "ifc_type": product["ifc_type"],
            "category": category,
            "category_label": DISPLAY_CATEGORY.get(category, category),
            "name": product.get("name"),
            "family": family,
            "type_name": type_name,
            "object_type": product.get("object_type"),
            "storey": storey["name"] if storey else None,
            "storey_id": storey["id"] if storey else None,
            "module_id": module_id,
            "module_source": "Workset" if workset else "missing_workset",
            "module_kind": kind,
            "properties": properties_out,
            "quantities": quantities,
        }
        elements.append(element)
        audit_counter[kind] += 1
        workset_counter[module_id] += 1

    elements_by_revit_id = {str(e.get("revit_element_id")): e for e in elements if e.get("revit_element_id")}
    for element in elements:
        other = (element.get("properties") or {}).get("Other") or {}
        constraints = (element.get("properties") or {}).get("Constraints") or {}
        host_id = other.get("Host Id")
        host = constraints.get("Host")
        dependency: dict[str, Any] = {}
        if host_id not in {None, "", "-"}:
            dependency["host_revit_element_id"] = str(host_id)
            host_element = elements_by_revit_id.get(str(host_id))
            if host_element:
                dependency["host_category"] = host_element.get("category")
                dependency["host_category_label"] = host_element.get("category_label")
                dependency["host_family"] = host_element.get("family")
                dependency["host_type_name"] = host_element.get("type_name")
        if host not in {None, "", "-"}:
            dependency["host"] = host
        if dependency:
            element["dependency"] = dependency

    elements.sort(key=lambda e: (e["module_kind"], e["module_id"], e.get("storey") or "", e["category"], e.get("type_name") or "", e["ifc_id"]))
    user_elements = [e for e in elements if is_user_module(e)]

    edges: list[dict[str, str]] = []
    for e in user_elements:
        edges.append({"from": f"ifc:project:yeoju-ar", "relation": "has_module", "to": f"ifc:module:{e['module_id']}"})
        if e.get("storey"):
            edges.append({"from": f"ifc:module:{e['module_id']}", "relation": "has_storey", "to": f"ifc:storey_name:{e['storey']}"})
            edges.append({"from": f"ifc:storey_name:{e['storey']}", "relation": "has_element", "to": e["id"]})
        edges.append({"from": f"ifc:module:{e['module_id']}", "relation": "has_element", "to": e["id"]})
        edges.append({"from": e["id"], "relation": "has_category", "to": f"ifc:category:{e['category']}"})
        if e.get("type_name"):
            edges.append({"from": e["id"], "relation": "has_type", "to": f"ifc:type:{e['type_name']}"})

    # Deduplicate edges while preserving order.
    seen_edges: set[tuple[str, str, str]] = set()
    deduped_edges: list[dict[str, str]] = []
    for edge in edges:
        key = (edge["from"], edge["relation"], edge["to"])
        if key not in seen_edges:
            seen_edges.add(key)
            deduped_edges.append(edge)

    write_jsonl(JSONL_DIR / "elements.jsonl", elements)
    write_jsonl(JSONL_DIR / "property_sets.jsonl", property_set_rows)
    write_jsonl(JSONL_DIR / "edges.jsonl", deduped_edges)
    write_jsonl(JSONL_DIR / "storeys.jsonl", list(storeys.values()))

    module_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    storey_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    category_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    type_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in user_elements:
        module_groups[e["module_id"]].append(e)
        storey_groups[e.get("storey") or "__NO_STOREY__"].append(e)
        category_groups[e["category"]].append(e)
        type_groups[e.get("type_name") or "__NO_TYPE__"].append(e)

    def summarize_group(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": key,
            "element_count": len(rows),
            "storey_count": len({r.get("storey") for r in rows if r.get("storey")}),
            "category_count": len({r["category"] for r in rows}),
            "type_count": len({r.get("type_name") for r in rows if r.get("type_name")}),
            "module_count": len({r["module_id"] for r in rows}),
            "door_count": sum(1 for r in rows if r["category"] == "Door"),
            "window_count": sum(1 for r in rows if r["category"] == "Window"),
            "wall_count": sum(1 for r in rows if r["category"] == "Wall"),
            "slab_count": sum(1 for r in rows if r["category"] == "Slab"),
            "beam_count": sum(1 for r in rows if r["category"] == "Beam"),
            "column_count": sum(1 for r in rows if r["category"] == "Column"),
            "by_storey": dict(Counter(r.get("storey") or "__NO_STOREY__" for r in rows).most_common()),
            "by_category": dict(Counter(r["category"] for r in rows).most_common()),
            "by_type": dict(Counter(r.get("type_name") or "__NO_TYPE__" for r in rows).most_common()),
            "revit_element_ids": [r["revit_element_id"] for r in rows if r.get("revit_element_id")],
        }

    module_rows: list[dict[str, Any]] = []
    for module_id, rows in module_groups.items():
        summary = summarize_group(module_id, rows)
        summary.update({"module_id": module_id, "module_kind": rows[0]["module_kind"]})
        module_rows.append(summary)
    module_rows.sort(key=lambda r: (r["module_kind"], r["module_id"]))

    storey_rows = [
        {"storey": key, **summarize_group(key, rows)}
        for key, rows in sorted(storey_groups.items())
    ]
    category_rows = [
        {"category": key, "category_label": DISPLAY_CATEGORY.get(key, key), **summarize_group(key, rows)}
        for key, rows in sorted(category_groups.items())
    ]
    type_rows = [
        {"type_name": key, **summarize_group(key, rows)}
        for key, rows in sorted(type_groups.items())
    ]
    module_category_rows = []
    for module_id, rows in module_groups.items():
        by_cat = defaultdict(list)
        for e in rows:
            by_cat[e["category"]].append(e)
        for category, cat_rows in by_cat.items():
            module_category_rows.append(
                {
                    "module_id": module_id,
                    "module_kind": rows[0]["module_kind"],
                    "category": category,
                    "category_label": DISPLAY_CATEGORY.get(category, category),
                    "element_count": len(cat_rows),
                    "by_type": dict(Counter(r.get("type_name") or "__NO_TYPE__" for r in cat_rows).most_common()),
                }
            )

    write_jsonl(JSONL_DIR / "modules.jsonl", module_rows)
    write_jsonl(JSONL_DIR / "categories.jsonl", category_rows)
    write_jsonl(JSONL_DIR / "types.jsonl", type_rows)

    model_summary = {
        "source_ifc": SOURCE_IFC.name,
        "file_size_bytes": SOURCE_IFC.stat().st_size,
        "total_element_count": len(elements),
        "user_element_count": len(user_elements),
        "workset_present_count": sum(1 for e in elements if e["module_kind"] != "missing_workset"),
        "workset_missing_count": audit_counter["missing_workset"],
        "module_count": sum(1 for r in module_rows if r["module_kind"] == "module"),
        "site_work_count": sum(1 for r in module_rows if r["module_kind"] == "site_work"),
        "non_module_workset_count": audit_counter["non_module_workset"],
        "by_module": {r["module_id"]: r["element_count"] for r in module_rows},
        "by_storey": dict(Counter(e.get("storey") or "__NO_STOREY__" for e in user_elements).most_common()),
        "by_category": dict(Counter(e["category"] for e in user_elements).most_common()),
        "by_type": dict(Counter(e.get("type_name") or "__NO_TYPE__" for e in user_elements).most_common()),
        "raw_ifc_entity_counts_top": dict(type_counts.most_common(50)),
    }
    write_json(SUMMARY_DIR / "model_summary.json", model_summary)
    write_jsonl(SUMMARY_DIR / "module_summary.jsonl", module_rows)
    write_jsonl(SUMMARY_DIR / "storey_summary.jsonl", storey_rows)
    write_jsonl(SUMMARY_DIR / "category_summary.jsonl", category_rows)
    write_jsonl(SUMMARY_DIR / "type_summary.jsonl", type_rows)
    write_jsonl(SUMMARY_DIR / "module_category_summary.jsonl", module_category_rows)

    audit_rows = []
    for workset, count in workset_counter.most_common():
        kind = module_kind(None if workset == "__UNASSIGNED__" else workset)
        if kind == "non_module_workset":
            audit_rows.append({"workset": workset, "module_kind": kind, "element_count": count})
    write_jsonl(SUMMARY_DIR / "workset_audit.jsonl", audit_rows)

    def compact_elements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "category": r["category"],
                "category_label": r["category_label"],
                "storey": r.get("storey"),
                "type_name": r.get("type_name"),
                "revit_element_id": r.get("revit_element_id"),
                "ifc_guid": r.get("ifc_guid"),
                "name": r.get("name"),
            }
            for r in rows
        ]

    module_views = []
    for module_id, rows in sorted(module_groups.items()):
        counts = Counter(r["category"] for r in rows)
        module_views.append(
            {
                "module_id": module_id,
                "module_kind": rows[0]["module_kind"],
                "summary": f"{module_id} Workset 모듈은 총 {len(rows)}개 객체로 구성됩니다.",
                "storey_counts": dict(Counter(r.get("storey") or "__NO_STOREY__" for r in rows).most_common()),
                "category_counts": dict(counts.most_common()),
                "type_counts": dict(Counter(r.get("type_name") or "__NO_TYPE__" for r in rows).most_common()),
                "elements": compact_elements(rows),
            }
        )
    write_jsonl(USER_VIEWS_DIR / "module_user_view.jsonl", module_views)
    write_jsonl(USER_VIEWS_DIR / "storey_user_view.jsonl", [{"storey": k, "elements": compact_elements(v)} for k, v in sorted(storey_groups.items())])
    write_jsonl(USER_VIEWS_DIR / "category_user_view.jsonl", [{"category": k, "elements": compact_elements(v)} for k, v in sorted(category_groups.items())])
    write_jsonl(USER_VIEWS_DIR / "type_user_view.jsonl", [{"type_name": k, "elements": compact_elements(v)} for k, v in sorted(type_groups.items())])
    write_json(
        USER_VIEWS_DIR / "query_aliases.json",
        {
            "창호": ["Window", "Door"],
            "창": ["Window"],
            "문": ["Door"],
            "벽": ["Wall"],
            "벽체": ["Wall"],
            "바닥": ["Slab"],
            "슬래브": ["Slab"],
            "보": ["Beam"],
            "기둥": ["Column"],
            "모듈": ["Workset"],
        },
    )

    write_documents(module_groups, storey_groups, category_groups, type_rows, model_summary, audit_rows)
    write_manifest(module_rows, model_summary)
    write_readme()


def write_documents(
    module_groups: dict[str, list[dict[str, Any]]],
    storey_groups: dict[str, list[dict[str, Any]]],
    category_groups: dict[str, list[dict[str, Any]]],
    type_rows: list[dict[str, Any]],
    model_summary: dict[str, Any],
    audit_rows: list[dict[str, Any]],
) -> None:
    for sub in ["indexes", "modules", "storeys", "categories", "quantities", "quantities/modules"]:
        (DOCS_DIR / sub).mkdir(parents=True, exist_ok=True)

    module_rows = sorted(
        [
            (module_id, rows[0]["module_kind"], len(rows), Counter(r["category"] for r in rows), Counter(r.get("storey") or "__NO_STOREY__" for r in rows))
            for module_id, rows in module_groups.items()
        ],
        key=lambda x: (x[1], x[0]),
    )

    overview = [
        "# Revit IFC Workset Module Pack",
        "",
        "이 팩은 Revit IFC에서 추출한 Workset 기준 모듈 evidence입니다.",
        "",
        "## Model Summary",
        "",
        table(
            ["항목", "값"],
            [
                ["전체 IFC element 수", model_summary["total_element_count"]],
                ["사용자 모듈/현장공사 element 수", model_summary["user_element_count"]],
                ["Workset 누락 element 수", model_summary["workset_missing_count"]],
                ["모듈 Workset 수", model_summary["module_count"]],
                ["현장공사 Workset 수", model_summary["site_work_count"]],
                ["제외 Workset element 수", model_summary["non_module_workset_count"]],
            ],
        ),
        "",
        "## Category Counts",
        "",
        table(["카테고리", "수량"], [[DISPLAY_CATEGORY.get(k, k), v] for k, v in model_summary["by_category"].items()]),
    ]
    (DOCS_DIR / "model_overview.md").write_text("\n".join(overview), encoding="utf-8")

    module_list_rows = []
    site_rows = []
    for module_id, kind, count, by_category, by_storey in module_rows:
        row = [
            module_id,
            count,
            ", ".join(f"{DISPLAY_CATEGORY.get(k, k)} {v}" for k, v in by_category.most_common()),
            ", ".join(f"{display_storey(k)} {v}" for k, v in by_storey.most_common()),
        ]
        if kind == "module":
            module_list_rows.append(row)
        elif kind == "site_work":
            site_rows.append(row)
    module_doc = [
        "# Workset Module List",
        "",
        "## Modules",
        "",
        table(["모듈", "객체 수", "카테고리", "층"], module_list_rows),
        "",
        "## Site Work",
        "",
        table(["Workset", "객체 수", "카테고리", "층"], site_rows),
    ]
    (DOCS_DIR / "indexes" / "module_list.md").write_text("\n".join(module_doc), encoding="utf-8")

    (DOCS_DIR / "indexes" / "storey_list.md").write_text(
        "\n".join(
            [
                "# Storey List",
                "",
                table(
                    ["층", "객체 수", "모듈 수"],
                    [
                        [display_storey(storey), len(rows), len({r["module_id"] for r in rows})]
                        for storey, rows in sorted(storey_groups.items())
                    ],
                ),
            ]
        ),
        encoding="utf-8",
    )
    (DOCS_DIR / "indexes" / "category_list.md").write_text(
        "\n".join(
            [
                "# Category List",
                "",
                table(
                    ["카테고리", "객체 수", "모듈 수", "타입 수"],
                    [
                        [DISPLAY_CATEGORY.get(cat, cat), len(rows), len({r["module_id"] for r in rows}), len({r.get("type_name") for r in rows if r.get("type_name")})]
                        for cat, rows in sorted(category_groups.items())
                    ],
                ),
            ]
        ),
        encoding="utf-8",
    )
    (DOCS_DIR / "indexes" / "type_count_index.md").write_text(
        "\n".join(
            [
                "# Type Count Index",
                "",
                table(
                    ["타입", "객체 수", "카테고리 수", "모듈 수"],
                    [
                        [r["type_name"], r["element_count"], r["category_count"], r["module_count"]]
                        for r in sorted(type_rows, key=lambda x: x["element_count"], reverse=True)[:250]
                    ],
                ),
            ]
        ),
        encoding="utf-8",
    )
    module_category_rows = []
    for module_id, rows in module_groups.items():
        for category, count in Counter(r["category"] for r in rows).most_common():
            module_category_rows.append([module_id, DISPLAY_CATEGORY.get(category, category), count])
    (DOCS_DIR / "indexes" / "module_category_index.md").write_text(
        "\n".join(["# Module Category Index", "", table(["모듈", "카테고리", "수량"], module_category_rows)]),
        encoding="utf-8",
    )

    for module_id, rows in module_groups.items():
        if rows[0]["module_kind"] not in {"module", "site_work"}:
            continue
        lines = [
            f"# Module {module_id}",
            "",
            f"Workset 기준 `{module_id}` 구성 객체입니다.",
            "",
            "## Summary",
            "",
            table(
                ["항목", "값"],
                [
                    ["객체 수", len(rows)],
                    ["층 수", len({r.get("storey") for r in rows if r.get("storey")})],
                    ["카테고리 수", len({r["category"] for r in rows})],
                    ["타입 수", len({r.get("type_name") for r in rows if r.get("type_name")})],
                ],
            ),
            "",
            "## Storey Counts",
            "",
            table(["층", "수량"], [[display_storey(k), v] for k, v in Counter(r.get("storey") or "__NO_STOREY__" for r in rows).most_common()]),
            "",
            "## Category Counts",
            "",
            table(["카테고리", "수량"], [[DISPLAY_CATEGORY.get(k, k), v] for k, v in Counter(r["category"] for r in rows).most_common()]),
            "",
            "## Type Counts",
            "",
            table(["타입", "카테고리", "수량"], [[t, ", ".join(sorted({r["category_label"] for r in rows if (r.get("type_name") or "__NO_TYPE__") == t})), c] for t, c in Counter(r.get("type_name") or "__NO_TYPE__" for r in rows).most_common()]),
            "",
            "## Quantity Summary By Type",
            "",
            "길이, 면적, 부피, 크기 값은 IFC/Revit quantity와 Dimensions property에서 추출한 사용자 질의용 evidence입니다.",
            f"상위 20개 타입만 표시합니다. 전체 수량 evidence는 `documents/quantities/modules/{sanitize_filename(module_id)}.md`에 있습니다.",
            "",
            table(["카테고리", "타입", "수량", "길이 합계(mm)", "면적 합계(m²)", "콘크리트 부피 합계(m³)", "재료", "크기/치수 값", "종속 정보"], quantity_type_rows_limited(rows)),
            "",
            "## Element List",
            "",
            table(
                ["카테고리", "층", "타입", "Revit ElementId", "IFC GUID"],
                [
                    [r["category_label"], r.get("storey") or "-", r.get("type_name") or "-", r.get("revit_element_id") or "-", r.get("ifc_guid") or "-"]
                    for r in sorted(rows, key=lambda x: (x["category"], x.get("type_name") or "", x.get("revit_element_id") or ""))
                ],
            ),
        ]
        (DOCS_DIR / "modules" / f"{sanitize_filename(module_id)}.md").write_text("\n".join(lines), encoding="utf-8")

        quantity_lines = [
            f"# Module {module_id} Quantity Evidence",
            "",
            f"Workset `{module_id}`의 길이, 면적, 부피, 크기/치수 검색용 evidence입니다.",
            "",
            "## Type Quantity Summary",
            "",
            table(["카테고리", "타입", "수량", "길이 합계(mm)", "면적 합계(m²)", "콘크리트 부피 합계(m³)", "재료", "크기/치수 값", "종속 정보"], quantity_type_rows(rows)),
            "",
            "## Element Quantity Details",
            "",
            table(["카테고리", "타입", "층", "Revit ElementId", "길이(mm)", "면적(m²)", "콘크리트 부피(m³)", "재료", "크기/치수 값", "종속 정보"], element_quantity_rows(rows)),
        ]
        (DOCS_DIR / "quantities" / "modules" / f"{sanitize_filename(module_id)}.md").write_text("\n".join(quantity_lines), encoding="utf-8")

    for storey, rows in storey_groups.items():
        if storey == "__NO_STOREY__":
            continue
        lines = [
            f"# Storey {storey}",
            "",
            "## Module Counts",
            "",
            table(["모듈", "수량"], Counter(r["module_id"] for r in rows).most_common()),
            "",
            "## Category Counts",
            "",
            table(["카테고리", "수량"], [[DISPLAY_CATEGORY.get(k, k), v] for k, v in Counter(r["category"] for r in rows).most_common()]),
            "",
            "## Door And Window List",
            "",
            table(
                ["카테고리", "모듈", "타입", "Revit ElementId"],
                [
                    [r["category_label"], r["module_id"], r.get("type_name") or "-", r.get("revit_element_id") or "-"]
                    for r in rows
                    if r["category"] in {"Door", "Window"}
                ],
            ),
        ]
        (DOCS_DIR / "storeys" / f"{sanitize_filename(storey)}.md").write_text("\n".join(lines), encoding="utf-8")

    for category, file_name in DOCUMENT_CATEGORY_FILES.items():
        rows = category_groups.get(category, [])
        if not rows:
            continue
        lines = [
            f"# Category {DISPLAY_CATEGORY.get(category, category)}",
            "",
            "## Module Counts",
            "",
            table(["모듈", "수량"], Counter(r["module_id"] for r in rows).most_common()),
            "",
            "## Storey Counts",
            "",
            table(["층", "수량"], [[display_storey(k), v] for k, v in Counter(r.get("storey") or "__NO_STOREY__" for r in rows).most_common()]),
            "",
            "## Type Counts",
            "",
            table(["타입", "수량"], Counter(r.get("type_name") or "__NO_TYPE__" for r in rows).most_common()),
            "",
            "## Element List",
            "",
            table(
                ["모듈", "층", "타입", "Revit ElementId", "IFC GUID"],
                [
                    [r["module_id"], r.get("storey") or "-", r.get("type_name") or "-", r.get("revit_element_id") or "-", r.get("ifc_guid") or "-"]
                    for r in sorted(rows, key=lambda x: (x["module_id"], x.get("storey") or "", x.get("type_name") or ""))
                ],
            ),
        ]
        (DOCS_DIR / "categories" / file_name).write_text("\n".join(lines), encoding="utf-8")


def write_manifest(module_rows: list[dict[str, Any]], model_summary: dict[str, Any]) -> None:
    manifest = {
        "format": "opencrab-data-zip-readable-documents",
        "pack_id": "revit-yeoju-ar-ifc-workset-module-pack",
        "title": "Yeoju Revit AR IFC Workset Module Pack",
        "version": "1.0.0",
        "primary_module_source": "Identity Data.Workset",
        "documents": {
            "model_overview": "documents/model_overview.md",
            "modules": "documents/modules/",
            "quantity_modules": "documents/quantities/modules/",
            "storeys": "documents/storeys/",
            "categories": "documents/categories/",
            "indexes": "documents/indexes/",
        },
        "backdata": {
            "jsonl": "backdata/jsonl/",
            "summary": "backdata/summary/",
            "user_views": "backdata/user_views/",
        },
        "counts": {
            "module_count": model_summary["module_count"],
            "site_work_count": model_summary["site_work_count"],
            "element_count": model_summary["user_element_count"],
            "workset_missing_count": model_summary["workset_missing_count"],
        },
    }
    write_json(PACK_DIR / "manifest.json", manifest)


def write_readme() -> None:
    content = """# Yeoju Revit AR IFC Workset Module Pack

This pack converts a Revit IFC export into OpenCrab-friendly Workset module evidence.

The authoritative module id is `Identity Data.Workset`. User-facing search evidence is in `documents/`; producer JSONL, summaries, and user views are retained under `backdata/`.
"""
    (PACK_DIR / "README.md").write_text(content, encoding="utf-8")


if __name__ == "__main__":
    build()
    print(f"Built {PACK_DIR}")
