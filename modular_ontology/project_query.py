"""End-to-end canonical project query orchestration.

The language model is deliberately confined to producing a typed query plan.
Snapshot resolution, full-pack scanning, aggregation, and claim validation are
all deterministic operations implemented in this package.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from .claim_validation import Claim, ClaimValidation, Evidence, validate_claim
from .query_contract import (
    ENTITY_POLICIES,
    FORBIDDEN_KEYS,
    PLAN_FIELDS,
    BIMQueryPlan,
    validate_query_plan,
)
from .query_engine import QueryResult, execute_query
from .snapshot_store import ProjectSnapshot, SnapshotVerification, load_snapshot, verify_snapshot


class ProjectQueryError(RuntimeError):
    """Raised when a canonical project query cannot be completed safely."""


Planner = Callable[[str, Mapping[str, Any]], Mapping[str, Any] | str]


@dataclass(frozen=True)
class ProjectQueryExecution:
    snapshot: ProjectSnapshot
    verification: SnapshotVerification
    plan: BIMQueryPlan
    result: QueryResult
    question: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "snapshot": {
                "project_id": self.snapshot.project_id,
                "snapshot_id": self.snapshot.snapshot_id,
                "snapshot_hash": self.snapshot.snapshot_hash,
                "source_signature": self.snapshot.source_signature,
                "manifest_path": str(self.snapshot.manifest_path),
            },
            "verification": {
                "valid": self.verification.valid,
                "checked_pack_ids": list(self.verification.checked_pack_ids),
                "issues": [asdict(issue) for issue in self.verification.issues],
                "warnings": [asdict(issue) for issue in self.verification.warnings],
            },
            "plan": self.plan.as_dict(),
            "result": self.result.as_dict(),
        }


def query_contract_manifest() -> dict[str, Any]:
    """Return the exact plan grammar supplied to an NL-to-plan model."""

    entities: dict[str, Any] = {}
    for name, policy in ENTITY_POLICIES.items():
        entities[name] = {
            "fields": sorted(policy.fields),
            "scope_fields": sorted(policy.scope_fields),
            "operators": sorted(policy.operators),
            "metrics": {metric: sorted(fields) for metric, fields in policy.metrics.items()},
        }
    return {
        "top_level_fields": sorted(PLAN_FIELDS),
        "forbidden_fields": sorted(FORBIDDEN_KEYS),
        "entities": entities,
        "rule": "Return only a query-plan JSON object. Never calculate or answer the question.",
    }


def execute_structured_project_query(
    plan_payload: Mapping[str, Any],
    snapshot: ProjectSnapshot | str | Path,
    *,
    verify: bool = True,
) -> ProjectQueryExecution:
    """Validate a plan, resolve one pinned snapshot, then execute to EOF."""

    plan = validate_query_plan(plan_payload)
    loaded = snapshot if isinstance(snapshot, ProjectSnapshot) else load_snapshot(snapshot)
    if plan.project_id != loaded.project_id:
        raise ProjectQueryError(
            f"query project {plan.project_id!r} does not match snapshot project {loaded.project_id!r}"
        )
    verification = verify_snapshot(loaded)
    if verify:
        verification.require_valid()
    result = execute_query(plan, loaded)
    return ProjectQueryExecution(
        snapshot=loaded,
        verification=verification,
        plan=plan,
        result=result,
    )


def answer_project_question(
    question: str,
    snapshot: ProjectSnapshot | str | Path,
    planner: Planner,
    *,
    verify: bool = True,
) -> ProjectQueryExecution:
    """Use an LLM only for NL-to-plan conversion; all values come from the executor."""

    normalized_question = question.strip()
    if not normalized_question:
        raise ProjectQueryError("question is required")
    raw_plan = planner(normalized_question, query_contract_manifest())
    if isinstance(raw_plan, str):
        try:
            raw_plan = json.loads(raw_plan)
        except json.JSONDecodeError as exc:
            raise ProjectQueryError("planner did not return valid JSON") from exc
    if not isinstance(raw_plan, Mapping):
        raise ProjectQueryError("planner must return one query-plan object")
    execution = execute_structured_project_query(raw_plan, snapshot, verify=verify)
    return ProjectQueryExecution(
        snapshot=execution.snapshot,
        verification=execution.verification,
        plan=execution.plan,
        result=execution.result,
        question=normalized_question,
    )


def execution_scope(execution: ProjectQueryExecution, group: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the exact semantic scope bound to an aggregate result."""

    scope: dict[str, Any] = {
        "project_id": execution.plan.project_id,
        "entity": execution.plan.entity,
        "scope": dict(execution.plan.scope),
        "filters": [item.as_dict() for item in execution.plan.filters],
        "group_by": list(execution.plan.group_by),
    }
    if group is not None:
        scope["group"] = dict(group)
    return scope


def validate_execution_claim(
    execution: ProjectQueryExecution,
    expected: Mapping[str, Any],
) -> ClaimValidation:
    """Validate an expected value against the value/scope/version query envelope.

    Required expected fields are ``claim_id``, ``metric``, ``value``, and
    ``unit``.  Grouped results additionally use a ``group`` object.  An
    optional explicit ``scope`` is compared exactly with the derived scope.
    """

    claim_id = str(expected.get("claim_id") or "").strip()
    metric = str(expected.get("metric") or "").strip()
    if not claim_id or not metric or "value" not in expected or "unit" not in expected:
        raise ProjectQueryError("expected claim requires claim_id, metric, value, and unit")

    group_value = expected.get("group")
    group = dict(group_value) if isinstance(group_value, Mapping) else None
    values = execution.result.values
    if execution.result.rows:
        if group is None:
            raise ProjectQueryError("grouped result requires an expected group selector")
        matches = [
            row
            for row in execution.result.rows
            if all(row.get(field) == value for field, value in group.items())
        ]
        if len(matches) != 1:
            raise ProjectQueryError(f"expected group matched {len(matches)} result rows")
        values = matches[0]
    if metric not in values:
        raise ProjectQueryError(f"metric {metric!r} is not present in the result")

    derived_scope = execution_scope(execution, group)
    declared_scope = expected.get("scope")
    claim_scope = dict(declared_scope) if isinstance(declared_scope, Mapping) else derived_scope
    all_pack_hashes = {
        pack.pack_id: pack.sha256 for pack in execution.snapshot.packs if pack.included
    }
    evidence_pack_hashes = {
        str(pack["pack_id"]): str(pack["sha256"])
        for pack in execution.result.source_packs
    }
    result_unit = values.get(f"{metric}_unit")
    if result_unit is None:
        metric_spec = next(
            (
                item
                for item in execution.plan.metrics
                if (item.alias or (item.field if item.field not in {None, "*"} else item.name)) == metric
            ),
            None,
        )
        if metric_spec and metric_spec.name in {"count", "count_distinct"}:
            result_unit = "count"
        else:
            # Never manufacture evidence metadata from the expected claim.
            # A numeric aggregate without a source-derived unit is
            # insufficient evidence and must be rejected by validate_claim.
            result_unit = None

    anchor = {
        "anchor_type": "aggregate_query",
        "confidence": "exact",
        "snapshot_id": execution.result.snapshot_id,
        "query_hash": execution.result.query_hash,
        "result_hash": execution.result.result_hash,
        "contribution_digest": execution.result.evidence["contribution_digest"],
        "contribution_count": execution.result.evidence["contribution_count"],
    }
    claim = Claim(
        claim_id=claim_id,
        value=expected["value"],
        unit=str(expected["unit"]) if expected.get("unit") is not None else None,
        scope=claim_scope,
        snapshot_id=execution.result.snapshot_id,
        pack_hashes=all_pack_hashes,
        absolute_tolerance=float(expected.get("absolute_tolerance") or 0.0),
        relative_tolerance=float(expected.get("relative_tolerance") or 0.0),
    )
    evidence = Evidence(
        evidence_id=f"{claim_id}:{execution.result.result_hash[:16]}",
        value=values[metric],
        unit=str(result_unit) if result_unit is not None else None,
        scope=derived_scope,
        snapshot_id=execution.result.snapshot_id,
        pack_hashes=evidence_pack_hashes,
        anchors=(anchor,),
        query_hash=execution.result.query_hash,
        result_hash=execution.result.result_hash,
        contribution_digest=execution.result.evidence["contribution_digest"],
        contribution_count=execution.result.evidence["contribution_count"],
    )
    return validate_claim(claim, (evidence,))
