"""Natural-language planning and deterministic canonical answer rendering.

The language model is allowed to translate a question into the public
seven-field query contract only.  Snapshot selection, execution, arithmetic,
claim comparison, and evidence binding remain deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any

from .project_query import ProjectQueryExecution, query_contract_manifest
from .query_contract import PLAN_FIELDS, QueryContractError, validate_query_plan


class CanonicalPlannerError(RuntimeError):
    """Raised when the NL-to-plan model cannot produce a valid plan."""


_ENTITY_ALIASES = {
    "bim_element": "element",
    "bimelement": "element",
    "aggregated_boq_item": "boq_item",
    "boq": "boq_item",
}
_FIELD_ALIASES = {
    "element_id": "source_element_id",
    "level": "level_name",
}
_DISPLAY_UNITS = {
    "square_meter": "㎡",
    "metric_ton": "TON",
    "count": "개",
}
_CLAIM_RE = re.compile(r"(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?:개|종)\s*(?:라는|이라는)?\s*주장")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_error(error: BaseException, secret: str) -> str:
    text = str(error).replace(secret, "[redacted-api-key]") if secret else str(error)
    return re.sub(r"sk-[A-Za-z0-9_*.-]{4,}", "sk-[redacted]", text)


def _response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return str(text)
    output = getattr(response, "output", None)
    if output:
        return str(output)
    return str(response)


def _normalise_filter(item: Mapping[str, Any], unit_aliases: Mapping[str, str]) -> dict[str, Any]:
    raw_field = item.get("field") or item.get("property") or item.get("attribute")
    field = _FIELD_ALIASES.get(str(raw_field or "").strip(), str(raw_field or "").strip())
    operator = str(item.get("operator") or item.get("op") or "eq").strip()
    value = item.get("value")
    if field == "normalized_unit" and isinstance(value, str):
        value = unit_aliases.get(value, unit_aliases.get(value.casefold(), value))
    return {"field": field, "operator": operator, "value": value}


def _normalise_metric(item: Mapping[str, Any], question: str, group_by: list[str]) -> dict[str, Any]:
    name = str(
        item.get("name")
        or item.get("agg")
        or item.get("aggregation")
        or item.get("operation")
        or item.get("op")
        or item.get("function")
        or item.get("metric")
        or ""
    ).strip()
    field_value = item.get("field") or item.get("property") or item.get("attribute")
    field = _FIELD_ALIASES.get(str(field_value).strip(), str(field_value).strip()) if field_value is not None else None
    alias = item.get("alias") or item.get("as") or item.get("output_field") or item.get("output")
    is_claim = bool(_CLAIM_RE.search(question))
    if not alias:
        if name == "count_distinct" and field == "source_element_id":
            alias = "actual" if is_claim else "object_count"
        elif name == "count_distinct" and field == "type_name":
            alias = "actual" if is_claim else "type_count"
        elif name == "collect_distinct" and field == "type_name":
            alias = "types"
        elif name == "sum" and field == "quantity":
            alias = "total" if "specification" in group_by else "quantity_sum"
    payload: dict[str, Any] = {"name": name}
    if field is not None:
        payload["field"] = field
    if alias:
        payload["alias"] = str(alias)
    return payload


def _catalogue_filters(question: str, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    folded = question.casefold()
    for item in context.get("boq_material_catalog") or []:
        if not isinstance(item, Mapping):
            continue
        grade = str(item.get("grade") or "")
        shape = str(item.get("shape") or "")
        if not grade or not shape or grade.casefold() not in folded or shape.casefold() not in folded:
            continue
        matches.extend(
            [
                {"field": "item_name", "operator": "eq", "value": item.get("item_name")},
                {"field": "specification", "operator": "in", "value": list(item.get("specifications") or [])},
                {"field": "normalized_unit", "operator": "eq", "value": item.get("normalized_unit")},
            ]
        )
    return matches


def normalise_query_plan(
    payload: Mapping[str, Any],
    question: str,
    planner_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize schema vocabulary without calculating any answer value."""

    project_id = str(planner_context.get("canonical_project_id") or payload.get("project_id") or "").strip()
    entity_raw = str(payload.get("entity") or "").strip()
    entity = _ENTITY_ALIASES.get(entity_raw.casefold(), entity_raw)
    group_by = [
        _FIELD_ALIASES.get(str(field).strip(), str(field).strip())
        for field in (payload.get("group_by") or [])
    ]
    scope = dict(payload.get("scope") or {}) if isinstance(payload.get("scope"), Mapping) else {}
    unit_aliases = {
        str(key): str(value) for key, value in (planner_context.get("unit_aliases") or {}).items()
    }
    unit_aliases.update({key.casefold(): value for key, value in list(unit_aliases.items())})
    filters = [
        _normalise_filter(item, unit_aliases)
        for item in (payload.get("filters") or [])
        if isinstance(item, Mapping)
    ]

    # Scope fields are canonical selectors, not ordinary row filters.
    scope_fields = {"element": {"category"}, "boq_item": {"source_sheet", "work_category"}}.get(entity, set())
    remaining_filters: list[dict[str, Any]] = []
    for item in filters:
        if item["field"] in scope_fields and item["operator"] == "eq":
            scope.setdefault(item["field"], item["value"])
        else:
            remaining_filters.append(item)
    filters = remaining_filters

    if entity == "boq_item":
        by_field = {item["field"]: item for item in filters}
        for item in _catalogue_filters(question, planner_context):
            by_field[item["field"]] = item
        filters = list(by_field.values())

    filter_order = list((planner_context.get("filter_order") or {}).get(entity) or [])
    rank = {field: index for index, field in enumerate(filter_order)}
    filters.sort(key=lambda item: (rank.get(item["field"], len(rank)), item["field"]))

    metrics = [
        _normalise_metric(item, question, group_by)
        for item in (payload.get("metrics") or [])
        if isinstance(item, Mapping)
    ]
    if metrics:
        intent = "aggregate"
    else:
        intent = str(payload.get("intent") or "").strip()

    normalized = {
        "project_id": project_id,
        "entity": entity,
        "intent": intent,
        "scope": scope,
        "filters": filters,
        "group_by": group_by,
        "metrics": metrics,
    }
    # The validator enforces both grammar and allowed schema vocabulary.
    return validate_query_plan(normalized).as_dict()


def openai_plan_question(
    question: str,
    planner_context: Mapping[str, Any],
    *,
    api_key: str,
    model: str = "gpt-4.1-mini",
    llm_client: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Translate natural language to one exact seven-field plan, retrying once."""

    clean_question = question.strip()
    clean_key = api_key.strip()
    if not clean_question:
        raise CanonicalPlannerError("question is required")
    if not clean_key and llm_client is None:
        raise CanonicalPlannerError("OpenAI API key is required")

    if llm_client is None:
        from openai import OpenAI

        llm_client = OpenAI(api_key=clean_key)

    contract = query_contract_manifest()
    instructions = (
        "You translate a Korean or English BIM question into exactly one deterministic query plan. "
        "Return one JSON object with exactly these seven top-level fields: project_id, entity, intent, "
        "scope, filters, group_by, metrics. Never answer the question, calculate a value, select a pack, "
        "or select a snapshot. Use scope for category/source_sheet/work_category when allowed. "
        "A physical Revit object count means count_distinct(source_element_id). A type count means "
        "count_distinct(type_name). Preserve exact Korean labels. Use the supplied catalogue to entity-link "
        "named BOQ materials. For a claim question, plan only the actual aggregate; comparison happens later. "
        "Each filter must use {field, operator, value}. Each metric must use "
        "{name, field, alias}; for example a generic object count metric has name=count_distinct, "
        "field=source_element_id, and a descriptive alias. Do not use agg, aggregation, operation, "
        "function, property, as, or any alternative key names."
    )
    previous: str | None = None
    validation_error: str | None = None
    for attempt in (1, 2):
        payload = {
            "question": clean_question,
            "query_contract": contract,
            "project_context": dict(planner_context),
        }
        if previous is not None:
            payload["previous_invalid_output"] = previous
            payload["validation_error"] = validation_error
            payload["repair_instruction"] = "Repair only the schema/query-plan error. Return the full JSON object."
        try:
            response = llm_client.responses.create(
                model=model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False),
                text={"format": {"type": "json_object"}},
                temperature=0,
                max_output_tokens=1800,
            )
            raw = _response_text(response).strip()
            parsed = json.loads(raw)
            if not isinstance(parsed, Mapping):
                raise CanonicalPlannerError("planner must return one JSON object")
            plan = normalise_query_plan(parsed, clean_question, planner_context)
            if set(plan) != PLAN_FIELDS:
                raise CanonicalPlannerError("planner did not return the exact seven-field contract")
            return plan, {
                "model": model,
                "attempts": attempt,
                "plan_hash": _sha256(plan),
                "contract": "bim-query-plan/7-field",
            }
        except (json.JSONDecodeError, QueryContractError, CanonicalPlannerError) as exc:
            previous = locals().get("raw", "")
            validation_error = str(exc)
        except Exception as exc:  # SDK/network errors vary by deployment.
            raise CanonicalPlannerError(_safe_error(exc, clean_key)) from exc
    raise CanonicalPlannerError(f"planner output failed validation after repair: {validation_error}")


def _clean_number(value: Any) -> Any:
    if not isinstance(value, float):
        return value
    rounded = round(value, 10)
    return int(rounded) if rounded.is_integer() else rounded


def _claim_value(question: str) -> int | float | None:
    match = _CLAIM_RE.search(question)
    if not match:
        return None
    value = float(match.group("value").replace(",", ""))
    return int(value) if value.is_integer() else value


def _metric_unit(execution: ProjectQueryExecution, alias: str, values: Mapping[str, Any]) -> str | None:
    source_unit = values.get(f"{alias}_unit")
    if source_unit:
        return _DISPLAY_UNITS.get(str(source_unit), str(source_unit))
    spec = next(
        (item for item in execution.plan.metrics if (item.alias or item.field or item.name) == alias),
        None,
    )
    if not spec:
        return None
    if spec.name == "count_distinct" and spec.field == "type_name":
        return "종"
    if spec.name in {"count", "count_distinct"}:
        return "개"
    return None


def _answer_values(execution: ProjectQueryExecution) -> tuple[Any, Any]:
    result = execution.result
    if result.rows:
        group_fields = list(execution.plan.group_by)
        metric_aliases = [item.alias or item.field or item.name for item in execution.plan.metrics]
        metric = metric_aliases[0]
        unit = _metric_unit(execution, metric, result.rows[0])
        if group_fields == ["specification"]:
            breakdown = {
                str(row["specification"]): _clean_number(row[metric])
                for row in result.rows
            }
            return {"total": _clean_number(sum(float(value) for value in breakdown.values())), "breakdown": breakdown}, unit
        if len(group_fields) == 1 and len(metric_aliases) == 1:
            group_field = group_fields[0]
            grouped = {str(row[group_field]): _clean_number(row[metric]) for row in result.rows}
            numeric = {key: float(value) for key, value in grouped.items() if isinstance(value, (int, float))}
            if numeric:
                maximum = max(numeric.values())
                minimum = min(numeric.values())
                leaders = [key for key, value in numeric.items() if value == maximum]
                grouped["total"] = _clean_number(sum(numeric.values()))
                grouped["larger_level"] = leaders[0] if len(leaders) == 1 else leaders
                grouped["difference"] = _clean_number(maximum - minimum)
            return grouped, unit
        return [
            {key: _clean_number(value) for key, value in row.items() if not key.endswith("_unit")}
            for row in result.rows
        ], unit

    values = {key: _clean_number(value) for key, value in result.values.items() if not key.endswith("_unit")}
    claim = _claim_value(execution.question or "")
    if claim is not None and "actual" in values:
        actual = values["actual"]
        answer: dict[str, Any] = {
            "claim": claim,
            "actual": actual,
            "matches": actual == claim,
        }
        if isinstance(actual, (int, float)) and claim > actual:
            answer["overstatement"] = _clean_number(claim - actual)
        if "types" in values:
            answer["types"] = values["types"]
        return answer, _metric_unit(execution, "actual", result.values)
    units = {key: _metric_unit(execution, key, result.values) for key in values}
    if len(values) == 1:
        key, value = next(iter(values.items()))
        return value, units[key]
    return values, {key: value for key, value in units.items() if value is not None}


def _answer_scope(question: str, execution: ProjectQueryExecution) -> dict[str, Any]:
    scope = dict(execution.plan.scope)
    for item in execution.plan.filters:
        if item.field == "level_name" and item.operator == "in":
            scope["levels"] = list(item.value)
        elif item.field == "normalized_unit" and "단위가" in question:
            scope["normalized_unit"] = item.value
    if execution.plan.entity == "element" and any(
        item.name == "count_distinct" and item.field == "source_element_id"
        for item in execution.plan.metrics
    ) and ("물리 객체" in question or "물리객체" in question):
        scope.setdefault("granularity", "physical_instance")
    excluded = [category for category in ("계단진행", "계단참") if category in question]
    if excluded:
        scope["excluded_categories"] = excluded
    grade = re.search(r"\b(SM\d{3})\b", question, flags=re.I)
    if grade:
        scope["material_grade"] = grade.group(1).upper()
    if "발주량" in question and "시트" in question:
        scope["semantic"] = "worksheet_quantity_sum_not_order_quantity"
    return scope


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "일치" if value else "불일치"
    if isinstance(value, float):
        return f"{value:,.10f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _format_answer(values: Any, unit: Any, execution: ProjectQueryExecution) -> str:
    if isinstance(values, Mapping) and {"claim", "actual", "matches"}.issubset(values):
        suffix = f" {unit}" if isinstance(unit, str) and unit else ""
        text = (
            f"주장 {_format_value(values['claim'])}{suffix}, 실제 {_format_value(values['actual'])}{suffix}로 "
            f"두 값은 {'일치합니다' if values['matches'] else '일치하지 않습니다'}."
        )
        if "overstatement" in values:
            text += f" 주장이 {_format_value(values['overstatement'])}{suffix} 많습니다."
        if values.get("types"):
            text += " 실제 타입은 " + ", ".join(str(item) for item in values["types"]) + "입니다."
        return text
    if isinstance(values, Mapping) and "breakdown" in values:
        rows = ", ".join(f"{key} {_format_value(value)}" for key, value in values["breakdown"].items())
        return f"총 {_format_value(values['total'])} {unit}입니다. 규격별 수량은 {rows} {unit}입니다."
    if isinstance(values, Mapping) and "larger_level" in values:
        reserved = {"total", "larger_level", "difference"}
        rows = ", ".join(f"{key} {_format_value(value)} {unit}" for key, value in values.items() if key not in reserved)
        return (
            f"{rows}이며 합계는 {_format_value(values['total'])} {unit}입니다. "
            f"{values['larger_level']}이 {_format_value(values['difference'])} {unit} 더 많습니다."
        )
    if isinstance(values, Mapping):
        parts = []
        for key, value in values.items():
            suffix = unit.get(key) if isinstance(unit, Mapping) else unit
            parts.append(f"{key} {_format_value(value)}{f' {suffix}' if suffix else ''}")
        return ", ".join(parts) + "입니다."
    return f"{_format_value(values)}{f' {unit}' if unit else ''}입니다."


def build_canonical_answer(question: str, execution: ProjectQueryExecution) -> dict[str, Any]:
    """Render final values, scope, version, and evidence without another LLM call."""

    bound_execution = ProjectQueryExecution(
        snapshot=execution.snapshot,
        verification=execution.verification,
        plan=execution.plan,
        result=execution.result,
        question=question.strip(),
    )
    values, unit = _answer_values(bound_execution)
    scope = _answer_scope(question, bound_execution)
    result = bound_execution.result
    evidence = {
        "snapshot_id": result.snapshot_id,
        "snapshot_hash": bound_execution.snapshot.snapshot_hash,
        "source_signature": bound_execution.snapshot.source_signature,
        "query_hash": result.query_hash,
        "result_hash": result.result_hash,
        "contribution_digest": result.evidence.get("contribution_digest"),
        "contribution_count": result.evidence.get("contribution_count"),
        "source_packs": list(result.source_packs),
    }
    return {
        "answer": _format_answer(values, unit, bound_execution),
        "values": values,
        "unit": unit,
        "scope": scope,
        "complete": result.complete,
        "evidence": evidence,
    }
