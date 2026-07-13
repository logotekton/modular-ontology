from __future__ import annotations

from dataclasses import replace
import json

import pytest

from modular_ontology import mcp_server


CANONICAL_ID = "YEOJU-CANONICAL-20260710-092149-R1"
INTERNAL_SNAPSHOT_ID = "snapshot-2987d484bb28afb3d8047f98"


def test_mcp_token_is_read_from_full_mounted_request_path(monkeypatch) -> None:
    request = type("Request", (), {"path_params": {}, "query_params": {}, "url": type("Url", (), {"path": "/mcp/mom_test_token"})(), "scope": {}})()
    context = type("Context", (), {"request_context": type("RequestContext", (), {"request": request})()})()
    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: context)

    assert mcp_server._request_mcp_token() == "mom_test_token"


def _a01_plan() -> dict:
    return {
        "project_id": "yeoju",
        "entity": "element",
        "intent": "aggregate",
        "scope": {"category": "\ubb38"},
        "filters": [],
        "group_by": [],
        "metrics": [
            {
                "name": "count_distinct",
                "field": "source_element_id",
                "alias": "object_count",
            },
            {
                "name": "count_distinct",
                "field": "type_name",
                "alias": "type_count",
            },
        ],
    }


def _a01_bundle() -> dict:
    return {
        "project_id": "yeoju",
        "plans": {"a01": _a01_plan()},
        "reducers": [
            {
                "id": "object_count_is_94",
                "op": "equals",
                "left": {"ref": "plans.a01.values.object_count"},
                "right": {"value": 94, "unit": "count"},
            }
        ],
    }


def test_mcp_snapshot_status_verifies_all_173_pinned_packs() -> None:
    listed = json.loads(mcp_server.mo_snapshot_list())
    status = json.loads(mcp_server.mo_snapshot_status(CANONICAL_ID))
    manifest = json.loads(mcp_server.mo_tool_manifest())
    registered_tools = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}

    assert listed["status"] == "ok"
    assert [item["canonical_id"] for item in listed["snapshots"]] == [CANONICAL_ID]
    assert listed["defaults"] == {"yeoju": CANONICAL_ID}
    assert listed["snapshots"][0]["default_for_project"] is True
    assert status["status"] == "ok"
    assert status["snapshot"]["canonical_id"] == CANONICAL_ID
    assert status["snapshot"]["internal_snapshot_id"] == INTERNAL_SNAPSHOT_ID
    assert status["snapshot"]["included_pack_count"] == 173
    assert status["verification"] == {
        "valid": True,
        "checked_pack_count": 173,
        "issues": [],
        "warnings": [],
    }
    assert {
        "mo_snapshot_list",
        "mo_snapshot_status",
        "mo_snapshot_query",
        "mo_snapshot_bundle_query",
    }.issubset(manifest["canonicalTools"])
    assert {
        "mo_snapshot_list",
        "mo_snapshot_status",
        "mo_snapshot_query",
        "mo_snapshot_bundle_query",
    }.issubset(registered_tools)


def test_mcp_snapshot_query_a01_binds_result_to_verified_snapshot_and_hashes() -> None:
    payload = json.loads(mcp_server.mo_snapshot_query(_a01_plan(), CANONICAL_ID))

    assert payload["status"] == "ok"
    assert payload["verification"]["valid"] is True
    assert payload["verification"]["checked_pack_count"] == 173
    assert payload["plan"] == _a01_plan()

    snapshot = payload["snapshot"]
    result = payload["result"]
    evidence = result["evidence"]
    assert snapshot["internal_snapshot_id"] == INTERNAL_SNAPSHOT_ID
    assert result["snapshot_id"] == snapshot["internal_snapshot_id"]
    assert result["values"] == {"object_count": 94, "type_count": 13}
    assert result["matched_records"] == 94
    assert result["complete"] is True
    assert evidence["snapshot_id"] == result["snapshot_id"]
    assert evidence["query_hash"] == result["query_hash"]
    assert evidence["result_hash"] == result["result_hash"]
    assert len(result["query_hash"]) == 64
    assert len(result["result_hash"]) == 64
    assert len(evidence["contribution_digest"]) == 64
    assert evidence["contribution_count"] == 94
    assert result["source_packs"]
    assert all(len(pack["sha256"]) == 64 for pack in result["source_packs"])


def test_mcp_snapshot_status_resolves_promoted_yeoju_default() -> None:
    payload = json.loads(mcp_server.mo_snapshot_status(project_id="yeoju"))

    assert payload["status"] == "ok"
    assert payload["routing"] == {
        "mode": "project_default",
        "canonical_id": CANONICAL_ID,
    }
    assert payload["snapshot"]["default_for_project"] is True
    assert payload["verification"]["checked_pack_count"] == 173


def test_mcp_snapshot_query_automatically_uses_promoted_yeoju_default() -> None:
    payload = json.loads(mcp_server.mo_snapshot_query(_a01_plan()))

    assert payload["status"] == "ok"
    assert payload["routing"] == {
        "mode": "project_default",
        "canonical_id": CANONICAL_ID,
    }
    assert payload["result"]["values"] == {"object_count": 94, "type_count": 13}


def test_mcp_snapshot_bundle_automatically_uses_promoted_yeoju_default() -> None:
    payload = json.loads(mcp_server.mo_snapshot_bundle_query(_a01_bundle()))

    assert payload["status"] == "ok"
    assert payload["routing"] == {
        "mode": "project_default",
        "canonical_id": CANONICAL_ID,
    }
    assert payload["result"]["reducers"]["object_count_is_94"] is True


@pytest.mark.parametrize(
    ("forbidden_key", "forbidden_value"),
    [
        ("pack_id", "revit-yeoju-architecture-model-inventory-01"),
        ("snapshot_id", INTERNAL_SNAPSHOT_ID),
    ],
)
def test_mcp_snapshot_query_rejects_plan_selected_pack_or_snapshot(
    forbidden_key: str,
    forbidden_value: str,
) -> None:
    plan = {**_a01_plan(), forbidden_key: forbidden_value}

    payload = json.loads(mcp_server.mo_snapshot_query(plan, CANONICAL_ID))

    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_query_plan"
    assert "result" not in payload
    assert any(
        issue["code"] == "forbidden_field" and forbidden_key in issue["path"]
        for issue in payload["error"]["issues"]
    )


def test_mcp_snapshot_bundle_query_binds_snapshot_result_and_subplan_pack_hashes() -> None:
    payload = json.loads(mcp_server.mo_snapshot_bundle_query(_a01_bundle(), CANONICAL_ID))

    assert payload["status"] == "ok"
    assert payload["verification"] == {
        "valid": True,
        "checked_pack_count": 173,
        "issues": [],
        "warnings": [],
    }
    assert payload["bundle"] == _a01_bundle()

    snapshot = payload["snapshot"]
    result = payload["result"]
    bindings = payload["bindings"]
    subplan = result["plans"]["a01"]
    subplan_binding = bindings["subplans"]["a01"]

    assert snapshot["internal_snapshot_id"] == INTERNAL_SNAPSHOT_ID
    assert result["snapshot_id"] == INTERNAL_SNAPSHOT_ID
    assert result["complete"] is True
    assert result["reducers"] == {"object_count_is_94": True}
    assert subplan["values"] == {"object_count": 94, "type_count": 13}
    assert bindings["snapshot"]["internal_snapshot_id"] == INTERNAL_SNAPSHOT_ID
    assert bindings["query"]["bundle_hash"] == result["bundle_hash"]
    assert bindings["query"]["query_hash"] == result["query_hash"]
    assert bindings["result"]["result_hash"] == result["result_hash"]
    assert subplan_binding["snapshot_id"] == INTERNAL_SNAPSHOT_ID
    assert subplan_binding["query_hash"] == subplan["query_hash"]
    assert subplan_binding["result_hash"] == subplan["result_hash"]
    assert subplan_binding["source_packs"] == subplan["source_packs"]
    assert subplan_binding["source_packs"]
    assert all(len(pack["sha256"]) == 64 for pack in subplan_binding["source_packs"])


@pytest.mark.parametrize(
    ("forbidden_key", "forbidden_value"),
    [
        ("pack_id", "revit-yeoju-architecture-model-inventory-01"),
        ("snapshot_id", INTERNAL_SNAPSHOT_ID),
    ],
)
def test_mcp_snapshot_bundle_query_rejects_pack_or_snapshot_selector_at_any_depth(
    forbidden_key: str,
    forbidden_value: str,
) -> None:
    bundle = _a01_bundle()
    bundle["reducers"][0]["right"][forbidden_key] = forbidden_value

    payload = json.loads(mcp_server.mo_snapshot_bundle_query(bundle, CANONICAL_ID))

    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_plan_bundle"
    assert "result" not in payload
    assert "bindings" not in payload
    assert any(
        issue["code"] == "forbidden_field" and forbidden_key in issue["path"]
        for issue in payload["error"]["issues"]
    )


def test_mcp_snapshot_bundle_query_requires_exact_top_level_field_set() -> None:
    bundle = _a01_bundle()
    bundle.pop("reducers")

    payload = json.loads(mcp_server.mo_snapshot_bundle_query(bundle, CANONICAL_ID))

    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_plan_bundle"
    assert "result" not in payload
    assert any(issue["code"] == "field_set_mismatch" for issue in payload["error"]["issues"])


def test_mcp_snapshot_bundle_query_fails_closed_on_non_sha256_query_hash(monkeypatch) -> None:
    execute_query_bundle = mcp_server.execute_query_bundle

    def execute_with_short_hash(bundle: dict, snapshot: object):
        result = execute_query_bundle(bundle, snapshot)
        evidence = {
            **result.evidence,
            "bundle_hash": "same-but-not-sha256",
            "query_hash": "same-but-not-sha256",
        }
        return replace(
            result,
            bundle_hash="same-but-not-sha256",
            query_hash="same-but-not-sha256",
            evidence=evidence,
        )

    monkeypatch.setattr(mcp_server, "execute_query_bundle", execute_with_short_hash)

    payload = json.loads(mcp_server.mo_snapshot_bundle_query(_a01_bundle(), CANONICAL_ID))

    assert payload["status"] == "error"
    assert payload["error"]["code"] == "query_binding_failed"
    assert "result" not in payload
    assert "bindings" not in payload
