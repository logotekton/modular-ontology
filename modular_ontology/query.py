"""Validated query primitives shared by graph nodes and evidence rows.

Compile once per request; field resolution belongs to the calling data source.
Numeric comparisons accept whole numeric strings, never digits inside an ID.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation


OPERATORS = frozenset({
    "eq", "ne", "in", "not_in", "contains", "startswith", "endswith",
    "regex", "gt", "gte", "lt", "lte", "exists", "wildcard",
})
_NUMBER = re.compile(r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
FieldGetter = Callable[[dict, str], object]
Predicate = Callable[[dict], bool]


def numeric_value(value: object) -> Decimal | None:
    """Return a finite, lossless number only for a numeric scalar/string."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _equal_to(expected: object) -> Callable[[object], bool]:
    expected_number = numeric_value(expected)

    def equal(value: object) -> bool:
        if isinstance(value, bool) or isinstance(expected, bool):
            return type(value) is type(expected) and value == expected
        if value is None or expected is None:
            return value is expected
        if expected_number is not None:
            value_number = numeric_value(value)
            if value_number is not None:
                return value_number == expected_number
        return value == expected

    return equal


def equal_values(value: object, expected: object) -> bool:
    return _equal_to(expected)(value)


def split_where_key(field: str) -> tuple[str, str | None]:
    if "__" in field:
        base, operator = field.rsplit("__", 1)
        if operator in OPERATORS:
            return base, operator
    return field, None


def _comparison(operator: str, expected: object, missing: object) -> Callable[[object], bool]:
    if operator not in OPERATORS:
        raise ValueError(f"Unknown where operator: {operator!r}")
    if operator == "exists":
        if not isinstance(expected, bool):
            raise ValueError("where exists requires a boolean")
        return lambda value: (value is not missing) == expected
    if operator in {"eq", "ne"}:
        equal = _equal_to(expected)
        if operator == "eq":
            return lambda value: value is not missing and equal(value)
        return lambda value: (expected is not None if value is missing else not equal(value))
    if operator in {"in", "not_in"}:
        candidates = expected if isinstance(expected, (list, tuple)) else [expected]
        comparisons = [_equal_to(candidate) for candidate in candidates]

        def membership(value: object) -> bool:
            if value is missing:
                return False
            found = any(equal(value) for equal in comparisons)
            return found if operator == "in" else not found

        return membership
    if operator in {"gt", "gte", "lt", "lte"}:
        expected_number = numeric_value(expected)
        if expected_number is None:
            raise ValueError(f"where {operator} requires a finite numeric value")

        def compare_number(value: object) -> bool:
            number = numeric_value(value)
            if number is None:
                return False
            if operator == "gt":
                return number > expected_number
            if operator == "gte":
                return number >= expected_number
            if operator == "lt":
                return number < expected_number
            return number <= expected_number

        return compare_number
    if operator == "regex":
        try:
            pattern = re.compile(str(expected))
        except re.error as exc:
            raise ValueError(f"Invalid where regex: {exc}") from exc
        return lambda value: value is not missing and bool(pattern.search(str(value)))
    if operator == "wildcard":
        pattern = re.compile(fnmatch.translate(str(expected)))
        return lambda value: value is not missing and bool(pattern.match(str(value)))
    expected_text = str(expected).casefold()
    if operator == "contains":
        return lambda value: value is not missing and expected_text in str(value).casefold()
    if operator == "startswith":
        return lambda value: value is not missing and str(value).casefold().startswith(expected_text)
    return lambda value: value is not missing and str(value).casefold().endswith(expected_text)


def compile_where(where: dict | None, getter: FieldGetter, missing: object) -> Predicate:
    """Compile implicit AND, suffix operators, and $and/$or/$not expressions.

    Validation covers all branches before scanning data, even for an empty pack.
    Empty $and is true; empty $or is false. Existing empty field objects match all.
    """
    clause_count = 0

    def compile_expression(expression: object, depth: int) -> Predicate:
        nonlocal clause_count
        if not isinstance(expression, dict):
            raise ValueError("where and its logical branches must be objects")
        if depth > 32:
            raise ValueError("where exceeds the maximum nesting depth of 32")
        predicates: list[Predicate] = []
        for raw_field, condition in expression.items():
            clause_count += 1
            if clause_count > 1024:
                raise ValueError("where exceeds the maximum of 1024 clauses")
            if not isinstance(raw_field, str) or not raw_field:
                raise ValueError("where field names must be non-empty strings")
            if raw_field in {"$and", "$or"}:
                if not isinstance(condition, list):
                    raise ValueError(f"{raw_field} requires a list of where objects")
                branches = [compile_expression(branch, depth + 1) for branch in condition]
                if raw_field == "$and":
                    predicates.append(lambda row, branches=branches: all(branch(row) for branch in branches))
                else:
                    predicates.append(lambda row, branches=branches: any(branch(row) for branch in branches))
                continue
            if raw_field == "$not":
                branch = compile_expression(condition, depth + 1)
                predicates.append(lambda row, branch=branch: not branch(row))
                continue
            if raw_field.startswith("$"):
                raise ValueError(f"Unknown where logical operator: {raw_field!r}")
            field, suffix = split_where_key(raw_field)
            if not field:
                raise ValueError("where operators require a field name")
            conditions = {suffix: condition} if suffix else condition if isinstance(condition, dict) else {"eq": condition}
            comparisons = [_comparison(operator, expected, missing) for operator, expected in conditions.items()]

            def matches_field(row: dict, field=field, comparisons=comparisons) -> bool:
                value = getter(row, field)
                return all(compare(value) for compare in comparisons)

            predicates.append(matches_field)
        return lambda row: all(predicate(row) for predicate in predicates)

    return compile_expression({} if where is None else where, 0)


def normalize_order_by(order_by: Sequence | Mapping | None) -> list[dict]:
    if order_by is None:
        return []
    if isinstance(order_by, str):
        order_by = [field.strip() for field in order_by.split(",") if field.strip()]
    elif isinstance(order_by, Mapping):
        order_by = [order_by]
    if not isinstance(order_by, Sequence):
        raise ValueError("order_by requires a list of fields or sort objects")
    specs = []
    for raw in order_by:
        if isinstance(raw, str):
            spec = {"field": raw[1:] if raw.startswith("-") else raw, "direction": "desc" if raw.startswith("-") else "asc"}
        elif isinstance(raw, Mapping):
            spec = dict(raw)
        else:
            raise ValueError("order_by entries must be fields or sort objects")
        if not isinstance(spec.get("field"), str) or not spec["field"]:
            raise ValueError("order_by requires a non-empty field")
        direction = str(spec.get("direction", "asc")).lower()
        nulls = str(spec.get("nulls", "last")).lower()
        if direction not in {"asc", "desc"}:
            raise ValueError("order_by direction must be asc or desc")
        if nulls not in {"first", "last"}:
            raise ValueError("order_by nulls must be first or last")
        if not isinstance(spec.get("natural", False), bool):
            raise ValueError("order_by natural requires a boolean")
        specs.append({**spec, "direction": direction, "nulls": nulls})
    return specs


def natural_key(value: object) -> tuple:
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"(\d+)", str(value)) if part)


def sort_value_key(value: object) -> tuple:
    number = numeric_value(value)
    return (0, number) if number is not None else (1, str(value).casefold())


def sort_records(rows: list[dict], order_by: Sequence | Mapping | None, getter: FieldGetter, missing: object) -> list[dict]:
    specs = normalize_order_by(order_by)
    if not specs:
        return rows
    ordered = list(rows)
    for spec in reversed(specs):
        present, absent = [], []
        key = natural_key if spec.get("natural", False) else sort_value_key
        for row in ordered:
            value = getter(row, spec["field"])
            if value is None or value is missing:
                absent.append(row)
            else:
                present.append((key(value), row))
        present.sort(key=lambda item: item[0], reverse=spec["direction"] == "desc")
        non_null_rows = [row for _, row in present]
        ordered = absent + non_null_rows if spec["nulls"] == "first" else non_null_rows + absent
    return ordered
