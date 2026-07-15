from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


VERDICTS = frozenset({"supported", "contradicted", "insufficient", "mixed_scope", "stale"})


@dataclass(frozen=True)
class Claim:
    claim_id: str
    value: Any
    unit: str | None
    scope: Mapping[str, Any]
    snapshot_id: str
    pack_hashes: Mapping[str, str]
    absolute_tolerance: float = 0.0
    relative_tolerance: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "value": _json_value(self.value),
            "unit": self.unit,
            "scope": _json_value(self.scope),
            "snapshot_id": self.snapshot_id,
            "pack_hashes": dict(self.pack_hashes),
            "absolute_tolerance": self.absolute_tolerance,
            "relative_tolerance": self.relative_tolerance,
        }


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    value: Any
    unit: str | None
    scope: Mapping[str, Any]
    snapshot_id: str
    pack_hashes: Mapping[str, str]
    anchors: tuple[Mapping[str, Any], ...] = ()
    query_hash: str | None = None
    result_hash: str | None = None
    contribution_digest: str | None = None
    contribution_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "value": _json_value(self.value),
            "unit": self.unit,
            "scope": _json_value(self.scope),
            "snapshot_id": self.snapshot_id,
            "pack_hashes": dict(self.pack_hashes),
            "anchors": [_json_value(anchor) for anchor in self.anchors],
            "query_hash": self.query_hash,
            "result_hash": self.result_hash,
            "contribution_digest": self.contribution_digest,
            "contribution_count": self.contribution_count,
        }


@dataclass(frozen=True)
class ClaimIssue:
    code: str
    message: str
    evidence_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.evidence_id is not None:
            payload["evidence_id"] = self.evidence_id
        if self.details:
            payload["details"] = _json_value(self.details)
        return payload


@dataclass(frozen=True)
class ClaimValidation:
    claim_id: str
    verdict: str
    issues: tuple[ClaimIssue, ...]
    matched_evidence_ids: tuple[str, ...] = ()
    checked_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"Unsupported verdict: {self.verdict}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "verdict": self.verdict,
            "issues": [issue.as_dict() for issue in self.issues],
            "matched_evidence_ids": list(self.matched_evidence_ids),
            "checked_evidence_ids": list(self.checked_evidence_ids),
        }


def validate_claim(claim: Claim, evidence: Sequence[Evidence]) -> ClaimValidation:
    rows = tuple(evidence)
    issues: list[ClaimIssue] = []
    checked = tuple(row.evidence_id for row in rows)
    matched: list[str] = []

    if not claim.claim_id.strip():
        issues.append(ClaimIssue("missing_claim_id", "claim_id is required"))
    if not claim.snapshot_id.strip():
        issues.append(ClaimIssue("missing_snapshot", "claim must name the resolved canonical snapshot"))
    if not claim.pack_hashes or any(not str(key).strip() or not str(value).strip() for key, value in claim.pack_hashes.items()):
        issues.append(ClaimIssue("missing_pack_hash", "claim must carry the canonical pack hashes"))
    if claim.absolute_tolerance < 0 or claim.relative_tolerance < 0:
        issues.append(ClaimIssue("invalid_tolerance", "claim tolerances cannot be negative"))
    if not rows:
        issues.append(ClaimIssue("no_evidence", "no evidence was supplied"))
        return ClaimValidation(claim.claim_id, "insufficient", tuple(issues), checked_evidence_ids=checked)

    scope_keys: set[tuple[tuple[str, Any], ...]] = set()
    has_stale = False
    has_scope_error = False
    has_insufficient = bool(issues)
    has_contradiction = False

    for row in rows:
        evidence_id = row.evidence_id
        scope_keys.add(_scope_key(row.scope))

        if not row.snapshot_id:
            has_insufficient = True
            issues.append(ClaimIssue("missing_snapshot", "evidence has no snapshot id", evidence_id))
        elif claim.snapshot_id and row.snapshot_id != claim.snapshot_id:
            has_stale = True
            issues.append(
                ClaimIssue(
                    "snapshot_mismatch",
                    "evidence was produced from a different snapshot",
                    evidence_id,
                    {"expected": claim.snapshot_id, "actual": row.snapshot_id},
                )
            )

        pack_state = _check_pack_hashes(claim.pack_hashes, row.pack_hashes, evidence_id)
        issues.extend(pack_state)
        if any(issue.code in {"pack_hash_mismatch", "pack_not_in_snapshot"} for issue in pack_state):
            has_stale = True
        if any(issue.code == "missing_pack_hash" for issue in pack_state):
            has_insufficient = True

        if not _scope_equal(claim.scope, row.scope):
            has_scope_error = True
            issues.append(
                ClaimIssue(
                    "scope_mismatch",
                    "evidence scope does not exactly match the claim scope",
                    evidence_id,
                    {"expected": claim.scope, "actual": row.scope},
                )
            )

        anchor_issues = _check_anchors(row)
        issues.extend(anchor_issues)
        if anchor_issues:
            has_insufficient = True

        binding_issues = _check_aggregate_anchor_bindings(row)
        issues.extend(binding_issues)
        if binding_issues:
            has_insufficient = True

        comparison, comparison_issue = _compare_value(claim, row)
        if comparison == "match":
            matched.append(evidence_id)
        elif comparison == "insufficient":
            has_insufficient = True
            if comparison_issue:
                issues.append(comparison_issue)
        else:
            has_contradiction = True
            if comparison_issue:
                issues.append(comparison_issue)

    if len(scope_keys) > 1:
        has_scope_error = True
        issues.append(
            ClaimIssue(
                "mixed_evidence_scopes",
                "evidence rows span more than one scope",
                details={"scope_count": len(scope_keys)},
            )
        )

    # Version and scope failures are never allowed to degrade into a value judgement.
    if has_stale:
        verdict = "stale"
    elif has_scope_error:
        verdict = "mixed_scope"
    elif has_insufficient:
        verdict = "insufficient"
    elif has_contradiction:
        verdict = "contradicted"
    else:
        verdict = "supported"

    return ClaimValidation(
        claim_id=claim.claim_id,
        verdict=verdict,
        issues=tuple(issues),
        matched_evidence_ids=tuple(matched),
        checked_evidence_ids=checked,
    )


def anchor_completeness(anchor: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    anchor_type = str(anchor.get("anchor_type") or "unknown").casefold()
    confidence = str(anchor.get("confidence") or "").casefold()
    ids_value = anchor.get("ids")
    ids = ids_value if isinstance(ids_value, Mapping) else {}
    document_key = _text(anchor.get("document_key")) or _text(ids.get("source_file"))

    missing: list[str] = []
    if confidence == "unresolvable" or anchor_type == "unknown":
        return False, ("resolvable_anchor_type",)

    if anchor_type == "revit_element":
        if _present(ids.get("unique_id")) or _present(ids.get("ifc_guid")):
            return True, ()
        if not _present(ids.get("element_id")):
            missing.append("ids.element_id|ids.unique_id|ids.ifc_guid")
        if not document_key:
            missing.append("document_key")
    elif anchor_type == "revit_sheet":
        if _present(ids.get("sheet_unique_id")):
            return True, ()
        if not (_present(ids.get("sheet_number")) or _present(ids.get("sheet_id"))):
            missing.append("ids.sheet_number|ids.sheet_id|ids.sheet_unique_id")
        if not document_key:
            missing.append("document_key")
    elif anchor_type == "revit_view":
        if _present(ids.get("view_unique_id")):
            return True, ()
        if not (_present(ids.get("view_id")) or _present(ids.get("view_name"))):
            missing.append("ids.view_id|ids.view_name|ids.view_unique_id")
        if not document_key:
            missing.append("document_key")
    elif anchor_type == "revit_schedule_row":
        if not (_present(ids.get("schedule_unique_id")) or _present(ids.get("schedule_name"))):
            missing.append("ids.schedule_unique_id|ids.schedule_name")
        if not _present(ids.get("row")):
            missing.append("ids.row")
    elif anchor_type == "boq_sheet_row":
        if not (_present(ids.get("source_sheet")) or document_key):
            missing.append("ids.source_sheet|document_key")
        if not _present(ids.get("source_row")):
            missing.append("ids.source_row")
    elif anchor_type == "dxf_entity":
        if not (_present(ids.get("source_file")) or document_key):
            missing.append("ids.source_file|document_key")
        if not any(_present(ids.get(key)) for key in ("handle", "entity_key", "entity_id")):
            missing.append("ids.handle|ids.entity_key|ids.entity_id")
    elif anchor_type == "pdf_page":
        if not (_present(ids.get("source_file")) or document_key):
            missing.append("ids.source_file|document_key")
        if not _present(ids.get("page")):
            missing.append("ids.page")
    elif anchor_type == "aggregate_query":
        # Aggregate claims are supported by the complete deterministic query
        # envelope, not by pretending that one sampled source row proves the
        # total.  The contribution digest binds the exact matched ID set.
        for key in ("snapshot_id", "query_hash", "result_hash", "contribution_digest", "contribution_count"):
            if not _present(anchor.get(key)):
                missing.append(key)
    else:
        missing.append("supported_anchor_type")
    return not missing, tuple(missing)


def _check_anchors(row: Evidence) -> list[ClaimIssue]:
    if not row.anchors:
        return [ClaimIssue("missing_anchor", "evidence has no source anchor", row.evidence_id)]
    complete_count = 0
    incomplete: list[dict[str, Any]] = []
    for index, anchor in enumerate(row.anchors):
        complete, missing = anchor_completeness(anchor)
        if complete:
            complete_count += 1
        else:
            incomplete.append({"index": index, "missing": list(missing)})
    if complete_count:
        return []
    return [
        ClaimIssue(
            "incomplete_anchor",
            "evidence has no complete, resolvable source anchor",
            row.evidence_id,
            {"anchors": incomplete},
        )
    ]


def _check_aggregate_anchor_bindings(row: Evidence) -> list[ClaimIssue]:
    """Bind aggregate anchors to the typed evidence execution envelope."""

    expected = {
        "snapshot_id": row.snapshot_id,
        "query_hash": row.query_hash,
        "result_hash": row.result_hash,
        "contribution_digest": row.contribution_digest,
        "contribution_count": row.contribution_count,
    }
    issues: list[ClaimIssue] = []
    for index, anchor in enumerate(row.anchors):
        if str(anchor.get("anchor_type") or "").casefold() != "aggregate_query":
            continue
        for field_name, expected_value in expected.items():
            actual_value = anchor.get(field_name)
            if expected_value is None:
                issues.append(
                    ClaimIssue(
                        "missing_execution_binding",
                        f"aggregate evidence envelope has no {field_name}",
                        row.evidence_id,
                        {"anchor_index": index, "field": field_name},
                    )
                )
            elif actual_value != expected_value:
                issues.append(
                    ClaimIssue(
                        "aggregate_anchor_mismatch",
                        f"aggregate anchor {field_name} does not match the evidence envelope",
                        row.evidence_id,
                        {
                            "anchor_index": index,
                            "field": field_name,
                            "expected": expected_value,
                            "actual": actual_value,
                        },
                    )
                )
    return issues


def _check_pack_hashes(
    expected: Mapping[str, str],
    actual: Mapping[str, str],
    evidence_id: str,
) -> list[ClaimIssue]:
    if not actual:
        return [ClaimIssue("missing_pack_hash", "evidence has no source pack hash", evidence_id)]
    issues: list[ClaimIssue] = []
    for pack_id, pack_hash in actual.items():
        expected_hash = expected.get(pack_id)
        if expected_hash is None:
            issues.append(
                ClaimIssue(
                    "pack_not_in_snapshot",
                    "evidence references a pack outside the canonical snapshot",
                    evidence_id,
                    {"pack_id": pack_id, "actual_hash": pack_hash},
                )
            )
        elif not str(pack_hash).strip():
            issues.append(
                ClaimIssue("missing_pack_hash", "evidence pack hash is empty", evidence_id, {"pack_id": pack_id})
            )
        elif pack_hash != expected_hash:
            issues.append(
                ClaimIssue(
                    "pack_hash_mismatch",
                    "evidence pack content does not match the canonical snapshot",
                    evidence_id,
                    {"pack_id": pack_id, "expected": expected_hash, "actual": pack_hash},
                )
            )
    return issues


def _compare_value(claim: Claim, row: Evidence) -> tuple[str, ClaimIssue | None]:
    if row.value is None:
        return "insufficient", ClaimIssue("missing_value", "evidence has no value", row.evidence_id)

    claim_number = _number(claim.value)
    evidence_number = _number(row.value)
    if claim_number is not None and evidence_number is not None:
        converted, unit_state = _convert_numeric(evidence_number, row.unit, claim.unit)
        if unit_state == "missing":
            return "insufficient", ClaimIssue(
                "missing_unit",
                "numeric evidence has no unit required by the claim",
                row.evidence_id,
                {"expected": claim.unit},
            )
        if unit_state == "incompatible":
            return "mismatch", ClaimIssue(
                "unit_mismatch",
                "evidence unit is not compatible with the claim unit",
                row.evidence_id,
                {"expected": claim.unit, "actual": row.unit},
            )
        assert converted is not None
        if math.isclose(
            claim_number,
            converted,
            rel_tol=claim.relative_tolerance,
            abs_tol=claim.absolute_tolerance,
        ):
            return "match", None
        return "mismatch", ClaimIssue(
            "value_mismatch",
            "evidence value contradicts the claim",
            row.evidence_id,
            {"expected": claim.value, "actual": row.value, "actual_converted": converted, "unit": claim.unit},
        )

    if _scalar_equal(claim.value, row.value):
        if claim.unit and not row.unit:
            return "insufficient", ClaimIssue("missing_unit", "evidence has no unit", row.evidence_id)
        if claim.unit and row.unit and _unit_definition(claim.unit) != _unit_definition(row.unit):
            return "mismatch", ClaimIssue(
                "unit_mismatch",
                "evidence unit is not compatible with the claim unit",
                row.evidence_id,
                {"expected": claim.unit, "actual": row.unit},
            )
        return "match", None
    return "mismatch", ClaimIssue(
        "value_mismatch",
        "evidence value contradicts the claim",
        row.evidence_id,
        {"expected": claim.value, "actual": row.value},
    )


def _convert_numeric(value: float, source_unit: str | None, target_unit: str | None) -> tuple[float | None, str]:
    if not target_unit:
        return value, "ok"
    if not source_unit:
        return None, "missing"
    source = _unit_definition(source_unit)
    target = _unit_definition(target_unit)
    if source is None or target is None:
        if _normalize_unit(source_unit) == _normalize_unit(target_unit):
            return value, "ok"
        return None, "incompatible"
    source_dimension, source_factor = source
    target_dimension, target_factor = target
    if source_dimension != target_dimension:
        return None, "incompatible"
    return value * source_factor / target_factor, "ok"


_UNITS: Mapping[str, tuple[str, float]] = {
    "ea": ("count", 1.0),
    "개": ("count", 1.0),
    "pcs": ("count", 1.0),
    "count": ("count", 1.0),
    "mm": ("length", 0.001),
    "cm": ("length", 0.01),
    "m": ("length", 1.0),
    "mm2": ("area", 0.000001),
    "cm2": ("area", 0.0001),
    "m2": ("area", 1.0),
    "mm3": ("volume", 0.000000001),
    "cm3": ("volume", 0.000001),
    "m3": ("volume", 1.0),
    "g": ("mass", 0.001),
    "kg": ("mass", 1.0),
    "t": ("mass", 1000.0),
    "ton": ("mass", 1000.0),
}


def _unit_definition(value: str | None) -> tuple[str, float] | None:
    if value is None:
        return None
    return _UNITS.get(_normalize_unit(value))


def _normalize_unit(value: str) -> str:
    normalized = value.strip().casefold().replace("²", "2").replace("³", "3").replace("^", "")
    normalized = re.sub(r"\s+", "", normalized)
    aliases = {
        "㎡": "m2",
        "㎟": "mm2",
        "㎥": "m3",
        "㎣": "mm3",
        "square_meter": "m2",
        "square_metre": "m2",
        "cubic_meter": "m3",
        "cubic_metre": "m3",
        "linear_meter": "m",
        "linear_metre": "m",
        "metric_ton": "ton",
        "kilogram": "kg",
        "location_count": "ea",
        "sheet": "ea",
        "set": "ea",
        "unit": "ea",
        "each": "ea",
        "종": "ea",
        "건": "ea",
        "장": "ea",
        "개소": "ea",
        "세트": "ea",
        "식": "ea",
    }
    return aliases.get(normalized, normalized)


def _scope_equal(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return _scope_key(expected) == _scope_key(actual)


def _scope_key(scope: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((str(key), _hashable(value)) for key, value in scope.items()))


def _hashable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _hashable(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, str):
        return value.strip().casefold()
    return value


def _scalar_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.strip().casefold() == right.strip().casefold()
    return left == right


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", cleaned):
            result = float(cleaned)
            return result if math.isfinite(result) else None
    return None


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _text(value: Any) -> str | None:
    return str(value).strip() if _present(value) else None


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


__all__ = [
    "Claim",
    "ClaimIssue",
    "ClaimValidation",
    "Evidence",
    "VERDICTS",
    "anchor_completeness",
    "validate_claim",
]
