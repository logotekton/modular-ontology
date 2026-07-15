from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Sequence


MISSING = object()
HEAVY_FIELDS = {
    "bim_references",
    "content",
    "embedding",
    "formula",
    "formula_details",
    "formula_source",
    "raw",
    "raw_json",
    "raw_text",
    "source_text",
}
DEFAULT_NODE_FIELDS = [
    "id",
    "label",
    "type",
    "module_id",
    "module_type",
    "workset_name",
    "category",
    "class",
    "family_name",
    "family_and_type",
    "type_name",
    "work_category",
    "item_name",
    "specification",
    "quantity",
    "unit",
    "normalized_unit",
    "source_sheet",
    "source_row",
    "source_element_id",
    "ifc_guid",
]


def normalize_dict(value: dict | None) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_list(value: Sequence | None) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(value)


def node_field(node: dict, field: str) -> object:
    if field in node:
        return node.get(field)
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return MISSING
    if field.startswith("properties."):
        field = field.split(".", 1)[1]
    current: object = properties
    for part in field.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def node_has_field(node: dict, field: str) -> bool:
    return node_field(node, field) is not MISSING


def is_heavy_field(field: str) -> bool:
    return field.split(".", 1)[-1] in HEAVY_FIELDS


def to_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).strip().replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def loose_equal(left: object, right: object) -> bool:
    left_number = to_number(left)
    right_number = to_number(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return left == right or str(left) == str(right)


def match_one(value: object, operator: str, expected: object, *, exists: bool) -> bool:
    if operator == "exists":
        return exists if bool(expected) else not exists
    if not exists:
        return operator == "ne" and expected is not None
    if operator == "eq":
        return loose_equal(value, expected)
    if operator == "ne":
        return not loose_equal(value, expected)
    if operator in {"in", "not_in"}:
        candidates = expected if isinstance(expected, Sequence) and not isinstance(expected, str) else [expected]
        matched = any(loose_equal(value, item) for item in normalize_list(candidates))
        return matched if operator == "in" else not matched
    if operator == "contains":
        return str(expected).casefold() in str(value).casefold()
    if operator == "startswith":
        return str(value).casefold().startswith(str(expected).casefold())
    if operator == "endswith":
        return str(value).casefold().endswith(str(expected).casefold())
    if operator == "regex":
        try:
            return bool(re.search(str(expected), str(value)))
        except re.error:
            return False
    if operator in {"gt", "gte", "lt", "lte"}:
        left_number = to_number(value)
        right_number = to_number(expected)
        if left_number is None or right_number is None:
            return False
        if operator == "gt":
            return left_number > right_number
        if operator == "gte":
            return left_number >= right_number
        if operator == "lt":
            return left_number < right_number
        return left_number <= right_number
    if operator == "wildcard":
        return fnmatch.fnmatchcase(str(value), str(expected))
    return False


def matches_where(node: dict, where: dict | None) -> bool:
    for field, condition in normalize_dict(where).items():
        value = node_field(node, str(field))
        exists = value is not MISSING
        if isinstance(condition, dict):
            if condition and not all(
                match_one(value, str(operator), expected, exists=exists)
                for operator, expected in condition.items()
            ):
                return False
        elif not match_one(value, "eq", condition, exists=exists):
            return False
    return True


def natural_key(value: object) -> tuple:
    if value is None or value is MISSING:
        return (1, "")
    parts = re.split(r"(\d+)", str(value))
    key: list[tuple[int, object]] = []
    for part in parts:
        if not part:
            continue
        key.append((0, int(part)) if part.isdigit() else (1, part.casefold()))
    return (0, tuple(key))


def hashable_value(value: object) -> object:
    if value is MISSING:
        return None
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def sort_value_key(value: object) -> tuple:
    if value is None or value is MISSING:
        return (1, 0, "")
    number = to_number(value)
    if number is not None:
        return (0, 0, number)
    return (0, 1, str(value).casefold())


def sort_rows(rows: list[dict], order_by: Sequence | None) -> list[dict]:
    specs = normalize_list(order_by)
    if not specs:
        return rows
    ordered = list(rows)
    for raw_spec in reversed(specs):
        spec = {"field": raw_spec} if isinstance(raw_spec, str) else raw_spec if isinstance(raw_spec, dict) else {}
        field = str(spec.get("field") or "")
        if not field:
            continue
        reverse = str(spec.get("direction", "asc")).lower() == "desc"
        natural = bool(spec.get("natural", False))
        ordered.sort(
            key=lambda row: natural_key(row.get(field)) if natural else sort_value_key(row.get(field)),
            reverse=reverse,
        )
    return ordered


def sort_values(values: list[object], order: str = "asc") -> list[object]:
    return sorted(values, key=natural_key, reverse=str(order).lower() == "desc")


def compact_sample_value(value: object) -> object:
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    if isinstance(value, dict):
        return {"type": "object", "keys": sorted(str(key) for key in value)[:12]}
    return value


def project_node(
    node: dict,
    fields: Sequence | None = None,
    *,
    include_heavy_fields: bool = False,
) -> dict:
    selected_fields = normalize_list(fields) or DEFAULT_NODE_FIELDS
    row: dict[str, object] = {}
    for field_obj in selected_fields:
        field = str(field_obj)
        if not include_heavy_fields and is_heavy_field(field):
            continue
        value = node_field(node, field)
        if value is not MISSING:
            row[field.split(".", 1)[-1]] = value
    return row
