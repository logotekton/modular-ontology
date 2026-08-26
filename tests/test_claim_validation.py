from __future__ import annotations

from modular_ontology.claim_validation import Claim, Evidence, anchor_completeness, validate_claim


PACKS = {"yeoju-revit": "sha256:canonical"}
SCOPE = {"project_id": "yeoju", "category": "Doors"}
ANCHOR = {
    "anchor_type": "revit_element",
    "confidence": "exact",
    "document_key": "yeoju.rvt",
    "ids": {"unique_id": "revit-unique-1", "element_id": 1234},
}


def _claim(**changes: object) -> Claim:
    values = {
        "claim_id": "A01",
        "value": 94,
        "unit": "ea",
        "scope": SCOPE,
        "snapshot_id": "yeoju-2026-07-13",
        "pack_hashes": PACKS,
    }
    values.update(changes)
    return Claim(**values)  # type: ignore[arg-type]


def _evidence(**changes: object) -> Evidence:
    values = {
        "evidence_id": "ev-1",
        "value": 94,
        "unit": "개",
        "scope": SCOPE,
        "snapshot_id": "yeoju-2026-07-13",
        "pack_hashes": PACKS,
        "anchors": (ANCHOR,),
    }
    values.update(changes)
    return Evidence(**values)  # type: ignore[arg-type]


def test_supported_requires_value_unit_scope_version_hash_and_anchor() -> None:
    result = validate_claim(_claim(), [_evidence()])

    assert result.verdict == "supported"
    assert result.matched_evidence_ids == ("ev-1",)
    assert result.as_dict()["verdict"] == "supported"


def test_contradicted_when_current_same_scope_value_differs() -> None:
    result = validate_claim(_claim(), [_evidence(value=88)])

    assert result.verdict == "contradicted"
    assert any(issue.code == "value_mismatch" for issue in result.issues)


def test_stale_snapshot_or_pack_hash_takes_precedence_over_value() -> None:
    stale_snapshot = validate_claim(_claim(), [_evidence(snapshot_id="yeoju-old", value=88)])
    stale_pack = validate_claim(_claim(), [_evidence(pack_hashes={"yeoju-revit": "sha256:old"})])

    assert stale_snapshot.verdict == "stale"
    assert any(issue.code == "snapshot_mismatch" for issue in stale_snapshot.issues)
    assert stale_pack.verdict == "stale"
    assert any(issue.code == "pack_hash_mismatch" for issue in stale_pack.issues)


def test_mixed_scope_rejects_cross_module_aggregation() -> None:
    claim = _claim(scope={"project_id": "yeoju", "category": "Doors", "module_id": "1-01-A"})
    first = _evidence(
        evidence_id="ev-1",
        scope={"project_id": "yeoju", "category": "Doors", "module_id": "1-01-A"},
    )
    second = _evidence(
        evidence_id="ev-2",
        scope={"project_id": "yeoju", "category": "Doors", "module_id": "2-01-A"},
    )

    result = validate_claim(claim, [first, second])

    assert result.verdict == "mixed_scope"
    assert any(issue.code in {"scope_mismatch", "mixed_evidence_scopes"} for issue in result.issues)


def test_missing_hash_unit_or_complete_anchor_is_insufficient() -> None:
    no_hash = validate_claim(_claim(), [_evidence(pack_hashes={})])
    no_unit = validate_claim(_claim(), [_evidence(unit=None)])
    bad_anchor = validate_claim(
        _claim(),
        [_evidence(anchors=({"anchor_type": "revit_element", "confidence": "partial", "ids": {}},))],
    )

    assert no_hash.verdict == "insufficient"
    assert no_unit.verdict == "insufficient"
    assert bad_anchor.verdict == "insufficient"
    assert any(issue.code == "incomplete_anchor" for issue in bad_anchor.issues)


def test_compatible_units_are_converted_before_numeric_comparison() -> None:
    claim = _claim(value=2.395, unit="m")
    evidence = _evidence(value=2395, unit="mm")

    assert validate_claim(claim, [evidence]).verdict == "supported"


def test_boq_and_korean_count_unit_vocabularies_are_compatible() -> None:
    area_claim = _claim(value=3446.77, unit="㎡")
    area_evidence = _evidence(value=3446.77, unit="square_meter")
    type_claim = _claim(value=13, unit="종")
    type_evidence = _evidence(value=13, unit="count")

    assert validate_claim(area_claim, [area_evidence]).verdict == "supported"
    assert validate_claim(type_claim, [type_evidence]).verdict == "supported"


def test_anchor_completeness_knows_source_specific_identifiers() -> None:
    complete_boq = {
        "anchor_type": "boq_sheet_row",
        "confidence": "exact",
        "ids": {"source_sheet": "건축", "source_row": 80},
    }
    incomplete_dxf = {
        "anchor_type": "dxf_entity",
        "confidence": "partial",
        "ids": {"source_file": "A-711.dxf"},
    }

    assert anchor_completeness(complete_boq) == (True, ())
    complete, missing = anchor_completeness(incomplete_dxf)
    assert not complete
    assert "ids.handle|ids.entity_key|ids.entity_id" in missing


def test_aggregate_query_anchor_binds_the_complete_deterministic_result() -> None:
    anchor = {
        "anchor_type": "aggregate_query",
        "confidence": "exact",
        "snapshot_id": "snapshot-123",
        "query_hash": "q" * 64,
        "result_hash": "r" * 64,
        "contribution_digest": "c" * 64,
        "contribution_count": 0,
    }

    assert anchor_completeness(anchor) == (True, ())


def test_aggregate_anchor_must_match_typed_evidence_envelope() -> None:
    correct_anchor = {
        "anchor_type": "aggregate_query",
        "confidence": "exact",
        "snapshot_id": "yeoju-2026-07-13",
        "query_hash": "q" * 64,
        "result_hash": "r" * 64,
        "contribution_digest": "c" * 64,
        "contribution_count": 94,
    }
    envelope = {
        "anchors": (correct_anchor,),
        "query_hash": "q" * 64,
        "result_hash": "r" * 64,
        "contribution_digest": "c" * 64,
        "contribution_count": 94,
    }
    supported = validate_claim(_claim(), [_evidence(**envelope)])
    bad_anchor = {**correct_anchor, "snapshot_id": "wrong", "result_hash": "x" * 64}
    rejected = validate_claim(_claim(), [_evidence(**{**envelope, "anchors": (bad_anchor,)})])

    assert supported.verdict == "supported"
    assert rejected.verdict == "insufficient"
    assert any(issue.code == "aggregate_anchor_mismatch" for issue in rejected.issues)
