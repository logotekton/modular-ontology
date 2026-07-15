from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping


PLAN_FIELDS = frozenset(
    {
        "project_id",
        "entity",
        "intent",
        "scope",
        "filters",
        "group_by",
        "metrics",
    }
)
FORBIDDEN_KEYS = frozenset({"pack_id", "snapshot_id"})
FILTER_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "in",
        "not_in",
        "contains",
        "startswith",
        "endswith",
        "gt",
        "gte",
        "lt",
        "lte",
        "exists",
    }
)
# Only intents with a complete deterministic execution path are public. Raw
# lookup/list/trace need an explicit projection/limit contract and must not be
# accepted until that contract exists.
INTENTS = frozenset({"count", "aggregate", "compare", "coverage"})

# A bundle never broadens the seven-field plan grammar above.  It only gives
# several independently validated plans stable names and applies a short,
# deterministic reducer pipeline to their completed results.
PLAN_BUNDLE_FIELDS = frozenset({"project_id", "plans", "reducers"})
REDUCER_OPERATORS = frozenset(
    {
        "argmax",
        "all_equal",
        "artifact_role_absence",
        "count",
        "coverage_absence",
        "equals",
        "evaluate_decimal_formula",
        "extract_constraint",
        "extract_dimension_token",
        "extract_section_list",
        "inner_join_rank",
        "left_join_zero",
        "only",
        "round",
        "satisfies_constraint",
        "subtract",
        "sum_field",
        "sum_values",
        "top",
        "traverse_edge",
    }
)
MAX_BUNDLE_PLANS = 16
MAX_BUNDLE_REDUCERS = 32
_BUNDLE_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SECTION_PATTERN = re.compile(r"^\d{1,2}(?:\.\d{1,2}){1,3}$")

CONSTRAINT_PROFILES = frozenset(
    {
        "granite_compressive_strength",
        "granite_water_absorption",
        "sg_panel_fire_gypsum_board_thickness",
        "urethane_waterproofing_total_thickness",
        "eps_bead_initial_thermal_conductivity",
    }
)
DIMENSION_PROFILES = frozenset(
    {"type_name_t_thickness_mm", "boq_spec_t_thickness_mm"}
)
TRAVERSE_EDGE_PROFILES = frozenset(
    {"boq_calculated_by", "quantity_evidence_bim_reference"}
)
DECIMAL_FORMULA_PROFILES = frozenset({"decimal_additive"})
COVERAGE_SCAN_PROFILES = frozenset(
    {
        "boq_estimate_fact_text",
        "boq_graph_text",
        "boq_wall_item_exact_token",
        "boq_work_item_text",
        "drawing_schedule_text",
        "drawing_sheet_text",
        "law_entity_text",
        "model_floor_context_text",
        "model_lift_context_text",
        "model_quantity_text",
        "model_structural_context_text",
        "model_wall_type_exact_token",
        "spec_requirement_text",
    }
)
EXACT_TOKEN_COVERAGE_PROFILES = frozenset(
    {"boq_wall_item_exact_token", "model_wall_type_exact_token"}
)
SNAPSHOT_PACK_ABSENCE_ROLES = frozenset(
    {"contract_document", "project_structural_calculation"}
)

_REDUCER_FIELDS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    # operator: (required fields, optional fields)
    "argmax": (frozenset({"source", "field"}), frozenset()),
    "all_equal": (frozenset({"inputs"}), frozenset()),
    "artifact_role_absence": (
        frozenset({"source", "generic_context"}),
        frozenset(),
    ),
    "count": (frozenset({"source"}), frozenset()),
    "coverage_absence": (frozenset({"inputs"}), frozenset()),
    "equals": (frozenset({"left", "right"}), frozenset()),
    "evaluate_decimal_formula": (
        frozenset({"profile", "formula", "source_type", "unit"}),
        frozenset(),
    ),
    "extract_constraint": (frozenset({"source", "profile"}), frozenset()),
    "extract_dimension_token": (frozenset({"source", "profile"}), frozenset()),
    "extract_section_list": (
        frozenset({"source", "section", "heading"}),
        frozenset({"item_prefix", "max_items"}),
    ),
    "inner_join_rank": (
        frozenset({"left", "right", "right_key", "value_field", "limit"}),
        frozenset({"left_key"}),
    ),
    "left_join_zero": (
        frozenset({"left", "right", "right_key", "value_field"}),
        frozenset({"left_key"}),
    ),
    "only": (frozenset({"source"}), frozenset()),
    "round": (frozenset({"source", "digits"}), frozenset()),
    "satisfies_constraint": (frozenset({"constraint", "actual"}), frozenset()),
    "subtract": (frozenset({"left", "right"}), frozenset()),
    "sum_field": (frozenset({"source", "field"}), frozenset()),
    "sum_values": (frozenset({"inputs"}), frozenset()),
    "top": (frozenset({"source", "field", "limit"}), frozenset()),
    "traverse_edge": (
        frozenset({"source", "edges", "target", "profile"}),
        frozenset(),
    ),
}


@dataclass(frozen=True)
class EntityPolicy:
    fields: frozenset[str]
    scope_fields: frozenset[str]
    operators: frozenset[str]
    metrics: Mapping[str, frozenset[str]]


_COMMON_IDENTITY_FIELDS = frozenset({"id", "label", "name"})
_COMMON_SCOPE_FIELDS = frozenset(
    {"building", "discipline", "level_name", "module_id", "module_type", "workset_name", "zone"}
)
_TEXT_OPERATORS = frozenset(
    {"eq", "ne", "in", "not_in", "contains", "startswith", "endswith", "exists"}
)
_ALL_OPERATORS = FILTER_OPERATORS
_COUNT_METRICS = {
    "count": frozenset({"*"}),
    "count_distinct": frozenset(),
}


def _metrics(
    fields: set[str] | frozenset[str],
    *,
    numeric: set[str] | frozenset[str] = frozenset(),
) -> dict[str, frozenset[str]]:
    return {
        "count": frozenset({"*"}),
        "count_distinct": frozenset(fields),
        "collect_distinct": frozenset(fields),
        "sum": frozenset(numeric),
        "min": frozenset(numeric),
        "max": frozenset(numeric),
        "avg": frozenset(numeric),
    }


ENTITY_POLICIES: Mapping[str, EntityPolicy] = {
    "element": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | _COMMON_SCOPE_FIELDS
        | frozenset(
            {
                "category",
                "class",
                "family_name",
                "family_and_type",
                "type_name",
                "source_element_id",
                "unique_id",
                "ifc_guid",
                "material",
                "length",
                "area",
                "volume",
                "thickness",
            }
        ),
        scope_fields=_COMMON_SCOPE_FIELDS | frozenset({"category"}),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {
                "id",
                "category",
                "class",
                "family_name",
                "family_and_type",
                "type_name",
                "module_id",
                "module_type",
                "workset_name",
                "level_name",
                "source_element_id",
                "unique_id",
                "ifc_guid",
                "material",
            },
            numeric={"length", "area", "volume", "thickness"},
        ),
    ),
    "type": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset(
            {
                "category",
                "class",
                "family_name",
                "family_and_type",
                "type_name",
                "material",
                "thickness",
            }
        ),
        scope_fields=frozenset({"category", "discipline"}),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {"id", "category", "class", "family_name", "family_and_type", "type_name", "material"},
            numeric={"thickness"},
        ),
    ),
    "module": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset({"module_id", "workset_name", "kind"}),
        scope_fields=frozenset({"module_id", "workset_name"}),
        operators=_ALL_OPERATORS,
        metrics=_metrics({"id", "name", "module_id", "workset_name", "kind"}),
    ),
    "level": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS | frozenset({"level_name", "elevation", "element_count"}),
        scope_fields=frozenset({"level_name"}),
        operators=_ALL_OPERATORS,
        metrics=_metrics({"id", "level_name"}, numeric={"elevation", "element_count"}),
    ),
    "boq_item": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset(
            {
                "node_type",
                "item_name",
                "work_category",
                "specification",
                "quantity",
                "unit",
                "normalized_unit",
                "module_id",
                "module_type",
                "identity_key",
                "formula",
                "formula_source_type",
                "bim_reference_count",
                "source_sheet",
                "source_row",
                "source_pack_id",
                "source_pack_sha256",
            }
        ),
        scope_fields=frozenset({"module_id", "module_type", "source_sheet", "work_category"}),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {
                "id",
                "item_name",
                "work_category",
                "specification",
                "unit",
                "normalized_unit",
                "module_id",
                "module_type",
                "identity_key",
                "formula",
                "formula_source_type",
                "bim_reference_count",
                "source_sheet",
                "source_row",
                "source_pack_id",
                "source_pack_sha256",
            },
            numeric={"quantity", "bim_reference_count"},
        ),
    ),
    "relationship_edge": EntityPolicy(
        fields=frozenset(
            {
                "source",
                "relation",
                "target",
                "reference_index",
                "source_pack_id",
                "source_pack_sha256",
            }
        ),
        scope_fields=frozenset({"source_role"}),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {
                "source",
                "relation",
                "target",
                "reference_index",
                "source_pack_id",
                "source_pack_sha256",
            },
            numeric={"reference_index"},
        ),
    ),
    "bim_reference": EntityPolicy(
        fields=frozenset(
            {
                "id",
                "measurement_value",
                "type",
                "source_expression",
                "source_sheet",
                "source_row",
                "source_pack_id",
                "source_pack_sha256",
            }
        ),
        scope_fields=frozenset({"type", "source_sheet"}),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {
                "id",
                "type",
                "source_expression",
                "source_sheet",
                "source_row",
                "source_pack_id",
                "source_pack_sha256",
            },
            numeric={"measurement_value", "source_row"},
        ),
    ),
    "specification": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset(
            {
                "category",
                "section",
                "material",
                "thickness",
                "source_file",
                "page",
                "requirement_text",
                "standard_code",
                "evidence_ref",
            }
        ),
        scope_fields=frozenset(
            {"category", "discipline", "section", "source_file", "standard_code"}
        ),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {
                "id",
                "category",
                "section",
                "material",
                "source_file",
                "page",
                "requirement_text",
                "standard_code",
                "evidence_ref",
            },
            numeric={"thickness"},
        ),
    ),
    "quantity_fact": EntityPolicy(
        fields=frozenset(
            {
                "id",
                "category",
                "type_name",
                "level_name",
                "module_id",
                "workset_name",
                "quantity_role",
                "measure_kind",
                "is_primary_measure",
                "aggregation_scope",
                "source_element_id",
                "area_m2",
            }
        ),
        scope_fields=frozenset(
            {
                "category",
                "type_name",
                "level_name",
                "module_id",
                "workset_name",
                "quantity_role",
                "measure_kind",
                "is_primary_measure",
                "aggregation_scope",
            }
        ),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {
                "id",
                "category",
                "type_name",
                "level_name",
                "module_id",
                "workset_name",
                "quantity_role",
                "measure_kind",
                "is_primary_measure",
                "aggregation_scope",
                "source_element_id",
                "area_m2",
            },
            numeric={"area_m2"},
        ),
    ),
    "drawing_entity": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset(
            {
                "source_file",
                "sheet_number",
                "view_name",
                "entity_type",
                "handle",
                "layer",
                "text",
                "x",
                "y",
            }
        ),
        scope_fields=frozenset({"source_file", "sheet_number", "view_name", "layer"}),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {"id", "source_file", "sheet_number", "view_name", "entity_type", "handle", "layer", "text"},
            numeric={"x", "y"},
        ),
    ),
    "sheet": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset({"sheet_number", "sheet_name", "source_file", "discipline"}),
        scope_fields=frozenset({"sheet_number", "source_file", "discipline"}),
        operators=_TEXT_OPERATORS,
        metrics=_metrics({"id", "sheet_number", "sheet_name", "source_file", "discipline"}),
    ),
    "view": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset({"view_name", "view_type", "sheet_number", "level_name", "discipline"}),
        scope_fields=frozenset({"view_name", "sheet_number", "level_name", "discipline"}),
        operators=_TEXT_OPERATORS,
        metrics=_metrics({"id", "view_name", "view_type", "sheet_number", "level_name", "discipline"}),
    ),
    "project_metadata": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset(
            {
                "node_type",
                "project_name",
                "project_title",
                "project_number",
                "document_key",
                "revit_version",
                "schema_version",
                "exported_at",
                "parser",
            }
        ),
        scope_fields=frozenset({"node_type", "project_number", "document_key"}),
        operators=_TEXT_OPERATORS,
        metrics=_metrics(
            {
                "id",
                "node_type",
                "project_name",
                "project_title",
                "project_number",
                "document_key",
                "revit_version",
                "schema_version",
                "exported_at",
                "parser",
            }
        ),
    ),
    "drawing_file": EntityPolicy(
        fields=_COMMON_IDENTITY_FIELDS
        | frozenset(
            {
                "node_type",
                "sheet_id",
                "sheet_number",
                "sheet_name",
                "file_name",
                "file_size_bytes",
                "status",
                "source_kind",
                "record_type",
            }
        ),
        scope_fields=frozenset(
            {"node_type", "sheet_number", "sheet_name", "status", "source_kind", "record_type"}
        ),
        operators=_ALL_OPERATORS,
        metrics=_metrics(
            {
                "id",
                "node_type",
                "sheet_id",
                "sheet_number",
                "sheet_name",
                "file_name",
                "file_size_bytes",
                "status",
                "source_kind",
                "record_type",
            },
            numeric={"file_size_bytes"},
        ),
    ),
    # Coverage entities expose no row/filter/metric surface.  Their complete
    # schema and source roles are selected only through fixed profiles below.
    "coverage_scan": EntityPolicy(
        fields=frozenset(),
        scope_fields=frozenset({"profile", "terms"}),
        operators=frozenset(),
        metrics={},
    ),
    "snapshot_pack": EntityPolicy(
        fields=frozenset(),
        scope_fields=frozenset({"role"}),
        operators=frozenset(),
        metrics={},
    ),
}


@dataclass(frozen=True)
class ContractIssue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class QueryContractError(ValueError):
    def __init__(self, issues: list[ContractIssue] | tuple[ContractIssue, ...]) -> None:
        self.issues = tuple(issues)
        message = "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues)
        super().__init__(message or "Invalid BIM query plan")

    def as_dict(self) -> dict[str, Any]:
        return {"error": "invalid_query_plan", "issues": [issue.as_dict() for issue in self.issues]}


@dataclass(frozen=True)
class FilterSpec:
    field: str
    operator: str
    value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "operator": self.operator, "value": _json_value(self.value)}


@dataclass(frozen=True)
class MetricSpec:
    name: str
    field: str | None = None
    alias: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name}
        if self.field is not None:
            payload["field"] = self.field
        if self.alias is not None:
            payload["alias"] = self.alias
        return payload


@dataclass(frozen=True)
class BIMQueryPlan:
    project_id: str
    entity: str
    intent: str
    scope: Mapping[str, Any] = field(default_factory=dict)
    filters: tuple[FilterSpec, ...] = ()
    group_by: tuple[str, ...] = ()
    metrics: tuple[MetricSpec, ...] = ()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BIMQueryPlan":
        return validate_query_plan(payload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "entity": self.entity,
            "intent": self.intent,
            "scope": _json_value(self.scope),
            "filters": [item.as_dict() for item in self.filters],
            "group_by": list(self.group_by),
            "metrics": [item.as_dict() for item in self.metrics],
        }


@dataclass(frozen=True)
class QueryPlanValidation:
    valid: bool
    plan: BIMQueryPlan | None
    issues: tuple[ContractIssue, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "plan": self.plan.as_dict() if self.plan else None,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class NamedQueryPlan:
    id: str
    plan: BIMQueryPlan

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "plan": self.plan.as_dict()}


@dataclass(frozen=True)
class ReducerSpec:
    id: str
    op: str
    config: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "op": self.op, **_json_value(self.config)}


@dataclass(frozen=True)
class PlanBundle:
    project_id: str
    plans: tuple[NamedQueryPlan, ...]
    reducers: tuple[ReducerSpec, ...] = ()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlanBundle":
        return validate_plan_bundle(payload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "plans": {item.id: item.plan.as_dict() for item in self.plans},
            "reducers": [item.as_dict() for item in self.reducers],
        }


class PlanBundleContractError(QueryContractError):
    def as_dict(self) -> dict[str, Any]:
        return {"error": "invalid_plan_bundle", "issues": [issue.as_dict() for issue in self.issues]}


@dataclass(frozen=True)
class PlanBundleValidation:
    valid: bool
    bundle: PlanBundle | None
    issues: tuple[ContractIssue, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "bundle": self.bundle.as_dict() if self.bundle else None,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def try_validate_query_plan(payload: Mapping[str, Any]) -> QueryPlanValidation:
    try:
        plan = validate_query_plan(payload)
    except QueryContractError as exc:
        return QueryPlanValidation(valid=False, plan=None, issues=exc.issues)
    return QueryPlanValidation(valid=True, plan=plan)


def try_validate_plan_bundle(payload: Mapping[str, Any]) -> PlanBundleValidation:
    try:
        bundle = validate_plan_bundle(payload)
    except PlanBundleContractError as exc:
        return PlanBundleValidation(valid=False, bundle=None, issues=exc.issues)
    return PlanBundleValidation(valid=True, bundle=bundle)


def validate_plan_bundle(payload: Mapping[str, Any]) -> PlanBundle:
    """Validate a bounded collection of exact seven-field query plans.

    Reducers may read only completed plan results or preceding reducer values.
    Unknown operators, fields, forward references, and snapshot/pack selectors
    are rejected before any pack is opened.
    """

    if not isinstance(payload, Mapping):
        raise PlanBundleContractError(
            [ContractIssue("invalid_type", "$", "plan bundle must be an object")]
        )

    issues: list[ContractIssue] = []
    _check_forbidden_keys(payload, "$", issues)
    for raw_key in payload:
        key = str(raw_key)
        if key not in PLAN_BUNDLE_FIELDS:
            issues.append(
                ContractIssue("unknown_field", f"$.{key}", "field is not part of the plan-bundle contract")
            )

    project_id = _required_text(payload.get("project_id"), "$.project_id", issues)
    raw_plans = payload.get("plans")
    plans: list[NamedQueryPlan] = []
    plan_ids: set[str] = set()
    plan_entities: dict[str, str] = {}
    if not isinstance(raw_plans, Mapping):
        issues.append(ContractIssue("invalid_type", "$.plans", "plans must be an object keyed by plan id"))
    else:
        if not raw_plans:
            issues.append(ContractIssue("missing_plan", "$.plans", "at least one query plan is required"))
        if len(raw_plans) > MAX_BUNDLE_PLANS:
            issues.append(
                ContractIssue(
                    "too_many_plans",
                    "$.plans",
                    f"at most {MAX_BUNDLE_PLANS} query plans are allowed",
                )
            )
        for raw_id, raw_plan in raw_plans.items():
            plan_id = str(raw_id)
            path = f"$.plans.{plan_id}"
            if not _BUNDLE_ID_PATTERN.fullmatch(plan_id):
                issues.append(ContractIssue("invalid_id", path, "plan id must be an ASCII identifier"))
                continue
            plan_ids.add(plan_id)
            if not isinstance(raw_plan, Mapping):
                issues.append(ContractIssue("invalid_type", path, "query plan must be an object"))
                continue
            raw_plan_fields = {str(key) for key in raw_plan}
            for field_name in sorted(PLAN_FIELDS - raw_plan_fields):
                issues.append(
                    ContractIssue(
                        "missing_field",
                        f"{path}.{field_name}",
                        "all seven query-plan fields are required inside a bundle",
                    )
                )
            try:
                plan = validate_query_plan(raw_plan)
            except QueryContractError as exc:
                issues.extend(_prefix_issues(exc.issues, path))
                continue
            if project_id and plan.project_id != project_id:
                issues.append(
                    ContractIssue(
                        "project_mismatch",
                        f"{path}.project_id",
                        "nested plan project_id must equal the bundle project_id",
                    )
                )
                continue
            plans.append(NamedQueryPlan(id=plan_id, plan=plan))
            plan_entities[plan_id] = plan.entity

    raw_reducers = payload.get("reducers", [])
    reducers: list[ReducerSpec] = []
    reducer_ids: set[str] = set()
    reducer_ops: dict[str, str] = {}
    if not isinstance(raw_reducers, list | tuple):
        issues.append(ContractIssue("invalid_type", "$.reducers", "reducers must be an array"))
    else:
        if len(raw_reducers) > MAX_BUNDLE_REDUCERS:
            issues.append(
                ContractIssue(
                    "too_many_reducers",
                    "$.reducers",
                    f"at most {MAX_BUNDLE_REDUCERS} reducers are allowed",
                )
            )
        for index, raw_reducer in enumerate(raw_reducers):
            path = f"$.reducers[{index}]"
            reducer = _parse_reducer(
                raw_reducer,
                path,
                plan_ids,
                plan_entities,
                reducer_ids,
                reducer_ops,
                issues,
            )
            if reducer is not None:
                reducers.append(reducer)
                reducer_ids.add(reducer.id)
                reducer_ops[reducer.id] = reducer.op

    if issues:
        raise PlanBundleContractError(issues)
    return PlanBundle(project_id=project_id, plans=tuple(plans), reducers=tuple(reducers))


def validate_query_plan(payload: Mapping[str, Any]) -> BIMQueryPlan:
    if not isinstance(payload, Mapping):
        raise QueryContractError([ContractIssue("invalid_type", "$", "plan must be an object")])

    issues: list[ContractIssue] = []
    _check_forbidden_keys(payload, "$", issues)

    unknown = sorted(str(key) for key in payload if str(key) not in PLAN_FIELDS)
    for key in unknown:
        issues.append(ContractIssue("unknown_field", f"$.{key}", "field is not part of the query contract"))

    project_id = _required_text(payload.get("project_id"), "$.project_id", issues)
    entity = _required_text(payload.get("entity"), "$.entity", issues).casefold()
    intent = _required_text(payload.get("intent"), "$.intent", issues).casefold()

    policy = ENTITY_POLICIES.get(entity)
    if entity and policy is None:
        issues.append(
            ContractIssue(
                "unsupported_entity",
                "$.entity",
                f"allowed entities: {', '.join(sorted(ENTITY_POLICIES))}",
            )
        )
    if intent and intent not in INTENTS:
        issues.append(
            ContractIssue("unsupported_intent", "$.intent", f"allowed intents: {', '.join(sorted(INTENTS))}")
        )

    scope = _parse_scope(payload.get("scope", {}), policy, issues)
    filters = _parse_filters(payload.get("filters", []), policy, issues)
    group_by = _parse_group_by(payload.get("group_by", []), policy, issues)
    metrics = _parse_metrics(payload.get("metrics", []), policy, issues)

    _validate_coverage_plan_contract(
        entity,
        intent,
        scope,
        filters,
        group_by,
        metrics,
        issues,
    )

    if intent == "count" and not metrics:
        metrics = (MetricSpec(name="count", field="*"),)

    _validate_output_name_reservations(entity, group_by, metrics, issues)

    if entity == "relationship_edge" and scope.get("source_role") != "boq":
        issues.append(
            ContractIssue(
                "required_source_role",
                "$.scope.source_role",
                "relationship_edge requires the exact source_role 'boq'",
            )
        )

    if entity == "boq_item" and any(
        metric.name in {"sum", "avg", "min", "max"} and metric.field == "quantity"
        for metric in metrics
    ):
        if not _has_exact_source_sheet_pin(scope, filters):
            issues.append(
                ContractIssue(
                    "ambiguous_source_projection",
                    "$.scope.source_sheet",
                    "BOQ quantity aggregation must pin exactly one source sheet with a scalar scope, eq, or singleton in filter",
                )
            )

    if intent in {"aggregate", "compare"} and not metrics:
        issues.append(ContractIssue("missing_metric", "$.metrics", f"{intent} intent requires at least one metric"))
    if intent == "compare" and not group_by:
        issues.append(ContractIssue("missing_group", "$.group_by", "compare intent requires group_by"))
    if issues:
        raise QueryContractError(issues)
    return BIMQueryPlan(
        project_id=project_id,
        entity=entity,
        intent=intent,
        scope=scope,
        filters=filters,
        group_by=group_by,
        metrics=metrics,
    )


def _validate_coverage_plan_contract(
    entity: str,
    intent: str,
    scope: Mapping[str, Any],
    filters: tuple[FilterSpec, ...],
    group_by: tuple[str, ...],
    metrics: tuple[MetricSpec, ...],
    issues: list[ContractIssue],
) -> None:
    coverage_entities = {"coverage_scan", "snapshot_pack"}
    if entity not in coverage_entities:
        if intent == "coverage":
            issues.append(
                ContractIssue(
                    "coverage_entity_required",
                    "$.entity",
                    "coverage intent is available only for coverage_scan or snapshot_pack",
                )
            )
        return
    if intent != "coverage":
        issues.append(
            ContractIssue(
                "coverage_intent_required",
                "$.intent",
                f"{entity} requires the exact coverage intent",
            )
        )
    if filters:
        issues.append(
            ContractIssue(
                "coverage_shape",
                "$.filters",
                "coverage plans do not accept runtime filters",
            )
        )
    if group_by:
        issues.append(
            ContractIssue(
                "coverage_shape",
                "$.group_by",
                "coverage plans do not accept group_by",
            )
        )
    if metrics:
        issues.append(
            ContractIssue(
                "coverage_shape",
                "$.metrics",
                "coverage plans do not accept metrics",
            )
        )

    if entity == "snapshot_pack":
        if set(scope) != {"role"}:
            issues.append(
                ContractIssue(
                    "invalid_coverage_scope",
                    "$.scope",
                    "snapshot_pack scope must contain exactly role",
                )
            )
            return
        role = scope.get("role")
        if not isinstance(role, str) or role not in SNAPSHOT_PACK_ABSENCE_ROLES:
            issues.append(
                ContractIssue(
                    "unsupported_role",
                    "$.scope.role",
                    "allowed absence roles: "
                    + ", ".join(sorted(SNAPSHOT_PACK_ABSENCE_ROLES)),
                )
            )
        return

    if set(scope) != {"profile", "terms"}:
        issues.append(
            ContractIssue(
                "invalid_coverage_scope",
                "$.scope",
                "coverage_scan scope must contain exactly profile and terms",
            )
        )
        return
    profile = scope.get("profile")
    if not isinstance(profile, str) or profile not in COVERAGE_SCAN_PROFILES:
        issues.append(
            ContractIssue(
                "unsupported_profile",
                "$.scope.profile",
                "allowed coverage profiles: "
                + ", ".join(sorted(COVERAGE_SCAN_PROFILES)),
            )
        )
    terms = scope.get("terms")
    if not isinstance(terms, list | tuple) or not 1 <= len(terms) <= 16:
        issues.append(
            ContractIssue(
                "invalid_terms",
                "$.scope.terms",
                "terms must contain from 1 to 16 literal strings",
            )
        )
        return
    normalized: list[str] = []
    for index, term in enumerate(terms):
        if not isinstance(term, str) or not term.strip() or len(term) > 100:
            issues.append(
                ContractIssue(
                    "invalid_term",
                    f"$.scope.terms[{index}]",
                    "term must be non-blank text of at most 100 characters",
                )
            )
            continue
        normalized_term = unicodedata.normalize("NFKC", term).casefold()
        normalized.append(normalized_term)
        if (
            profile in EXACT_TOKEN_COVERAGE_PROFILES
            and (
                unicodedata.category(normalized_term[0])[0] not in {"L", "N"}
                or unicodedata.category(normalized_term[-1])[0] not in {"L", "N"}
            )
        ):
            issues.append(
                ContractIssue(
                    "invalid_term",
                    f"$.scope.terms[{index}]",
                    "exact-token terms must start and end with a Unicode letter or number",
                )
            )
    if len(normalized) != len(set(normalized)):
        issues.append(
            ContractIssue(
                "duplicate_term",
                "$.scope.terms",
                "terms must remain unique after NFKC case-folding",
            )
        )


def _validate_output_name_reservations(
    entity: str,
    group_by: tuple[str, ...],
    metrics: tuple[MetricSpec, ...],
    issues: list[ContractIssue],
) -> None:
    """Reserve every executor output key before execution.

    Unit-aware BOQ quantity metrics emit an implicit ``<alias>_unit`` key in
    addition to their numeric output.  Treat both keys, every other metric
    output, and group keys as one namespace so validation is independent of
    metric order.
    """

    reservations: dict[str, list[tuple[str, str]]] = {}
    for index, field_name in enumerate(group_by):
        reservations.setdefault(field_name, []).append((f"$.group_by[{index}]", "group key"))

    for index, metric in enumerate(metrics):
        output_name = metric.alias or (
            metric.field if metric.field not in {None, "*"} else metric.name
        )
        if not output_name:
            continue
        path = f"$.metrics[{index}].alias"
        reservations.setdefault(output_name, []).append((path, "metric output"))
        if _emits_unit_companion(entity, metric):
            reservations.setdefault(f"{output_name}_unit", []).append(
                (path, "implicit unit companion")
            )

    for output_name in sorted(reservations):
        owners = reservations[output_name]
        if len(owners) < 2:
            continue
        owner_kinds = ", ".join(kind for _, kind in owners)
        for path, _ in owners:
            issues.append(
                ContractIssue(
                    "duplicate_alias",
                    path,
                    f"output name {output_name!r} is reserved by multiple outputs ({owner_kinds})",
                )
            )


def _emits_unit_companion(entity: str, metric: MetricSpec) -> bool:
    return metric.name in {"sum", "avg", "min", "max"} and (
        (entity == "boq_item" and metric.field == "quantity")
        or (entity == "quantity_fact" and metric.field == "area_m2")
    )


def _has_exact_source_sheet_pin(
    scope: Mapping[str, Any],
    filters: tuple[FilterSpec, ...],
) -> bool:
    pins: list[Any] = []
    if "source_sheet" in scope:
        value = scope["source_sheet"]
        if not _is_nonempty_json_scalar(value):
            return False
        pins.append(value)

    for item in filters:
        if item.field != "source_sheet":
            continue
        if item.operator == "eq" and _is_nonempty_json_scalar(item.value):
            pins.append(item.value)
            continue
        if (
            item.operator == "in"
            and isinstance(item.value, list | tuple)
            and len(item.value) == 1
            and _is_nonempty_json_scalar(item.value[0])
        ):
            pins.append(item.value[0])
            continue
        return False

    if not pins:
        return False
    first = pins[0]
    return all(type(value) is type(first) and value == first for value in pins[1:])


def _is_nonempty_json_scalar(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _parse_scope(
    value: Any,
    policy: EntityPolicy | None,
    issues: list[ContractIssue],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        issues.append(ContractIssue("invalid_type", "$.scope", "scope must be an object"))
        return {}
    result: dict[str, Any] = {}
    for raw_field, raw_value in value.items():
        field_name = str(raw_field)
        path = f"$.scope.{field_name}"
        if policy is not None and field_name not in policy.scope_fields:
            issues.append(ContractIssue("field_not_allowed", path, "field is not allowed in this entity scope"))
            continue
        if not _is_json_value(raw_value):
            issues.append(ContractIssue("invalid_value", path, "scope value must be JSON-compatible"))
            continue
        result[field_name] = _json_value(raw_value)
    return result


def _parse_filters(
    value: Any,
    policy: EntityPolicy | None,
    issues: list[ContractIssue],
) -> tuple[FilterSpec, ...]:
    if not isinstance(value, list | tuple):
        issues.append(ContractIssue("invalid_type", "$.filters", "filters must be an array"))
        return ()
    result: list[FilterSpec] = []
    for index, raw_filter in enumerate(value):
        path = f"$.filters[{index}]"
        if not isinstance(raw_filter, Mapping):
            issues.append(ContractIssue("invalid_type", path, "filter must be an object"))
            continue
        allowed_keys = {"field", "operator", "value"}
        for key in raw_filter:
            if str(key) not in allowed_keys:
                issues.append(ContractIssue("unknown_field", f"{path}.{key}", "field is not part of a filter"))
        field_name = _required_text(raw_filter.get("field"), f"{path}.field", issues)
        operator = _required_text(raw_filter.get("operator"), f"{path}.operator", issues).casefold()
        has_value = "value" in raw_filter
        expected = raw_filter.get("value")
        if policy is not None and field_name and field_name not in policy.fields:
            issues.append(ContractIssue("field_not_allowed", f"{path}.field", "field is not allowed for this entity"))
        if policy is not None and operator and operator not in policy.operators:
            issues.append(
                ContractIssue("operator_not_allowed", f"{path}.operator", "operator is not allowed for this entity")
            )
        if operator == "exists":
            if has_value and not isinstance(expected, bool):
                issues.append(ContractIssue("invalid_value", f"{path}.value", "exists value must be boolean"))
            expected = True if not has_value else expected
        elif not has_value:
            issues.append(ContractIssue("missing_field", f"{path}.value", "filter value is required"))
        elif operator in {"in", "not_in"} and not isinstance(expected, list | tuple):
            issues.append(ContractIssue("invalid_value", f"{path}.value", f"{operator} value must be an array"))
        if has_value and not _is_json_value(expected):
            issues.append(ContractIssue("invalid_value", f"{path}.value", "filter value must be JSON-compatible"))
        result.append(FilterSpec(field=field_name, operator=operator, value=_json_value(expected)))
    return tuple(result)


def _parse_group_by(
    value: Any,
    policy: EntityPolicy | None,
    issues: list[ContractIssue],
) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        issues.append(ContractIssue("invalid_type", "$.group_by", "group_by must be an array"))
        return ()
    result: list[str] = []
    for index, raw_field in enumerate(value):
        path = f"$.group_by[{index}]"
        field_name = _required_text(raw_field, path, issues)
        if policy is not None and field_name and field_name not in policy.fields:
            issues.append(ContractIssue("field_not_allowed", path, "group field is not allowed for this entity"))
        if field_name in result:
            issues.append(ContractIssue("duplicate_field", path, "group field must be unique"))
        elif field_name:
            result.append(field_name)
    return tuple(result)


def _parse_metrics(
    value: Any,
    policy: EntityPolicy | None,
    issues: list[ContractIssue],
) -> tuple[MetricSpec, ...]:
    if not isinstance(value, list | tuple):
        issues.append(ContractIssue("invalid_type", "$.metrics", "metrics must be an array"))
        return ()
    result: list[MetricSpec] = []
    aliases: set[str] = set()
    for index, raw_metric in enumerate(value):
        path = f"$.metrics[{index}]"
        if isinstance(raw_metric, str):
            metric_name = raw_metric.casefold().strip()
            field_name = "*" if metric_name == "count" else None
            alias = None
        elif isinstance(raw_metric, Mapping):
            allowed_keys = {"name", "field", "alias"}
            for key in raw_metric:
                if str(key) not in allowed_keys:
                    issues.append(ContractIssue("unknown_field", f"{path}.{key}", "field is not part of a metric"))
            metric_name = _required_text(raw_metric.get("name"), f"{path}.name", issues).casefold()
            raw_field = raw_metric.get("field")
            field_name = str(raw_field).strip() if raw_field is not None else None
            raw_alias = raw_metric.get("alias")
            alias = str(raw_alias).strip() if raw_alias is not None else None
        else:
            issues.append(ContractIssue("invalid_type", path, "metric must be a name or object"))
            continue

        allowed_fields = policy.metrics.get(metric_name) if policy is not None else None
        if policy is not None and allowed_fields is None:
            issues.append(ContractIssue("metric_not_allowed", f"{path}.name", "metric is not allowed for this entity"))
        elif allowed_fields is not None:
            if metric_name == "count":
                field_name = field_name or "*"
            elif not field_name:
                issues.append(ContractIssue("missing_field", f"{path}.field", f"{metric_name} requires a field"))
            if field_name and field_name not in allowed_fields:
                issues.append(
                    ContractIssue(
                        "metric_field_not_allowed",
                        f"{path}.field",
                        f"field is not allowed for {metric_name}",
                    )
                )
        if alias:
            if not alias.replace("_", "").isalnum() or alias[0].isdigit():
                issues.append(ContractIssue("invalid_alias", f"{path}.alias", "alias must be an identifier"))
            elif alias in aliases:
                issues.append(ContractIssue("duplicate_alias", f"{path}.alias", "metric alias must be unique"))
            aliases.add(alias)
        result.append(MetricSpec(name=metric_name, field=field_name, alias=alias))
    return tuple(result)


def _check_forbidden_keys(value: Any, path: str, issues: list[ContractIssue]) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if key.casefold() in FORBIDDEN_KEYS:
                issues.append(
                    ContractIssue(
                        "forbidden_field",
                        child_path,
                        "pack and snapshot selection is owned by the project snapshot resolver",
                    )
                )
            _check_forbidden_keys(child, child_path, issues)
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _check_forbidden_keys(child, f"{path}[{index}]", issues)


def _parse_reducer(
    value: Any,
    path: str,
    plan_ids: set[str],
    plan_entities: Mapping[str, str],
    prior_reducer_ids: set[str],
    prior_reducer_ops: Mapping[str, str],
    issues: list[ContractIssue],
) -> ReducerSpec | None:
    if not isinstance(value, Mapping):
        issues.append(ContractIssue("invalid_type", path, "reducer must be an object"))
        return None
    reducer_id = _required_text(value.get("id"), f"{path}.id", issues)
    op = _required_text(value.get("op"), f"{path}.op", issues).casefold()
    if reducer_id and not _BUNDLE_ID_PATTERN.fullmatch(reducer_id):
        issues.append(ContractIssue("invalid_id", f"{path}.id", "reducer id must be an ASCII identifier"))
    if reducer_id and reducer_id in prior_reducer_ids:
        issues.append(ContractIssue("duplicate_id", f"{path}.id", "reducer id must be unique"))
    field_rules = _REDUCER_FIELDS.get(op)
    if op and field_rules is None:
        issues.append(
            ContractIssue(
                "unsupported_reducer",
                f"{path}.op",
                f"allowed reducers: {', '.join(sorted(REDUCER_OPERATORS))}",
            )
        )
        return None

    required, optional = field_rules or (frozenset(), frozenset())
    allowed = frozenset({"id", "op"}) | required | optional
    for raw_key in value:
        key = str(raw_key)
        if key not in allowed:
            issues.append(ContractIssue("unknown_field", f"{path}.{key}", "field is not allowed for this reducer"))
    for key in sorted(required):
        if key not in value:
            issues.append(ContractIssue("missing_field", f"{path}.{key}", "field is required for this reducer"))

    config = {str(key): _json_value(item) for key, item in value.items() if str(key) not in {"id", "op"}}
    reference_fields = {"source", "left", "right", "edges", "target"}
    for key in reference_fields & config.keys():
        if key in {"left", "right"} and op in {"equals", "subtract"}:
            _validate_operand(config[key], f"{path}.{key}", plan_ids, prior_reducer_ids, issues)
        else:
            _validate_reference(config[key], f"{path}.{key}", plan_ids, prior_reducer_ids, issues)
    if op in {"extract_constraint", "extract_section_list"}:
        _validate_exact_plan_rows_reference(config.get("source"), f"{path}.source", issues)
    if op == "extract_constraint" and "profile" in config:
        _validate_profile(
            config["profile"],
            CONSTRAINT_PROFILES,
            f"{path}.profile",
            issues,
        )
    if op == "extract_dimension_token" and "profile" in config:
        _validate_profile(
            config["profile"],
            DIMENSION_PROFILES,
            f"{path}.profile",
            issues,
        )
        _validate_exact_plan_rows_reference(config.get("source"), f"{path}.source", issues)
    if op == "extract_section_list":
        _validate_section_list_config(config, path, issues)
    if op == "traverse_edge":
        if "profile" in config:
            _validate_profile(
                config["profile"],
                TRAVERSE_EDGE_PROFILES,
                f"{path}.profile",
                issues,
            )
        for key in ("source", "edges", "target"):
            if key in config:
                _validate_exact_plan_rows_reference(
                    config[key], f"{path}.{key}", issues
                )
    if op == "evaluate_decimal_formula":
        if "profile" in config:
            _validate_profile(
                config["profile"],
                DECIMAL_FORMULA_PROFILES,
                f"{path}.profile",
                issues,
            )
        _validate_decimal_formula_references(
            config,
            path,
            plan_ids,
            prior_reducer_ids,
            prior_reducer_ops,
            issues,
        )
    if op == "artifact_role_absence":
        _validate_exact_plan_values_entity_reference(
            config.get("source"),
            f"{path}.source",
            plan_entities,
            {"snapshot_pack"},
            issues,
        )
        generic_context = config.get("generic_context")
        if (
            not isinstance(generic_context, list | tuple)
            or not 1 <= len(generic_context) <= 8
        ):
            issues.append(
                ContractIssue(
                    "invalid_context_count",
                    f"{path}.generic_context",
                    "generic_context must contain from 1 to 8 coverage references",
                )
            )
        else:
            seen_context: set[str] = set()
            for index, operand in enumerate(generic_context):
                operand_path = f"{path}.generic_context[{index}]"
                _validate_operand(
                    operand, operand_path, plan_ids, prior_reducer_ids, issues
                )
                reference = (
                    operand.get("ref")
                    if isinstance(operand, Mapping)
                    and {str(key) for key in operand} == {"ref"}
                    else None
                )
                _validate_exact_plan_values_entity_reference(
                    reference,
                    operand_path,
                    plan_entities,
                    {"coverage_scan"},
                    issues,
                )
                if isinstance(reference, str):
                    if reference in seen_context:
                        issues.append(
                            ContractIssue(
                                "duplicate_operand",
                                operand_path,
                                "generic_context references must be unique",
                            )
                        )
                    seen_context.add(reference)
    if op == "round" and "digits" in config:
        digits = config["digits"]
        if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 6:
            issues.append(
                ContractIssue(
                    "invalid_digits",
                    f"{path}.digits",
                    "digits must be an integer from 0 to 6",
                )
            )
    if op == "satisfies_constraint":
        if "constraint" in config:
            _validate_reference(
                config["constraint"],
                f"{path}.constraint",
                plan_ids,
                prior_reducer_ids,
                issues,
            )
            parts = config["constraint"].split(".") if isinstance(config["constraint"], str) else []
            if (
                len(parts) != 2
                or parts[0] != "reducers"
                or prior_reducer_ops.get(parts[1]) != "extract_constraint"
            ):
                issues.append(
                    ContractIssue(
                        "invalid_constraint_reference",
                        f"{path}.constraint",
                        "constraint must reference the root of a preceding extract_constraint reducer",
                    )
                )
        if "actual" in config:
            _validate_operand(
                config["actual"],
                f"{path}.actual",
                plan_ids,
                prior_reducer_ids,
                issues,
            )
            if isinstance(config["actual"], Mapping) and "value" in config["actual"]:
                literal = config["actual"]
                if {str(key) for key in literal} != {"value", "unit"}:
                    issues.append(
                        ContractIssue(
                            "unit_required",
                            f"{path}.actual.unit",
                            "numeric literal actual requires an explicit unit",
                        )
                    )
                number = literal.get("value")
                if not _is_finite_json_number(number):
                    issues.append(
                        ContractIssue(
                            "invalid_value",
                            f"{path}.actual.value",
                            "actual literal must be a finite JSON number",
                        )
                    )
    if "inputs" in config:
        inputs = config["inputs"]
        if not isinstance(inputs, list | tuple) or not inputs:
            issues.append(ContractIssue("invalid_type", f"{path}.inputs", "inputs must be a non-empty array"))
        else:
            for index, operand in enumerate(inputs):
                _validate_operand(
                    operand,
                    f"{path}.inputs[{index}]",
                    plan_ids,
                    prior_reducer_ids,
                    issues,
                )
        if op in {"sum_values", "all_equal", "coverage_absence"} and isinstance(inputs, list | tuple) and any(
            not isinstance(item, Mapping) or {str(key) for key in item} != {"ref"}
            for item in inputs
        ):
            issues.append(
                ContractIssue(
                    "source_required",
                    f"{path}.inputs",
                    f"{op} accepts source references only",
                )
            )
        if op == "all_equal" and isinstance(inputs, list | tuple) and not 2 <= len(inputs) <= 8:
            issues.append(
                ContractIssue(
                    "invalid_input_count",
                    f"{path}.inputs",
                    "all_equal requires from 2 to 8 source references",
                )
            )
        if op == "coverage_absence" and isinstance(inputs, list | tuple):
            if not 1 <= len(inputs) <= 8:
                issues.append(
                    ContractIssue(
                        "invalid_input_count",
                        f"{path}.inputs",
                        "coverage_absence requires from 1 to 8 coverage references",
                    )
                )
            for index, operand in enumerate(inputs):
                reference = (
                    operand.get("ref")
                    if isinstance(operand, Mapping)
                    and {str(key) for key in operand} == {"ref"}
                    else None
                )
                _validate_exact_plan_values_entity_reference(
                    reference,
                    f"{path}.inputs[{index}]",
                    plan_entities,
                    {"coverage_scan"},
                    issues,
                )
    if op == "subtract" and all(
        not isinstance(config.get(key), Mapping)
        or {str(item) for item in config[key]} != {"ref"}
        for key in ("left", "right")
        if key in config
    ):
        issues.append(
            ContractIssue(
                "source_required",
                path,
                "subtract requires at least one source reference",
            )
        )
    if op == "subtract":
        for key in ("left", "right"):
            operand = config.get(key)
            if not isinstance(operand, Mapping) or "value" not in operand:
                continue
            if {str(item) for item in operand} != {"value", "unit"}:
                issues.append(
                    ContractIssue(
                        "unit_required",
                        f"{path}.{key}.unit",
                        "subtract literal operands require an explicit unit",
                    )
                )
            number = operand.get("value")
            if not _is_finite_json_number(number):
                issues.append(
                    ContractIssue(
                        "invalid_value",
                        f"{path}.{key}.value",
                        "subtract literal must be a finite JSON number",
                    )
                )
    if op == "equals" and all(
        not isinstance(config.get(key), Mapping)
        or {str(item) for item in config[key]} != {"ref"}
        for key in ("left", "right")
        if key in config
    ):
        issues.append(
            ContractIssue(
                "source_required",
                path,
                "equals requires at least one source reference",
            )
        )
    _validate_unique_reducer_operands(op, config, path, issues)
    for key in {"field", "left_key", "right_key", "value_field"} & config.keys():
        if not isinstance(config[key], str) or not config[key].strip():
            issues.append(ContractIssue("invalid_field", f"{path}.{key}", "non-empty field name is required"))
    if "limit" in config and (
        isinstance(config["limit"], bool)
        or not isinstance(config["limit"], int)
        or not 1 <= config["limit"] <= 100
    ):
        issues.append(ContractIssue("invalid_limit", f"{path}.limit", "limit must be an integer from 1 to 100"))
    return ReducerSpec(id=reducer_id, op=op, config=config) if reducer_id and op in REDUCER_OPERATORS else None


def _validate_decimal_formula_references(
    config: Mapping[str, Any],
    path: str,
    plan_ids: set[str],
    prior_reducer_ids: set[str],
    prior_reducer_ops: Mapping[str, str],
    issues: list[ContractIssue],
) -> None:
    expected_suffixes = {
        "formula": "formula",
        "source_type": "formula_source_type",
        "unit": "normalized_unit",
    }
    roots: set[tuple[str, str]] = set()
    for key, expected_suffix in expected_suffixes.items():
        if key not in config:
            continue
        value = config[key]
        _validate_reference(
            value,
            f"{path}.{key}",
            plan_ids,
            prior_reducer_ids,
            issues,
        )
        parts = value.split(".") if isinstance(value, str) else []
        if (
            len(parts) != 3
            or parts[0] != "reducers"
            or prior_reducer_ops.get(parts[1]) != "traverse_edge"
            or parts[2] != expected_suffix
        ):
            issues.append(
                ContractIssue(
                    "invalid_formula_reference",
                    f"{path}.{key}",
                    "decimal formula inputs must use the matching field of one preceding traverse_edge reducer",
                )
            )
            continue
        roots.add((parts[0], parts[1]))
    if len(roots) > 1:
        issues.append(
            ContractIssue(
                "mixed_formula_source",
                path,
                "formula, source_type, and unit must come from the same traversal result",
            )
        )


def _validate_exact_plan_rows_reference(
    value: Any,
    path: str,
    issues: list[ContractIssue],
) -> None:
    parts = value.split(".") if isinstance(value, str) else []
    if len(parts) != 3 or parts[0] != "plans" or parts[2] != "rows":
        issues.append(
            ContractIssue(
                "invalid_source_reference",
                path,
                "extractor source must be exactly plans.<plan_id>.rows",
            )
        )


def _validate_exact_plan_values_entity_reference(
    value: Any,
    path: str,
    plan_entities: Mapping[str, str],
    allowed_entities: set[str] | frozenset[str],
    issues: list[ContractIssue],
) -> None:
    parts = value.split(".") if isinstance(value, str) else []
    if len(parts) != 3 or parts[0] != "plans" or parts[2] != "values":
        issues.append(
            ContractIssue(
                "invalid_coverage_reference",
                path,
                "coverage reducers require exactly plans.<plan_id>.values",
            )
        )
        return
    entity = plan_entities.get(parts[1])
    if entity is not None and entity not in allowed_entities:
        issues.append(
            ContractIssue(
                "invalid_coverage_source",
                path,
                "coverage reducer source must use entity "
                + " or ".join(sorted(allowed_entities)),
            )
        )


def _validate_profile(
    value: Any,
    allowed: frozenset[str],
    path: str,
    issues: list[ContractIssue],
) -> None:
    if not isinstance(value, str) or value not in allowed:
        issues.append(
            ContractIssue(
                "unsupported_profile",
                path,
                f"allowed profiles: {', '.join(sorted(allowed))}",
            )
        )


def _validate_section_list_config(
    config: Mapping[str, Any],
    path: str,
    issues: list[ContractIssue],
) -> None:
    section = config.get("section")
    if not isinstance(section, str) or _SECTION_PATTERN.fullmatch(section) is None:
        issues.append(
            ContractIssue(
                "invalid_section",
                f"{path}.section",
                "section must contain two to four dot-separated numeric components",
            )
        )
    for field_name, max_length in (("heading", 200), ("item_prefix", 200)):
        if field_name not in config:
            continue
        value = config[field_name]
        if not isinstance(value, str) or not value.strip() or len(value) > max_length:
            issues.append(
                ContractIssue(
                    "invalid_literal",
                    f"{path}.{field_name}",
                    f"{field_name} must be a non-empty literal of at most {max_length} characters",
                )
            )
    if "max_items" in config:
        max_items = config["max_items"]
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or not 1 <= max_items <= 50
        ):
            issues.append(
                ContractIssue(
                    "invalid_max_items",
                    f"{path}.max_items",
                    "max_items must be an integer from 1 to 50",
                )
            )


def _validate_unique_reducer_operands(
    op: str,
    config: Mapping[str, Any],
    path: str,
    issues: list[ContractIssue],
) -> None:
    if op in {"equals", "subtract"} and "left" in config and "right" in config:
        left = _operand_identity(config["left"])
        right = _operand_identity(config["right"])
        if left is not None and left == right:
            issues.append(
                ContractIssue(
                    "duplicate_operand",
                    f"{path}.right",
                    f"{op} operands must refer to two distinct inputs",
                )
            )

    if op not in {"sum_values", "all_equal", "coverage_absence"} or not isinstance(
        config.get("inputs"), list | tuple
    ):
        return
    seen: dict[tuple[Any, ...], int] = {}
    for index, operand in enumerate(config["inputs"]):
        identity = _operand_identity(operand)
        if identity is None:
            continue
        if identity in seen:
            issues.append(
                ContractIssue(
                    "duplicate_operand",
                    f"{path}.inputs[{index}]",
                    f"operand duplicates inputs[{seen[identity]}]",
                )
            )
        else:
            seen[identity] = index


def _operand_identity(value: Any) -> tuple[Any, ...] | None:
    if not isinstance(value, Mapping):
        return None
    keys = {str(key) for key in value}
    if keys == {"ref"} and isinstance(value.get("ref"), str):
        return ("ref", value["ref"])
    if keys not in ({"value"}, {"value", "unit"}) or not _is_json_value(value.get("value")):
        return None
    return (
        "literal",
        _typed_json_identity(value.get("value")),
        ("unit", value.get("unit")) if "unit" in value else ("unit", None),
    )


def _typed_json_identity(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Mapping):
        return (
            "object",
            tuple(
                (str(key), _typed_json_identity(item))
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            ),
        )
    if isinstance(value, list | tuple):
        return ("array", tuple(_typed_json_identity(item) for item in value))
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, int):
        return ("integer", value)
    if isinstance(value, float):
        return ("float", repr(value))
    return (type(value).__name__, repr(value))


def _validate_operand(
    value: Any,
    path: str,
    plan_ids: set[str],
    prior_reducer_ids: set[str],
    issues: list[ContractIssue],
) -> None:
    if not isinstance(value, Mapping):
        issues.append(ContractIssue("invalid_operand", path, "operand must contain exactly one of ref or value"))
        return
    keys = {str(key) for key in value}
    if keys == {"ref"}:
        _validate_reference(value.get("ref"), f"{path}.ref", plan_ids, prior_reducer_ids, issues)
    elif keys in ({"value"}, {"value", "unit"}):
        if not _is_json_value(value.get("value")):
            issues.append(ContractIssue("invalid_value", f"{path}.value", "literal must be JSON-compatible"))
        if "unit" in value and (not isinstance(value.get("unit"), str) or not value.get("unit", "").strip()):
            issues.append(ContractIssue("invalid_unit", f"{path}.unit", "unit must be a non-empty string"))
    else:
        issues.append(
            ContractIssue(
                "invalid_operand",
                path,
                "operand must contain ref, or value with an optional unit",
            )
        )


def _validate_reference(
    value: Any,
    path: str,
    plan_ids: set[str],
    prior_reducer_ids: set[str],
    issues: list[ContractIssue],
) -> None:
    if not isinstance(value, str) or not value.strip():
        issues.append(ContractIssue("invalid_reference", path, "reference must be a non-empty string"))
        return
    parts = value.split(".")
    if any(not part for part in parts):
        issues.append(ContractIssue("invalid_reference", path, "reference contains an empty path segment"))
        return
    if parts[0] == "plans":
        if len(parts) < 3 or parts[1] not in plan_ids or parts[2] not in {"values", "rows"}:
            issues.append(
                ContractIssue(
                    "unknown_reference",
                    path,
                    "plan reference must start with plans.<plan_id>.values or plans.<plan_id>.rows",
                )
            )
    elif parts[0] == "reducers":
        if len(parts) < 2 or parts[1] not in prior_reducer_ids:
            issues.append(
                ContractIssue(
                    "forward_or_unknown_reference",
                    path,
                    "reducer references may target preceding reducers only",
                )
            )
    else:
        issues.append(ContractIssue("unknown_reference", path, "reference root must be plans or reducers"))


def _prefix_issues(issues: tuple[ContractIssue, ...], prefix: str) -> list[ContractIssue]:
    prefixed: list[ContractIssue] = []
    for issue in issues:
        suffix = issue.path[1:] if issue.path.startswith("$") else f".{issue.path}"
        prefixed.append(ContractIssue(issue.code, f"{prefix}{suffix}", issue.message))
    return prefixed


def _required_text(value: Any, path: str, issues: list[ContractIssue]) -> str:
    if not isinstance(value, str) or not value.strip():
        issues.append(ContractIssue("missing_field", path, "non-empty string is required"))
        return ""
    return value.strip()


def _is_json_value(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if value is None or isinstance(value, str | int | bool):
        return True
    if isinstance(value, list | tuple):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _is_finite_json_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "BIMQueryPlan",
    "ContractIssue",
    "COVERAGE_SCAN_PROFILES",
    "ENTITY_POLICIES",
    "EntityPolicy",
    "FilterSpec",
    "MetricSpec",
    "NamedQueryPlan",
    "PLAN_BUNDLE_FIELDS",
    "PlanBundle",
    "PlanBundleContractError",
    "PlanBundleValidation",
    "QueryContractError",
    "QueryPlanValidation",
    "REDUCER_OPERATORS",
    "ReducerSpec",
    "SNAPSHOT_PACK_ABSENCE_ROLES",
    "try_validate_plan_bundle",
    "try_validate_query_plan",
    "validate_plan_bundle",
    "validate_query_plan",
]
