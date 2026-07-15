from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .coverage_scan import (
    CoverageScanError,
    scan_snapshot_nodes,
    scan_snapshot_pack_role,
)
from .decimal_formula import DecimalFormulaError, evaluate_decimal_additive
from .query_contract import (
    BIMQueryPlan,
    COVERAGE_SCAN_PROFILES,
    PlanBundle,
    ReducerSpec,
    validate_plan_bundle,
    validate_query_plan,
)


class QueryExecutionError(RuntimeError):
    """Raised when a validated query cannot be executed completely."""


class MixedUnitError(QueryExecutionError):
    """Raised when a sum would combine values with different units."""


class BundleReductionError(QueryExecutionError):
    """Raised when a validated reducer cannot produce one complete value."""


ENTITY_NODE_TYPES: dict[str, frozenset[str]] = {
    # Public query-contract names.  The aliases below keep the lower-level
    # names working for existing callers while allowing a validated
    # BIMQueryPlan to flow into this executor without an ad-hoc adapter.
    "element": frozenset({"BIMElement"}),
    "type": frozenset({"BIMElementType"}),
    "level": frozenset({"BIMLevel"}),
    "specification": frozenset({"Requirement", "RequirementReference", "LawRequirementReference"}),
    "drawing_entity": frozenset({"DxfInsert", "DxfText", "DxfHatch", "DxfDimension", "DrawingAnnotation"}),
    "sheet": frozenset({"DrawingSheet"}),
    "view": frozenset({"DrawingView"}),
    "bim_element": frozenset({"BIMElement"}),
    # The project module universe is the complete set of named Revit
    # UserWorksets, including valid modules that currently contain no model
    # elements. BIMModuleZone omits such empty modules and is therefore not a
    # safe denominator for absence questions.
    "module": frozenset({"BIMWorkset"}),
    "inventory_aggregate": frozenset({"InventoryAggregate"}),
    "quantity_fact": frozenset({"BIMQuantityFact"}),
    "quantity_aggregate": frozenset({"QuantityAggregate"}),
    "drawing_sheet": frozenset({"DrawingSheet"}),
    "drawing_view": frozenset({"DrawingView"}),
    "drawing_file": frozenset({"DxfDrawingFile", "SheetPdfFileResource"}),
    "project_metadata": frozenset({"BIMProject", "BIMExportRun"}),
    "schedule": frozenset({"DrawingSchedule", "ScheduleCell"}),
    "requirement": frozenset({"Requirement", "RequirementReference", "LawRequirementReference"}),
    "boq_item": frozenset(
        {
            "EstimateItem",
            "BOQItem",
            "AggregatedBOQItem",
            "SiteWorkItem",
            "QuantityEvidence",
            "QuantityTakeoffEvidence",
        }
    ),
    "bim_reference": frozenset({"BIMReference"}),
    # Relationship records are read from graph/edges.jsonl and do not carry a
    # node_type discriminator.
    "relationship_edge": frozenset(),
}

DEFAULT_ENTITY_ROLES: dict[str, frozenset[str]] = {
    "element": frozenset({"model_inventory"}),
    "type": frozenset({"project_reference"}),
    "level": frozenset({"project_reference"}),
    "specification": frozenset({"spec", "law", "bridge"}),
    "drawing_entity": frozenset({"drawing_entity"}),
    "sheet": frozenset({"drawing_reference"}),
    "view": frozenset({"drawing_reference"}),
    "bim_element": frozenset({"model_inventory"}),
    "module": frozenset({"project_reference"}),
    "inventory_aggregate": frozenset({"inventory_aggregate"}),
    "quantity_fact": frozenset({"model_quantity"}),
    "quantity_aggregate": frozenset({"quantity_aggregate"}),
    "drawing_sheet": frozenset({"drawing_reference"}),
    "drawing_view": frozenset({"drawing_reference"}),
    "drawing_file": frozenset({"drawing_reference"}),
    "project_metadata": frozenset({"project_reference"}),
    "schedule": frozenset({"drawing_reference"}),
    "requirement": frozenset({"spec", "law", "bridge"}),
    "boq_item": frozenset({"boq"}),
    "bim_reference": frozenset({"boq"}),
    "relationship_edge": frozenset({"boq"}),
}

_DRAWING_COVERAGE_ROLES = [
    "drawing_entity",
    "drawing_representation_primary",
    "drawing_representation_topology",
    "drawing_representation_index",
    "drawing_representation_review",
    "drawing_representation_qa",
    "drawing_reference",
]

# Public coverage plans select one audited schema profile plus bounded literal
# terms.  Callers never provide roles, node types, fields, filters, or regexes.
_COVERAGE_PROFILE_SPECS: Mapping[str, Mapping[str, Any]] = {
    "boq_wall_item_exact_token": {
        "roles": ["boq"],
        "node_types": ["BOQItem", "AggregatedBOQItem"],
        "search_fields": ["item_name", "specification"],
        "scope_filters": [
            {"field": "work_category", "operator": "eq", "value": "벽체"}
        ],
        "search_op": "exact_token",
    },
    "model_wall_type_exact_token": {
        "roles": ["model_inventory"],
        "node_types": ["BIMElement"],
        "search_fields": ["type_name"],
        "scope_filters": [
            {"field": "category", "operator": "eq", "value": "벽"}
        ],
        "search_op": "exact_token",
    },
    "boq_estimate_fact_text": {
        "roles": ["boq"],
        "node_types": [
            "BOQItem",
            "EstimateItem",
            "SiteWorkItem",
            "AggregatedBOQItem",
            "QuantityTakeoffEvidence",
        ],
        "search_fields": [
            "label",
            "module_type",
            "item_name",
            "specification",
            "note",
        ],
        "search_op": "contains_any",
    },
    "model_floor_context_text": {
        "roles": ["model_inventory"],
        "node_types": ["BIMElement"],
        "search_fields": ["level", "category", "type_name"],
        "search_op": "contains_any",
    },
    "model_lift_context_text": {
        "roles": ["model_inventory"],
        "node_types": ["BIMElement"],
        "search_fields": [
            "category", "family_name", "type_name", "parameters_sample"
        ],
        "nested_fields": {
            "parameters_sample": {"max_depth": 4, "max_leaves": 1024}
        },
        "search_op": "contains_any",
    },
    "model_structural_context_text": {
        "roles": ["model_inventory"],
        "node_types": ["BIMElement"],
        "search_fields": ["label", "type_name"],
        "search_op": "contains_any",
    },
    "drawing_sheet_text": {
        "roles": _DRAWING_COVERAGE_ROLES,
        "node_types": ["DrawingSheet"],
        "search_fields": ["label", "sheet_number", "sheet_name"],
        "search_op": "contains_any",
    },
    "drawing_schedule_text": {
        "roles": _DRAWING_COVERAGE_ROLES,
        "node_types": ["DrawingSchedule", "ScheduleCell"],
        "search_fields": ["label", "schedule_name", "text"],
        "search_op": "contains_any",
    },
    "boq_work_item_text": {
        "roles": ["boq"],
        "node_types": [
            "WorkCategory",
            "BOQItem",
            "EstimateItem",
            "AggregatedBOQItem",
        ],
        "search_fields": [
            "label",
            "name",
            "work_category",
            "item_name",
            "specification",
            "note",
        ],
        "search_op": "contains_any",
    },
    "model_quantity_text": {
        "roles": ["model_quantity"],
        "node_types": ["BIMQuantityFact"],
        "search_fields": ["label", "type_name"],
        "search_op": "contains_any",
    },
    "spec_requirement_text": {
        "roles": ["spec"],
        "node_types": ["Requirement"],
        "search_fields": ["label", "requirement_text"],
        "search_op": "contains_any",
    },
    "boq_graph_text": {
        "roles": ["boq"],
        "node_types": [
            "BIMReference",
            "BOQItem",
            "EstimateItem",
            "SiteWorkItem",
            "AggregatedBOQItem",
            "QuantityTakeoffEvidence",
            "WorkCategory",
            "EstimateSheet",
            "EstimateWorkbook",
            "Formula",
            "ModuleType",
            "Project",
            "Unit",
        ],
        "search_fields": ["label", "name", "item_name", "specification"],
        "search_op": "contains_any",
    },
    "law_entity_text": {
        "roles": ["law"],
        "node_types": ["Entity"],
        "search_fields": ["label", "name"],
        "search_op": "contains_any",
    },
}

if frozenset(_COVERAGE_PROFILE_SPECS) != COVERAGE_SCAN_PROFILES:
    raise RuntimeError("coverage profile contract and executor registry differ")

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "object_id": ("source_element_id", "element_id", "unique_id", "id"),
    "source_element_id": ("source_element_id", "element_id", "unique_id", "id"),
    "module_id": ("module_id", "module_name", "workset_name", "workset", "name"),
    "workset_name": ("workset_name", "workset", "module_name", "name"),
    "level_name": ("level_name", "level", "derived_level", "name"),
    "level": ("level", "derived_level"),
    "type_id": ("type_id", "type_name", "family_and_type"),
    "text": ("text", "decoded_text"),
    "sheet_number": ("sheet_number", "source_context.sheet_number"),
    "sheet_name": ("sheet_name", "source_context.sheet_name"),
    "file_name": ("dxf_file_name", "pdf_file_name"),
    "source_file": ("source_file", "dxf_file_name", "pdf_file_name"),
    "pack_id": ("pack_id", "_pack_id"),
    # These three fields are executor-bound provenance.  Never trust a source
    # node or edge property with the same spelling.
    "source_pack_id": ("_pack_id",),
    "source_pack_sha256": ("_pack_hash",),
    "source_role": ("_source_role",),
    "type": ("type", "measurement_type"),
}

# These aliases identify one public fact assembled from alternative source
# schemas.  Conflicting populated candidates are malformed evidence and must
# not be resolved by whichever candidate happens to appear first.
STRICT_FIELD_ALIASES = frozenset(
    {"sheet_number", "sheet_name", "file_name", "type"}
)

CANONICAL_OBJECT_EXCLUDED_CLASSES = frozenset({"Autodesk.Revit.DB.ModelLine"})
CANONICAL_OBJECT_EXCLUDED_CATEGORIES = frozenset({"<스케치>", "범례 구성요소"})
CANONICAL_MODULE_PATTERN = re.compile(r"^[12]-\d{2}-[A-Z]+$")


@dataclass(frozen=True)
class QueryResult:
    snapshot_id: str
    query_hash: str
    result_hash: str
    complete: bool
    source_packs: tuple[dict[str, Any], ...]
    scanned_records: int
    matched_records: int
    duplicate_records_removed: int
    values: dict[str, Any]
    rows: tuple[dict[str, Any], ...]
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "query_hash": self.query_hash,
            "result_hash": self.result_hash,
            "complete": self.complete,
            "source_packs": list(self.source_packs),
            "scanned_records": self.scanned_records,
            "matched_records": self.matched_records,
            "duplicate_records_removed": self.duplicate_records_removed,
            "values": self.values,
            "rows": list(self.rows),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class QueryBundleResult:
    snapshot_id: str
    bundle_hash: str
    query_hash: str
    result_hash: str
    complete: bool
    plan_results: Mapping[str, QueryResult]
    reducer_values: Mapping[str, Any]
    reducer_units: Mapping[str, str | None]
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "bundle_hash": self.bundle_hash,
            "query_hash": self.query_hash,
            "result_hash": self.result_hash,
            "complete": self.complete,
            "plans": {plan_id: result.as_dict() for plan_id, result in self.plan_results.items()},
            "reducers": _json_safe(self.reducer_values),
            "reducer_units": _json_safe(self.reducer_units),
            "evidence": _json_safe(self.evidence),
        }


def execute_query_bundle(
    bundle: Mapping[str, Any] | PlanBundle,
    snapshot: Mapping[str, Any] | object,
) -> QueryBundleResult:
    """Execute all plans and whitelist reducers as one fail-closed unit.

    No reducer runs until every nested seven-field plan has completed.  A
    missing reference, duplicate join key, non-numeric arithmetic input, or
    empty/ambiguous singleton aborts the whole bundle without a partial result.
    """

    if isinstance(bundle, PlanBundle):
        # Dataclasses are public construction helpers, not a validation bypass.
        validated = validate_plan_bundle(bundle.as_dict())
    else:
        validated = validate_plan_bundle(_plain_mapping(bundle))
    snapshot_data = _plain_mapping(snapshot)
    snapshot_project_id = str(snapshot_data.get("project_id") or "").strip()
    if snapshot_project_id and snapshot_project_id != validated.project_id:
        raise QueryExecutionError(
            f"bundle project {validated.project_id!r} does not match snapshot project {snapshot_project_id!r}"
        )

    plan_results: dict[str, QueryResult] = {}
    reference_context: dict[str, Any] = {"plans": {}, "reducers": {}}
    unit_context: dict[str, str | None] = {}
    for named_plan in sorted(validated.plans, key=lambda item: item.id):
        result = execute_query(named_plan.plan, snapshot_data)
        if not result.complete:
            raise QueryExecutionError(f"bundle plan {named_plan.id!r} did not complete")
        plan_results[named_plan.id] = result
        reference_context["plans"][named_plan.id] = {
            "values": result.values,
            "rows": list(result.rows),
            # Internal provenance for fixed traversal reducers.  The public
            # reference grammar still permits only values/rows.
            "source_packs": list(result.source_packs),
            "duplicate_records_removed": result.duplicate_records_removed,
        }
        unit_context.update(_plan_result_units(named_plan.id, named_plan.plan, result))

    reducer_values: dict[str, Any] = {}
    reducer_units: dict[str, str | None] = {}
    for reducer in validated.reducers:
        output = _execute_reducer(reducer, reference_context, unit_context)
        reducer_values[reducer.id] = output.value
        reducer_units[reducer.id] = output.unit
        reference_context["reducers"][reducer.id] = output.value
        root_reference = f"reducers.{reducer.id}"
        unit_context[root_reference] = output.unit
        for suffix, nested_unit in (output.value_units or {}).items():
            canonical = _canonical_unit(nested_unit)
            if canonical is None:
                raise BundleReductionError(
                    f"reducer {reducer.id!r} produced an invalid unit for {suffix!r}"
                )
            unit_context[f"{root_reference}.{suffix}"] = canonical

    bundle_hash = _sha256_text(_canonical_json(validated.as_dict()))
    snapshot_ids = {result.snapshot_id for result in plan_results.values()}
    if len(snapshot_ids) != 1:
        raise QueryExecutionError("bundle plans did not bind to one snapshot")
    snapshot_id = next(iter(snapshot_ids))
    evidence = {
        "kind": "BUNDLE",
        "snapshot_id": snapshot_id,
        "bundle_hash": bundle_hash,
        "query_hash": bundle_hash,
        "reducer_units": reducer_units,
        "value_units": {
            reference: unit
            for reference, unit in sorted(unit_context.items())
            if unit is not None
        },
        "plan_bindings": {
            plan_id: {
                "query_hash": result.query_hash,
                "result_hash": result.result_hash,
                "source_packs": list(result.source_packs),
            }
            for plan_id, result in plan_results.items()
        },
    }
    result_payload = {
        "snapshot_id": snapshot_id,
        "bundle_hash": bundle_hash,
        "query_hash": bundle_hash,
        "complete": True,
        "plans": {plan_id: result.as_dict() for plan_id, result in plan_results.items()},
        "reducers": reducer_values,
        "reducer_units": reducer_units,
        "evidence": evidence,
    }
    result_hash = _sha256_text(_canonical_json(result_payload))
    evidence["result_hash"] = result_hash
    return QueryBundleResult(
        snapshot_id=snapshot_id,
        bundle_hash=bundle_hash,
        query_hash=bundle_hash,
        result_hash=result_hash,
        complete=True,
        plan_results=plan_results,
        reducer_values=reducer_values,
        reducer_units=reducer_units,
        evidence=evidence,
    )


def execute_query(plan: Mapping[str, Any] | object, snapshot: Mapping[str, Any] | object) -> QueryResult:
    """Execute a typed query against every row in the pinned snapshot.

    This executor intentionally bypasses the sampled graph and SQLite search index.
    Every selected pack member is streamed to EOF, filtered, and aggregated in
    Python. Nodes are deduplicated by canonical ID. Relationship edges are
    deliberately not deduplicated so duplicate evidence remains observable and
    fixed traversal reducers can fail closed. The LLM is not involved in arithmetic.
    """

    plan_data = _plain_mapping(plan)
    snapshot_data = _plain_mapping(snapshot)
    entity = str(plan_data.get("entity") or "").strip()
    if not entity:
        raise QueryExecutionError("query plan is missing entity")
    project_id = str(plan_data.get("project_id") or "").strip()
    snapshot_project_id = str(snapshot_data.get("project_id") or "").strip()
    if project_id and snapshot_project_id and project_id != snapshot_project_id:
        raise QueryExecutionError(
            f"query project {project_id!r} does not match snapshot project {snapshot_project_id!r}"
        )
    if entity.casefold() in {"coverage_scan", "snapshot_pack"}:
        validated_coverage_plan = validate_query_plan(plan_data)
        return _execute_coverage_query(
            validated_coverage_plan.as_dict(), snapshot_data
        )
    if entity in {"element", "bim_element"} and not plan_data.get("entity_granularity"):
        plan_data["entity_granularity"] = "canonical_bim_object"
    if entity == "module" and not plan_data.get("entity_granularity"):
        plan_data["entity_granularity"] = "canonical_module_registry"
    node_types = _node_types(plan_data, entity)
    entries = _selected_entries(plan_data, snapshot_data, entity)
    if not entries:
        raise QueryExecutionError(f"snapshot contains no included packs for entity {entity!r}")

    canonical_plan = _canonical_json(plan_data)
    query_hash = _sha256_text(canonical_plan)
    scanned_records = 0
    duplicate_records_removed = 0
    matched: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    source_packs: list[dict[str, Any]] = []
    contribution_hasher = hashlib.sha256()
    evidence_samples: list[dict[str, Any]] = []

    for entry in entries:
        path = _entry_path(entry, snapshot_data)
        pack_id = str(entry.get("pack_id") or path.stem)
        expected_hash = str(entry.get("sha256") or "")
        actual_hash = _sha256_file(path) if path.is_file() else _hash_directory_pack(path)
        if expected_hash and actual_hash.casefold() != expected_hash.casefold():
            raise QueryExecutionError(
                f"snapshot pack hash mismatch for {pack_id}: expected {expected_hash}, got {actual_hash}"
            )
        source_packs.append(
            {
                "pack_id": pack_id,
                "role": entry.get("role"),
                "sha256": actual_hash,
                "shard_index": entry.get("shard_index"),
            }
        )
        records = (
            _iter_pack_edges(path)
            if entity == "relationship_edge"
            else _iter_pack_nodes(path)
        )
        for node in records:
            scanned_records += 1
            node_type = str(node.get("node_type") or node.get("type") or "")
            if node_types and node_type not in node_types:
                continue
            row = _flatten_node(
                node,
                pack_id=pack_id,
                pack_hash=actual_hash,
                source_role=str(entry.get("role") or ""),
            )
            if not _matches_granularity(row, plan_data):
                continue
            if not _matches_scope(row, plan_data.get("scope")):
                continue
            if not _matches_filters(row, plan_data.get("filters")):
                continue
            dedupe_key = _dedupe_key(row, plan_data)
            if entity != "relationship_edge":
                if dedupe_key in seen_ids:
                    duplicate_records_removed += 1
                    continue
                seen_ids.add(dedupe_key)
            matched.append(row)
            contribution_hasher.update(dedupe_key.encode("utf-8"))
            contribution_hasher.update(b"\n")
            if len(evidence_samples) < 20:
                evidence_samples.append(_evidence_sample(node, row))

    group_by = tuple(str(field) for field in plan_data.get("group_by") or ())
    metrics = _normalize_metrics(plan_data.get("metrics"), entity=entity)
    if not metrics:
        metrics = ({"agg": "count_distinct", "field": "object_id", "as": "count"},)
    _validate_execution_output_names(group_by, metrics)

    values: dict[str, Any] = {}
    result_rows: list[dict[str, Any]] = []
    if group_by:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in matched:
            grouped[tuple(_field(row, field) for field in group_by)].append(row)
        for group_key, group_rows in grouped.items():
            output = {field: value for field, value in zip(group_by, group_key, strict=True)}
            metric_output = _aggregate_metrics(group_rows, metrics)
            collisions = sorted(set(output).intersection(metric_output))
            if collisions:
                raise QueryExecutionError(
                    f"output name collision: {', '.join(repr(name) for name in collisions)}"
                )
            output.update(metric_output)
            result_rows.append(output)
        result_rows = _sort_result_rows(result_rows, plan_data.get("order_by"), group_by, metrics)
        offset = max(0, int(plan_data.get("offset") or 0))
        limit_value = plan_data.get("limit")
        end = None if limit_value is None else offset + max(0, int(limit_value))
        result_rows = result_rows[offset:end]
    else:
        values = _aggregate_metrics(matched, metrics)

    snapshot_id = _snapshot_result_id(snapshot_data)
    source_packs.sort(key=lambda item: (str(item.get("role")), int(item.get("shard_index") or 0), item["pack_id"]))
    evidence = {
        "kind": "AGG",
        "snapshot_id": snapshot_id,
        "query_hash": query_hash,
        "scope_hash": _sha256_text(_canonical_json(plan_data.get("scope") or {})),
        "contribution_digest": contribution_hasher.hexdigest(),
        "contribution_count": len(matched),
        "samples": evidence_samples,
    }
    result_payload = {
        "snapshot_id": snapshot_id,
        "query_hash": query_hash,
        "complete": True,
        "source_packs": source_packs,
        "scanned_records": scanned_records,
        "matched_records": len(matched),
        "duplicate_records_removed": duplicate_records_removed,
        "values": values,
        "rows": result_rows,
        "evidence": evidence,
    }
    result_hash = _sha256_text(_canonical_json(result_payload))
    evidence["result_hash"] = result_hash
    return QueryResult(
        snapshot_id=snapshot_id,
        query_hash=query_hash,
        result_hash=result_hash,
        complete=True,
        source_packs=tuple(source_packs),
        scanned_records=scanned_records,
        matched_records=len(matched),
        duplicate_records_removed=duplicate_records_removed,
        values=values,
        rows=tuple(result_rows),
        evidence=evidence,
    )


def _execute_coverage_query(
    plan_data: Mapping[str, Any],
    snapshot_data: Mapping[str, Any],
) -> QueryResult:
    entity = str(plan_data["entity"])
    scope = plan_data.get("scope")
    if not isinstance(scope, Mapping):
        raise QueryExecutionError("coverage query requires a structured scope")
    canonical_plan = _canonical_json(plan_data)
    query_hash = _sha256_text(canonical_plan)
    try:
        if entity == "coverage_scan":
            profile_name = str(scope["profile"])
            profile = _COVERAGE_PROFILE_SPECS.get(profile_name)
            if profile is None:
                raise QueryExecutionError(
                    f"unsupported coverage profile {profile_name!r}"
                )
            scan_spec: dict[str, Any] = {
                "roles": list(profile["roles"]),
                "node_types": list(profile["node_types"]),
                "search_fields": list(profile["search_fields"]),
                "search": {
                    "op": str(profile["search_op"]),
                    "terms": list(scope["terms"]),
                },
            }
            if profile.get("scope_filters") is not None:
                scan_spec["scope_filters"] = _json_safe(
                    profile["scope_filters"]
                )
            if profile.get("nested_fields") is not None:
                scan_spec["nested_fields"] = _json_safe(
                    profile["nested_fields"]
                )
            allowed_fields = set(scan_spec["search_fields"])
            allowed_fields.update(
                str(item["field"])
                for item in scan_spec.get("scope_filters", [])
            )
            certificate = scan_snapshot_nodes(
                snapshot_data,
                scan_spec,
                allowed_fields=allowed_fields,
            )
            if certificate.get("coverage_complete") is not True:
                raise QueryExecutionError(
                    "coverage scanner returned an incomplete certificate"
                )
            source_packs = list(certificate["selected_source_packs"])
            scanned_records = int(certificate["scanned_record_count"])
            matched_records = int(certificate["match_count"])
            duplicate_records_removed = int(
                certificate["duplicate_records_removed"]
            )
            digest = str(certificate["coverage_digest"])
        elif entity == "snapshot_pack":
            certificate = scan_snapshot_pack_role(
                snapshot_data, str(scope["role"])
            )
            completeness = certificate.get("certificate")
            if (
                not isinstance(completeness, Mapping)
                or completeness.get("coverage_complete") is not True
            ):
                raise QueryExecutionError(
                    "snapshot role scanner returned an incomplete certificate"
                )
            source_packs = list(certificate["source_pack_descriptors"])
            scanned_records = int(certificate["manifest_entry_count"])
            matched_records = int(certificate["role_match_count"])
            duplicate_records_removed = 0
            digest = str(certificate["digest"])
        else:  # pragma: no cover - validated plan routes only the two entities
            raise QueryExecutionError(f"unsupported coverage entity {entity!r}")
    except CoverageScanError as exc:
        raise QueryExecutionError(f"coverage query failed closed: {exc}") from exc

    source_packs.sort(
        key=lambda item: (str(item.get("role")), str(item.get("pack_id")))
    )
    snapshot_id = _snapshot_result_id(snapshot_data)
    evidence = {
        "kind": "COVERAGE",
        "snapshot_id": snapshot_id,
        "query_hash": query_hash,
        "scope_hash": _sha256_text(_canonical_json(scope)),
        "schema_version": certificate.get("schema_version"),
        "certificate_digest": digest,
        "contribution_digest": digest,
        "contribution_count": scanned_records,
        "contribution_kind": "coverage_certificate_records",
    }
    result_payload = {
        "snapshot_id": snapshot_id,
        "query_hash": query_hash,
        "complete": True,
        "source_packs": source_packs,
        "scanned_records": scanned_records,
        "matched_records": matched_records,
        "duplicate_records_removed": duplicate_records_removed,
        "values": certificate,
        "rows": [],
        "evidence": evidence,
    }
    result_hash = _sha256_text(_canonical_json(result_payload))
    evidence["result_hash"] = result_hash
    return QueryResult(
        snapshot_id=snapshot_id,
        query_hash=query_hash,
        result_hash=result_hash,
        complete=True,
        source_packs=tuple(source_packs),
        scanned_records=scanned_records,
        matched_records=matched_records,
        duplicate_records_removed=duplicate_records_removed,
        values=dict(certificate),
        rows=(),
        evidence=evidence,
    )


def _snapshot_result_id(snapshot: Mapping[str, Any]) -> str:
    return str(
        snapshot.get("snapshot_id")
        or snapshot.get("canonical_id")
        or snapshot.get("id")
        or _sha256_text(_canonical_json(snapshot))[:20]
    )


def _plain_mapping(value: Mapping[str, Any] | object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "as_dict"):
        payload = value.as_dict()
        if isinstance(payload, Mapping):
            return dict(payload)
    if is_dataclass(value):
        payload = asdict(value)
        if isinstance(payload, Mapping):
            return dict(payload)
    raise TypeError(f"expected a mapping-like value, got {type(value).__name__}")


@dataclass(frozen=True)
class _ResolvedOperand:
    value: Any
    unit: str | None


@dataclass(frozen=True)
class _ReducerOutput:
    value: Any
    unit: str | None = None
    value_units: Mapping[str, str] | None = None


def _execute_reducer(
    reducer: ReducerSpec,
    context: Mapping[str, Any],
    units: Mapping[str, str | None],
) -> _ReducerOutput:
    config = reducer.config
    label = f"reducer {reducer.id!r}"
    if reducer.op == "coverage_absence":
        scans: list[dict[str, Any]] = []
        total_match_count = 0
        for operand in config["inputs"]:
            reference = str(operand["ref"])
            certificate = _validated_coverage_plan_certificate(
                reference, context, label
            )
            match_count = _required_nonnegative_integral(
                certificate.get("match_count"), "match_count", label
            )
            total_match_count += match_count
            scans.append(certificate)
        if total_match_count != 0:
            raise BundleReductionError(
                f"{label} cannot certify absence with match_count={total_match_count}"
            )
        return _ReducerOutput(
            {
                "classification": "not_in_snapshot",
                "total_match_count": 0,
                "scans": scans,
            }
        )
    if reducer.op == "artifact_role_absence":
        role_certificate = _validated_role_plan_certificate(
            str(config["source"]), context, label
        )
        role_match_count = _required_nonnegative_integral(
            role_certificate.get("role_match_count"),
            "role_match_count",
            label,
        )
        if role_match_count != 0:
            raise BundleReductionError(
                f"{label} cannot certify artifact-role absence with "
                f"role_match_count={role_match_count}"
            )
        generic_context = [
            _validated_coverage_plan_certificate(
                str(operand["ref"]), context, label
            )
            for operand in config["generic_context"]
        ]
        return _ReducerOutput(
            {
                "classification": "not_in_snapshot",
                "missing_role": str(role_certificate["requested_role"]),
                "role_certificate": role_certificate,
                "generic_context": generic_context,
            }
        )
    if reducer.op == "sum_values":
        operands = [_resolve_operand(item, context, units, label) for item in config["inputs"]]
        unit = _common_unit(operands, label)
        value = sum((_required_decimal(item.value, label) for item in operands), Decimal(0))
        return _ReducerOutput(_json_number(value), unit)
    if reducer.op == "all_equal":
        operands = [_resolve_operand(item, context, units, label) for item in config["inputs"]]
        _common_unit(operands, label)
        values = [_required_decimal(item.value, label) for item in operands]
        return _ReducerOutput(all(value == values[0] for value in values[1:]))
    if reducer.op == "sum_field":
        source = str(config["source"])
        rows = _required_rows(_resolve_reference(source, context), label)
        field = str(config["field"])
        total = sum((_required_decimal(_required_field(row, field, label), label) for row in rows), Decimal(0))
        unit = _collection_field_unit(source, rows, field, units, label)
        return _ReducerOutput(_json_number(total), unit)
    if reducer.op == "subtract":
        left = _resolve_operand(config["left"], context, units, label)
        right = _resolve_operand(config["right"], context, units, label)
        unit = _common_unit([left, right], label)
        return _ReducerOutput(
            _json_number(
                _required_decimal(left.value, label) - _required_decimal(right.value, label)
            ),
            unit,
        )
    if reducer.op == "equals":
        left = _resolve_operand(config["left"], context, units, label)
        right = _resolve_operand(config["right"], context, units, label)
        left_value = _required_scalar(left.value, label)
        right_value = _required_scalar(right.value, label)
        if _decimal(left_value) is not None or _decimal(right_value) is not None:
            _common_unit([left, right], label)
        elif left.unit != right.unit:
            raise BundleReductionError(f"{label} cannot compare values with different unit envelopes")
        return _ReducerOutput(_equal(left_value, right_value))
    if reducer.op == "count":
        values = _required_sequence(_resolve_reference(str(config["source"]), context), label)
        return _ReducerOutput(len(values), "count")
    if reducer.op == "only":
        source = str(config["source"])
        values = _required_sequence(_resolve_reference(source, context), label)
        if len(values) != 1:
            raise BundleReductionError(f"{label} expected exactly one value, got {len(values)}")
        return _ReducerOutput(_json_safe(values[0]), units.get(source))
    if reducer.op == "extract_constraint":
        result = _extract_constraint(
            _resolve_reference(str(config["source"]), context),
            str(config["profile"]),
            label,
        )
        value_units = {"value": str(result["unit"])}
        if "mean_temperature_components" in result:
            value_units.update(
                {
                    "mean_temperature_components.value": "degree_celsius",
                    "mean_temperature_components.tolerance": "degree_celsius",
                }
            )
        return _ReducerOutput(result, value_units=value_units)
    if reducer.op == "extract_dimension_token":
        result = _extract_dimension_token(
            _resolve_reference(str(config["source"]), context),
            str(config["profile"]),
            label,
        )
        return _ReducerOutput(result, value_units={"value": str(result["unit"])})
    if reducer.op == "extract_section_list":
        result = _extract_section_list(
            _resolve_reference(str(config["source"]), context),
            section=str(config["section"]),
            heading=str(config["heading"]),
            item_prefix=(
                str(config["item_prefix"]) if config.get("item_prefix") is not None else None
            ),
            max_items=int(config.get("max_items", 50)),
            label=label,
        )
        return _ReducerOutput(result, value_units={"count": "count"})
    if reducer.op == "traverse_edge":
        return _traverse_edge(config, context, label)
    if reducer.op == "evaluate_decimal_formula":
        formula = _required_nonempty_text(
            _required_scalar(
                _resolve_reference(str(config["formula"]), context), label
            ),
            "formula",
            label,
        )
        source_type = _required_nonempty_text(
            _required_scalar(
                _resolve_reference(str(config["source_type"]), context), label
            ),
            "formula_source_type",
            label,
        )
        if source_type != "manual_formula":
            raise BundleReductionError(
                f"{label} requires formula_source_type='manual_formula'"
            )
        raw_unit = _required_nonempty_text(
            _required_scalar(
                _resolve_reference(str(config["unit"]), context), label
            ),
            "unit",
            label,
        )
        unit = _canonical_unit(raw_unit)
        if unit is None or unit == "count":
            raise BundleReductionError(f"{label} requires a non-count unit reference")
        try:
            value = evaluate_decimal_additive(formula)
        except DecimalFormulaError as exc:
            raise BundleReductionError(f"{label} rejected decimal formula: {exc}") from exc
        result = {
            "profile": str(config["profile"]),
            "formula": formula,
            "formula_source_type": source_type,
            "value": _json_number(value),
            "unit": raw_unit,
        }
        return _ReducerOutput(result, value_units={"value": unit})
    if reducer.op == "satisfies_constraint":
        constraint = _resolve_reference(str(config["constraint"]), context)
        actual = _resolve_operand(config["actual"], context, units, label)
        return _ReducerOutput(_satisfies_constraint(constraint, actual, label))
    if reducer.op == "round":
        source = str(config["source"])
        unit = _canonical_unit(units.get(source))
        if unit is None or unit == "count":
            raise BundleReductionError(
                f"{label} requires a source-bound non-count unit"
            )
        number = _required_decimal(_required_scalar(_resolve_reference(source, context), label), label)
        quantum = Decimal(1).scaleb(-int(config["digits"]))
        return _ReducerOutput(
            _json_number(number.quantize(quantum, rounding=ROUND_HALF_UP)),
            unit,
        )
    if reducer.op in {"argmax", "top"}:
        source = str(config["source"])
        rows = _required_rows(_resolve_reference(source, context), label)
        field = str(config["field"])
        _collection_field_unit(source, rows, field, units, label)
        ranked = _rank_rows(rows, field, label)
        if not ranked:
            raise BundleReductionError(f"{label} cannot rank an empty row set")
        if reducer.op == "argmax":
            _reject_rank_boundary_tie(ranked, field, 1, label)
            return _ReducerOutput(_json_safe(ranked[0]))
        limit = int(config["limit"])
        _reject_rank_boundary_tie(ranked, field, limit, label)
        return _ReducerOutput(_json_safe(ranked[:limit]))
    if reducer.op == "left_join_zero":
        left = _required_sequence(_resolve_reference(str(config["left"]), context), label)
        right = _required_rows(_resolve_reference(str(config["right"]), context), label)
        return _ReducerOutput(
            _left_join_zero(
                left,
                right,
                left_key=str(config["left_key"]) if config.get("left_key") else None,
                right_key=str(config["right_key"]),
                value_field=str(config["value_field"]),
                label=label,
            )
        )
    if reducer.op == "inner_join_rank":
        left = _required_sequence(_resolve_reference(str(config["left"]), context), label)
        right_source = str(config["right"])
        right = _required_rows(_resolve_reference(right_source, context), label)
        joined = _inner_join_rows(
            left,
            right,
            left_key=str(config["left_key"]) if config.get("left_key") else None,
            right_key=str(config["right_key"]),
            label=label,
        )
        _collection_field_unit(
            right_source,
            joined,
            str(config["value_field"]),
            units,
            label,
        )
        value_field = str(config["value_field"])
        ranked = _rank_rows(joined, value_field, label)
        limit = int(config["limit"])
        _reject_rank_boundary_tie(ranked, value_field, limit, label)
        return _ReducerOutput(_json_safe(ranked[:limit]))
    # validate_plan_bundle rejects this before any plan runs.  Keep a runtime
    # guard so manually constructed dataclasses cannot bypass the allowlist.
    raise BundleReductionError(f"{label} uses unsupported operator {reducer.op!r}")


_CONSTRAINT_PROFILE_SPECS: Mapping[str, Mapping[str, Any]] = {
    "granite_compressive_strength": {
        "standard_code": "EXCS 41 35 01",
        "section": "2.1.1 화강석 판재",
        "section_code": "2.1.1",
        "parameter": "compressive_strength",
        "operator": ">=",
        "operator_token": "gte",
        "unit": "MPa",
        "anchor": "2.1.1 화강석 판재",
        "window_start": "2.1.1 화강석 판재",
        "window_end": "(2) 대리석",
        "pattern": r"압축강도\s*:\s*(?P<value>\d+(?:\.\d+)?)\s*MPa\s*이상",
    },
    "granite_water_absorption": {
        "standard_code": "EXCS 41 35 01",
        "section": "2.1.1 화강석 판재",
        "section_code": "2.1.1",
        "parameter": "water_absorption",
        "operator": "<",
        "operator_token": "lt",
        "unit": "%",
        "anchor": "2.1.1 화강석 판재",
        "window_start": "2.1.1 화강석 판재",
        "window_end": "(2) 대리석",
        "pattern": r"흡수율\s*:\s*(?P<value>\d+(?:\.\d+)?)\s*%\s*미만",
    },
    "sg_panel_fire_gypsum_board_thickness": {
        "standard_code": "EXCS 41 51 02",
        "section": "2.2.3.2 패널심재",
        "section_code": "2.2.3.2",
        "context": "S.G 패널 칸막이",
        "parameter": "fire_gypsum_board_thickness",
        "operator": "eq",
        "operator_token": "eq",
        "unit": "mm",
        "anchor": "2.2.3.2 패널심재",
        "window_start": "2.2.3.2 패널심재",
        "pattern": (
            r"방화석고보드\s*두께\s*(?P<value>\d+(?:\.\d+)?)\s*mm의\s*것을\s*사용"
        ),
    },
    "urethane_waterproofing_total_thickness": {
        "standard_code": "LHCS 41 40 06",
        "section": "3.3.1",
        "section_code": "3.3.1",
        "parameter": "waterproofing_total_thickness",
        "operator": ">=",
        "operator_token": "gte",
        "unit": "mm",
        "anchor": "표 3.3-2 우레탄 도막방수의 시공순서",
        "window_start": "표 3.3-2 우레탄 도막방수의 시공순서",
        "pattern": (
            r"1\s*,\s*2\s*,\s*3차\s*총두께\s*(?P<value>\d+(?:\.\d+)?)\s*mm\s*이상"
        ),
    },
    "eps_bead_initial_thermal_conductivity": {
        "standard_code": "LHCS 41 42 00",
        "section": "2.1.2(1)",
        "section_code": "2.1.2(1)",
        "parameter": "initial_thermal_conductivity",
        "operator": "<=",
        "operator_token": "lte",
        "unit": "W/m·K",
        "anchor": "2.1.2 단열재 (1) 비드법",
        "window_start": "(1) 비드법 발포 폴리스티렌 단열재",
        "window_end": "(2) 압출법 발포 폴리스티렌 단열재",
        "pattern": (
            r"초기열전도율은\s*(?P<value>\d+(?:\.\d+)?)\s*W\s*/\s*m\s*[kK]"
            r"\s*이하\s*\((?P<qualifier>평균온도\s*"
            r"(?P<mean_temperature>\d+(?:\.\d+)?)\s*±\s*"
            r"(?P<mean_tolerance>\d+(?:\.\d+)?)\s*°?C)\)"
        ),
    },
}


def _extract_constraint(source: Any, profile: str, label: str) -> dict[str, Any]:
    spec = _CONSTRAINT_PROFILE_SPECS.get(profile)
    if spec is None:
        raise BundleReductionError(f"{label} uses unsupported constraint profile {profile!r}")
    row = _required_extractor_row(source, label)
    standard_code = _normalized_text(row["standard_code"])
    expected_standard = _normalized_text(spec["standard_code"])
    if standard_code.casefold() != expected_standard.casefold():
        raise BundleReductionError(
            f"{label} expected standard {expected_standard!r}, got {standard_code!r}"
        )
    text = _normalized_text(row["requirement_text"])
    anchor = _normalized_text(spec["anchor"])
    if text.count(anchor) != 1:
        raise BundleReductionError(f"{label} expected exactly one profile anchor")
    window = _profile_window(text, spec, label)
    candidates = list(re.finditer(str(spec["pattern"]), window))
    if len(candidates) != 1:
        raise BundleReductionError(
            f"{label} expected exactly one constraint candidate, got {len(candidates)}"
        )
    candidate = candidates[0]
    number = _required_decimal(candidate.group("value"), label)
    canonical_unit = _canonical_unit(spec["unit"])
    if canonical_unit is None:
        raise BundleReductionError(f"{label} profile has no canonical unit")
    evidence = {
        "requirement_id": row["id"],
        "standard_code": standard_code,
        "evidence_ref": row["evidence_ref"],
    }
    result: dict[str, Any] = {
        "profile": profile,
        "operator": spec["operator"],
        "operator_token": spec["operator_token"],
        "value": _json_number(number),
        "unit": spec["unit"],
        "section": spec["section"],
        "section_code": spec["section_code"],
        "parameter": spec["parameter"],
        "evidence": evidence,
        "requirement_id": row["id"],
        "standard_code": standard_code,
        "evidence_ref": row["evidence_ref"],
        "matched_text": candidate.group(0),
    }
    if spec.get("context") is not None:
        result["context"] = spec["context"]
    qualifier = candidate.groupdict().get("qualifier")
    if qualifier is not None:
        result["qualifier"] = qualifier.strip()
    mean_temperature = candidate.groupdict().get("mean_temperature")
    mean_tolerance = candidate.groupdict().get("mean_tolerance")
    if mean_temperature is not None and mean_tolerance is not None:
        result["mean_temperature"] = f"{mean_temperature}±{mean_tolerance}℃"
        result["mean_temperature_components"] = {
            "value": _json_number(_required_decimal(mean_temperature, label)),
            "tolerance": _json_number(_required_decimal(mean_tolerance, label)),
            "unit": "degree_celsius",
        }
    return result


def _profile_window(text: str, spec: Mapping[str, Any], label: str) -> str:
    start_literal = _normalized_text(spec["window_start"])
    if text.count(start_literal) != 1:
        raise BundleReductionError(f"{label} expected exactly one profile window start")
    start = text.index(start_literal)
    raw_end = spec.get("window_end")
    if raw_end is None:
        return text[start:]
    end_literal = _normalized_text(raw_end)
    suffix = text[start + len(start_literal) :]
    if suffix.count(end_literal) != 1:
        raise BundleReductionError(f"{label} expected exactly one profile window end")
    end = start + len(start_literal) + suffix.index(end_literal)
    return text[start:end]


def _extract_dimension_token(source: Any, profile: str, label: str) -> dict[str, Any]:
    if profile == "boq_spec_t_thickness_mm":
        rows = _required_rows(source, label)
        if len(rows) != 1:
            raise BundleReductionError(
                f"{label} expected exactly one grouped BOQ row, got {len(rows)}"
            )
        row = rows[0]
        boq_item_id = _required_nonempty_text(
            _required_field(row, "id", label), "id", label
        )
        source_row_count = _required_decimal(
            _required_field(row, "source_row_count", label), label
        )
        if source_row_count != 1:
            raise BundleReductionError(f"{label} requires source_row_count=1")
        specification = _required_nonempty_text(
            _required_field(row, "specification", label), "specification", label
        )
        token_markers = list(re.finditer(r"T\s*", specification))
        tokens = list(
            re.finditer(
                r"(?<![A-Za-z0-9])T\s*(\d+(?:\.\d+)?)(?![\d.eE~～/+-])",
                specification,
            )
        )
        if len(token_markers) != 1 or len(tokens) != 1:
            raise BundleReductionError(
                f"{label} requires exactly one BOQ T<decimal> thickness token"
            )
        number = _required_decimal(tokens[0].group(1), label)
        if number <= 0:
            raise BundleReductionError(f"{label} thickness token must be positive")
        return {
            "profile": profile,
            "parameter": "thickness",
            "boq_item_id": boq_item_id,
            "specification": specification,
            "token": f"T{tokens[0].group(1)}",
            "value": _json_number(number),
            "unit": "millimeter",
        }
    if profile != "type_name_t_thickness_mm":
        raise BundleReductionError(f"{label} uses unsupported dimension profile {profile!r}")
    rows = _required_rows(source, label)
    if len(rows) != 1:
        raise BundleReductionError(f"{label} expected exactly one grouped model row, got {len(rows)}")
    row = rows[0]
    type_name = _required_nonempty_text(_required_field(row, "type_name", label), "type_name", label)
    instance_count = _required_decimal(_required_field(row, "instance_count", label), label)
    if instance_count <= 0 or instance_count != instance_count.to_integral_value():
        raise BundleReductionError(f"{label} requires a positive integral instance_count")
    token_markers = list(re.finditer(r"T\s*=", type_name))
    tokens = list(re.finditer(r"\(\s*T\s*=\s*(\d+(?:\.\d+)?)\s*\)", type_name))
    if len(token_markers) != 1 or len(tokens) != 1:
        raise BundleReductionError(f"{label} requires exactly one parenthesized T=<decimal> token")
    number = _required_decimal(tokens[0].group(1), label)
    if number <= 0:
        raise BundleReductionError(f"{label} thickness token must be positive")
    return {
        "profile": profile,
        "parameter": "thickness",
        "type_name": type_name,
        "instance_count": int(instance_count),
        "token": f"T={tokens[0].group(1)}",
        "value": _json_number(number),
        "unit": "millimeter",
    }


def _satisfies_constraint(
    constraint: Any,
    actual: _ResolvedOperand,
    label: str,
) -> bool:
    if not isinstance(constraint, Mapping):
        raise BundleReductionError(f"{label} constraint must be a structured mapping")
    for field_name in ("value", "unit"):
        if field_name not in constraint:
            raise BundleReductionError(f"{label} constraint is missing {field_name!r}")
    raw_operator = constraint.get("operator_token", constraint.get("operator"))
    if raw_operator is None:
        raise BundleReductionError(f"{label} constraint is missing 'operator'")
    operator = {
        "eq": "eq",
        "=": "eq",
        "==": "eq",
        "gte": "gte",
        ">=": "gte",
        "gt": "gt",
        ">": "gt",
        "lte": "lte",
        "<=": "lte",
        "lt": "lt",
        "<": "lt",
    }.get(str(raw_operator))
    if operator not in {"eq", "gte", "gt", "lte", "lt"}:
        raise BundleReductionError(
            f"{label} uses unsupported constraint operator {raw_operator!r}"
        )
    expected = _required_decimal(constraint["value"], label)
    actual_value = _required_decimal(_required_scalar(actual.value, label), label)
    expected_unit = _canonical_unit(constraint["unit"])
    actual_unit = _canonical_unit(actual.unit)
    if expected_unit is None or actual_unit is None:
        raise BundleReductionError(f"{label} requires units for constraint and actual")
    if expected_unit != actual_unit:
        raise BundleReductionError(
            f"{label} cannot compare {actual_unit!r} with {expected_unit!r}"
        )
    return {
        "eq": actual_value == expected,
        "gte": actual_value >= expected,
        "gt": actual_value > expected,
        "lte": actual_value <= expected,
        "lt": actual_value < expected,
    }[operator]


def _extract_section_list(
    source: Any,
    *,
    section: str,
    heading: str,
    item_prefix: str | None,
    max_items: int,
    label: str,
) -> dict[str, Any]:
    row = _required_extractor_row(source, label)
    text = _normalized_text(row["requirement_text"])
    normalized_section = _normalized_text(section)
    normalized_heading = _normalized_text(heading)
    anchor_pattern = (
        rf"(?<![\d.]){re.escape(normalized_section)}\s+"
        rf"{re.escape(normalized_heading)}(?=\s|$)"
    )
    anchors = list(re.finditer(anchor_pattern, text))
    if len(anchors) != 1:
        raise BundleReductionError(f"{label} expected exactly one section heading anchor")
    section_parts = normalized_section.split(".")
    section_parts[-1] = str(int(section_parts[-1]) + 1)
    next_section = ".".join(section_parts)
    next_pattern = rf"(?<![\d.]){re.escape(next_section)}(?=\s|$)"
    boundaries = list(re.finditer(next_pattern, text[anchors[0].end() :]))
    if len(boundaries) != 1:
        raise BundleReductionError(f"{label} could not determine one next-section boundary")
    body_start = anchors[0].end()
    body_end = body_start + boundaries[0].start()
    body = text[body_start:body_end]
    bullet_matches = list(re.finditer(r"(?<!\S)[•∙·]\s*", body))
    if not bullet_matches or body[: bullet_matches[0].start()].strip():
        raise BundleReductionError(f"{label} section does not contain a pure supported bullet list")
    raw_items: list[str] = []
    for index, bullet in enumerate(bullet_matches):
        end = bullet_matches[index + 1].start() if index + 1 < len(bullet_matches) else len(body)
        item = body[bullet.end() : end].strip()
        if not item:
            raise BundleReductionError(f"{label} section contains an empty bullet item")
        raw_items.append(item)
    normalized_prefix = _normalized_text(item_prefix) if item_prefix is not None else None
    items: list[str] = []
    for raw_item in raw_items:
        item = _normalized_text(raw_item)
        if normalized_prefix is not None:
            if not item.startswith(normalized_prefix):
                raise BundleReductionError(
                    f"{label} found a bullet without the required item_prefix"
                )
            item = item[len(normalized_prefix) :].strip()
            if not item:
                raise BundleReductionError(f"{label} prefix removal produced an empty item")
        if len(item) > 500:
            raise BundleReductionError(f"{label} section item exceeds 500 characters")
        items.append(item)
    if not items:
        raise BundleReductionError(f"{label} section list is empty after prefix filtering")
    if len(items) > max_items:
        raise BundleReductionError(
            f"{label} section list contains {len(items)} items, exceeding max_items={max_items}"
        )
    normalized_keys = [item.casefold() for item in items]
    if len(set(normalized_keys)) != len(normalized_keys):
        raise BundleReductionError(f"{label} section list contains duplicate items")
    evidence = {
        "requirement_id": row["id"],
        "standard_code": row["standard_code"],
        "evidence_ref": row["evidence_ref"],
    }
    return {
        "section": normalized_section,
        "heading": normalized_heading,
        "items": items,
        "count": len(items),
        "last_item": items[-1],
        "evidence": evidence,
        "requirement_id": row["id"],
        "standard_code": row["standard_code"],
        "evidence_ref": row["evidence_ref"],
    }


def _traverse_edge(
    config: Mapping[str, Any],
    context: Mapping[str, Any],
    label: str,
) -> _ReducerOutput:
    profile = str(config["profile"])
    source_reference = str(config["source"])
    edge_reference = str(config["edges"])
    target_reference = str(config["target"])
    binding = _required_shared_boq_binding(
        (source_reference, edge_reference, target_reference),
        context,
        label,
    )
    source_rows = _required_rows(
        _resolve_reference(source_reference, context), label
    )
    edge_rows = _validated_edge_rows(
        _resolve_reference(edge_reference, context), binding, label
    )
    target_rows = _required_rows(
        _resolve_reference(target_reference, context), label
    )
    if profile == "boq_calculated_by":
        return _traverse_boq_calculated_by(
            source_rows, edge_rows, target_rows, binding, label
        )
    if profile == "quantity_evidence_bim_reference":
        return _traverse_quantity_evidence_bim_reference(
            source_rows, edge_rows, target_rows, binding, label
        )
    raise BundleReductionError(f"{label} uses unsupported traversal profile {profile!r}")


def _traverse_boq_calculated_by(
    source_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
    binding: tuple[str, str],
    label: str,
) -> _ReducerOutput:
    estimate = _required_single_bound_row(source_rows, binding, label)
    takeoff = _required_single_bound_row(target_rows, binding, label)
    if _required_nonempty_text(
        _required_field(estimate, "node_type", label), "node_type", label
    ) != "EstimateItem":
        raise BundleReductionError(f"{label} source must be an EstimateItem")
    if _required_nonempty_text(
        _required_field(takeoff, "node_type", label), "node_type", label
    ) != "QuantityTakeoffEvidence":
        raise BundleReductionError(
            f"{label} target must be a QuantityTakeoffEvidence"
        )

    estimate_id = _required_nonempty_text(
        _required_field(estimate, "id", label), "id", label
    )
    takeoff_id = _required_nonempty_text(
        _required_field(takeoff, "id", label), "id", label
    )
    calculated_edges = [
        row
        for row in edge_rows
        if row["relation"] == "CALCULATED_BY" and row["source"] == estimate_id
    ]
    if len(calculated_edges) != 1:
        raise BundleReductionError(
            f"{label} expected exactly one CALCULATED_BY edge, got {len(calculated_edges)}"
        )
    edge = calculated_edges[0]
    if edge["target"] != takeoff_id:
        raise BundleReductionError(
            f"{label} CALCULATED_BY target does not match the target row"
        )

    identity: dict[str, str] = {}
    for field_name in (
        "module_type",
        "work_category",
        "item_name",
        "specification",
    ):
        left = _required_nonempty_text(
            _required_field(estimate, field_name, label), field_name, label
        )
        right = _required_nonempty_text(
            _required_field(takeoff, field_name, label), field_name, label
        )
        if _normalized_text(left) != _normalized_text(right):
            raise BundleReductionError(
                f"{label} identity mismatch for {field_name!r}"
            )
        identity[field_name] = left

    display_unit, normalized_unit = _required_matching_quantity_units(
        estimate, takeoff, label
    )
    estimate_quantity = _required_decimal(
        _required_field(estimate, "quantity", label), label
    )
    takeoff_quantity = _required_decimal(
        _required_field(takeoff, "quantity", label), label
    )
    formula = _required_nonempty_text(
        _required_field(takeoff, "formula", label), "formula", label
    )
    formula_source_type = _required_nonempty_text(
        _required_field(takeoff, "formula_source_type", label),
        "formula_source_type",
        label,
    )
    bim_reference_count = _required_nonnegative_integral(
        _required_field(takeoff, "bim_reference_count", label),
        "bim_reference_count",
        label,
    )
    estimate_sheet = _required_nonempty_text(
        _required_field(estimate, "source_sheet", label), "source_sheet", label
    )
    takeoff_sheet = _required_nonempty_text(
        _required_field(takeoff, "source_sheet", label), "source_sheet", label
    )
    estimate_row = _required_positive_integral(
        _required_field(estimate, "source_row", label), "source_row", label
    )
    takeoff_row = _required_positive_integral(
        _required_field(takeoff, "source_row", label), "source_row", label
    )
    estimate_number = _json_number(estimate_quantity)
    takeoff_number = _json_number(takeoff_quantity)
    pack_id, pack_sha = binding
    result = {
        "profile": "boq_calculated_by",
        "relation": "CALCULATED_BY",
        "source_id": estimate_id,
        "target_id": takeoff_id,
        "source_pack_id": pack_id,
        "source_pack_sha256": pack_sha,
        **identity,
        "unit": display_unit,
        "normalized_unit": normalized_unit,
        "quantity": estimate_number,
        "estimate_quantity": estimate_number,
        "takeoff_quantity": takeoff_number,
        "quantity_matches": estimate_quantity == takeoff_quantity,
        "formula": formula,
        "formula_source_type": formula_source_type,
        "bim_reference_count": bim_reference_count,
        "estimate_source_sheet": estimate_sheet,
        "estimate_source_row": estimate_row,
        "takeoff_source_sheet": takeoff_sheet,
        "takeoff_source_row": takeoff_row,
        "estimate": {
            "id": estimate_id,
            "quantity": estimate_number,
            "source_sheet": estimate_sheet,
            "source_row": estimate_row,
        },
        "takeoff": {
            "id": takeoff_id,
            "quantity": takeoff_number,
            "source_sheet": takeoff_sheet,
            "source_row": takeoff_row,
            "formula": formula,
            "formula_source_type": formula_source_type,
            "bim_reference_count": bim_reference_count,
        },
        "edge": {
            "source": edge["source"],
            "relation": edge["relation"],
            "target": edge["target"],
            "edge_row_count": edge["edge_row_count"],
        },
    }
    return _ReducerOutput(
        result,
        value_units={
            "quantity": normalized_unit,
            "estimate_quantity": normalized_unit,
            "takeoff_quantity": normalized_unit,
            "estimate.quantity": normalized_unit,
            "takeoff.quantity": normalized_unit,
        },
    )


def _traverse_quantity_evidence_bim_reference(
    source_rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
    binding: tuple[str, str],
    label: str,
) -> _ReducerOutput:
    takeoff = _required_single_bound_row(source_rows, binding, label)
    if _required_nonempty_text(
        _required_field(takeoff, "node_type", label), "node_type", label
    ) != "QuantityTakeoffEvidence":
        raise BundleReductionError(
            f"{label} source must be a QuantityTakeoffEvidence"
        )
    takeoff_id = _required_nonempty_text(
        _required_field(takeoff, "id", label), "id", label
    )
    declared_count = _required_nonnegative_integral(
        _required_field(takeoff, "bim_reference_count", label),
        "bim_reference_count",
        label,
    )
    if declared_count > 1:
        raise BundleReductionError(
            f"{label} fixed profile supports zero or one BIM reference"
        )
    reference_edges = [
        row
        for row in edge_rows
        if row["relation"] == "REFERENCES_BIM_ELEMENT"
        and row["source"] == takeoff_id
    ]
    if len(reference_edges) != declared_count:
        raise BundleReductionError(
            f"{label} declared bim_reference_count={declared_count} but found "
            f"{len(reference_edges)} REFERENCES_BIM_ELEMENT edges"
        )

    indexes = [
        _required_positive_integral(
            _required_field(edge, "reference_index", label),
            "reference_index",
            label,
        )
        for edge in reference_edges
    ]
    if sorted(indexes) != list(range(1, declared_count + 1)):
        raise BundleReductionError(
            f"{label} BIM reference indexes must be contiguous from 1"
        )

    target_index: dict[str, dict[str, Any]] = {}
    for row in target_rows:
        bound = _required_bound_row(row, binding, label)
        target_id = _required_nonempty_text(
            _required_field(bound, "id", label), "id", label
        )
        if target_id in target_index:
            raise BundleReductionError(
                f"{label} found ambiguous BIMReference target {target_id!r}"
            )
        target_index[target_id] = bound

    takeoff_sheet = _required_nonempty_text(
        _required_field(takeoff, "source_sheet", label), "source_sheet", label
    )
    takeoff_row = _required_positive_integral(
        _required_field(takeoff, "source_row", label), "source_row", label
    )
    display_unit, normalized_unit = _required_row_quantity_unit(takeoff, label)
    takeoff_quantity = _required_decimal(
        _required_field(takeoff, "quantity", label), label
    )
    references: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for edge, reference_index in sorted(
        zip(reference_edges, indexes, strict=True), key=lambda item: item[1]
    ):
        target_id = edge["target"]
        if target_id in seen_targets:
            raise BundleReductionError(f"{label} contains duplicate BIM reference targets")
        seen_targets.add(target_id)
        target = target_index.get(target_id)
        if target is None:
            raise BundleReductionError(
                f"{label} BIMReference target {target_id!r} does not exist"
            )
        target_sheet = _required_nonempty_text(
            _required_field(target, "source_sheet", label), "source_sheet", label
        )
        target_row = _required_positive_integral(
            _required_field(target, "source_row", label), "source_row", label
        )
        if target_sheet != takeoff_sheet or target_row != takeoff_row:
            raise BundleReductionError(
                f"{label} BIMReference source sheet/row does not match its quantity evidence"
            )
        measurement_value = _required_decimal(
            _required_field(target, "measurement_value", label), label
        )
        measurement_type = _required_nonempty_text(
            _required_field(target, "type", label), "type", label
        )
        _validate_measurement_type_unit(
            measurement_type, normalized_unit, label
        )
        references.append(
            {
                "id": target_id,
                "reference_index": reference_index,
                "measurement_value": _json_number(measurement_value),
                "type": measurement_type,
                "source_expression": _required_nonempty_text(
                    _required_field(target, "source_expression", label),
                    "source_expression",
                    label,
                ),
                "source_sheet": target_sheet,
                "source_row": target_row,
            }
        )

    measurement = (
        _required_decimal(references[0]["measurement_value"], label)
        if references
        else None
    )
    pack_id, pack_sha = binding
    result: dict[str, Any] = {
        "profile": "quantity_evidence_bim_reference",
        "relation": "REFERENCES_BIM_ELEMENT",
        "quantity_evidence_id": takeoff_id,
        "source_pack_id": pack_id,
        "source_pack_sha256": pack_sha,
        "unit": display_unit,
        "normalized_unit": normalized_unit,
        "takeoff_quantity": _json_number(takeoff_quantity),
        "declared_bim_reference_count": declared_count,
        "edge_count": len(reference_edges),
        "resolved_target_count": len(references),
        "reference_count_matches": True,
        "bim_reference_count": declared_count,
        "bim_reference_ids": [item["id"] for item in references],
        "bim_references": references,
        "bim_measurement_value": (
            _json_number(measurement) if measurement is not None else None
        ),
        "measurement_matches_quantity": (
            measurement == takeoff_quantity if measurement is not None else None
        ),
    }
    value_units = {"takeoff_quantity": normalized_unit}
    if measurement is not None:
        value_units["bim_measurement_value"] = normalized_unit
        value_units["bim_references.0.measurement_value"] = normalized_unit
    return _ReducerOutput(result, value_units=value_units)


_MEASUREMENT_TYPE_UNITS: Mapping[str, str] = {
    "area": "square_meter",
    "length": "meter",
    "volume": "cubic_meter",
    "count": "count",
    "item_count": "count",
    "location_count": "count",
    "element_count": "count",
}


def _validate_measurement_type_unit(
    measurement_type: str,
    normalized_unit: str,
    label: str,
) -> None:
    type_key = _normalized_text(measurement_type).casefold()
    expected_unit = _MEASUREMENT_TYPE_UNITS.get(type_key)
    if expected_unit is None:
        raise BundleReductionError(
            f"{label} uses unsupported BIM measurement type {measurement_type!r}"
        )
    actual_unit = _canonical_unit(normalized_unit)
    if actual_unit != expected_unit:
        raise BundleReductionError(
            f"{label} BIM measurement type/unit mismatch: "
            f"{measurement_type!r} requires {expected_unit!r}, got {actual_unit!r}"
        )


def _required_shared_boq_binding(
    references: Sequence[str],
    context: Mapping[str, Any],
    label: str,
) -> tuple[str, str]:
    bindings = [_required_plan_binding(reference, context, label) for reference in references]
    if any(binding != bindings[0] for binding in bindings[1:]):
        raise BundleReductionError(
            f"{label} source, edge, and target plans must bind to the same BOQ pack SHA"
        )
    return bindings[0]


def _required_plan_binding(
    reference: str,
    context: Mapping[str, Any],
    label: str,
) -> tuple[str, str]:
    parts = reference.split(".")
    if len(parts) != 3 or parts[0] != "plans" or parts[2] != "rows":
        raise BundleReductionError(f"{label} requires an exact plan rows reference")
    plans = context.get("plans")
    plan = plans.get(parts[1]) if isinstance(plans, Mapping) else None
    duplicate_records_removed = (
        plan.get("duplicate_records_removed") if isinstance(plan, Mapping) else None
    )
    if (
        isinstance(duplicate_records_removed, bool)
        or not isinstance(duplicate_records_removed, int)
        or duplicate_records_removed != 0
    ):
        raise BundleReductionError(
            f"{label} traversal plan {parts[1]!r} removed duplicate records"
        )
    packs = plan.get("source_packs") if isinstance(plan, Mapping) else None
    if not isinstance(packs, Sequence) or isinstance(packs, (str, bytes)) or len(packs) != 1:
        raise BundleReductionError(
            f"{label} each traversal plan must bind to exactly one source pack"
        )
    pack = packs[0]
    if not isinstance(pack, Mapping) or str(pack.get("role") or "") != "boq":
        raise BundleReductionError(f"{label} traversal plans must bind to the BOQ role")
    pack_id = _required_nonempty_text(pack.get("pack_id"), "source_pack_id", label)
    pack_sha = _required_nonempty_text(
        pack.get("sha256"), "source_pack_sha256", label
    ).casefold()
    if re.fullmatch(r"[0-9a-f]{64}", pack_sha) is None:
        raise BundleReductionError(f"{label} source pack SHA-256 is malformed")
    return pack_id, pack_sha


def _validated_edge_rows(
    value: Any,
    binding: tuple[str, str],
    label: str,
) -> list[dict[str, Any]]:
    rows = _required_rows(value, label)
    validated: list[dict[str, Any]] = []
    for row in rows:
        bound = _required_bound_row(row, binding, label, count_field="edge_row_count")
        validated.append(
            {
                **bound,
                "source": _required_nonempty_text(
                    _required_field(bound, "source", label), "source", label
                ),
                "relation": _required_nonempty_text(
                    _required_field(bound, "relation", label), "relation", label
                ),
                "target": _required_nonempty_text(
                    _required_field(bound, "target", label), "target", label
                ),
            }
        )
    return validated


def _required_single_bound_row(
    rows: list[dict[str, Any]],
    binding: tuple[str, str],
    label: str,
) -> dict[str, Any]:
    if len(rows) != 1:
        raise BundleReductionError(
            f"{label} expected exactly one grouped source/target row, got {len(rows)}"
        )
    return _required_bound_row(rows[0], binding, label)


def _required_bound_row(
    row: Mapping[str, Any],
    binding: tuple[str, str],
    label: str,
    *,
    count_field: str = "source_row_count",
) -> dict[str, Any]:
    result = dict(row)
    count = _required_nonnegative_integral(
        _required_field(result, count_field, label), count_field, label
    )
    if count != 1:
        raise BundleReductionError(f"{label} requires {count_field}=1")
    pack_id = _required_nonempty_text(
        _required_field(result, "source_pack_id", label), "source_pack_id", label
    )
    pack_sha = _required_nonempty_text(
        _required_field(result, "source_pack_sha256", label),
        "source_pack_sha256",
        label,
    ).casefold()
    if (pack_id, pack_sha) != binding:
        raise BundleReductionError(
            f"{label} row provenance does not match the bound BOQ pack SHA"
        )
    return result


def _required_matching_quantity_units(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    label: str,
) -> tuple[str, str]:
    left_display, left_normalized = _required_row_quantity_unit(left, label)
    right_display, right_normalized = _required_row_quantity_unit(right, label)
    if left_normalized != right_normalized:
        raise BundleReductionError(f"{label} quantity unit identity mismatch")
    if _normalized_text(left_display) != _normalized_text(right_display):
        raise BundleReductionError(f"{label} display unit identity mismatch")
    return left_display, left_normalized


def _required_row_quantity_unit(
    row: Mapping[str, Any],
    label: str,
) -> tuple[str, str]:
    display = _required_nonempty_text(
        _required_field(row, "unit", label), "unit", label
    )
    normalized = _required_nonempty_text(
        _required_field(row, "normalized_unit", label), "normalized_unit", label
    )
    display_canonical = _canonical_unit(display)
    normalized_canonical = _canonical_unit(normalized)
    if (
        display_canonical is None
        or normalized_canonical is None
        or display_canonical != normalized_canonical
    ):
        raise BundleReductionError(
            f"{label} display and normalized quantity units are inconsistent"
        )
    return display, normalized_canonical


def _required_nonnegative_integral(
    value: Any,
    field_name: str,
    label: str,
) -> int:
    number = _required_decimal(value, label)
    if number < 0 or number != number.to_integral_value():
        raise BundleReductionError(
            f"{label} requires a non-negative integral {field_name!r}"
        )
    return int(number)


def _required_positive_integral(
    value: Any,
    field_name: str,
    label: str,
) -> int:
    number = _required_nonnegative_integral(value, field_name, label)
    if number < 1:
        raise BundleReductionError(f"{label} requires positive {field_name!r}")
    return number


def _required_extractor_row(source: Any, label: str) -> dict[str, Any]:
    rows = _required_rows(source, label)
    if len(rows) != 1:
        raise BundleReductionError(f"{label} expected exactly one source row, got {len(rows)}")
    row = rows[0]
    for field_name in ("id", "standard_code", "evidence_ref", "requirement_text"):
        row[field_name] = _required_nonempty_text(
            _required_field(row, field_name, label),
            field_name,
            label,
        )
    source_row_count = _required_decimal(_required_field(row, "source_row_count", label), label)
    if source_row_count != 1:
        raise BundleReductionError(f"{label} requires source_row_count=1")
    return row


def _required_nonempty_text(value: Any, field_name: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BundleReductionError(f"{label} requires non-empty {field_name!r}")
    return value.strip()


def _normalized_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def _validated_coverage_plan_certificate(
    reference: str,
    context: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    certificate, plan_source_packs = _required_plan_values_envelope(
        reference, context, label
    )
    if certificate.get("schema_version") != "mo-coverage-scan/1.0":
        raise BundleReductionError(f"{label} received an unknown coverage schema")
    if certificate.get("coverage_complete") is not True:
        raise BundleReductionError(f"{label} requires coverage_complete=true")

    descriptors = _validated_source_pack_descriptors(
        certificate.get("selected_source_packs"), label
    )
    if _canonical_json(descriptors) != _canonical_json(plan_source_packs):
        raise BundleReductionError(
            f"{label} coverage certificate source packs do not match its plan binding"
        )
    selected_pack_count = _required_nonnegative_integral(
        certificate.get("selected_pack_count"), "selected_pack_count", label
    )
    if selected_pack_count <= 0 or selected_pack_count != len(descriptors):
        raise BundleReductionError(
            f"{label} coverage selected pack count is incomplete"
        )
    for field_name in (
        "verified_sha256_pack_count",
        "schema_valid_pack_count",
        "jsonl_schema_valid_pack_count",
        "eof_pack_count",
    ):
        if (
            _required_nonnegative_integral(
                certificate.get(field_name), field_name, label
            )
            != selected_pack_count
        ):
            raise BundleReductionError(
                f"{label} coverage certificate has incomplete {field_name}"
            )

    declared_count = _required_nonnegative_integral(
        certificate.get("declared_node_count_pack_count"),
        "declared_node_count_pack_count",
        label,
    )
    declared_matches = _required_nonnegative_integral(
        certificate.get("declared_count_match_pack_count"),
        "declared_count_match_pack_count",
        label,
    )
    declared_mismatches = _required_nonnegative_integral(
        certificate.get("declared_count_mismatch_pack_count"),
        "declared_count_mismatch_pack_count",
        label,
    )
    if (
        declared_count > selected_pack_count
        or declared_matches + declared_mismatches != declared_count
    ):
        raise BundleReductionError(
            f"{label} coverage declared-count diagnostics are inconsistent"
        )

    digest_basis = certificate.get("coverage_digest_basis")
    if not isinstance(digest_basis, Mapping):
        raise BundleReductionError(f"{label} coverage digest basis is missing")
    basis_fields = (
        "scanned_record_count",
        "declared_node_count_pack_count",
        "declared_count_match_pack_count",
        "declared_count_mismatch_pack_count",
        "declared_count_mismatch_sample",
        "selected_node_type_counts",
        "scoped_record_count",
        "scoped_node_type_counts",
        "search_field_presence_counts",
        "match_count",
        "term_match_counts",
        "duplicate_records_removed",
        "match_digest",
    )
    reconstructed_basis = {
        "snapshot": certificate.get("snapshot_identity"),
        "spec": certificate.get("scan_spec"),
        "source_packs": descriptors,
        **{field: certificate.get(field) for field in basis_fields},
    }
    if _canonical_json(dict(digest_basis)) != _canonical_json(reconstructed_basis):
        raise BundleReductionError(
            f"{label} coverage digest basis does not match certificate fields"
        )
    digest = _required_sha256(
        certificate.get("coverage_digest"), "coverage_digest", label
    )
    if _sha256_text(_canonical_json(reconstructed_basis)) != digest:
        raise BundleReductionError(f"{label} coverage digest verification failed")

    scanned_count = _required_nonnegative_integral(
        certificate.get("scanned_record_count"), "scanned_record_count", label
    )
    scoped_count = _required_nonnegative_integral(
        certificate.get("scoped_record_count"), "scoped_record_count", label
    )
    match_count = _required_nonnegative_integral(
        certificate.get("match_count"), "match_count", label
    )
    duplicate_count = _required_nonnegative_integral(
        certificate.get("duplicate_records_removed"),
        "duplicate_records_removed",
        label,
    )
    if scoped_count > scanned_count or match_count > scoped_count:
        raise BundleReductionError(f"{label} coverage record counts are inconsistent")
    if duplicate_count > scanned_count:
        raise BundleReductionError(f"{label} coverage duplicate count is inconsistent")
    match_digest = _required_sha256(
        certificate.get("match_digest"), "match_digest", label
    )
    if match_count == 0 and match_digest != _sha256_text(_canonical_json([])):
        raise BundleReductionError(
            f"{label} zero-match coverage has a non-empty match digest"
        )
    term_counts = certificate.get("term_match_counts")
    if not isinstance(term_counts, Mapping) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in term_counts.values()
    ):
        raise BundleReductionError(f"{label} term match counts are malformed")
    if any(value > match_count for value in term_counts.values()):
        raise BundleReductionError(f"{label} term match count exceeds total matches")
    return dict(certificate)


def _validated_role_plan_certificate(
    reference: str,
    context: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    certificate, plan_source_packs = _required_plan_values_envelope(
        reference, context, label
    )
    if certificate.get("schema_version") != "mo-role-coverage/1.0":
        raise BundleReductionError(f"{label} received an unknown role schema")
    completeness = certificate.get("certificate")
    if (
        not isinstance(completeness, Mapping)
        or completeness.get("coverage_complete") is not True
    ):
        raise BundleReductionError(f"{label} requires complete role coverage")
    descriptors = _validated_source_pack_descriptors(
        certificate.get("source_pack_descriptors"), label
    )
    if _canonical_json(descriptors) != _canonical_json(plan_source_packs):
        raise BundleReductionError(
            f"{label} role certificate source packs do not match its plan binding"
        )
    included_count = _required_nonnegative_integral(
        completeness.get("included_entry_count"), "included_entry_count", label
    )
    if included_count != len(descriptors):
        raise BundleReductionError(f"{label} role included count is inconsistent")
    for field_name in (
        "validated_descriptor_count",
        "existing_path_count",
        "verified_sha256_count",
        "post_verified_sha256_count",
    ):
        if (
            _required_nonnegative_integral(
                completeness.get(field_name), field_name, label
            )
            != included_count
        ):
            raise BundleReductionError(
                f"{label} role certificate has incomplete {field_name}"
            )
    manifest_entry_count = _required_nonnegative_integral(
        certificate.get("manifest_entry_count"), "manifest_entry_count", label
    )
    if manifest_entry_count < included_count:
        raise BundleReductionError(f"{label} role manifest count is inconsistent")
    role_match_count = _required_nonnegative_integral(
        certificate.get("role_match_count"), "role_match_count", label
    )
    matched = _validated_source_pack_descriptors(
        certificate.get("matched_source_pack_descriptors"), label
    )
    if len(matched) != role_match_count:
        raise BundleReductionError(f"{label} role match count is inconsistent")
    requested_role = _required_nonempty_text(
        certificate.get("requested_role"), "requested_role", label
    )
    if any(item["role"] != requested_role for item in matched):
        raise BundleReductionError(f"{label} role matches contain a different role")
    descriptor_keys = {
        _canonical_json(item) for item in descriptors
    }
    if any(_canonical_json(item) not in descriptor_keys for item in matched):
        raise BundleReductionError(f"{label} role match is outside the manifest")

    digest_basis = certificate.get("role_digest_basis")
    reconstructed_basis = {
        "snapshot": certificate.get("snapshot_identity"),
        "requested_role": requested_role,
        "manifest_entry_count": manifest_entry_count,
        "included_source_packs": descriptors,
        "verified_sha256_count": included_count,
        "post_verified_sha256_count": included_count,
        "role_match_count": role_match_count,
        "matched_source_packs": matched,
    }
    if not isinstance(digest_basis, Mapping) or _canonical_json(
        dict(digest_basis)
    ) != _canonical_json(reconstructed_basis):
        raise BundleReductionError(
            f"{label} role digest basis does not match certificate fields"
        )
    digest = _required_sha256(certificate.get("digest"), "digest", label)
    if _sha256_text(_canonical_json(reconstructed_basis)) != digest:
        raise BundleReductionError(f"{label} role digest verification failed")
    return dict(certificate)


def _required_plan_values_envelope(
    reference: str,
    context: Mapping[str, Any],
    label: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    parts = reference.split(".")
    if len(parts) != 3 or parts[0] != "plans" or parts[2] != "values":
        raise BundleReductionError(
            f"{label} requires an exact plans.<plan_id>.values reference"
        )
    plans = context.get("plans")
    plan = plans.get(parts[1]) if isinstance(plans, Mapping) else None
    if not isinstance(plan, Mapping):
        raise BundleReductionError(f"{label} coverage plan binding is missing")
    values = plan.get("values")
    if not isinstance(values, Mapping):
        raise BundleReductionError(f"{label} coverage plan values are malformed")
    source_packs = _validated_source_pack_descriptors(
        plan.get("source_packs"), label
    )
    duplicate_records_removed = plan.get("duplicate_records_removed")
    if (
        isinstance(duplicate_records_removed, bool)
        or not isinstance(duplicate_records_removed, int)
        or duplicate_records_removed < 0
    ):
        raise BundleReductionError(
            f"{label} coverage plan duplicate count is malformed"
        )
    return dict(values), source_packs


def _validated_source_pack_descriptors(
    value: Any,
    label: str,
) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BundleReductionError(f"{label} source pack descriptors must be an array")
    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or {str(key) for key in item} != {
            "pack_id",
            "role",
            "sha256",
        }:
            raise BundleReductionError(
                f"{label} source pack descriptor has an invalid shape"
            )
        pack_id = _required_nonempty_text(item.get("pack_id"), "pack_id", label)
        role = _required_nonempty_text(item.get("role"), "role", label)
        sha256 = _required_sha256(item.get("sha256"), "sha256", label)
        if pack_id in seen_ids:
            raise BundleReductionError(f"{label} source pack IDs must be unique")
        seen_ids.add(pack_id)
        result.append({"pack_id": pack_id, "role": role, "sha256": sha256})
    return result


def _required_sha256(value: Any, field_name: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise BundleReductionError(f"{label} requires a valid {field_name} SHA-256")
    return value.casefold()


def _resolve_operand(
    value: Any,
    context: Mapping[str, Any],
    units: Mapping[str, str | None],
    label: str,
) -> _ResolvedOperand:
    if not isinstance(value, Mapping):
        raise BundleReductionError(f"{label} received an invalid operand")
    if set(value) == {"ref"}:
        reference = str(value["ref"])
        return _ResolvedOperand(_resolve_reference(reference, context), units.get(reference))
    if set(value) in ({"value"}, {"value", "unit"}):
        unit = _canonical_unit(value.get("unit")) if value.get("unit") is not None else None
        return _ResolvedOperand(value["value"], unit)
    raise BundleReductionError(f"{label} received an invalid operand")


def _resolve_reference(reference: str, context: Mapping[str, Any]) -> Any:
    current: Any = context
    for part in reference.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise BundleReductionError(f"reference {reference!r} did not resolve at {part!r}")
        current = current[part]
    return current


def _plan_result_units(
    plan_id: str,
    plan: BIMQueryPlan,
    result: QueryResult,
) -> dict[str, str | None]:
    location = "rows" if plan.group_by else "values"
    units: dict[str, str | None] = {}
    for metric in plan.metrics:
        alias = metric.alias or (
            metric.field if metric.field not in {None, "*"} else metric.name
        )
        reference = f"plans.{plan_id}.{location}.{alias}"
        if metric.name in {"count", "count_distinct"}:
            units[reference] = "count"
            continue
        companion = f"{alias}_unit"
        if location == "values":
            raw_unit = result.values.get(companion)
            units[reference] = _canonical_unit(raw_unit) if raw_unit is not None else None
            continue
        row_units = {
            _canonical_unit(row.get(companion))
            for row in result.rows
            if row.get(companion) is not None
        }
        row_units.discard(None)
        units[reference] = next(iter(row_units)) if len(row_units) == 1 else None
    return units


def _collection_field_unit(
    source: str,
    rows: list[dict[str, Any]],
    field: str,
    units: Mapping[str, str | None],
    label: str,
) -> str:
    declared = units.get(f"{source}.{field}")
    if declared == "count":
        return declared
    companion = f"{field}_unit"
    discovered: set[str] = set()
    for row in rows:
        if companion not in row or row[companion] is None:
            raise BundleReductionError(f"{label} cannot sum {field!r} without a source unit")
        unit = _canonical_unit(row[companion])
        if unit is None:
            raise BundleReductionError(f"{label} cannot sum {field!r} without a source unit")
        discovered.add(unit)
    if len(discovered) > 1:
        raise BundleReductionError(
            f"{label} cannot combine incompatible units: {sorted(discovered)}"
        )
    if discovered:
        return next(iter(discovered))
    if declared is None:
        raise BundleReductionError(f"{label} cannot establish a unit for an empty numeric result")
    return declared


def _common_unit(operands: Sequence[_ResolvedOperand], label: str) -> str:
    normalized = [item.unit for item in operands]
    if any(unit is None for unit in normalized):
        raise BundleReductionError(f"{label} requires a source-bound unit for every numeric operand")
    unique = {str(unit) for unit in normalized}
    if len(unique) != 1:
        raise BundleReductionError(f"{label} cannot combine incompatible units: {sorted(unique)}")
    return next(iter(unique))


def _canonical_unit(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    if not text:
        return None
    aliases = {
        "count": "count",
        "ea": "count",
        "each": "count",
        "item_count": "count",
        "location_count": "count",
        "pcs": "count",
        "piece": "count",
        "place": "count",
        "unit": "count",
        "개": "count",
        "개소": "count",
        "m2": "square_meter",
        "m²": "square_meter",
        "㎡": "square_meter",
        "square_meter": "square_meter",
        "square metre": "square_meter",
        "m^2": "square_meter",
        "autodesk.unit.unit:squaremeters-1.0.1": "square_meter",
        "mm": "millimeter",
        "millimeter": "millimeter",
        "millimetre": "millimeter",
        "mpa": "megapascal",
        "megapascal": "megapascal",
        "%": "percent",
        "percent": "percent",
        "percentage": "percent",
        "w/mk": "watt_per_meter_kelvin",
        "w/m·k": "watt_per_meter_kelvin",
        "w/m∙k": "watt_per_meter_kelvin",
        "w/m⋅k": "watt_per_meter_kelvin",
        "w/(m·k)": "watt_per_meter_kelvin",
        "w/(m∙k)": "watt_per_meter_kelvin",
        "w/(m⋅k)": "watt_per_meter_kelvin",
        "watt_per_meter_kelvin": "watt_per_meter_kelvin",
        "°c": "degree_celsius",
        "degree_celsius": "degree_celsius",
        "ton": "metric_ton",
        "metric_ton": "metric_ton",
        "tonne": "metric_ton",
        "m3": "cubic_meter",
        "m³": "cubic_meter",
        "㎥": "cubic_meter",
        "cubic_meter": "cubic_meter",
        "m": "meter",
        "meter": "meter",
    }
    return aliases.get(text, text)


def _required_sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BundleReductionError(f"{label} requires an array input")
    return list(value)


def _required_rows(value: Any, label: str) -> list[dict[str, Any]]:
    values = _required_sequence(value, label)
    if not all(isinstance(item, Mapping) for item in values):
        raise BundleReductionError(f"{label} requires an array of row objects")
    return [dict(item) for item in values]


def _required_field(row: Mapping[str, Any], field: str, label: str) -> Any:
    if field not in row:
        raise BundleReductionError(f"{label} row is missing field {field!r}")
    return row[field]


def _required_decimal(value: Any, label: str) -> Decimal:
    number = _decimal(value)
    if number is None:
        raise BundleReductionError(f"{label} requires finite numeric values, got {value!r}")
    return number


def _required_scalar(value: Any, label: str) -> Any:
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    ):
        raise BundleReductionError(f"{label} requires scalar comparison values")
    return value


def _rank_rows(rows: list[dict[str, Any]], field: str, label: str) -> list[dict[str, Any]]:
    scored = [(_required_decimal(_required_field(row, field, label), label), row) for row in rows]
    return [
        row
        for _, row in sorted(
            scored,
            key=lambda item: (-item[0], _canonical_json(item[1])),
        )
    ]


def _reject_rank_boundary_tie(
    ranked: list[dict[str, Any]],
    field: str,
    limit: int,
    label: str,
) -> None:
    if limit >= len(ranked):
        return
    included_score = _required_decimal(_required_field(ranked[limit - 1], field, label), label)
    excluded_score = _required_decimal(_required_field(ranked[limit], field, label), label)
    if included_score == excluded_score:
        raise BundleReductionError(
            f"{label} found a tie at rank boundary {limit} for field {field!r}"
        )


def _join_value(item: Any, field: str | None, label: str) -> Any:
    if field is None:
        if isinstance(item, Mapping | list | tuple):
            raise BundleReductionError(f"{label} join key must be a scalar value")
        return item
    if not isinstance(item, Mapping):
        raise BundleReductionError(f"{label} left_key requires row-object left inputs")
    return _required_field(item, field, label)


def _unique_join_keys(items: Sequence[Any], field: str | None, label: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for item in items:
        value = _join_value(item, field, label)
        key = _normalized_join_key(value, label)
        if key in indexed:
            raise BundleReductionError(f"{label} found duplicate join key {value!r}")
        indexed[key] = item
    return indexed


def _normalized_join_key(value: Any, label: str) -> str:
    """Normalize join scalars with the same semantics used by equality filters.

    Booleans remain a distinct domain from numbers, finite numeric strings and
    numeric values share a key, and text keys are trimmed and case-folded.
    Null and structured values are not safe identity keys and fail closed.
    """

    if value is None:
        raise BundleReductionError(f"{label} join key cannot be null")
    if isinstance(value, bool):
        return _canonical_json(["bool", value])
    if isinstance(value, Mapping | list | tuple):
        raise BundleReductionError(f"{label} join key must be a scalar value")
    number = _decimal(value)
    if number is not None:
        if number == 0:
            number = Decimal(0)
        return _canonical_json(["number", str(number.normalize())])
    if isinstance(value, (int, float, Decimal)):
        raise BundleReductionError(f"{label} join key must be a finite number")
    if isinstance(value, str):
        return _canonical_json(["text", value.strip().casefold()])
    raise BundleReductionError(
        f"{label} join key has unsupported scalar type {type(value).__name__}"
    )


def _left_join_zero(
    left: list[Any],
    right: list[dict[str, Any]],
    *,
    left_key: str | None,
    right_key: str,
    value_field: str,
    label: str,
) -> list[Any]:
    """Return left-side identities whose complete right aggregate is zero.

    A missing right row is treated as zero only because bundle plans are full
    snapshot scans and ``execute_query_bundle`` rejects incomplete plan
    results before reducers run. This helper must not be reused with sampled,
    limited, or otherwise partial right-side rows.
    """

    left_index = _unique_join_keys(left, left_key, label)
    right_index = _unique_join_keys(right, right_key, label)
    zero_items: list[Any] = []
    for key, left_item in left_index.items():
        right_row = right_index.get(key)
        if right_row is None:
            zero_items.append(left_item)
            continue
        value = _required_decimal(_required_field(right_row, value_field, label), label)
        if value == 0:
            zero_items.append(left_item)
    return sorted((_json_safe(item) for item in zero_items), key=_canonical_json)


def _inner_join_rows(
    left: list[Any],
    right: list[dict[str, Any]],
    *,
    left_key: str | None,
    right_key: str,
    label: str,
) -> list[dict[str, Any]]:
    left_index = _unique_join_keys(left, left_key, label)
    right_index = _unique_join_keys(right, right_key, label)
    return [dict(row) for key, row in right_index.items() if key in left_index]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item) for item in value]
    return value


def _node_types(plan: Mapping[str, Any], entity: str) -> frozenset[str]:
    explicit = plan.get("node_types")
    if explicit:
        return frozenset(str(value) for value in explicit)
    if entity == "graph_node":
        return frozenset()
    try:
        return ENTITY_NODE_TYPES[entity]
    except KeyError as exc:
        raise QueryExecutionError(f"unsupported entity: {entity}") from exc


def _snapshot_entries(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = snapshot.get("packs") or snapshot.get("pack_entries") or []
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise QueryExecutionError("snapshot packs must be a list")
    normalized = []
    for entry in entries:
        if isinstance(entry, Mapping):
            normalized.append(dict(entry))
        elif is_dataclass(entry):
            normalized.append(asdict(entry))
        elif hasattr(entry, "as_dict"):
            normalized.append(dict(entry.as_dict()))
        else:
            raise QueryExecutionError("snapshot pack entry must be mapping-like")
    return normalized


def _selected_entries(
    plan: Mapping[str, Any], snapshot: Mapping[str, Any], entity: str
) -> list[dict[str, Any]]:
    requested_roles = {
        str(role)
        for role in (plan.get("source_roles") or DEFAULT_ENTITY_ROLES.get(entity, ()))
        if str(role)
    }
    entries = []
    for entry in _snapshot_entries(snapshot):
        if entry.get("included", True) is False:
            continue
        role = str(entry.get("role") or "")
        if requested_roles and role not in requested_roles:
            continue
        entries.append(entry)
    return sorted(
        entries,
        key=lambda item: (
            str(item.get("role") or ""),
            int(item.get("shard_index") or 0),
            str(item.get("pack_id") or ""),
        ),
    )


def _entry_path(entry: Mapping[str, Any], snapshot: Mapping[str, Any]) -> Path:
    raw = str(entry.get("path") or entry.get("zip_path") or "").strip()
    if not raw:
        raise QueryExecutionError(f"snapshot pack {entry.get('pack_id')!r} is missing path")
    path = Path(raw)
    if not path.is_absolute():
        base = Path(str(snapshot.get("base_path") or snapshot.get("manifest_dir") or Path.cwd()))
        path = base / path
    path = path.resolve()
    if not path.exists():
        raise QueryExecutionError(f"snapshot pack path does not exist: {path}")
    return path


def _iter_pack_nodes(path: Path) -> Iterable[dict[str, Any]]:
    if path.is_dir():
        member = path / "graph" / "nodes.jsonl"
        if member.is_file():
            yield from _iter_jsonl_lines(
                member.read_text(encoding="utf-8-sig").splitlines(),
                str(member),
            )
            return
        raise QueryExecutionError(f"pack has no graph nodes JSONL: {path}")
    if not zipfile.is_zipfile(path):
        raise QueryExecutionError(f"snapshot pack is not a ZIP or directory: {path}")
    with zipfile.ZipFile(path) as archive:
        member = "graph/nodes.jsonl"
        member_count = archive.namelist().count(member)
        if member_count != 1:
            raise QueryExecutionError(
                f"pack must contain exactly one {member} member, got {member_count}: {path}"
            )
        with archive.open(member) as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    payload = json.loads(
                        raw_line.decode("utf-8-sig"),
                        parse_constant=_reject_json_constant,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise QueryExecutionError(
                        f"invalid JSONL at {path}!{member}:{line_number}: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise QueryExecutionError(
                        f"JSONL record must be an object at {path}!{member}:{line_number}"
                    )
                yield payload


def _iter_pack_edges(path: Path) -> Iterable[dict[str, Any]]:
    """Stream the complete public graph/edges.jsonl member to EOF."""

    if path.is_dir():
        member = path / "graph" / "edges.jsonl"
        if member.is_file():
            yield from _iter_jsonl_lines(
                member.read_text(encoding="utf-8-sig").splitlines(),
                str(member),
            )
            return
        raise QueryExecutionError(f"pack has no graph edges JSONL: {path}")
    if not zipfile.is_zipfile(path):
        raise QueryExecutionError(f"snapshot pack is not a ZIP or directory: {path}")
    with zipfile.ZipFile(path) as archive:
        member = "graph/edges.jsonl"
        member_count = archive.namelist().count(member)
        if member_count != 1:
            raise QueryExecutionError(
                f"pack must contain exactly one {member} member, got {member_count}: {path}"
            )
        with archive.open(member) as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    payload = json.loads(
                        raw_line.decode("utf-8-sig"),
                        parse_constant=_reject_json_constant,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise QueryExecutionError(
                        f"invalid JSONL at {path}!{member}:{line_number}: {exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise QueryExecutionError(
                        f"JSONL record must be an object at {path}!{member}:{line_number}"
                    )
                yield payload


def _iter_jsonl_lines(lines: Iterable[str], source: str) -> Iterable[dict[str, Any]]:
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise QueryExecutionError(
                f"invalid JSONL at {source}:{line_number}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise QueryExecutionError(f"JSONL record must be an object at {source}:{line_number}")
        yield payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite numeric JSON constant {value!r} is not allowed")


def _flatten_node(
    node: Mapping[str, Any],
    *,
    pack_id: str,
    pack_hash: str,
    source_role: str = "",
) -> dict[str, Any]:
    properties = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
    row = dict(properties)
    for key, value in node.items():
        if key == "properties":
            continue
        if key in row:
            # Domain records legitimately reuse wrapper keys such as ``id``,
            # ``node_type``, and ``label`` for their raw source properties.
            # Preserve the historical property-first projection for those
            # nodes.  Relationship endpoints are different: allowing a
            # property payload to shadow the graph edge would make traversal
            # evidence forgeable, so those three fields remain fail-closed.
            if (
                key in {"source", "relation", "target"}
                and _canonical_json(row[key]) != _canonical_json(value)
            ):
                raise QueryExecutionError(
                    f"conflicting top-level/properties field {key!r}"
                )
            continue
        row[key] = value
    row["_pack_id"] = pack_id
    row["_pack_hash"] = pack_hash
    row["_source_role"] = source_role
    return row


def _field(row: Mapping[str, Any], field: str) -> Any:
    candidates = FIELD_ALIASES.get(field, (field,))
    resolved: list[tuple[str, Any]] = []
    for candidate in candidates:
        current: Any = row
        found = True
        for part in candidate.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found and current is not None:
            if field not in STRICT_FIELD_ALIASES:
                return current
            resolved.append((candidate, current))
    if not resolved:
        return None
    first_candidate, first_value = resolved[0]
    for candidate, value in resolved[1:]:
        if not _equal(first_value, value):
            raise QueryExecutionError(
                f"ambiguous field alias {field!r}: conflicting candidates "
                f"{first_candidate!r} and {candidate!r}"
            )
    return first_value


def _matches_granularity(row: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    granularity = str(plan.get("entity_granularity") or "").strip()
    if granularity == "canonical_module_registry":
        return (
            _equal(_field(row, "kind"), "UserWorkset")
            and CANONICAL_MODULE_PATTERN.fullmatch(str(_field(row, "name") or "")) is not None
        )
    if granularity != "canonical_bim_object":
        return True
    if _field(row, "class") in CANONICAL_OBJECT_EXCLUDED_CLASSES:
        return False
    return _field(row, "category") not in CANONICAL_OBJECT_EXCLUDED_CATEGORIES


def _matches_scope(row: Mapping[str, Any], scope: object) -> bool:
    if not isinstance(scope, Mapping):
        return True
    mapping = {
        "category": "category",
        "categories": "category",
        "level": "level",
        "levels": "level",
        "level_name": "level_name",
        "module_id": "module_id",
        "module_ids": "module_id",
        "module_type": "module_type",
        "workset_name": "workset_name",
        "type_name": "type_name",
        "type_names": "type_name",
    }
    internal_keys = {"exclude_categories", "exclude_classes"}
    for scope_key, expected in scope.items():
        if scope_key in internal_keys:
            continue
        field = mapping.get(str(scope_key), str(scope_key))
        candidates = expected if isinstance(expected, Sequence) and not isinstance(expected, str) else [expected]
        if not any(_equal(_field(row, field), item) for item in candidates):
            return False
    excluded_categories = scope.get("exclude_categories") or []
    if any(_equal(_field(row, "category"), item) for item in excluded_categories):
        return False
    excluded_classes = scope.get("exclude_classes") or []
    if any(_equal(_field(row, "class"), item) for item in excluded_classes):
        return False
    return True


def _matches_filters(row: Mapping[str, Any], filters: object) -> bool:
    if not filters:
        return True
    if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
        raise QueryExecutionError("filters must be a list")
    for condition in filters:
        if not isinstance(condition, Mapping):
            raise QueryExecutionError("filter must be an object")
        field = str(condition.get("field") or "")
        operator = str(condition.get("operator") or "eq")
        expected = condition.get("value")
        value = _field(row, field)
        if not _match(value, operator, expected):
            return False
    return True


def _match(value: Any, operator: str, expected: Any) -> bool:
    if operator == "exists":
        return (value is not None) is bool(expected)
    if operator == "eq":
        return _equal(value, expected)
    if operator == "ne":
        return not _equal(value, expected)
    if operator in {"in", "not_in"}:
        candidates = expected if isinstance(expected, Sequence) and not isinstance(expected, str) else [expected]
        answer = any(_equal(value, candidate) for candidate in candidates)
        return answer if operator == "in" else not answer
    if operator == "contains":
        return str(expected).casefold() in str(value).casefold()
    if operator == "startswith":
        return str(value).casefold().startswith(str(expected).casefold())
    if operator == "endswith":
        return str(value).casefold().endswith(str(expected).casefold())
    if operator == "regex":
        try:
            return re.search(str(expected), str(value)) is not None
        except re.error as exc:
            raise QueryExecutionError(f"invalid filter regex: {expected}") from exc
    if operator in {"gt", "gte", "lt", "lte"}:
        left = _decimal(value)
        right = _decimal(expected)
        if left is None or right is None:
            return False
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[operator]
    raise QueryExecutionError(f"unsupported filter operator: {operator}")


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return str(left).strip().casefold() == str(right).strip().casefold()


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            result = Decimal(str(value))
            return result if result.is_finite() else None
        except InvalidOperation:
            return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _dedupe_key(row: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    fields = plan.get("dedupe_by")
    if fields:
        values = [_field(row, str(field)) for field in fields]
        return _canonical_json(values)
    node_id = _field(row, "id")
    if node_id is not None:
        return str(node_id)
    return _sha256_text(_canonical_json(row))


def _normalize_metrics(metrics: object, *, entity: str = "") -> tuple[dict[str, Any], ...]:
    if not metrics:
        return ()
    if not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes)):
        raise QueryExecutionError("metrics must be a list")
    normalized = []
    for metric in metrics:
        if not isinstance(metric, Mapping):
            raise QueryExecutionError("metric must be an object")
        item = dict(metric)
        item["agg"] = str(item.get("agg") or item.get("fn") or item.get("name") or "")
        default_alias = item.get("field") if item.get("field") not in {None, "*"} else item["agg"]
        item["as"] = str(item.get("as") or item.get("alias") or default_alias)
        # Quantities are never summed across implicit units.  Contract callers
        # cannot choose a unit field, so the executor binds the canonical one.
        if entity == "boq_item" and item["agg"] in {"sum", "avg", "min", "max"} and item.get("field") == "quantity":
            item.setdefault("unit_field", "normalized_unit")
        if (
            entity == "quantity_fact"
            and item["agg"] in {"sum", "avg", "min", "max"}
            and item.get("field") == "area_m2"
        ):
            item.setdefault("static_unit", "square_meter")
        normalized.append(item)
    return tuple(normalized)


def _aggregate_metrics(rows: list[dict[str, Any]], metrics: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for metric in metrics:
        agg = metric["agg"]
        alias = metric["as"]
        field = metric.get("field")
        fields = metric.get("fields")
        values = [_field(row, str(field)) for row in rows] if field else []
        if agg == "count":
            _write_output(output, alias, len(rows))
        elif agg == "count_distinct":
            if fields:
                distinct = {_canonical_json([_field(row, str(name)) for name in fields]) for row in rows}
            else:
                distinct = {_canonical_json(value) for value in values if value is not None}
            _write_output(output, alias, len(distinct))
        elif agg == "collect_distinct":
            distinct_values = {_canonical_json(value): value for value in values if value is not None}
            _write_output(output, alias, [distinct_values[key] for key in sorted(distinct_values)])
        elif agg in {"sum", "avg", "min", "max"}:
            numbers: list[Decimal] = []
            unit_field = metric.get("unit_field")
            static_unit = metric.get("static_unit")
            units: set[str] = set()
            for row_index, (row, value) in enumerate(zip(rows, values, strict=True)):
                if static_unit and (
                    isinstance(value, bool) or not isinstance(value, int | float | Decimal)
                ):
                    raise QueryExecutionError(
                        f"numeric aggregate {alias!r} requires a numeric {field!r} value "
                        f"at matched row {row_index}"
                    )
                number = _decimal(value)
                if number is None:
                    raise QueryExecutionError(
                        f"numeric aggregate {alias!r} has a missing or non-numeric {field!r} value "
                        f"at matched row {row_index}"
                    )
                numbers.append(number)
                if unit_field:
                    raw_unit = _field(row, str(unit_field))
                    if not isinstance(raw_unit, str) or not raw_unit.strip():
                        raise QueryExecutionError(
                            f"numeric aggregate {alias!r} has a missing or invalid unit "
                            f"{unit_field!r} at matched row {row_index}"
                        )
                    units.add(raw_unit.strip())
            if static_unit:
                if not numbers:
                    raise QueryExecutionError(
                        f"numeric aggregate {alias!r} cannot aggregate an empty result"
                    )
                _write_output(output, f"{alias}_unit", str(static_unit))
            elif unit_field:
                if len(units) > 1:
                    raise MixedUnitError(f"heterogeneous_unit_sum: {sorted(str(unit) for unit in units)}")
                if units:
                    _write_output(output, f"{alias}_unit", next(iter(units)))
            if not numbers:
                _write_output(output, alias, None)
            elif agg == "sum":
                _write_output(output, alias, _json_number(sum(numbers, Decimal(0))))
            elif agg == "avg":
                _write_output(
                    output,
                    alias,
                    _json_number(sum(numbers, Decimal(0)) / Decimal(len(numbers))),
                )
            elif agg == "min":
                _write_output(output, alias, _json_number(min(numbers)))
            else:
                _write_output(output, alias, _json_number(max(numbers)))
        else:
            raise QueryExecutionError(f"unsupported metric: {agg}")
    return output


def _validate_execution_output_names(
    group_by: tuple[str, ...],
    metrics: tuple[dict[str, Any], ...],
) -> None:
    reserved: set[str] = set()

    def reserve(name: str) -> None:
        if name in reserved:
            raise QueryExecutionError(f"output name collision: {name!r}")
        reserved.add(name)

    for field in group_by:
        reserve(field)
    for metric in metrics:
        alias = str(metric["as"])
        reserve(alias)
        if metric["agg"] in {"sum", "avg", "min", "max"} and (
            metric.get("unit_field") or metric.get("static_unit")
        ):
            reserve(f"{alias}_unit")


def _write_output(output: dict[str, Any], name: str, value: Any) -> None:
    if name in output:
        raise QueryExecutionError(f"output name collision: {name!r}")
    output[name] = value


def _sort_result_rows(
    rows: list[dict[str, Any]], order_by: object, group_by: tuple[str, ...], metrics: tuple[dict[str, Any], ...]
) -> list[dict[str, Any]]:
    specs = order_by if isinstance(order_by, Sequence) and not isinstance(order_by, str) else []
    if not specs:
        specs = [{"field": field, "direction": "asc"} for field in group_by]
    ordered = list(rows)
    for raw in reversed(specs):
        spec = {"field": raw} if isinstance(raw, str) else dict(raw)
        field = str(spec.get("field") or "")
        reverse = str(spec.get("direction") or "asc").casefold() == "desc"
        ordered.sort(key=lambda row: _sort_key(row.get(field)), reverse=reverse)
    return ordered


def _sort_key(value: Any) -> tuple[int, Any]:
    number = _decimal(value)
    if number is not None:
        return (0, number)
    if value is None:
        return (2, "")
    return (1, str(value).casefold())


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    # Source JSON may contain binary-float artefacts such as
    # 3446.7700000000004. Fifteen significant digits preserve normal double
    # precision while removing that serialization noise from public results
    # and hashes.
    number = float(format(value, ".15g"))
    if not math.isfinite(number):
        raise QueryExecutionError("non-finite aggregate result")
    return number


def _evidence_sample(node: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    refs = node.get("evidence_refs") if isinstance(node.get("evidence_refs"), list) else []
    return {
        "node_id": node.get("id"),
        "pack_id": row.get("_pack_id"),
        "pack_hash": row.get("_pack_hash"),
        "evidence_refs": refs[:3],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_directory_pack(path: Path) -> str:
    digest = hashlib.sha256()
    for member in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(member.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with member.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise QueryExecutionError("value is not finite canonical JSON") from exc
