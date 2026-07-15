from __future__ import annotations

import argparse
import json
import logging
import re
import time
import zipfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from .auth import get_company_project_access
from .config import ROOT, env
from .mcp_tokens import get_mcp_token_record
from .pack_index import (
    build_graph as read_graph,
    get_assembly_mark as read_assembly_mark,
    get_fasteners as read_fasteners,
    get_module as read_module,
    get_node_context as read_node_context,
    get_section_weight_index as read_section_weight_index,
    find_pack,
    list_assembly_marks as read_assembly_marks,
    list_edges as read_edges,
    list_modules as read_modules,
    list_nodes as read_nodes,
    list_pack_documents as read_pack_documents,
    list_packs as read_packs,
    list_projects as read_projects,
    list_sources as read_sources,
    read_pack_document as read_document,
    query_terms,
    search_pack as search_pack_evidence,
    search_packs as search_pack_catalog,
    search_nodes as search_graph_nodes,
    score_terms,
)
from .qa import answer_pack_question
from . import query_primitives
from .project_query import ProjectQueryError, execute_structured_project_query
from .query_contract import (
    ContractIssue,
    PLAN_BUNDLE_FIELDS,
    PLAN_FIELDS,
    PlanBundleContractError,
    QueryContractError,
    validate_plan_bundle,
    validate_query_plan,
)
from .query_engine import QueryBundleResult, QueryExecutionError, execute_query_bundle
from .snapshot_store import (
    ProjectSnapshot,
    SnapshotManifestError,
    SnapshotVerification,
    SnapshotVerificationError,
    load_snapshot,
    verify_snapshot,
)
from .source_anchor import anchors_from_chunk, coverage as source_anchor_coverage
from .store import (
    bm25_index_status,
    connect as connect_index_db,
    init_db as init_index_db,
    pack_ids_with_documents,
    search_documents as search_indexed_documents,
    search_documents_bm25_multi,
    search_documents_multi,
)


try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.server.transport_security import TransportSecuritySettings
except Exception as exc:  # pragma: no cover - import-time operator hint
    raise SystemExit(
        "The optional MCP runtime is not installed. Run `pip install -e .` from the project root first."
    ) from exc


DEFAULT_HOST = str(env("MODULAR_ONTOLOGY_MCP_HOST", "127.0.0.1"))
DEFAULT_PORT = int(str(env("MODULAR_ONTOLOGY_MCP_PORT", "8011")))
DEFAULT_PATH = str(env("MODULAR_ONTOLOGY_MCP_PATH", "/mcp/{mcp_token}"))
DEFAULT_ALLOWED_HOSTS = str(env(
    "MODULAR_ONTOLOGY_MCP_ALLOWED_HOSTS",
    "127.0.0.1:*,localhost:*,[::1]:*",
))
DEFAULT_ALLOWED_ORIGINS = str(env(
    "MODULAR_ONTOLOGY_MCP_ALLOWED_ORIGINS",
    "http://127.0.0.1:*,http://localhost:*,http://[::1]:*",
))

_REMOVED_LEGACY_TOOL_NAMES = (
    "copycrab_status",
    "list_projects",
    "list_packs",
    "search_packs",
    "list_sources",
    "list_pack_documents",
    "read_pack_document",
    "read_chunk_by_id",
    "search_pack",
    "search_documents",
    "query_quantity_facts",
    "search_drawing_text",
    "extract_room_area_tags",
    "extract_dimensions_from_view",
    "calculate_area_from_dimensions",
    "extract_schedule_table",
    "dxf_entity_summary",
    "dxf_entity_search",
    "dxf_text_search",
    "dxf_dimension_search",
    "dxf_layer_summary",
    "dxf_bbox_query",
    "query",
    "ask_pack_question",
    "get_graph",
    "list_nodes",
    "search_nodes",
    "get_node_context",
    "list_edges",
    "list_modules",
    "get_module",
    "list_assembly_marks",
    "get_assembly_mark",
    "get_fasteners",
    "get_section_weight_index",
)

TOOL_MANIFEST = [
    {"name": "mo_server_status", "domain": "server", "action": "status"},
    {"name": "mo_tool_manifest", "domain": "tool", "action": "manifest"},
    {"name": "mo_snapshot_list", "domain": "snapshot", "action": "list"},
    {"name": "mo_snapshot_status", "domain": "snapshot", "action": "status"},
    {"name": "mo_snapshot_query", "domain": "snapshot", "action": "query"},
    {"name": "mo_snapshot_bundle_query", "domain": "snapshot_bundle", "action": "query"},
    {"name": "mo_ontology_manifest", "domain": "ontology", "action": "manifest"},
    {"name": "mo_project_list", "domain": "project", "action": "list"},
    {"name": "mo_project_overview", "domain": "project", "action": "overview"},
    {"name": "mo_project_pack_list", "domain": "project_pack", "action": "list"},
    {"name": "mo_project_search", "domain": "project", "action": "search"},
    {"name": "mo_project_ask", "domain": "project", "action": "ask"},
    {"name": "mo_pack_list", "domain": "pack", "action": "list"},
    {"name": "mo_pack_overview", "domain": "pack", "action": "overview"},
    {"name": "mo_pack_search", "domain": "pack", "action": "search"},
    {"name": "mo_pack_schema", "domain": "pack", "action": "schema"},
    {"name": "mo_source_list", "domain": "source", "action": "list"},
    {"name": "mo_document_list", "domain": "document", "action": "list"},
    {"name": "mo_document_read", "domain": "document", "action": "read"},
    {"name": "mo_chunk_read", "domain": "chunk", "action": "read"},
    {"name": "mo_evidence_search", "domain": "evidence", "action": "search"},
    {"name": "mo_evidence_trace", "domain": "evidence", "action": "trace"},
    {"name": "mo_anchor_resolve", "domain": "anchor", "action": "resolve"},
    {"name": "mo_anchor_coverage", "domain": "anchor", "action": "coverage"},
    {"name": "mo_drawing_text_search", "domain": "drawing_text", "action": "search"},
    {"name": "mo_room_area_tag_extract", "domain": "room_area_tag", "action": "extract"},
    {"name": "mo_dimensions_extract", "domain": "dimension", "action": "extract"},
    {
        "name": "mo_area_from_dimensions_calculate",
        "domain": "measurement",
        "action": "calculate_area_from_dimensions",
    },
    {"name": "mo_schedule_table_extract", "domain": "schedule_table", "action": "extract"},
    {"name": "mo_dxf_entity_summary", "domain": "dxf_entity", "action": "summary"},
    {"name": "mo_dxf_entity_search", "domain": "dxf_entity", "action": "search"},
    {"name": "mo_dxf_text_search", "domain": "dxf_text", "action": "search"},
    {"name": "mo_dxf_dimension_search", "domain": "dxf_dimension", "action": "search"},
    {"name": "mo_dxf_layer_summary", "domain": "dxf_layer", "action": "summary"},
    {"name": "mo_dxf_bbox_query", "domain": "dxf_entity", "action": "bbox_query"},
    {"name": "mo_question_answer", "domain": "question", "action": "answer"},
    {"name": "mo_graph_get", "domain": "graph", "action": "get"},
    {"name": "mo_node_type_list", "domain": "node_type", "action": "list"},
    {"name": "mo_node_list", "domain": "node", "action": "list"},
    {"name": "mo_node_search", "domain": "node", "action": "search"},
    {"name": "mo_filtered_search_nodes", "domain": "node", "action": "filtered_search"},
    {"name": "mo_distinct_property_values", "domain": "node", "action": "distinct_property_values"},
    {"name": "mo_aggregate_nodes", "domain": "node", "action": "aggregate"},
    {"name": "mo_schema_profile", "domain": "schema", "action": "profile"},
    {"name": "mo_node_context", "domain": "node", "action": "context"},
    {"name": "mo_edge_list", "domain": "edge", "action": "list"},
    {"name": "mo_relation_type_list", "domain": "relation_type", "action": "list"},
    {"name": "mo_module_list", "domain": "module", "action": "list"},
    {"name": "mo_list_project_modules", "domain": "module", "action": "project_list"},
    {"name": "mo_list_module_elements", "domain": "module", "action": "element_list"},
    {"name": "mo_quantity_facts_query", "domain": "quantity_fact", "action": "query"},
    {"name": "mo_query_quantity_evidence", "domain": "quantity", "action": "evidence_query"},
    {"name": "mo_aggregate_quantity_by_module", "domain": "quantity", "action": "aggregate_by_module"},
    {"name": "mo_join_by_property", "domain": "node", "action": "join_by_property"},
    {"name": "mo_module_get", "domain": "module", "action": "get"},
    {"name": "mo_assembly_list", "domain": "assembly", "action": "list"},
    {"name": "mo_assembly_get", "domain": "assembly", "action": "get"},
    {"name": "mo_fastener_summary", "domain": "fastener", "action": "summary"},
    {
        "name": "mo_section_weight_index",
        "domain": "section",
        "action": "weight_index",
    },
]

TOOL_NAMES = [tool["name"] for tool in TOOL_MANIFEST]
CORE_TOOL_NAMES = [
    "mo_server_status",
    "mo_tool_manifest",
    "mo_project_list",
    "mo_project_overview",
    "mo_project_ask",
    "mo_pack_overview",
    "mo_evidence_search",
    "mo_document_read",
    "mo_graph_get",
    "mo_drawing_text_search",
    "mo_quantity_facts_query",
    "mo_module_get",
]
TOOL_PROFILE_NAMES = ("core", "expert")
_CLOUD_CHUNK_ROW_CACHE: dict[str, dict[str, object]] = {}
_DRAWING_ENTITY_SEARCH_CACHE: dict[str, dict[str, object]] = {}
_PROJECT_FALLBACK_MAX_PACKS = 10
_PROJECT_FALLBACK_BUDGET_SECONDS = 2.0
_PROJECT_SEARCH_MAX_MATCHES = 40
_LOGGER = logging.getLogger(__name__)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _split_many(values: Sequence[str]) -> list[str]:
    return [part for value in values for part in _split_csv(value)]


def _normalize_tool_profile(value: str | None) -> str:
    profile = str(value or "core").strip().casefold()
    aliases = {"canonical": "expert"}
    profile = aliases.get(profile, profile)
    if profile not in TOOL_PROFILE_NAMES:
        choices = ", ".join(TOOL_PROFILE_NAMES)
        raise ValueError(f"Unknown MCP tool profile {value!r}; expected one of: {choices}")
    return profile


def _tool_names_for_profile(profile: str) -> list[str]:
    normalized = _normalize_tool_profile(profile)
    if normalized == "core":
        return list(CORE_TOOL_NAMES)
    return list(TOOL_NAMES)


class ProfiledFastMCP(FastMCP):
    """FastMCP server that exposes only the tools selected by a public profile."""

    def __init__(self, *args: object, tool_profile: str = "core", **kwargs: object) -> None:
        self.tool_profile = _normalize_tool_profile(tool_profile)
        super().__init__(*args, **kwargs)

    def set_tool_profile(self, profile: str) -> None:
        self.tool_profile = _normalize_tool_profile(profile)

    def visible_tool_names(self) -> list[str]:
        allowed = set(_tool_names_for_profile(self.tool_profile))
        return [tool.name for tool in self._tool_manager.list_tools() if tool.name in allowed]

    async def list_tools(self):
        allowed = set(_tool_names_for_profile(self.tool_profile))
        return [tool for tool in await super().list_tools() if tool.name in allowed]

    async def call_tool(self, name: str, arguments: dict):
        if name not in set(_tool_names_for_profile(self.tool_profile)):
            raise ToolError(
                f"Tool {name!r} is not exposed by the {self.tool_profile!r} profile. "
                "Use MODULAR_ONTOLOGY_MCP_TOOL_PROFILE=expert for canonical specialist tools."
            )
        return await super().call_tool(name, arguments)


mcp = ProfiledFastMCP(
    "Modular Ontology",
    tool_profile=str(env("MODULAR_ONTOLOGY_MCP_TOOL_PROFILE", "core")),
    instructions=(
        "Use Modular Ontology tools to inspect BIM ontology packs, search evidence, "
        "sample graph nodes/edges, and answer natural language questions about Revit IFC "
        "and Advance Steel model data. For project questions, call mo_project_ask first "
        "and synthesize the final answer directly from its evidence."
    ),
    host=DEFAULT_HOST,
    port=DEFAULT_PORT,
    streamable_http_path=DEFAULT_PATH,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_split_csv(DEFAULT_ALLOWED_HOSTS),
        allowed_origins=_split_csv(DEFAULT_ALLOWED_ORIGINS),
    ),
)


def _json(payload: object, *, pretty: bool = True) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)


def _env_enabled(name: str, *, default: bool) -> bool:
    fallback = "1" if default else "0"
    return str(env(name, fallback) or fallback).strip().casefold() not in {"0", "false", "no", "off"}


def _mcp_company_env() -> str:
    return str(env("MODULAR_ONTOLOGY_MCP_COMPANY", "")).strip()


def _request_mcp_token() -> str:
    try:
        request = mcp.get_context().request_context.request
    except Exception:
        return ""
    if request is None:
        return ""
    token = ""
    path_params = getattr(request, "path_params", None)
    if isinstance(path_params, dict):
        token = str(path_params.get("mcp_token") or "").strip()
    if not token:
        query_params = getattr(request, "query_params", None)
        if query_params is not None:
            token = str(query_params.get("token", "")).strip()
    if not token:
        path = getattr(getattr(request, "url", None), "path", "") or getattr(request, "scope", {}).get("path", "")
        prefix = str(mcp.settings.streamable_http_path).split("{", 1)[0].rstrip("/") + "/"
        if path.startswith(prefix):
            token = path[len(prefix) :].split("/", 1)[0].strip()
    return token


def _mcp_scope() -> dict:
    token = _request_mcp_token()
    if token:
        record = get_mcp_token_record(token)
        if not record and _refresh_mcp_tokens_from_remote():
            record = get_mcp_token_record(token)
        if not record:
            return {"authorized": False, "token": token, "company": "", "userEmail": ""}
        return {"authorized": True, **record}
    return {"authorized": True, "company": _mcp_company_env(), "userEmail": "", "token": ""}


def _refresh_mcp_tokens_from_remote() -> bool:
    try:
        from .google_drive_sync import google_drive_sync_enabled, sync_google_drive_mcp_tokens_file

        if not google_drive_sync_enabled():
            return False
        result = sync_google_drive_mcp_tokens_file()
        return result.get("status") in {"synced", "cached"}
    except Exception:
        return False


def _mcp_authorized() -> bool:
    return bool(_mcp_scope().get("authorized"))


def _mcp_company() -> str:
    scope = _mcp_scope()
    if not scope.get("authorized"):
        return ""
    if str(scope.get("userEmail") or "").strip().lower().endswith("@kumkangkind.com"):
        return "Kumkang Kind"
    return str(scope.get("company") or "").strip()


def _is_internal_company(company: str) -> bool:
    lowered = company.casefold()
    if "금강" in company:
        return True
    return not company or "kumkang" in lowered or "금강" in company


def _visible_projects() -> list[dict]:
    if not _mcp_authorized():
        return []
    company = _mcp_company()
    projects = read_projects()
    if _is_internal_company(company):
        return projects
    allowed_project_ids = set(get_company_project_access(company))
    return [project for project in projects if project.get("id") in allowed_project_ids]


def _visible_pack_ids() -> set[str]:
    return {
        pack_id
        for project in _visible_projects()
        for pack_id in project.get("packIds", [])
        if isinstance(pack_id, str)
    }


def _pack_is_visible(pack_id: str) -> bool:
    if not _mcp_authorized():
        return False
    company = _mcp_company()
    return _is_internal_company(company) or pack_id in _visible_pack_ids()


def _forbidden_pack(pack_id: str) -> str:
    if not _mcp_authorized():
        return _json({"error": "unauthorized", "detail": "Invalid MCP user URL token."}, pretty=False)
    return _json(
        {
            "error": "forbidden",
            "detail": f"Pack is not available for MCP company scope: {pack_id}",
            "company": _mcp_company(),
        },
        pretty=False,
    )


def _filter_pack_list(packs: list[dict]) -> list[dict]:
    if not _mcp_authorized():
        return []
    company = _mcp_company()
    if _is_internal_company(company):
        return packs
    visible_pack_ids = _visible_pack_ids()
    return [pack for pack in packs if pack.get("id") in visible_pack_ids]


def _filter_sources(payload: dict) -> dict:
    if not _mcp_authorized():
        return {**payload, "count": 0, "sources": []}
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return payload
    visible_pack_ids = _visible_pack_ids()
    if _is_internal_company(_mcp_company()):
        return payload
    filtered_sources = [source for source in sources if source.get("pack_id") in visible_pack_ids]
    return {**payload, "count": len(filtered_sources), "sources": filtered_sources}


class CanonicalSnapshotToolError(RuntimeError):
    """Fail-closed error raised before a canonical snapshot result is exposed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and _SHA256_HEX_PATTERN.fullmatch(value) is not None


def _canonical_snapshot_root() -> Path:
    configured = env("MODULAR_ONTOLOGY_SNAPSHOT_DIR")
    return Path(configured or ROOT / "snapshots").expanduser().resolve()


def _canonical_snapshot_manifests() -> list[tuple[str, Path, dict]]:
    root = _canonical_snapshot_root()
    if not root.is_dir():
        raise CanonicalSnapshotToolError(
            "snapshot_store_unavailable",
            "Canonical snapshot directory is unavailable.",
        )

    manifests: list[tuple[str, Path, dict]] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*.json"), key=lambda item: item.name.casefold()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CanonicalSnapshotToolError(
                "snapshot_manifest_invalid",
                f"Cannot read canonical snapshot manifest {path.name}: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise CanonicalSnapshotToolError(
                "snapshot_manifest_invalid",
                f"Canonical snapshot manifest {path.name} must be a JSON object.",
            )
        canonical_id = str(payload.get("canonical_id") or "").strip()
        if not canonical_id:
            raise CanonicalSnapshotToolError(
                "snapshot_manifest_invalid",
                f"Canonical snapshot manifest {path.name} has no canonical_id.",
            )
        if canonical_id in seen:
            raise CanonicalSnapshotToolError(
                "snapshot_id_ambiguous",
                f"Canonical snapshot id is declared more than once: {canonical_id}",
            )
        seen.add(canonical_id)
        manifests.append((canonical_id, path, payload))
    return manifests


def _canonical_snapshot_manifest(canonical_id: str) -> tuple[Path, dict]:
    requested = canonical_id.strip()
    if not requested:
        raise CanonicalSnapshotToolError("snapshot_id_required", "canonical_id is required.")
    for current_id, path, payload in _canonical_snapshot_manifests():
        if current_id == requested:
            return path, payload
    raise CanonicalSnapshotToolError(
        "snapshot_not_found",
        f"Canonical snapshot is not available: {requested}",
    )


def _load_canonical_snapshot(canonical_id: str) -> tuple[dict, ProjectSnapshot]:
    path, manifest = _canonical_snapshot_manifest(canonical_id)
    snapshot = load_snapshot(path)
    source_composite = str(manifest.get("source_composite_sha256") or "").strip().lower()
    if not source_composite or snapshot.source_signature != f"sha256:{source_composite}":
        raise CanonicalSnapshotToolError(
            "snapshot_source_signature_mismatch",
            "Canonical snapshot source signature is not bound to its composite SHA-256.",
        )
    return manifest, snapshot


def _snapshot_is_visible(snapshot: ProjectSnapshot) -> bool:
    if not _mcp_authorized():
        return False
    if _is_internal_company(_mcp_company()):
        return True
    required_pack_ids = {pack.pack_id for pack in snapshot.packs if pack.included}
    return required_pack_ids.issubset(_visible_pack_ids())


def _require_snapshot_access(snapshot: ProjectSnapshot) -> None:
    if not _mcp_authorized():
        raise CanonicalSnapshotToolError("unauthorized", "Invalid MCP user URL token.")
    if not _snapshot_is_visible(snapshot):
        raise CanonicalSnapshotToolError(
            "forbidden",
            "Canonical snapshot is outside the current MCP company scope.",
        )


def _snapshot_summary(manifest: dict, snapshot: ProjectSnapshot) -> dict:
    included = [pack for pack in snapshot.packs if pack.included]
    return {
        "canonical_id": str(manifest["canonical_id"]),
        "project_id": snapshot.project_id,
        "internal_snapshot_id": snapshot.snapshot_id,
        "internal_snapshot_hash": snapshot.snapshot_hash,
        "source_signature": snapshot.source_signature,
        "source_composite_sha256": str(manifest["source_composite_sha256"]),
        "pack_count": len(snapshot.packs),
        "included_pack_count": len(included),
    }


def _snapshot_verification_payload(verification: SnapshotVerification) -> dict:
    return {
        "valid": verification.valid,
        "checked_pack_count": len(verification.checked_pack_ids),
        "issues": [
            {"code": issue.code, "message": issue.message, "pack_id": issue.pack_id}
            for issue in verification.issues
        ],
        "warnings": [
            {"code": issue.code, "message": issue.message, "pack_id": issue.pack_id}
            for issue in verification.warnings
        ],
    }


def _verify_canonical_snapshot(snapshot: ProjectSnapshot) -> SnapshotVerification:
    verification = verify_snapshot(snapshot)
    verification.require_valid()
    included_count = sum(pack.included for pack in snapshot.packs)
    if len(verification.checked_pack_ids) != included_count:
        raise CanonicalSnapshotToolError(
            "snapshot_verification_incomplete",
            "Snapshot verification did not check every included pack.",
        )
    return verification


def _exact_snapshot_query_plan(plan: dict) -> dict:
    validated = validate_query_plan(plan)
    actual_fields = {str(field) for field in plan}
    if actual_fields != PLAN_FIELDS:
        missing = sorted(PLAN_FIELDS - actual_fields)
        extra = sorted(actual_fields - PLAN_FIELDS)
        raise QueryContractError(
            [
                ContractIssue(
                    "field_set_mismatch",
                    "$",
                    f"plan must contain exactly the seven fields; missing={missing}, extra={extra}",
                )
            ]
        )
    return validated.as_dict()


def _exact_snapshot_query_bundle(bundle: dict) -> dict:
    validated = validate_plan_bundle(bundle)
    actual_fields = {str(field) for field in bundle}
    if actual_fields != PLAN_BUNDLE_FIELDS:
        missing = sorted(PLAN_BUNDLE_FIELDS - actual_fields)
        extra = sorted(actual_fields - PLAN_BUNDLE_FIELDS)
        raise PlanBundleContractError(
            [
                ContractIssue(
                    "field_set_mismatch",
                    "$",
                    f"bundle must contain exactly project_id, plans, and reducers; missing={missing}, extra={extra}",
                )
            ]
        )
    return validated.as_dict()


def _snapshot_bundle_bindings(
    manifest: dict,
    snapshot: ProjectSnapshot,
    result: QueryBundleResult,
) -> dict:
    """Validate and expose immutable snapshot/query/result/source-pack bindings."""

    if not result.complete or result.snapshot_id != snapshot.snapshot_id:
        raise CanonicalSnapshotToolError(
            "snapshot_binding_failed",
            "Bundle result is not completely bound to the requested canonical snapshot.",
        )
    if (
        result.bundle_hash != result.query_hash
        or not _is_sha256_hex(result.bundle_hash)
        or not _is_sha256_hex(result.query_hash)
    ):
        raise CanonicalSnapshotToolError(
            "query_binding_failed",
            "Bundle hash and query hash must match and be 64-digit SHA-256 hex values.",
        )

    evidence = result.evidence
    if (
        evidence.get("snapshot_id") != snapshot.snapshot_id
        or evidence.get("bundle_hash") != result.bundle_hash
        or evidence.get("query_hash") != result.query_hash
        or evidence.get("result_hash") != result.result_hash
        or not _is_sha256_hex(result.result_hash)
    ):
        raise CanonicalSnapshotToolError(
            "result_binding_failed",
            "Bundle evidence hashes do not match the completed result.",
        )

    expected_packs = {
        pack.pack_id: pack.sha256.casefold()
        for pack in snapshot.packs
        if pack.included
    }
    raw_plan_bindings = evidence.get("plan_bindings")
    if not isinstance(raw_plan_bindings, dict):
        raise CanonicalSnapshotToolError(
            "subplan_binding_failed",
            "Bundle evidence has no subplan bindings.",
        )

    subplans: dict[str, dict] = {}
    if set(raw_plan_bindings) != set(result.plan_results):
        raise CanonicalSnapshotToolError(
            "subplan_binding_failed",
            "Bundle evidence does not cover exactly the completed subplans.",
        )
    for plan_id, plan_result in sorted(result.plan_results.items()):
        raw_binding = raw_plan_bindings.get(plan_id)
        if not isinstance(raw_binding, dict):
            raise CanonicalSnapshotToolError(
                "subplan_binding_failed",
                f"Subplan {plan_id!r} has no hash binding.",
            )
        source_packs = [dict(pack) for pack in plan_result.source_packs]
        if (
            not plan_result.complete
            or plan_result.snapshot_id != snapshot.snapshot_id
            or plan_result.evidence.get("snapshot_id") != snapshot.snapshot_id
            or plan_result.evidence.get("query_hash") != plan_result.query_hash
            or plan_result.evidence.get("result_hash") != plan_result.result_hash
            or raw_binding.get("query_hash") != plan_result.query_hash
            or raw_binding.get("result_hash") != plan_result.result_hash
            or raw_binding.get("source_packs") != source_packs
            or not _is_sha256_hex(plan_result.query_hash)
            or not _is_sha256_hex(plan_result.result_hash)
        ):
            raise CanonicalSnapshotToolError(
                "subplan_binding_failed",
                f"Subplan {plan_id!r} hashes are not internally consistent.",
            )
        if not source_packs:
            raise CanonicalSnapshotToolError(
                "subplan_binding_failed",
                f"Subplan {plan_id!r} has no source-pack SHA binding.",
            )
        for source_pack in source_packs:
            pack_id = str(source_pack.get("pack_id") or "")
            source_sha = str(source_pack.get("sha256") or "").casefold()
            if not pack_id or expected_packs.get(pack_id) != source_sha:
                raise CanonicalSnapshotToolError(
                    "subplan_binding_failed",
                    f"Subplan {plan_id!r} source pack {pack_id!r} is outside "
                    "the canonical snapshot or has a stale SHA-256.",
                )
        subplans[plan_id] = {
            "snapshot_id": plan_result.snapshot_id,
            "query_hash": plan_result.query_hash,
            "result_hash": plan_result.result_hash,
            "source_packs": source_packs,
        }

    return {
        "snapshot": {
            "canonical_id": str(manifest["canonical_id"]),
            "internal_snapshot_id": snapshot.snapshot_id,
            "internal_snapshot_hash": snapshot.snapshot_hash,
            "source_signature": snapshot.source_signature,
            "source_composite_sha256": str(manifest["source_composite_sha256"]),
        },
        "query": {
            "bundle_hash": result.bundle_hash,
            "query_hash": result.query_hash,
        },
        "result": {"result_hash": result.result_hash},
        "subplans": subplans,
    }


def _snapshot_error_payload(exc: Exception, *, canonical_id: str | None = None) -> dict:
    if isinstance(exc, PlanBundleContractError):
        error = {"code": "invalid_plan_bundle", "detail": str(exc), "issues": exc.as_dict()["issues"]}
    elif isinstance(exc, QueryContractError):
        error = {"code": "invalid_query_plan", "detail": str(exc), "issues": exc.as_dict()["issues"]}
    elif isinstance(exc, SnapshotVerificationError):
        error = {
            "code": "snapshot_verification_failed",
            "detail": str(exc),
            "verification": _snapshot_verification_payload(exc.result),
        }
    elif isinstance(exc, CanonicalSnapshotToolError):
        error = {"code": exc.code, "detail": exc.detail}
    elif isinstance(exc, SnapshotManifestError):
        error = {"code": "snapshot_manifest_invalid", "detail": str(exc)}
    elif isinstance(exc, ProjectQueryError):
        error = {"code": "snapshot_query_rejected", "detail": str(exc)}
    elif isinstance(exc, QueryExecutionError):
        error = {"code": "snapshot_bundle_query_rejected", "detail": str(exc)}
    elif isinstance(exc, OSError):
        error = {"code": "snapshot_io_error", "detail": str(exc)}
    else:
        error = {"code": "snapshot_operation_failed", "detail": str(exc)}
    payload: dict[str, object] = {"status": "error", "error": error}
    if canonical_id is not None:
        payload["canonical_id"] = canonical_id
    return payload


def _tool_manifest_payload() -> dict:
    visible_tool_names = mcp.visible_tool_names()
    visible_tool_name_set = set(visible_tool_names)
    return {
        "naming": {
            "prefix": "mo",
            "pattern": "mo_<domain>_<action>",
            "meaning": "mo is short for Modular Ontology.",
            "canonicalOnlyForNewClients": True,
        },
        "exposure": {
            "profile": mcp.tool_profile,
            "visibleCount": len(visible_tool_names),
            "visibleTools": visible_tool_names,
            "availableProfiles": {
                "core": len(CORE_TOOL_NAMES),
                "expert": len(TOOL_NAMES),
            },
            "configuration": "MODULAR_ONTOLOGY_MCP_TOOL_PROFILE=core|expert",
        },
        "workflow": {
            "primaryUnit": "project",
            "guidance": "For a project question, call mo_project_ask first and answer directly from its evidence. Use pack tools only when the user explicitly wants to inspect a specific pack.",
            "recommendedStart": ["mo_project_ask", "mo_project_list", "mo_project_overview"],
            "retrieval": {
                "defaultMode": "bm25",
                "fallback": "sqlite-lexical",
                "vector": "disabled",
                "answerMode": "client_synthesis",
            },
            "canonicalSnapshotTools": [
                "mo_snapshot_list",
                "mo_snapshot_status",
                "mo_snapshot_query",
                "mo_snapshot_bundle_query",
            ],
            "packLevelTools": ["mo_pack_overview", "mo_pack_schema", "mo_document_list", "mo_document_read"],
        },
        "tools": TOOL_MANIFEST,
        "canonicalTools": TOOL_NAMES,
        "visibleCanonicalTools": [name for name in TOOL_NAMES if name in visible_tool_name_set],
    }


def _sorted_counter(counter: Counter[str], label: str) -> list[dict[str, int | str]]:
    return [
        {label: key, "count": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _field_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _add_field(counter: Counter[str], types: dict[str, set[str]], field: str, value: object) -> None:
    counter[field] += 1
    types.setdefault(field, set()).add(_field_type(value))


def _queryable_fields_payload(nodes: list[dict], edges: list[dict]) -> dict:
    node_counter: Counter[str] = Counter()
    node_types: dict[str, set[str]] = {}
    edge_counter: Counter[str] = Counter()
    edge_types: dict[str, set[str]] = {}

    for node in nodes:
        for field in ("id", "label", "type"):
            _add_field(node_counter, node_types, field, node.get(field))
        properties = node.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                _add_field(node_counter, node_types, f"properties.{key}", value)

    for edge in edges:
        for field in ("id", "source", "target", "relation"):
            _add_field(edge_counter, edge_types, field, edge.get(field))
        properties = edge.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                _add_field(edge_counter, edge_types, f"properties.{key}", value)

    return {
        "nodeFields": [
            {"field": field, "count": count, "types": sorted(node_types.get(field, []))}
            for field, count in sorted(node_counter.items(), key=lambda item: (-item[1], item[0]))
        ],
        "edgeFields": [
            {"field": field, "count": count, "types": sorted(edge_types.get(field, []))}
            for field, count in sorted(edge_counter.items(), key=lambda item: (-item[1], item[0]))
        ],
        "documentFields": [
            {"field": "path", "types": ["string"]},
            {"field": "prefix", "types": ["string"]},
            {"field": "suffix", "types": ["string"]},
            {"field": "content", "types": ["string"]},
        ],
        "searchFields": {
            "evidence": ["path", "title", "content"],
            "nodes": ["id", "label", "type", "properties.*"],
            "edges": ["id", "source", "target", "relation", "properties.*"],
        },
    }


def _pack_schema_payload(pack_id: str, node_limit: int = 1000, edge_limit: int = 2000) -> dict:
    graph = read_graph(pack_id, max_nodes=max(0, node_limit), max_edges=max(0, edge_limit))
    documents_payload = read_pack_documents(pack_id=pack_id, prefix="", suffix="", limit=5000)
    documents = documents_payload.get("documents", [])
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    node_types = Counter(str(node.get("type") or "unknown") for node in nodes)
    relation_types = Counter(str(edge.get("relation") or "unknown") for edge in edges)
    document_prefixes = Counter(
        str(path).rsplit("/", 1)[0] + "/" if "/" in str(path) else ""
        for path in documents
    )

    return {
        "packId": graph.get("pack", {}).get("id", pack_id),
        "title": graph.get("pack", {}).get("title", ""),
        "naming": {"pattern": "mo_<domain>_<action>"},
        "limits": {"nodeLimit": max(0, node_limit), "edgeLimit": max(0, edge_limit)},
        "counts": graph.get("pack", {}).get("counts", {}),
        "sampled": graph.get("stats", {}),
        "nodeTypes": _sorted_counter(node_types, "type"),
        "relationTypes": _sorted_counter(relation_types, "relation"),
        "documentPrefixes": _sorted_counter(document_prefixes, "prefix"),
        "queryableFields": _queryable_fields_payload(nodes, edges),
        "sampleNodeIds": [str(node.get("id", "")) for node in nodes[:12]],
    }


def _ontology_manifest_payload(pack_id: str = "", node_limit: int = 1000, edge_limit: int = 2000) -> dict:
    packs = _filter_pack_list(read_packs())
    pack_ids = [pack_id] if pack_id else [str(pack.get("id")) for pack in packs if pack.get("id")]
    schemas = []
    for current_pack_id in pack_ids:
        if not _pack_is_visible(current_pack_id):
            continue
        schemas.append(_pack_schema_payload(current_pack_id, node_limit=node_limit, edge_limit=edge_limit))
    return {
        "naming": _tool_manifest_payload()["naming"],
        "packId": pack_id or None,
        "packCount": len(schemas),
        "tools": {
            "schema": "mo_pack_schema",
            "nodeTypes": "mo_node_type_list",
            "relationTypes": "mo_relation_type_list",
            "documents": "mo_document_list",
            "evidence": "mo_evidence_search",
            "question": "mo_question_answer",
        },
        "schemas": schemas,
    }


def _visible_pack_by_id() -> dict[str, dict]:
    return {str(pack.get("id")): pack for pack in _filter_pack_list(read_packs())}


def _visible_project_by_id() -> dict[str, dict]:
    return {str(project.get("id")): project for project in _visible_projects()}


def _not_found_payload(kind: str, identifier: str) -> str:
    return _json(
        {
            "error": "not_found",
            "kind": kind,
            "id": identifier,
            "detail": f"{kind} is not available in the current MCP scope: {identifier}",
        },
        pretty=False,
    )


_MISSING = query_primitives.MISSING
_DEFAULT_NODE_FIELDS = query_primitives.DEFAULT_NODE_FIELDS
_MODULE_PATTERN = re.compile(r"^[0-9]+-[0-9]{2}-(A|ST|L|G|O)$")


def _normalize_tool_dict(value: dict | None) -> dict:
    return query_primitives.normalize_dict(value)


def _normalize_tool_list(value: Sequence | None) -> list:
    return query_primitives.normalize_list(value)


def _node_field(node: dict, field: str) -> object:
    return query_primitives.node_field(node, field)


def _node_has_field(node: dict, field: str) -> bool:
    return query_primitives.node_has_field(node, field)


def _is_heavy_field(field: str) -> bool:
    return query_primitives.is_heavy_field(field)


def _to_number(value: object) -> float | None:
    return query_primitives.to_number(value)


def _loose_equal(left: object, right: object) -> bool:
    return query_primitives.loose_equal(left, right)


def _match_one(value: object, operator: str, expected: object, *, exists: bool) -> bool:
    return query_primitives.match_one(value, operator, expected, exists=exists)


def _matches_where(node: dict, where: dict | None) -> bool:
    return query_primitives.matches_where(node, where)


def _natural_key(value: object) -> tuple:
    return query_primitives.natural_key(value)


def _hashable_value(value: object) -> object:
    return query_primitives.hashable_value(value)


def _sort_value_key(value: object) -> tuple:
    return query_primitives.sort_value_key(value)


def _sort_rows(rows: list[dict], order_by: Sequence | None) -> list[dict]:
    return query_primitives.sort_rows(rows, order_by)


def _sort_values(values: list[object], order: str = "asc") -> list[object]:
    return query_primitives.sort_values(values, order)


def _compact_sample_value(value: object) -> object:
    return query_primitives.compact_sample_value(value)


def _project_node(node: dict, fields: Sequence | None = None, *, include_heavy_fields: bool = False) -> dict:
    return query_primitives.project_node(node, fields, include_heavy_fields=include_heavy_fields)


def _pack_nodes(pack_id: str, *, node_limit: int = 50000) -> tuple[dict, list[dict]]:
    graph = read_graph(pack_id, max_nodes=max(1, node_limit), max_edges=0)
    return graph, list(graph.get("nodes", []))


def _filtered_nodes(
    pack_id: str,
    *,
    node_type: str | None = None,
    where: dict | None = None,
    node_limit: int = 50000,
) -> tuple[dict, list[dict]]:
    graph, nodes = _pack_nodes(pack_id, node_limit=node_limit)
    if node_type:
        nodes = [node for node in nodes if str(node.get("type", "")).casefold() == str(node_type).casefold()]
    nodes = [node for node in nodes if _matches_where(node, where)]
    return graph, nodes


def _filtered_rows(
    pack_id: str,
    *,
    node_type: str | None = None,
    where: dict | None = None,
    fields: Sequence | None = None,
    include_heavy_fields: bool = False,
    node_limit: int = 50000,
) -> tuple[dict, list[dict]]:
    graph, nodes = _filtered_nodes(pack_id, node_type=node_type, where=where, node_limit=node_limit)
    return graph, [_project_node(node, fields, include_heavy_fields=include_heavy_fields) for node in nodes]


def _metric_name(metric: dict) -> str:
    field = str(metric.get("field") or "")
    agg = str(metric.get("agg") or "count")
    return str(metric.get("as") or (f"{field}_{agg}" if field else agg))


def _aggregate_filtered_nodes(
    nodes: list[dict],
    *,
    group_by: Sequence | None,
    metrics: Sequence | None,
) -> tuple[list[dict], dict]:
    group_fields = [str(field) for field in _normalize_tool_list(group_by)]
    metric_specs = [
        metric if isinstance(metric, dict) else {"agg": str(metric)}
        for metric in (_normalize_tool_list(metrics) or [{"agg": "count", "as": "row_count"}])
    ]
    groups: dict[tuple, list[dict]] = {}
    for node in nodes:
        key = tuple(_hashable_value(_node_field(node, field)) for field in group_fields)
        groups.setdefault(key, []).append(node)

    skipped_rows: dict[str, int] = {}
    rows: list[dict] = []
    for key, group_nodes in groups.items():
        row = {field: key[index] for index, field in enumerate(group_fields)}
        for metric in metric_specs:
            agg = str(metric.get("agg") or "count").lower()
            field = str(metric.get("field") or "")
            name = _metric_name(metric)
            values = [_node_field(node, field) for node in group_nodes] if field else []
            present_values = [value for value in values if value is not _MISSING and value is not None]
            if agg == "count":
                row[name] = len(group_nodes if not field else present_values)
            elif agg == "count_distinct":
                row[name] = len({json.dumps(value, ensure_ascii=False, sort_keys=True) for value in present_values})
            elif agg in {"sum", "avg", "min", "max"}:
                numbers = []
                skipped = 0
                for value in present_values:
                    number = _to_number(value)
                    if number is None:
                        skipped += 1
                    else:
                        numbers.append(number)
                if skipped:
                    skipped_rows[name] = skipped_rows.get(name, 0) + skipped
                if agg == "sum":
                    row[name] = round(sum(numbers), 6)
                elif agg == "avg":
                    row[name] = round(sum(numbers) / len(numbers), 6) if numbers else None
                elif agg == "min":
                    row[name] = min(numbers) if numbers else None
                else:
                    row[name] = max(numbers) if numbers else None
            elif agg == "first":
                row[name] = present_values[0] if present_values else None
            elif agg == "collect_set":
                unique = []
                seen = set()
                for value in present_values:
                    key_value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if key_value not in seen:
                        seen.add(key_value)
                        unique.append(value)
                row[name] = _sort_values(unique)
            elif agg == "collect_list":
                row[name] = present_values
            else:
                row[name] = None
        rows.append(row)
    return rows, {"skipped_rows": skipped_rows}


def _parse_module_id(module_id: object) -> dict:
    text = str(module_id or "").strip()
    parts = text.split("-", 2)
    parsed = {"module_id": text, "floor": None, "module_code": "", "type_code": ""}
    if len(parts) == 3:
        parsed["module_code"] = parts[1]
        parsed["type_code"] = parts[2]
        try:
            parsed["floor"] = int(parts[0])
        except ValueError:
            parsed["floor"] = None
    return parsed


def _module_candidates_from_nodes(
    pack_id: str,
    *,
    module_pattern: str = _MODULE_PATTERN.pattern,
    include_sources: bool = True,
) -> dict[str, dict]:
    try:
        regex = re.compile(module_pattern)
    except re.error:
        regex = _MODULE_PATTERN
    _, nodes = _pack_nodes(pack_id)
    modules: dict[str, dict] = {}
    fields = ["module_id", "module_type", "modules", "module", "workset_name"]
    for node in nodes:
        candidate_fields = fields + (["id", "label"] if str(node.get("type", "")).casefold() == "module" else [])
        for field in candidate_fields:
            value = _node_field(node, field)
            if value is _MISSING or value is None:
                continue
            for candidate in value if isinstance(value, list) else [value]:
                module_id = str(candidate).strip()
                if not regex.search(module_id):
                    continue
                entry = modules.setdefault(module_id, {**_parse_module_id(module_id), "sources": []})
                source = {"pack_id": pack_id, "field": field, "node_type": node.get("type", "")}
                if include_sources and source not in entry["sources"]:
                    entry["sources"].append(source)
    return modules


_WHERE_OPERATORS = {
    "eq",
    "ne",
    "contains",
    "startswith",
    "endswith",
    "in",
    "not_in",
    "regex",
    "gt",
    "gte",
    "lt",
    "lte",
    "exists",
    "wildcard",
}
_MEASURE_DISPLAY_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*([^\d\s]+)?")
_AREA_TAG_RE = re.compile(
    r"(?P<label>[\w가-힣\s()/_\-.]{1,60}?)\s*(?P<area>\d+(?:\.\d+)?)\s*(?P<unit>m2|㎡|m\^2|제곱미터|평방미터)",
    flags=re.I,
)
_DIMENSION_VALUE_RE = re.compile(r"(?<![\d.])(\d{2,5}(?:\.\d+)?)(?!\s*(?:m2|㎡|m\^2|%))", flags=re.I)


def _read_pack_json_member(pack_id: str, path: str) -> dict:
    try:
        pack = find_pack(pack_id)
    except FileNotFoundError:
        return {}
    with zipfile.ZipFile(pack.path) as zf:
        if path not in zf.namelist():
            return {}
        try:
            return json.loads(zf.read(path).decode("utf-8-sig", errors="replace"))
        except Exception:
            return {}


def _pack_logical_name(pack_id: str) -> str:
    pack_json = _read_pack_json_member(pack_id, "pack.json")
    if pack_json.get("logical_pack"):
        return str(pack_json.get("logical_pack"))
    manifest = _read_pack_json_member(pack_id, "manifest.json")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    if source.get("derived_file"):
        return str(source.get("derived_file")).removesuffix(".jsonl")
    pack_id_lower = str(pack_id).lower()
    for logical in (
        "drawing_evidence",
        "drawing_documents",
        "schedule_rows",
        "schedule_tables",
        "quantity_facts",
        "material_facts",
        "bim_objects",
        "relationships",
    ):
        if logical in pack_id_lower:
            return logical
    for pack in read_packs():
        if str(pack.get("id") or "") != str(pack_id):
            continue
        text = " ".join(
            str(pack.get(key) or "")
            for key in ("filename", "displayFilename", "displayName", "title")
        ).lower()
        for logical in (
            "drawing_evidence",
            "drawing_documents",
            "schedule_rows",
            "schedule_tables",
            "quantity_facts",
            "material_facts",
            "bim_objects",
            "relationships",
        ):
            if logical in text or logical.replace("_", " ") in text:
                return logical
    return ""


def _iter_pack_jsonl_member(pack_id: str, path: str):
    pack = find_pack(pack_id)
    with zipfile.ZipFile(pack.path) as zf:
        if path not in zf.namelist():
            return
        with zf.open(path) as stream:
            for raw in stream:
                line = raw.decode("utf-8-sig", errors="replace").strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value


def _resolve_pack_ids(
    *,
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    project_id: str = "",
    logical_pack: str = "",
    include_sibling_shards: bool = True,
) -> list[str]:
    projects_by_id = _visible_project_by_id()
    packs_by_id = _visible_pack_by_id()
    candidates: list[str] = []
    explicit_pack_id = str(pack_id) if pack_id else ""
    if pack_ids:
        candidates = [str(item) for item in _normalize_tool_list(pack_ids)]
    elif pack_id:
        candidates = [explicit_pack_id]
        if include_sibling_shards:
            logical = logical_pack or _pack_logical_name(str(pack_id))
            if logical:
                if project_id and project_id in projects_by_id:
                    pool = [candidate for candidate in _project_pack_ids(projects_by_id[project_id]) if candidate in packs_by_id]
                else:
                    pool = list(packs_by_id)
                siblings = [candidate for candidate in pool if _pack_logical_name(candidate) == logical]
                candidates = list(dict.fromkeys([explicit_pack_id, *siblings]))
    elif project_id:
        if project_id not in projects_by_id:
            return []
        candidates = [candidate for candidate in _project_pack_ids(projects_by_id[project_id]) if candidate in packs_by_id]
    else:
        candidates = list(packs_by_id)

    resolved: list[str] = []
    for candidate in candidates:
        if candidate not in packs_by_id or not _pack_is_visible(candidate):
            continue
        if logical_pack and _pack_logical_name(candidate) != logical_pack:
            if not (explicit_pack_id and candidate == explicit_pack_id and not _pack_logical_name(candidate)):
                continue
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


def _chunk_row_from_chunk(pack_id: str, chunk: dict) -> dict:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    join_keys = metadata.get("join_keys") if isinstance(metadata.get("join_keys"), dict) else {}
    compact_row = metadata.get("compact_row") if isinstance(metadata.get("compact_row"), dict) else {}
    source_refs = metadata.get("source_refs") if isinstance(metadata.get("source_refs"), list) else []
    row: dict[str, object] = {
        "pack_id": pack_id,
        "chunk_id": chunk.get("chunk_id"),
        "document_id": chunk.get("document_id"),
        "text": chunk.get("text"),
        "metadata": metadata,
        "source_refs": source_refs,
        "derived_file": metadata.get("derived_file"),
        "row_id": metadata.get("row_id"),
        "category": metadata.get("category"),
        "compact_row": compact_row,
    }
    row.update(join_keys)
    if "parameter" not in row and row.get("parameter_name") is not None:
        row["parameter"] = row.get("parameter_name")
    if "display" not in row and row.get("parameter_display") is not None:
        row["display"] = row.get("parameter_display")
    value, unit = _measurement_value_unit(row)
    row.setdefault("value", value)
    row.setdefault("unit", unit)
    row.setdefault("source_pack_id", pack_id)
    row.setdefault("source_chunk_id", chunk.get("chunk_id"))
    return row


def _measurement_value_unit(row: dict) -> tuple[float | None, str | None]:
    area = _to_number(row.get("area_m2"))
    if area is not None:
        return area, "m2"
    volume = _to_number(row.get("volume_m3"))
    if volume is not None:
        return volume, "m3"
    length = _to_number(row.get("length_m"))
    if length is not None:
        return length, "m"
    display = row.get("parameter_display") or row.get("display")
    if display is not None:
        match = _MEASURE_DISPLAY_RE.search(str(display).replace(",", ""))
        if match:
            try:
                return float(match.group(1)), match.group(2)
            except ValueError:
                pass
    return None, None


def _row_get(row: dict, field: str) -> object:
    aliases = {
        "parameter": "parameter_name",
        "display": "parameter_display",
        "sheet_no": "sheet_number",
        "sheet": "sheet_number",
        "text_type": "evidence_type",
        "source_chunk_id": "chunk_id",
        "source_pack_id": "pack_id",
    }
    candidates = [field]
    if field == "category":
        candidates.extend(["quantity_category", "category_name", "element_category", "evidence_type"])
    if field in aliases:
        candidates.append(aliases[field])
    for candidate in candidates:
        current: object = row
        found = True
        for part in str(candidate).split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            return current
    compact_row = row.get("compact_row")
    if isinstance(compact_row, dict) and field in compact_row:
        return compact_row[field]
    return _MISSING


def _split_where_key(field: str) -> tuple[str, str | None]:
    if "__" not in field:
        return field, None
    base, operator = field.rsplit("__", 1)
    if operator in _WHERE_OPERATORS:
        return base, operator
    return field, None


def _row_matches_where(row: dict, where: dict | None) -> bool:
    for raw_field, condition in _normalize_tool_dict(where).items():
        field, suffix_operator = _split_where_key(str(raw_field))
        value = _row_get(row, field)
        exists = value is not _MISSING
        if suffix_operator:
            if not _match_one(value, suffix_operator, condition, exists=exists):
                return False
            continue
        if isinstance(condition, dict):
            if not all(_match_one(value, str(operator), expected, exists=exists) for operator, expected in condition.items()):
                return False
        elif not _match_one(value, "eq", condition, exists=exists):
            return False
    return True


def _project_row(row: dict, fields: Sequence | None = None) -> dict:
    requested = _normalize_tool_list(fields)
    if not requested:
        requested = [
            "pack_id",
            "chunk_id",
            "text",
            "workset_name",
            "element_id",
            "element_name",
            "family_name",
            "family_and_type",
            "type_name",
            "parameter",
            "display",
            "value",
            "unit",
            "area_m2",
            "volume_m3",
            "sheet_number",
            "view_name",
            "evidence_type",
            "source_refs",
        ]
    projected = {}
    for field in requested:
        key = str(field)
        value = _row_get(row, key)
        if value is not _MISSING:
            projected[key.split(".", 1)[-1]] = value
    return projected


def _sort_chunk_rows(rows: list[dict], order_by: Sequence | None) -> list[dict]:
    specs = _normalize_tool_list(order_by)
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
            key=lambda row: _natural_key(_row_get(row, field)) if natural else _sort_value_key(_row_get(row, field)),
            reverse=reverse,
        )
    return ordered


def _iter_cloud_chunk_rows(pack_id: str):
    try:
        pack = find_pack(pack_id)
        path = Path(pack.path)
        stat = path.stat()
    except (FileNotFoundError, OSError):
        for chunk in _iter_pack_jsonl_member(pack_id, "cloud/chunks.jsonl"):
            yield _chunk_row_from_chunk(pack_id, chunk)
        return
    cached = _CLOUD_CHUNK_ROW_CACHE.get(pack_id)
    if (
        cached
        and cached.get("mtime_ns") == stat.st_mtime_ns
        and cached.get("size") == stat.st_size
        and isinstance(cached.get("rows"), list)
    ):
        yield from cached["rows"]
        return
    rows = [_chunk_row_from_chunk(pack_id, chunk) for chunk in _iter_pack_jsonl_member(pack_id, "cloud/chunks.jsonl")]
    _CLOUD_CHUNK_ROW_CACHE[pack_id] = {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "rows": rows,
    }
    yield from rows


def _pack_source_derived_dir(pack_id: str) -> Path | None:
    candidates = [_read_pack_json_member(pack_id, "manifest.json"), _read_pack_json_member(pack_id, "pack.json")]
    for candidate in candidates:
        source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
        cloud_pack = candidate.get("cloud_pack") if isinstance(candidate.get("cloud_pack"), dict) else {}
        if not source and isinstance(cloud_pack.get("source"), dict):
            source = cloud_pack["source"]
        derived_dir = source.get("derived_dir")
        if not derived_dir:
            continue
        path = Path(str(derived_dir))
        if path.exists():
            return path
    return None


def _export_roots_for_packs(pack_ids: Sequence[str]) -> list[Path]:
    roots: list[Path] = []
    for pack_id in pack_ids:
        derived_dir = _pack_source_derived_dir(str(pack_id))
        if not derived_dir:
            continue
        root = derived_dir.parent
        if root.exists() and root not in roots:
            roots.append(root)
    return roots


def _iter_export_jsonl(root: Path, filename: str):
    path = root / filename
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for raw in stream:
            line = raw.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _iter_drawing_entity_search_rows(root: Path):
    path = root / "drawing_entities.jsonl"
    if not path.exists():
        return
    try:
        stat = path.stat()
    except OSError:
        return
    cache_key = str(path)
    cached = _DRAWING_ENTITY_SEARCH_CACHE.get(cache_key)
    if (
        cached
        and cached.get("mtime_ns") == stat.st_mtime_ns
        and cached.get("size") == stat.st_size
        and isinstance(cached.get("rows"), list)
    ):
        yield from cached["rows"]
        return
    searchable_types = {"TEXT", "MTEXT", "DIMENSION", "LAYER"}
    rows: list[dict] = []
    for row in _iter_export_jsonl(root, "drawing_entities.jsonl"):
        entity_type = str(row.get("entity_type") or row.get("record_type") or "").upper()
        if entity_type in searchable_types:
            rows.append(row)
    _DRAWING_ENTITY_SEARCH_CACHE[cache_key] = {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "rows": rows,
    }
    yield from rows


def _iter_drawing_entity_rows(root: Path):
    yield from _iter_export_jsonl(root, "drawing_entities.jsonl")


def _drawing_entity_context(row: dict) -> dict:
    context = row.get("source_context") if isinstance(row.get("source_context"), dict) else {}
    if context:
        return context
    return {
        "sheet_number": row.get("sheet_number") or row.get("sheet_no"),
        "sheet_name": row.get("sheet_name"),
        "view_name": row.get("view_name"),
        "view_type": row.get("view_type"),
        "source_kind": row.get("source_kind"),
    }


def _drawing_entity_geometry(row: dict) -> dict:
    return row.get("geometry") if isinstance(row.get("geometry"), dict) else {}


def _dxf_source_file(row: dict) -> str | None:
    value = row.get("dxf_file_name") or row.get("source_file") or row.get("file_name")
    if value:
        return str(value)
    path = row.get("dxf_path")
    if path:
        return Path(str(path)).name
    return None


def _drawing_entity_text(row: dict) -> str:
    geometry = _drawing_entity_geometry(row)
    entity_type = str(row.get("entity_type") or "")
    if entity_type == "DIMENSION":
        measurement = _to_number(geometry.get("measurement"))
        if measurement is not None:
            return f"{measurement:g}"
    for field in ("decoded_text", "plain_text", "text", "measurement_text"):
        value = geometry.get(field)
        if value is not None:
            return str(value)
    for field in ("text", "plain_text", "mtext", "dimension_text"):
        value = row.get(field)
        if value is not None:
            return str(value)
    return ""


def _point_xy(point: object) -> list[float] | None:
    if not isinstance(point, dict):
        return None
    try:
        return [float(point["x"]), float(point["y"])]
    except (KeyError, TypeError, ValueError):
        return None


def _bbox_from_entity_row(row: dict) -> list[float] | None:
    bbox = row.get("bbox") or row.get("bounding_box")
    if isinstance(bbox, list) and len(bbox) >= 4:
        try:
            return [float(value) for value in bbox[:4]]
        except (TypeError, ValueError):
            pass
    if isinstance(bbox, dict):
        minimum = bbox.get("min") if isinstance(bbox.get("min"), dict) else {}
        maximum = bbox.get("max") if isinstance(bbox.get("max"), dict) else {}
        try:
            return [float(minimum["x"]), float(minimum["y"]), float(maximum["x"]), float(maximum["y"])]
        except (KeyError, TypeError, ValueError):
            pass
    geometry = _drawing_entity_geometry(row)
    for key in ("insert_point", "text_middle_point", "definition_point"):
        point = _point_xy(geometry.get(key))
        if point:
            return [point[0], point[1], point[0], point[1]]
    return None


def _drawing_entity_line(row: dict) -> dict | None:
    geometry = _drawing_entity_geometry(row)
    start = _point_xy(geometry.get("first_point") or geometry.get("start_point") or geometry.get("start"))
    end = _point_xy(geometry.get("second_point") or geometry.get("end_point") or geometry.get("end"))
    if not start:
        raw_start = row.get("start_point")
        if isinstance(raw_start, list) and len(raw_start) >= 2:
            try:
                start = [float(raw_start[0]), float(raw_start[1])]
            except (TypeError, ValueError):
                start = None
    if not end:
        raw_end = row.get("end_point")
        if isinstance(raw_end, list) and len(raw_end) >= 2:
            try:
                end = [float(raw_end[0]), float(raw_end[1])]
            except (TypeError, ValueError):
                end = None
    if start and end:
        return {"start": start, "end": end}
    return None


def _drawing_entity_orientation(row: dict) -> str | None:
    line = _drawing_entity_line(row)
    if line:
        dx = abs(line["end"][0] - line["start"][0])
        dy = abs(line["end"][1] - line["start"][1])
        if dx >= dy * 1.5:
            return "horizontal"
        if dy >= dx * 1.5:
            return "vertical"
    return _bbox_orientation(_bbox_from_entity_row(row))


def _classify_drawing_entity_type(row: dict) -> str:
    entity_type = str(row.get("entity_type") or "").upper()
    text = _drawing_entity_text(row)
    if entity_type == "DIMENSION":
        return "dimension"
    if entity_type in {"TEXT", "MTEXT"}:
        if _AREA_TAG_RE.search(text):
            return "room_tag"
        return "annotation"
    if entity_type == "INSERT":
        return "symbol"
    if entity_type == "HATCH":
        return "hatch"
    if entity_type in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}:
        return "graphic"
    if entity_type == "LAYER":
        return "layer"
    return "drawing_entity"


def _drawing_entity_payload(row: dict, root: Path | None, source_pack_id: str, score: float = 1.0) -> dict:
    context = _drawing_entity_context(row)
    entity_key = row.get("entity_key") or row.get("record_key") or row.get("source_entity_key")
    entity_type = str(row.get("entity_type") or row.get("record_type") or "")
    geometry = _drawing_entity_geometry(row)
    line = _drawing_entity_line(row)
    insert_point = _point_xy(geometry.get("insert_point") or geometry.get("insertion_point"))
    if not insert_point:
        raw_insert = row.get("insert") or row.get("insert_point")
        if isinstance(raw_insert, list) and len(raw_insert) >= 2:
            try:
                insert_point = [float(raw_insert[0]), float(raw_insert[1])]
            except (TypeError, ValueError):
                insert_point = None
    payload = {
        "sheet_no": context.get("sheet_number"),
        "sheet_name": context.get("sheet_name"),
        "view_name": context.get("view_name"),
        "text": _drawing_entity_text(row),
        "text_type": _classify_drawing_entity_type(row),
        "evidence_type": entity_type,
        "entity_type": entity_type,
        "layer": row.get("layer"),
        "bbox": _bbox_from_entity_row(row),
        "line": line,
        "start_point": line.get("start") if line else None,
        "end_point": line.get("end") if line else None,
        "insert": insert_point,
        "insert_point": insert_point,
        "handle": row.get("handle"),
        "space": row.get("space"),
        "block_name": row.get("block_name"),
        "block_path": row.get("block_path"),
        "source_file": _dxf_source_file(row),
        "source_path": row.get("dxf_path"),
        "source_kind": row.get("source_kind"),
        "source_jsonl": str(root / "drawing_entities.jsonl") if root else str(row.get("source_jsonl") or "sqlite-index"),
        "source_pack_id": source_pack_id,
        "source_chunk_id": row.get("source_chunk_id") or (f"drawing_entity:{entity_key}" if entity_key else None),
        "source_entity_key": entity_key,
        "confidence": 0.82 if entity_type in {"TEXT", "MTEXT", "DIMENSION"} else 0.65,
        "score": round(score, 3),
    }
    if entity_type == "DIMENSION":
        measurement = _to_number(geometry.get("measurement") if geometry.get("measurement") is not None else row.get("value_mm"))
        if measurement is not None:
            payload["value_mm"] = measurement
            payload["value"] = measurement
            payload["unit"] = "mm"
            payload["orientation"] = _drawing_entity_orientation(row) or "unknown"
    return payload


def _read_drawing_entity_payload(pack_id: str, chunk_id: str) -> dict | None:
    requested = str(chunk_id)
    requested_key = requested.removeprefix("drawing_entity:")
    roots = _export_roots_for_packs([pack_id])
    for root in roots:
        for row in _iter_drawing_entity_search_rows(root):
            entity_key = str(row.get("entity_key") or row.get("record_key") or "")
            if not entity_key or entity_key != requested_key:
                continue
            payload = _drawing_entity_payload(row, root, pack_id)
            return {
                "pack_id": pack_id,
                "chunk_id": requested,
                "text": payload.get("text"),
                "metadata": {
                    "source_file": payload.get("source_file") or "drawing_entities.jsonl",
                    "dxf_file_name": payload.get("source_file"),
                    "source_jsonl": payload.get("source_jsonl") or str(root / "drawing_entities.jsonl"),
                    "entity_key": entity_key,
                    "source_entity_key": entity_key,
                    "entity_type": row.get("entity_type"),
                    "handle": payload.get("handle") or row.get("handle"),
                    "layer": row.get("layer"),
                    "source_context": _drawing_entity_context(row),
                    "bbox": payload.get("bbox"),
                    "line": payload.get("line"),
                    "geometry": _drawing_entity_geometry(row),
                },
                "source_refs": [],
            }
        for row in _iter_drawing_entity_rows(root):
            entity_key = str(row.get("entity_key") or row.get("record_key") or "")
            if not entity_key or entity_key != requested_key:
                continue
            payload = _drawing_entity_payload(row, root, pack_id)
            return {
                "pack_id": pack_id,
                "chunk_id": requested,
                "text": payload.get("text"),
                "metadata": {
                    "source_file": payload.get("source_file") or "drawing_entities.jsonl",
                    "dxf_file_name": payload.get("source_file"),
                    "source_jsonl": payload.get("source_jsonl") or str(root / "drawing_entities.jsonl"),
                    "entity_key": entity_key,
                    "source_entity_key": entity_key,
                    "entity_type": row.get("entity_type"),
                    "handle": payload.get("handle") or row.get("handle"),
                    "layer": row.get("layer"),
                    "source_context": _drawing_entity_context(row),
                    "bbox": payload.get("bbox"),
                    "line": payload.get("line"),
                    "geometry": _drawing_entity_geometry(row),
                },
                "source_refs": [],
            }
    return None


def _cloud_chunk_payload(pack_id: str, chunk: dict) -> dict:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    chunk_source_refs = []
    if isinstance(metadata.get("source_refs"), list):
        chunk_source_refs.extend(metadata.get("source_refs") or [])
    if isinstance(chunk.get("source_refs"), list):
        chunk_source_refs.extend(chunk.get("source_refs") or [])
    return {
        "pack_id": pack_id,
        "chunk_id": chunk.get("chunk_id"),
        "document_id": chunk.get("document_id"),
        "text": chunk.get("text"),
        "path": chunk.get("path"),
        "metadata": metadata,
        "source_refs": chunk_source_refs,
    }


def _read_chunk_payload(pack_id: str, chunk_id: str) -> dict:
    for chunk in _iter_pack_jsonl_member(pack_id, "cloud/chunks.jsonl"):
        if str(chunk.get("chunk_id")) == str(chunk_id):
            return _cloud_chunk_payload(pack_id, chunk)
    entity_payload = _read_drawing_entity_payload(pack_id, chunk_id)
    if entity_payload:
        return entity_payload
    return {"error": "not_found", "pack_id": pack_id, "chunk_id": chunk_id}


def _anchor_resolve_payload(pack_id: str, chunk_id: str) -> dict:
    chunk = _read_chunk_payload(pack_id=pack_id, chunk_id=chunk_id)
    if chunk.get("error"):
        return chunk
    anchors = anchors_from_chunk(chunk)
    anchor_payloads = [anchor.to_dict() for anchor in anchors]
    return {
        "packId": pack_id,
        "chunkId": chunk.get("chunk_id") or chunk_id,
        "documentId": chunk.get("document_id"),
        "anchorCount": len(anchor_payloads),
        "coverage": source_anchor_coverage(anchors),
        "anchors": anchor_payloads,
        "chunk": {
            "text": chunk.get("text"),
            "metadata": chunk.get("metadata") or {},
            "source_refs": chunk.get("source_refs") or [],
        },
    }


def _anchor_coverage_payload(pack_id: str, limit: int = 200, include_chunks: bool = False) -> dict:
    max_chunks = max(1, min(int(limit), 5000))
    all_anchors = []
    chunk_rows = []
    chunk_status_counts: Counter[str] = Counter()
    by_document: dict[str, dict[str, object]] = {}
    chunks_scanned = 0
    truncated = False
    for chunk in _iter_pack_jsonl_member(pack_id, "cloud/chunks.jsonl"):
        if max_chunks and chunks_scanned >= max_chunks:
            truncated = True
            break
        chunks_scanned += 1
        chunk_payload = _cloud_chunk_payload(pack_id, chunk)
        anchors = anchors_from_chunk(chunk_payload)
        chunk_coverage = source_anchor_coverage(anchors)
        if chunk_coverage["exact"]:
            chunk_status = "exact"
        elif chunk_coverage["resolvable"]:
            chunk_status = "partial"
        else:
            chunk_status = "unknown"
        chunk_status_counts[chunk_status] += 1
        document_id = str(chunk_payload.get("document_id") or "<none>")
        document_row = by_document.setdefault(
            document_id,
            {"documentId": document_id, "chunksScanned": 0, "resolvableChunks": 0, "exactChunks": 0, "unknownChunks": 0},
        )
        document_row["chunksScanned"] = int(document_row["chunksScanned"]) + 1
        if chunk_status != "unknown":
            document_row["resolvableChunks"] = int(document_row["resolvableChunks"]) + 1
        if chunk_status == "exact":
            document_row["exactChunks"] = int(document_row["exactChunks"]) + 1
        if chunk_status == "unknown":
            document_row["unknownChunks"] = int(document_row["unknownChunks"]) + 1
        all_anchors.extend(anchors)
        if include_chunks:
            chunk_rows.append(
                {
                    "chunkId": chunk_payload["chunk_id"],
                    "documentId": chunk_payload["document_id"],
                    "anchorCount": len(anchors),
                    "anchorStatus": chunk_status,
                    "coverage": chunk_coverage,
                    "anchors": [anchor.to_dict() for anchor in anchors],
                }
            )

    resolvable_chunks = int(chunk_status_counts["exact"] + chunk_status_counts["partial"])
    exact_chunks = int(chunk_status_counts["exact"])
    unknown_chunks = int(chunk_status_counts["unknown"])
    chunk_resolvable_rate = round(resolvable_chunks / chunks_scanned, 4) if chunks_scanned else 0.0
    chunk_exact_rate = round(exact_chunks / chunks_scanned, 4) if chunks_scanned else 0.0
    chunk_unknown_rate = round(unknown_chunks / chunks_scanned, 4) if chunks_scanned else 0.0
    quality_status = "pass" if chunk_resolvable_rate >= 0.8 else "warn" if chunk_resolvable_rate > 0 else "fail"

    return {
        "packId": pack_id,
        "limit": max_chunks,
        "scan": {"member": "cloud/chunks.jsonl", "truncated": truncated},
        "chunksScanned": chunks_scanned,
        "coverage": source_anchor_coverage(all_anchors),
        "chunkCoverage": {
            "total": chunks_scanned,
            "resolvable": resolvable_chunks,
            "unknown": unknown_chunks,
            "exact": exact_chunks,
            "partial": int(chunk_status_counts["partial"]),
            "resolvable_rate": chunk_resolvable_rate,
            "exact_rate": chunk_exact_rate,
            "unknown_rate": chunk_unknown_rate,
            "by_status": dict(chunk_status_counts),
            "by_document": list(by_document.values()),
        },
        "quality": {
            "metric": "anchor_chunk_coverage",
            "status": quality_status,
            "promotion_status": "draft" if quality_status != "fail" else "blocked",
            "sampled": truncated,
            "resolvable_rate": chunk_resolvable_rate,
            "unknown_rate": chunk_unknown_rate,
        },
        "chunks": chunk_rows if include_chunks else [],
    }


def _dxf_request_roots(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
) -> list[tuple[Path, str]]:
    active_pack_ids = _resolve_pack_ids(
        pack_id=pack_id,
        pack_ids=pack_ids,
        project_id=project_id,
        logical_pack="drawing_evidence",
    )
    roots: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for active_pack_id in active_pack_ids:
        derived_dir = _pack_source_derived_dir(active_pack_id)
        if not derived_dir:
            continue
        root = derived_dir.parent
        if root.exists() and root not in seen:
            roots.append((root, active_pack_id))
            seen.add(root)
    return roots


def _dxf_request_pack_ids(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
) -> list[str]:
    return _resolve_pack_ids(
        pack_id=pack_id,
        pack_ids=pack_ids,
        project_id=project_id,
        logical_pack="drawing_evidence",
    )


def _dxf_roots_for_pack_ids(active_pack_ids: Sequence[str]) -> list[tuple[Path, str]]:
    roots: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for active_pack_id in active_pack_ids:
        derived_dir = _pack_source_derived_dir(str(active_pack_id))
        if not derived_dir:
            continue
        root = derived_dir.parent
        if root.exists() and root not in seen:
            roots.append((root, str(active_pack_id)))
            seen.add(root)
    return roots


def _iter_index_dxf_entity_rows(pack_id: str):
    conn = None
    try:
        conn = connect_index_db()
        init_index_db(conn)
        rows = conn.execute(
            """
            SELECT id, label, properties_json
            FROM nodes
            WHERE pack_id = ?
              AND (
                properties_json LIKE '%dxf_entity%'
                OR properties_json LIKE '%drawing_entities%'
                OR properties_json LIKE '%"entity_type"%'
              )
            """,
            (pack_id,),
        ).fetchall()
    except Exception:
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass
    supported = {"LAYER", "LINE", "LWPOLYLINE", "POLYLINE", "TEXT", "MTEXT", "HATCH", "DIMENSION", "INSERT", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}
    for row in rows:
        try:
            properties = json.loads(row["properties_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(properties, dict):
            continue
        entity_type = str(properties.get("entity_type") or "").upper()
        if entity_type not in supported and properties.get("evidence_type") != "dxf_entity":
            continue
        properties.setdefault("source_chunk_id", row["id"])
        properties.setdefault("text", properties.get("raw_text") or row["label"])
        yield properties


def _iter_dxf_entity_rows_for_request(active_pack_ids: Sequence[str]):
    roots = _dxf_roots_for_pack_ids(active_pack_ids)
    raw_pack_ids = {source_pack_id for _root, source_pack_id in roots}
    for root, source_pack_id in roots:
        for row in _iter_drawing_entity_rows(root):
            yield row, root, source_pack_id
    for active_pack_id in active_pack_ids:
        if active_pack_id in raw_pack_ids:
            continue
        for row in _iter_index_dxf_entity_rows(str(active_pack_id)):
            yield row, None, str(active_pack_id)


def _entity_type_filter(entity_type: Sequence[str] | str | None) -> set[str]:
    return {str(item).upper() for item in _normalize_tool_list(entity_type)}


def _source_file_matches(row: dict, expected: str) -> bool:
    if not expected:
        return True
    source_file = str(_dxf_source_file(row) or "").casefold()
    source_path = str(row.get("dxf_path") or "").casefold()
    needle = str(expected).casefold()
    return needle in source_file or needle in source_path


def _dxf_entity_default_fields() -> list[str]:
    return [
        "source_file",
        "sheet_no",
        "sheet_name",
        "view_name",
        "entity_type",
        "layer",
        "text",
        "value_mm",
        "unit",
        "bbox",
        "start_point",
        "end_point",
        "insert",
        "handle",
        "source_chunk_id",
        "source_entity_key",
    ]


def _project_payload(payload: dict, fields: Sequence | None = None) -> dict:
    requested = _normalize_tool_list(fields)
    if not requested:
        return {key: value for key, value in payload.items() if value is not None}
    aliases = {
        "sheet_number": "sheet_no",
        "file": "source_file",
        "dxf_file_name": "source_file",
        "insert_point": "insert",
        "insertion_point": "insert",
        "value": "value_mm",
        "measurement": "value_mm",
    }
    projected = {}
    for raw_field in requested:
        field = str(raw_field)
        key = aliases.get(field, field)
        if key in payload and payload[key] is not None:
            projected[field] = payload[key]
    return projected


def _dxf_payload_for_match(row: dict, root: Path | None, source_pack_id: str, score: float = 1.0) -> dict:
    payload = _drawing_entity_payload(row, root, source_pack_id, score=score)
    geometry = _drawing_entity_geometry(row)
    if "value_mm" not in payload:
        measurement = _to_number(geometry.get("measurement"))
        if measurement is not None:
            payload["value_mm"] = measurement
            payload["value"] = measurement
            payload["unit"] = "mm"
    payload["record_type"] = row.get("record_type")
    payload["geometry_kind"] = geometry.get("kind")
    return payload


def _dxf_row_matches_basic_filters(
    row: dict,
    *,
    query: str = "",
    terms: list[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    entity_types: set[str] | None = None,
    layer: str = "",
) -> tuple[bool, float]:
    context = _drawing_entity_context(row)
    entity_type = str(row.get("entity_type") or row.get("record_type") or "").upper()
    if entity_types and entity_type not in entity_types:
        return False, 0.0
    if sheet_no and str(context.get("sheet_number") or "").casefold() != str(sheet_no).casefold():
        return False, 0.0
    if source_file and not _source_file_matches(row, source_file):
        return False, 0.0
    if layer and str(layer).casefold() not in str(row.get("layer") or "").casefold():
        return False, 0.0
    active_terms = terms if terms is not None else query_terms(query)
    if not active_terms:
        return True, 1.0
    text = _drawing_entity_text(row)
    haystack = (
        text
        + " "
        + str(_dxf_source_file(row) or "")
        + " "
        + str(context.get("sheet_number") or "")
        + " "
        + str(context.get("sheet_name") or "")
        + " "
        + str(context.get("view_name") or "")
        + " "
        + str(row.get("layer") or "")
        + " "
        + entity_type
    ).casefold()
    score = score_terms(haystack, active_terms)
    if score <= 0:
        return False, 0.0
    query_lower = str(query).casefold()
    text_lower = text.casefold()
    if query_lower and text_lower == query_lower:
        score += 10.0
    elif query_lower and query_lower in text_lower:
        score += 5.0
    return True, score


def _dxf_entity_search_payload(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    query: str = "",
    sheet_no: str = "",
    source_file: str = "",
    entity_type: Sequence[str] | str | None = None,
    layer: str = "",
    where: dict | None = None,
    fields: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    active_pack_ids = _dxf_request_pack_ids(project_id=project_id, pack_id=pack_id, pack_ids=pack_ids)
    entity_types = _entity_type_filter(entity_type)
    terms = query_terms(query)
    scored_rows: list[tuple[float, dict]] = []
    total = 0
    select_fields = fields or _dxf_entity_default_fields()
    for row, root, source_pack_id in _iter_dxf_entity_rows_for_request(active_pack_ids):
        matched, score = _dxf_row_matches_basic_filters(
            row,
            query=query,
            terms=terms,
            sheet_no=sheet_no,
            source_file=source_file,
            entity_types=entity_types,
            layer=layer,
        )
        if not matched:
            continue
        payload = _dxf_payload_for_match(row, root, source_pack_id, score=score)
        if where and not _row_matches_where(payload, where):
            continue
        total += 1
        scored_rows.append((score, _project_payload(payload, select_fields)))
    if terms:
        scored_rows.sort(key=lambda item: item[0], reverse=True)
    start = max(0, offset)
    end = start + max(0, limit)
    return {
        "project_id": project_id or None,
        "pack_id": pack_id or None,
        "pack_ids": active_pack_ids,
        "query": query,
        "sheet_no": sheet_no or None,
        "source_file": source_file or None,
        "entity_type": sorted(entity_types) if entity_types else None,
        "layer": layer or None,
        "where": _normalize_tool_dict(where),
        "count": total,
        "offset": start,
        "limit": max(0, limit),
        "rows": [row for _, row in scored_rows[start:end]],
        "truncated": total > end,
    }


def _dxf_entity_summary_payload(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    limit_layers: int = 50,
) -> dict:
    active_pack_ids = _dxf_request_pack_ids(project_id=project_id, pack_id=pack_id, pack_ids=pack_ids)
    entity_counts: Counter[str] = Counter()
    layer_counts: Counter[str] = Counter()
    file_counts: Counter[str] = Counter()
    sheet_counts: Counter[str] = Counter()
    total = 0
    for row, _root, _source_pack_id in _iter_dxf_entity_rows_for_request(active_pack_ids):
        context = _drawing_entity_context(row)
        if sheet_no and str(context.get("sheet_number") or "").casefold() != str(sheet_no).casefold():
            continue
        if source_file and not _source_file_matches(row, source_file):
            continue
        total += 1
        entity_counts[str(row.get("entity_type") or row.get("record_type") or "UNKNOWN")] += 1
        layer_counts[str(row.get("layer") or "<none>")] += 1
        file_counts[str(_dxf_source_file(row) or "<unknown>")] += 1
        sheet_counts[str(context.get("sheet_number") or "<none>")] += 1
    return {
        "project_id": project_id or None,
        "pack_id": pack_id or None,
        "pack_ids": active_pack_ids,
        "sheet_no": sheet_no or None,
        "source_file": source_file or None,
        "total_entities": total,
        "entity_counts": dict(entity_counts.most_common()),
        "layers": [{"layer": key, "count": value} for key, value in layer_counts.most_common(max(0, limit_layers))],
        "source_files": [{"source_file": key, "count": value} for key, value in file_counts.most_common(20)],
        "sheets": [{"sheet_no": key, "count": value} for key, value in sheet_counts.most_common(20)],
    }


def _dxf_layer_summary_payload(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    limit: int = 100,
) -> dict:
    active_pack_ids = _dxf_request_pack_ids(project_id=project_id, pack_id=pack_id, pack_ids=pack_ids)
    layer_entity_counts: dict[str, Counter[str]] = {}
    for row, _root, _source_pack_id in _iter_dxf_entity_rows_for_request(active_pack_ids):
        context = _drawing_entity_context(row)
        if sheet_no and str(context.get("sheet_number") or "").casefold() != str(sheet_no).casefold():
            continue
        if source_file and not _source_file_matches(row, source_file):
            continue
        layer_name = str(row.get("layer") or "<none>")
        entity_name = str(row.get("entity_type") or row.get("record_type") or "UNKNOWN")
        layer_entity_counts.setdefault(layer_name, Counter())[entity_name] += 1
    layers = []
    for layer_name, counts in layer_entity_counts.items():
        layers.append({"layer": layer_name, "total": sum(counts.values()), "entity_counts": dict(counts.most_common())})
    layers.sort(key=lambda item: int(item["total"]), reverse=True)
    max_limit = max(0, limit)
    return {
        "project_id": project_id or None,
        "pack_id": pack_id or None,
        "sheet_no": sheet_no or None,
        "source_file": source_file or None,
        "layer_count": len(layers),
        "layers": layers[:max_limit],
        "truncated": len(layers) > max_limit,
    }


def _bbox_relation(candidate: list[float] | None, target: Sequence[float], mode: str) -> bool:
    if not candidate or len(candidate) < 4 or len(target) < 4:
        return False
    ax1, ay1, ax2, ay2 = [float(value) for value in candidate[:4]]
    bx1, by1, bx2, by2 = [float(value) for value in target[:4]]
    aminx, amaxx = sorted([ax1, ax2])
    aminy, amaxy = sorted([ay1, ay2])
    bminx, bmaxx = sorted([bx1, bx2])
    bminy, bmaxy = sorted([by1, by2])
    if mode == "within":
        return aminx >= bminx and amaxx <= bmaxx and aminy >= bminy and amaxy <= bmaxy
    if mode == "contains":
        return aminx <= bminx and amaxx >= bmaxx and aminy <= bminy and amaxy >= bmaxy
    return not (amaxx < bminx or aminx > bmaxx or amaxy < bminy or aminy > bmaxy)


def _dxf_bbox_query_payload(
    *,
    bbox: Sequence[float],
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    entity_type: Sequence[str] | str | None = None,
    layer: str = "",
    mode: str = "intersects",
    fields: Sequence[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    active_pack_ids = _dxf_request_pack_ids(project_id=project_id, pack_id=pack_id, pack_ids=pack_ids)
    entity_types = _entity_type_filter(entity_type)
    rows: list[dict] = []
    total = 0
    select_fields = fields or _dxf_entity_default_fields()
    for row, root, source_pack_id in _iter_dxf_entity_rows_for_request(active_pack_ids):
        matched, score = _dxf_row_matches_basic_filters(
            row,
            sheet_no=sheet_no,
            source_file=source_file,
            entity_types=entity_types,
            layer=layer,
        )
        if not matched:
            continue
        payload = _dxf_payload_for_match(row, root, source_pack_id, score=score)
        if not _bbox_relation(payload.get("bbox"), bbox, mode):
            continue
        total += 1
        rows.append(_project_payload(payload, select_fields))
    start = max(0, offset)
    end = start + max(0, limit)
    return {
        "project_id": project_id or None,
        "pack_id": pack_id or None,
        "sheet_no": sheet_no or None,
        "source_file": source_file or None,
        "entity_type": sorted(entity_types) if entity_types else None,
        "bbox": list(bbox),
        "mode": mode,
        "count": total,
        "offset": start,
        "limit": max(0, limit),
        "rows": rows[start:end],
        "truncated": total > end,
    }


def _query_chunk_rows_payload(
    *,
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    project_id: str = "",
    logical_pack: str,
    where: dict | None = None,
    select: Sequence | None = None,
    order_by: Sequence | None = None,
    limit: int = 100,
    offset: int = 0,
    include_sibling_shards: bool = True,
) -> dict:
    active_pack_ids = _resolve_pack_ids(
        pack_id=pack_id,
        pack_ids=pack_ids,
        project_id=project_id,
        logical_pack=logical_pack,
        include_sibling_shards=include_sibling_shards,
    )
    rows: list[dict] = []
    for active_pack_id in active_pack_ids:
        for row in _iter_cloud_chunk_rows(active_pack_id):
            if _row_matches_where(row, where):
                rows.append(row)
    ordered = _sort_chunk_rows(rows, order_by)
    start = max(0, offset)
    end = start + max(0, limit)
    return {
        "project_id": project_id or None,
        "pack_ids": active_pack_ids,
        "logical_pack": logical_pack,
        "where": _normalize_tool_dict(where),
        "count": len(ordered),
        "offset": start,
        "limit": max(0, limit),
        "rows": [_project_row(row, select) for row in ordered[start:end]],
        "truncated": len(ordered) > end,
    }


def _bbox_from_row(row: dict) -> list[float] | None:
    compact = row.get("compact_row") if isinstance(row.get("compact_row"), dict) else {}
    drawing_ref = compact.get("drawing_ref") if isinstance(compact.get("drawing_ref"), dict) else {}
    candidates = [
        compact.get("bbox"),
        compact.get("bounding_box"),
        drawing_ref.get("sheet_paper_bbox"),
        drawing_ref.get("sheet_model_bbox"),
        drawing_ref.get("view_model_bbox"),
    ]
    for bbox in candidates:
        if isinstance(bbox, list) and len(bbox) >= 4:
            try:
                return [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
            except (TypeError, ValueError):
                continue
        if isinstance(bbox, dict):
            minimum = bbox.get("min") if isinstance(bbox.get("min"), dict) else {}
            maximum = bbox.get("max") if isinstance(bbox.get("max"), dict) else {}
            try:
                return [float(minimum["x"]), float(minimum["y"]), float(maximum["x"]), float(maximum["y"])]
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _classify_drawing_text_type(row: dict) -> str:
    evidence_type = str(_row_get(row, "evidence_type") or "")
    text = str(row.get("text") or "")
    if "dimension" in evidence_type:
        return "dimension"
    if "schedule" in evidence_type or evidence_type in {"data_row", "column_header", "schedule_title"}:
        return "schedule_cell"
    if "annotation" in evidence_type:
        if _AREA_TAG_RE.search(text):
            return "room_tag"
        return "annotation"
    if evidence_type in {"view_context", "sheet_pdf", "sheet_dxf", "view_dxf"}:
        return evidence_type
    if _AREA_TAG_RE.search(text):
        return "room_tag"
    return "drawing_evidence"


def _drawing_text_search_payload(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    query: str = "",
    sheet_no: str = "",
    view_name: str = "",
    text_type: Sequence[str] | str | None = None,
    limit: int = 50,
) -> dict:
    active_pack_ids = _resolve_pack_ids(
        pack_id=pack_id,
        pack_ids=pack_ids,
        project_id=project_id,
        logical_pack="drawing_evidence",
    )
    type_filter = {str(item).casefold() for item in _normalize_tool_list(text_type)}
    terms = query_terms(query)
    scored: list[tuple[float, dict]] = []
    for active_pack_id in active_pack_ids:
        for row in _iter_cloud_chunk_rows(active_pack_id):
            candidate_type = _classify_drawing_text_type(row)
            if type_filter and candidate_type.casefold() not in type_filter and str(_row_get(row, "evidence_type")).casefold() not in type_filter:
                continue
            if sheet_no and str(_row_get(row, "sheet_number") or "").casefold() != str(sheet_no).casefold():
                continue
            if view_name and str(view_name).casefold() not in str(_row_get(row, "view_name") or "").casefold():
                continue
            haystack = (
                str(row.get("text") or "")
                + " "
                + str(_row_get(row, "sheet_number") or "")
                + " "
                + str(_row_get(row, "view_name") or "")
                + " "
                + str(_row_get(row, "category_name") or "")
            ).casefold()
            score = score_terms(haystack, terms) if terms else 1.0
            if terms and score <= 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "sheet_no": _row_get(row, "sheet_number") if _row_get(row, "sheet_number") is not _MISSING else None,
                        "sheet_name": _row_get(row, "sheet_name") if _row_get(row, "sheet_name") is not _MISSING else None,
                        "view_name": _row_get(row, "view_name") if _row_get(row, "view_name") is not _MISSING else None,
                        "text": row.get("text"),
                        "text_type": candidate_type,
                        "evidence_type": _row_get(row, "evidence_type") if _row_get(row, "evidence_type") is not _MISSING else None,
                        "bbox": _bbox_from_row(row),
                        "source_pack_id": active_pack_id,
                        "source_chunk_id": row.get("chunk_id"),
                        "confidence": _row_get(row, "confidence") if _row_get(row, "confidence") is not _MISSING else None,
                        "score": round(score, 3),
                    },
                )
            )
    root_pack_ids: dict[Path, str] = {}
    for active_pack_id in active_pack_ids:
        derived_dir = _pack_source_derived_dir(active_pack_id)
        if not derived_dir:
            continue
        root = derived_dir.parent
        if root.exists() and root not in root_pack_ids:
            root_pack_ids[root] = active_pack_id
    for root, source_pack_id in root_pack_ids.items():
        for row in _iter_drawing_entity_search_rows(root):
            candidate_type = _classify_drawing_entity_type(row)
            entity_type = str(row.get("entity_type") or row.get("record_type") or "")
            if type_filter and candidate_type.casefold() not in type_filter and entity_type.casefold() not in type_filter:
                continue
            context = _drawing_entity_context(row)
            if sheet_no and str(context.get("sheet_number") or "").casefold() != str(sheet_no).casefold():
                continue
            if view_name and str(view_name).casefold() not in str(context.get("view_name") or "").casefold():
                continue
            text = _drawing_entity_text(row)
            haystack = (
                text
                + " "
                + str(context.get("sheet_number") or "")
                + " "
                + str(context.get("sheet_name") or "")
                + " "
                + str(context.get("view_name") or "")
                + " "
                + str(row.get("layer") or "")
                + " "
                + entity_type
            ).casefold()
            score = score_terms(haystack, terms) if terms else 1.0
            if terms and score <= 0:
                continue
            if terms:
                query_lower = str(query).casefold()
                text_lower = text.casefold()
                if query_lower and text_lower == query_lower:
                    score += 10.0
                elif query_lower and query_lower in text_lower:
                    score += 5.0
            if candidate_type in {"annotation", "room_tag", "dimension"}:
                score += 2.0
            scored.append((score, _drawing_entity_payload(row, root, source_pack_id, score=score)))
    scored.sort(key=lambda item: item[0], reverse=True)
    max_limit = max(0, limit)
    return {
        "project_id": project_id or None,
        "pack_ids": active_pack_ids,
        "query": query,
        "count": len(scored),
        "matches": [item[1] for item in scored[:max_limit]],
        "truncated": len(scored) > max_limit,
    }


def _dimension_value_mm(text: str) -> float | None:
    values = []
    for match in _DIMENSION_VALUE_RE.finditer(text.replace(",", "")):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if 50 <= value <= 20000:
            values.append(value)
    return values[0] if values else None


def _bbox_orientation(bbox: list[float] | None) -> str | None:
    if not bbox or len(bbox) < 4:
        return None
    width = abs(float(bbox[2]) - float(bbox[0]))
    height = abs(float(bbox[3]) - float(bbox[1]))
    if width <= 0 and height <= 0:
        return None
    if width >= height * 1.5:
        return "horizontal"
    if height >= width * 1.5:
        return "vertical"
    return "unknown"


def _dimensions_extract_payload(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    view_name: str = "",
    target_label: str = "",
    limit: int = 100,
) -> dict:
    label_search = (
        _drawing_text_search_payload(
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            query=target_label,
            sheet_no=sheet_no,
            view_name=view_name,
            text_type=["room_tag", "annotation", "drawing_evidence"],
            limit=25,
        )
        if target_label
        else {"matches": []}
    )
    nearby_label_texts = [
        str(match.get("text") or "")
        for match in label_search.get("matches", [])
        if str(match.get("text") or "").strip()
    ]
    search = _drawing_text_search_payload(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        query="",
        sheet_no=sheet_no,
        view_name=view_name,
        text_type=["dimension"],
        limit=max(500, limit * 6),
    )
    dimensions = []
    for match in search.get("matches", []):
        text = str(match.get("text") or "")
        value_mm = _to_number(match.get("value_mm"))
        if value_mm is None:
            value_mm = _dimension_value_mm(text)
        if value_mm is None:
            continue
        bbox = match.get("bbox")
        dimensions.append(
            {
                "text": text,
                "value_mm": value_mm,
                "unit": "mm",
                "orientation": match.get("orientation") or _bbox_orientation(bbox) or "unknown",
                "bbox": bbox,
                "line": match.get("line"),
                "nearby_labels": nearby_label_texts[:5],
                "source_pack_id": match.get("source_pack_id"),
                "source_chunk_id": match.get("source_chunk_id"),
                "confidence": match.get("confidence") or 0.55,
            }
        )
        if len(dimensions) >= max(0, limit):
            break
    return {
        "project_id": project_id or None,
        "sheet_no": sheet_no or None,
        "view_name": view_name or None,
        "target_label": target_label or None,
        "dimension_count": len(dimensions),
        "dimensions": dimensions,
        "truncated": len(dimensions) >= max(0, limit),
    }


def _calculate_area_from_dimensions_payload(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    view_name: str = "",
    target_label: str = "",
    method: str = "rectangular_area",
) -> dict:
    extracted = _dimensions_extract_payload(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        sheet_no=sheet_no,
        view_name=view_name,
        target_label=target_label,
        limit=50,
    )
    dimensions = extracted.get("dimensions", [])
    if len(dimensions) < 2:
        return {
            "target": target_label,
            "method": method,
            "status": "insufficient_dimension_evidence",
            "reason": "At least two usable dimension values are required for rectangular area calculation.",
            "dimensions": dimensions,
        }
    horizontal = [dim for dim in dimensions if dim.get("orientation") == "horizontal"]
    vertical = [dim for dim in dimensions if dim.get("orientation") == "vertical"]
    if horizontal and vertical:
        width = float(horizontal[0]["value_mm"])
        depth = float(vertical[0]["value_mm"])
        evidence = [horizontal[0], vertical[0]]
    else:
        ordered = sorted(dimensions, key=lambda dim: float(dim.get("confidence") or 0), reverse=True)
        width = float(ordered[0]["value_mm"])
        depth = float(ordered[1]["value_mm"])
        evidence = ordered[:2]
    area_m2 = width * depth / 1_000_000.0
    return {
        "target": target_label,
        "method": "dimension_line_rectangular_area",
        "width_mm": width,
        "depth_mm": depth,
        "area_m2": round(area_m2, 6),
        "formula": f"{width:g} mm × {depth:g} mm / 1,000,000 = {area_m2:.3f} m²",
        "basis": "dimension_text_heuristic",
        "confidence": min(float(item.get("confidence") or 0.55) for item in evidence),
        "evidence": evidence,
        "warning": "Dimension pairing is heuristic until DXF dimension geometry and room boundary topology are fully indexed.",
    }


def _room_area_tags_payload(
    *,
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    floor: str = "",
    room_name: str = "",
    sheet_no: str = "",
    limit: int = 50,
) -> dict:
    query = room_name or "㎡"
    search = _drawing_text_search_payload(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        query=query,
        sheet_no=sheet_no,
        text_type=["room_tag", "annotation", "schedule_cell", "drawing_evidence"],
        limit=max(200, limit * 4),
    )
    rooms = []
    for match in search.get("matches", []):
        text = str(match.get("text") or "")
        tag_match = _AREA_TAG_RE.search(text)
        if not tag_match:
            continue
        label = tag_match.group("label").strip(" -_|")
        if room_name and room_name.casefold() not in label.casefold() and room_name.casefold() not in text.casefold():
            continue
        try:
            area_m2 = float(tag_match.group("area"))
        except ValueError:
            continue
        rooms.append(
            {
                "room_name": room_name or label,
                "floor": floor or None,
                "module_id": None,
                "area_m2": area_m2,
                "raw_text": text,
                "source": {
                    "sheet_no": match.get("sheet_no"),
                    "sheet_name": match.get("sheet_name"),
                    "view_name": match.get("view_name"),
                    "bbox": match.get("bbox"),
                    "pack_id": match.get("source_pack_id"),
                    "chunk_id": match.get("source_chunk_id"),
                },
                "confidence": match.get("confidence") or 0.72,
            }
        )
        if len(rooms) >= max(0, limit):
            break
    return {
        "project_id": project_id or None,
        "room_name": room_name or None,
        "floor": floor or None,
        "sheet_no": sheet_no or None,
        "count": len(rooms),
        "rooms": rooms,
        "truncated": len(rooms) >= max(0, limit),
        "note": "This extracts explicit room-area text such as '화장실 3.42㎡'; adjacent tag pairing is a later geometry step.",
    }


def _schedule_table_payload(
    *,
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    project_id: str = "",
    schedule_name: str = "",
    limit: int = 5000,
) -> dict:
    active_pack_ids = _resolve_pack_ids(
        pack_id=pack_id,
        pack_ids=pack_ids,
        project_id=project_id,
        logical_pack="schedule_rows",
    )
    rows = []
    for active_pack_id in active_pack_ids:
        for row in _iter_cloud_chunk_rows(active_pack_id):
            if schedule_name and schedule_name.casefold() not in str(_row_get(row, "schedule_name") or "").casefold():
                continue
            rows.append(row)
    rows = _sort_chunk_rows(rows, [{"field": "schedule_name"}, {"field": "row"}])
    columns: list[str] = []
    data_rows = []
    for row in rows:
        compact = row.get("compact_row") if isinstance(row.get("compact_row"), dict) else {}
        values = compact.get("values") if isinstance(compact.get("values"), list) else []
        texts = [str(item.get("text") or "") for item in values if isinstance(item, dict)]
        role = str(compact.get("row_role") or _row_get(row, "row_role") or "")
        if role == "column_header" and texts:
            columns = [text for text in texts if text]
            continue
        if role == "data_row" and texts:
            if columns and len(columns) == len(texts):
                data = {column: value for column, value in zip(columns, texts)}
            else:
                data = {f"column_{index}": value for index, value in enumerate(texts)}
            data_rows.append(
                {
                    **data,
                    "_row": compact.get("row"),
                    "_text": row.get("text"),
                    "_source_pack_id": active_pack_id,
                    "_source_chunk_id": row.get("chunk_id"),
                }
            )
    max_limit = max(0, limit)
    return {
        "project_id": project_id or None,
        "pack_ids": active_pack_ids,
        "schedule_name": schedule_name or (str(_row_get(rows[0], "schedule_name")) if rows else None),
        "columns": columns,
        "row_count": len(data_rows),
        "rows": data_rows[:max_limit],
        "truncated": len(data_rows) > max_limit,
    }


def _tool_payload_error(message: str, **extra: object) -> str:
    return _json({"error": "invalid_request", "detail": message, **extra}, pretty=False)


def _pack_overview_payload(pack_id: str, schema_node_limit: int = 300, schema_edge_limit: int = 600) -> dict:
    packs_by_id = _visible_pack_by_id()
    pack = packs_by_id.get(pack_id)
    if not pack:
        return {"error": "not_found", "kind": "pack", "id": pack_id}

    documents = read_pack_documents(pack_id=pack_id, prefix="", suffix="", limit=24)
    sources = _filter_sources(read_sources(pack_id=pack_id))
    schema = _pack_schema_payload(pack_id=pack_id, node_limit=schema_node_limit, edge_limit=schema_edge_limit)
    entrypoints = [
        "mo_ontology_manifest",
        "mo_pack_schema",
        "mo_node_type_list",
        "mo_relation_type_list",
        "mo_evidence_search",
        "mo_evidence_trace",
        "mo_anchor_coverage",
        "mo_anchor_resolve",
        "mo_question_answer",
        "mo_graph_get",
        "mo_node_search",
    ]
    if any(item.get("type") == "Module" for item in schema.get("nodeTypes", [])):
        entrypoints.extend(["mo_module_list", "mo_module_get"])
    if any(item.get("type") == "Assembly" for item in schema.get("nodeTypes", [])):
        entrypoints.extend(["mo_assembly_list", "mo_assembly_get"])

    return {
        "pack": pack,
        "schema": schema,
        "sources": sources,
        "documents": documents,
        "recommendedTools": entrypoints,
    }


def _project_pack_ids(project: dict) -> list[str]:
    return [pack_id for pack_id in project.get("packIds", []) if isinstance(pack_id, str)]


def _project_pack_list_payload(project_id: str = "", include_empty: bool = True) -> dict:
    projects_by_id = _visible_project_by_id()
    packs_by_id = _visible_pack_by_id()
    if project_id:
        projects = [projects_by_id[project_id]] if project_id in projects_by_id else []
    else:
        projects = list(projects_by_id.values())

    assigned_pack_ids: set[str] = set()
    project_items = []
    for project in projects:
        pack_ids = _project_pack_ids(project)
        assigned_pack_ids.update(pack_ids)
        packs = [packs_by_id[pack_id] for pack_id in pack_ids if pack_id in packs_by_id]
        if packs or include_empty:
            project_items.append(
                {
                    "project": project,
                    "packCount": len(packs),
                    "packs": packs,
                    "recommendedNext": "For a project question use mo_project_ask first; use mo_project_overview or mo_project_search for exploration and mo_pack_overview only for pack-level inspection.",
                }
            )

    unassigned_packs = [
        pack
        for pack_id, pack in packs_by_id.items()
        if pack_id not in assigned_pack_ids
    ]
    return {
        "mode": "project-first",
        "guidance": "Load ontology data by project first. Packs are related evidence slices inside a project, not the primary user choice.",
        "projectId": project_id or None,
        "projectCount": len(project_items),
        "packCount": sum(item["packCount"] for item in project_items),
        "projects": project_items,
        "unassignedPacks": unassigned_packs,
        "tools": {
            "projectAsk": "mo_project_ask",
            "projectOverview": "mo_project_overview",
            "projectSearch": "mo_project_search",
            "packOverview": "mo_pack_overview",
        },
    }


def _project_overview_payload(
    project_id: str = "",
    schema_node_limit: int = 160,
    schema_edge_limit: int = 320,
) -> dict:
    projects_by_id = _visible_project_by_id()
    packs_by_id = _visible_pack_by_id()
    if project_id:
        projects = [projects_by_id[project_id]] if project_id in projects_by_id else []
    else:
        projects = list(projects_by_id.values())

    return {
        "projectId": project_id or None,
        "count": len(projects),
        "projects": [
            {
                "project": project,
                "packs": [
                    {
                        "pack": packs_by_id.get(pack_id, {"id": pack_id, "missing": True}),
                        "schema": (
                            _pack_schema_payload(
                                pack_id=pack_id,
                                node_limit=schema_node_limit,
                                edge_limit=schema_edge_limit,
                            )
                            if pack_id in packs_by_id and _pack_is_visible(pack_id)
                            else None
                        ),
                    }
                    for pack_id in _project_pack_ids(project)
                ],
            }
            for project in projects
        ],
    }


def _project_search_scope(project_id: str) -> tuple[dict[str, dict], list[str]]:
    projects_by_id = _visible_project_by_id()
    packs_by_id = _visible_pack_by_id()
    projects = [projects_by_id[project_id]] if project_id else list(projects_by_id.values())
    pack_ids = [
        pack_id
        for project in projects
        for pack_id in _project_pack_ids(project)
        if pack_id in packs_by_id and _pack_is_visible(pack_id)
    ]
    unique_pack_ids = list(dict.fromkeys(pack_ids))
    return packs_by_id, unique_pack_ids


def _project_pack_projection(pack: dict) -> dict:
    title = str(pack.get("title") or pack.get("displayName") or pack.get("id") or "")
    return {
        "id": pack.get("id"),
        "title": title,
        "displayName": pack.get("displayName") or title,
        "commonScoped": bool(pack.get("commonScoped", False)),
    }


def _legacy_project_search_payload(project_id: str, search_text: str, limit_per_pack: int = 3) -> dict:
    packs_by_id, unique_pack_ids = _project_search_scope(project_id)
    results = []
    for pack_id in unique_pack_ids:
        matches = search_indexed_documents(pack_id, search_text, limit=max(0, limit_per_pack))
        if not matches:
            matches = search_pack_evidence(pack_id, search_text, limit=max(0, limit_per_pack))
        results.append({"pack": packs_by_id[pack_id], "matches": matches})
    return {
        "projectId": project_id or None,
        "query": search_text,
        "packCount": len(unique_pack_ids),
        "matchCount": sum(len(item["matches"]) for item in results),
        "results": results,
    }


def _fast_project_search_payload(project_id: str, search_text: str, limit_per_pack: int = 3) -> dict:
    started = time.perf_counter()
    packs_by_id, unique_pack_ids = _project_search_scope(project_id)
    limit_per_pack = max(0, int(limit_per_pack))

    db_started = time.perf_counter()
    indexed_pack_ids = pack_ids_with_documents()
    indexed_scope = [pack_id for pack_id in unique_pack_ids if pack_id in indexed_pack_ids]
    requested_mode = str(env("MODULAR_ONTOLOGY_PROJECT_SEARCH_MODE", "bm25") or "bm25").strip().casefold()
    bm25_status = bm25_index_status() if requested_mode != "lexical" else {"available": False, "ready": False}
    matches_by_pack = None
    retrieval_mode = "lexical"
    lexical_fallback = False
    if bm25_status.get("ready"):
        matches_by_pack = search_documents_bm25_multi(
            indexed_scope,
            search_text,
            limit_per_pack=limit_per_pack,
        )
        if matches_by_pack is not None:
            retrieval_mode = "bm25"
            if not any(matches_by_pack.values()):
                lexical_fallback = True
                retrieval_mode = "bm25+lexical-fallback"
                matches_by_pack = search_documents_multi(
                    indexed_scope,
                    search_text,
                    limit_per_pack=limit_per_pack,
                )
    if matches_by_pack is None:
        lexical_fallback = requested_mode != "lexical"
        retrieval_mode = "lexical-fallback" if lexical_fallback else "lexical"
        matches_by_pack = search_documents_multi(
            indexed_scope,
            search_text,
            limit_per_pack=limit_per_pack,
        )
    db_ms = (time.perf_counter() - db_started) * 1000

    fallback_started = time.perf_counter()
    fallback_candidates = [pack_id for pack_id in unique_pack_ids if pack_id not in indexed_pack_ids]
    fallback_pack_count = 0
    if limit_per_pack > 0:
        deadline = fallback_started + _PROJECT_FALLBACK_BUDGET_SECONDS
        for pack_id in fallback_candidates:
            if fallback_pack_count >= _PROJECT_FALLBACK_MAX_PACKS or time.perf_counter() >= deadline:
                break
            matches_by_pack[pack_id] = search_pack_evidence(pack_id, search_text, limit=limit_per_pack)
            fallback_pack_count += 1
    fallback_ms = (time.perf_counter() - fallback_started) * 1000
    skipped_fallback_packs = max(0, len(fallback_candidates) - fallback_pack_count)

    all_matched_results = [
        {
            "pack": _project_pack_projection(packs_by_id[pack_id]),
            "matches": matches_by_pack.get(pack_id, []),
        }
        for pack_id in unique_pack_ids
        if matches_by_pack.get(pack_id)
    ]
    ranked_matches = sorted(
        (
            (float(match.get("score") or 0.0), result["pack"], match)
            for result in all_matched_results
            for match in result["matches"]
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    selected_by_pack: dict[str, dict] = {}
    for _, pack, match in ranked_matches[:_PROJECT_SEARCH_MAX_MATCHES]:
        pack_id = str(pack["id"])
        selected_by_pack.setdefault(pack_id, {"pack": pack, "matches": []})["matches"].append(match)
    matched_results = list(selected_by_pack.values())
    match_count = len(ranked_matches)
    returned_match_count = min(match_count, _PROJECT_SEARCH_MAX_MATCHES)
    if _env_enabled("MODULAR_ONTOLOGY_PROJECT_SEARCH_FULL", default=False):
        results = [
            {"pack": packs_by_id[pack_id], "matches": matches_by_pack.get(pack_id, [])}
            for pack_id in unique_pack_ids
        ]
        returned_match_count = match_count
    else:
        results = matched_results

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "projectId": project_id or None,
        "query": search_text,
        "packCount": len(unique_pack_ids),
        "matchedPackCount": len(all_matched_results),
        "matchCount": match_count,
        "returnedMatchCount": returned_match_count,
        "truncated": returned_match_count < match_count,
        "results": results,
        "retrieval": {
            "mode": retrieval_mode,
            "scannedPacks": len(unique_pack_ids),
            "indexedPacks": len(indexed_scope),
            "fallbackPacks": fallback_pack_count,
            "candidateCount": match_count,
            "returnedCandidateCount": returned_match_count,
            "bm25Available": bool(bm25_status.get("available")),
            "bm25Ready": bool(bm25_status.get("ready")),
            "bm25IndexedDocuments": int(bm25_status.get("indexedDocumentCount") or 0),
            "lexicalFallback": lexical_fallback,
            "partial": skipped_fallback_packs > 0,
            "skippedFallbackPacks": skipped_fallback_packs,
            "dbMs": round(db_ms, 3),
            "fallbackMs": round(fallback_ms, 3),
            "elapsedMs": round(elapsed_ms, 3),
        },
    }


def _project_search_payload(project_id: str, search_text: str, limit_per_pack: int = 3) -> dict:
    if not _env_enabled("MODULAR_ONTOLOGY_FAST_PROJECT_SEARCH", default=True):
        return _legacy_project_search_payload(project_id, search_text, limit_per_pack)
    return _fast_project_search_payload(project_id, search_text, limit_per_pack)


def _project_tool_response(tool: str, project_id: str, payload: dict) -> str:
    response = _json(payload, pretty=False)
    retrieval = payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else {}
    _LOGGER.info(
        "tool=%s project_id=%s total_ms=%s db_ms=%s fallback_ms=%s "
        "fallback_pack_count=%s candidate_count=%s result_bytes=%s",
        tool,
        project_id or "all",
        retrieval.get("elapsedMs", 0),
        retrieval.get("dbMs", 0),
        retrieval.get("fallbackMs", 0),
        retrieval.get("fallbackPacks", 0),
        retrieval.get("candidateCount", 0),
        len(response.encode("utf-8")),
    )
    return response


def _project_ask_payload(project_id: str, question: str, top_k: int = 8) -> dict:
    started = time.perf_counter()
    bounded_top_k = max(1, min(20, int(top_k)))
    search_payload = _fast_project_search_payload(project_id, question, limit_per_pack=3)
    candidates = []
    for result in search_payload["results"]:
        pack = result["pack"]
        for match in result["matches"]:
            path = str(match.get("path") or "")
            document_id = str(match.get("chunkId") or f"{pack['id']}:{path}")
            score = float(match.get("score") or 0.0)
            retrieval_source = str(match.get("retrievalSource") or "lexical")
            candidates.append(
                {
                    "packId": pack["id"],
                    "documentId": document_id,
                    "path": path,
                    "title": match.get("title"),
                    "snippet": match.get("snippet", ""),
                    "score": score,
                    "signals": {retrieval_source: score},
                }
            )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    evidence = candidates[:bounded_top_k]
    retrieval = dict(search_payload["retrieval"])
    retrieval["elapsedMs"] = round((time.perf_counter() - started) * 1000, 3)
    return {
        "status": "evidence_ready" if evidence else "no_answer",
        "projectId": project_id,
        "answerMode": "client_synthesis",
        "evidence": evidence,
        "retrieval": retrieval,
    }


def _evidence_trace_payload(pack_id: str, search_text: str, limit: int = 5, include_answer: bool = False) -> dict:
    evidence = search_pack_evidence(pack_id, search_text, limit=max(0, limit))
    nodes = search_graph_nodes(pack_id=pack_id, query=search_text, limit=max(0, limit))
    payload = {
        "packId": pack_id,
        "query": search_text,
        "evidence": evidence,
        "nodes": nodes,
    }
    if include_answer:
        payload["answer"] = answer_pack_question(pack_id, search_text, limit=max(1, limit), use_openai=False)
    return payload


@mcp.tool()
def copycrab_status() -> str:
    """Return MCP server, pack, project, and tool status for CopyCrab/Modular Ontology."""

    if not _mcp_authorized():
        return _json(
            {
                "status": "unauthorized",
                "server": "Modular Ontology MCP",
                "detail": "Invalid MCP user URL token.",
                "tools": [],
                "canonicalTools": [],
            }
        )
    packs = _filter_pack_list(read_packs())
    projects = _visible_projects()
    scope = _mcp_scope()
    visible_tool_names = mcp.visible_tool_names()
    visible_tool_name_set = set(visible_tool_names)
    return _json(
        {
            "status": "ok",
            "server": "Modular Ontology MCP",
            "company": _mcp_company() or "all",
            "userEmail": scope.get("userEmail") or "",
            "pack_count": len(packs),
            "project_count": len(projects),
            "toolProfile": mcp.tool_profile,
            "tools": visible_tool_names,
            "tool_count": len(visible_tool_names),
            "canonicalTools": [name for name in TOOL_NAMES if name in visible_tool_name_set],
            "implementedCanonicalToolCount": len(TOOL_NAMES),
            "toolNaming": _tool_manifest_payload()["naming"],
        }
    )


@mcp.tool()
def list_projects() -> str:
    """List BIM projects and the ontology packs attached to each project."""

    return _json(_visible_projects())


@mcp.tool()
def list_packs() -> str:
    """List available ontology ZIP packs with counts and validation status."""

    return _json(_filter_pack_list(read_packs()))


@mcp.tool()
def search_packs(query: str = "", limit: int = 20) -> str:
    """Search ontology packs by id, title, source, or description."""

    return _json(_filter_pack_list(search_pack_catalog(query=query, limit=limit)))


@mcp.tool()
def list_sources(pack_id: str | None = None) -> str:
    """List pack/source summaries and entrypoints."""

    if pack_id and not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(_filter_sources(read_sources(pack_id=pack_id)))


@mcp.tool()
def list_pack_documents(pack_id: str, prefix: str = "documents/", suffix: str = ".md", limit: int = 200) -> str:
    """List evidence document paths directly from the ZIP pack, bypassing the search index."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_pack_documents(pack_id=pack_id, prefix=prefix, suffix=suffix, limit=limit))


@mcp.tool()
def read_pack_document(pack_id: str, path: str, max_chars: int = 12000) -> str:
    """Read a single evidence document directly from the ZIP pack by path."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_document(pack_id=pack_id, path=path, max_chars=max_chars))


@mcp.tool()
def read_chunk_by_id(pack_id: str, chunk_id: str) -> str:
    """Read one cloud/chunks.jsonl evidence chunk by chunk_id without returning the whole chunks file."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(_read_chunk_payload(pack_id=pack_id, chunk_id=chunk_id))


@mcp.tool()
def query_quantity_facts(
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    project_id: str = "",
    where: dict | None = None,
    select: Sequence[str] | None = None,
    order_by: Sequence | None = None,
    limit: int = 100,
    offset: int = 0,
    include_sibling_shards: bool = True,
) -> str:
    """Query BIM/Revit quantity_facts chunks with where/select/order_by support."""

    return _json(
        _query_chunk_rows_payload(
            pack_id=pack_id,
            pack_ids=pack_ids,
            project_id=project_id,
            logical_pack="quantity_facts",
            where=where,
            select=select,
            order_by=order_by,
            limit=limit,
            offset=offset,
            include_sibling_shards=include_sibling_shards,
        )
    )


@mcp.tool()
def search_drawing_text(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    query: str = "",
    sheet_no: str = "",
    view_name: str = "",
    text_type: Sequence[str] | str | None = None,
    limit: int = 50,
) -> str:
    """Search drawing_evidence chunks for sheet/view text, annotations, dimensions, and room-tag-like text."""

    return _json(
        _drawing_text_search_payload(
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            query=query,
            sheet_no=sheet_no,
            view_name=view_name,
            text_type=text_type,
            limit=limit,
        )
    )


@mcp.tool()
def extract_room_area_tags(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    floor: str = "",
    room_name: str = "",
    sheet_no: str = "",
    limit: int = 50,
) -> str:
    """Extract explicit room area text such as '화장실 3.42㎡' from drawing evidence chunks."""

    return _json(
        _room_area_tags_payload(
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            floor=floor,
            room_name=room_name,
            sheet_no=sheet_no,
            limit=limit,
        )
    )


@mcp.tool()
def extract_dimensions_from_view(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    view_name: str = "",
    target_label: str = "",
    limit: int = 100,
) -> str:
    """Extract usable dimension text values from drawing evidence for a sheet/view/target label."""

    return _json(
        _dimensions_extract_payload(
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            sheet_no=sheet_no,
            view_name=view_name,
            target_label=target_label,
            limit=limit,
        )
    )


@mcp.tool()
def calculate_area_from_dimensions(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    view_name: str = "",
    target_label: str = "",
    method: str = "rectangular_area",
) -> str:
    """Calculate rectangular area from two extracted dimension values when enough evidence exists."""

    return _json(
        _calculate_area_from_dimensions_payload(
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            sheet_no=sheet_no,
            view_name=view_name,
            target_label=target_label,
            method=method,
        )
    )


@mcp.tool()
def extract_schedule_table(
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    project_id: str = "",
    schedule_name: str = "",
    limit: int = 5000,
) -> str:
    """Reconstruct a schedule table from schedule_rows chunks."""

    return _json(
        _schedule_table_payload(
            pack_id=pack_id,
            pack_ids=pack_ids,
            project_id=project_id,
            schedule_name=schedule_name,
            limit=limit,
        )
    )


@mcp.tool()
def dxf_entity_summary(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    limit_layers: int = 50,
) -> str:
    """Summarize parsed DXF entity counts and layers, optionally filtered by sheet or DXF file."""

    return _json(
        _dxf_entity_summary_payload(
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            sheet_no=sheet_no,
            source_file=source_file,
            limit_layers=limit_layers,
        )
    )


@mcp.tool()
def dxf_entity_search(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    query: str = "",
    sheet_no: str = "",
    source_file: str = "",
    entity_type: Sequence[str] | str | None = None,
    layer: str = "",
    where: dict | None = None,
    fields: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Search raw parsed DXF entities such as LINE, LWPOLYLINE, TEXT, MTEXT, DIMENSION, INSERT, and HATCH."""

    return _json(
        _dxf_entity_search_payload(
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            query=query,
            sheet_no=sheet_no,
            source_file=source_file,
            entity_type=entity_type,
            layer=layer,
            where=where,
            fields=fields,
            limit=limit,
            offset=offset,
        )
    )


@mcp.tool()
def dxf_text_search(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    query: str = "",
    sheet_no: str = "",
    source_file: str = "",
    layer: str = "",
    fields: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Search raw DXF TEXT and MTEXT entities."""

    return dxf_entity_search(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        query=query,
        sheet_no=sheet_no,
        source_file=source_file,
        entity_type=["TEXT", "MTEXT"],
        layer=layer,
        fields=fields,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def dxf_dimension_search(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    layer: str = "",
    where: dict | None = None,
    fields: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Search raw DXF DIMENSION entities, including measurement value in millimeters when available."""

    return dxf_entity_search(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        sheet_no=sheet_no,
        source_file=source_file,
        entity_type=["DIMENSION"],
        layer=layer,
        where=where,
        fields=fields,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def dxf_layer_summary(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    limit: int = 100,
) -> str:
    """Summarize DXF layers with entity counts."""

    return _json(
        _dxf_layer_summary_payload(
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            sheet_no=sheet_no,
            source_file=source_file,
            limit=limit,
        )
    )


@mcp.tool()
def dxf_bbox_query(
    bbox: Sequence[float],
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    entity_type: Sequence[str] | str | None = None,
    layer: str = "",
    mode: str = "intersects",
    fields: Sequence[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Find DXF entities whose bounding boxes intersect, contain, or fall within a target bbox."""

    return _json(
        _dxf_bbox_query_payload(
            bbox=bbox,
            project_id=project_id,
            pack_id=pack_id,
            pack_ids=pack_ids,
            sheet_no=sheet_no,
            source_file=source_file,
            entity_type=entity_type,
            layer=layer,
            mode=mode,
            fields=fields,
            limit=limit,
            offset=offset,
        )
    )


@mcp.tool()
def get_graph(pack_id: str, max_nodes: int = 250, max_edges: int = 500) -> str:
    """Return a sampled ontology graph for a pack."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_graph(pack_id, max_nodes=max_nodes, max_edges=max_edges), pretty=False)


@mcp.tool()
def list_nodes(pack_id: str, node_type: str | None = None, limit: int = 100) -> str:
    """List graph nodes, optionally filtered by node type."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_nodes(pack_id=pack_id, node_type=node_type, limit=limit))


@mcp.tool()
def search_nodes(
    pack_id: str,
    query: str,
    limit: int = 20,
    fields: Sequence[str] | None = None,
    include_heavy_fields: bool = False,
) -> str:
    """Search graph nodes by id, label, and properties."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    payload = search_graph_nodes(pack_id=pack_id, query=query, limit=limit)
    if fields:
        payload = {
            **payload,
            "nodes": [
                _project_node(node, fields, include_heavy_fields=include_heavy_fields)
                for node in payload.get("nodes", [])
                if isinstance(node, dict)
            ],
            "projection": {"fields": list(fields), "includeHeavyFields": include_heavy_fields},
        }
    return _json(payload)


@mcp.tool()
def mo_filtered_search_nodes(
    pack_id: str,
    node_type: str | None = None,
    where: dict | None = None,
    fields: Sequence[str] | None = None,
    order_by: Sequence | None = None,
    limit: int = 1000,
    offset: int = 0,
    include_heavy_fields: bool = False,
) -> str:
    """Filter graph nodes by exact property conditions and return only requested fields."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    graph, rows = _filtered_rows(
        pack_id,
        node_type=node_type,
        where=where,
        fields=fields,
        include_heavy_fields=include_heavy_fields,
    )
    ordered = _sort_rows(rows, order_by)
    start = max(0, offset)
    end = start + max(0, limit)
    return _json(
        {
            "pack_id": graph["pack"]["id"],
            "node_type": node_type,
            "where": _normalize_tool_dict(where),
            "count": len(ordered),
            "offset": start,
            "limit": max(0, limit),
            "rows": ordered[start:end],
            "truncated": len(ordered) > end,
            "projection": {
                "fields": list(_normalize_tool_list(fields) or _DEFAULT_NODE_FIELDS),
                "includeHeavyFields": include_heavy_fields,
            },
        }
    )


@mcp.tool()
def mo_distinct_property_values(
    pack_id: str,
    property: str,
    node_type: str | None = None,
    where: dict | None = None,
    order: str = "asc",
    limit: int = 1000,
) -> str:
    """Return distinct values for one node property, with optional node type and where filters."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    graph, nodes = _filtered_nodes(pack_id, node_type=node_type, where=where)
    seen = set()
    values: list[object] = []
    for node in nodes:
        value = _node_field(node, property)
        if value is _MISSING or value is None:
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            key = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                values.append(candidate)
    ordered = _sort_values(values, order=order)
    max_limit = max(0, limit)
    return _json(
        {
            "pack_id": graph["pack"]["id"],
            "node_type": node_type,
            "property": property,
            "where": _normalize_tool_dict(where),
            "count": len(ordered),
            "values": ordered[:max_limit],
            "truncated": len(ordered) > max_limit,
        }
    )


@mcp.tool()
def mo_aggregate_nodes(
    pack_id: str,
    node_type: str | None = None,
    where: dict | None = None,
    group_by: Sequence[str] | None = None,
    metrics: Sequence | None = None,
    order_by: Sequence | None = None,
    limit: int = 5000,
    offset: int = 0,
) -> str:
    """Aggregate graph nodes with server-side group_by and metrics such as sum/count/avg."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    graph, nodes = _filtered_nodes(pack_id, node_type=node_type, where=where)
    rows, diagnostics = _aggregate_filtered_nodes(nodes, group_by=group_by, metrics=metrics)
    ordered = _sort_rows(rows, order_by)
    start = max(0, offset)
    end = start + max(0, limit)
    metric_names = [_metric_name(metric if isinstance(metric, dict) else {"agg": str(metric)}) for metric in _normalize_tool_list(metrics)]
    return _json(
        {
            "pack_id": graph["pack"]["id"],
            "node_type": node_type,
            "where": _normalize_tool_dict(where),
            "group_by": list(_normalize_tool_list(group_by)),
            "metrics": metric_names,
            "input_count": len(nodes),
            "count": len(ordered),
            "offset": start,
            "limit": max(0, limit),
            "rows": ordered[start:end],
            "truncated": len(ordered) > end,
            **diagnostics,
        }
    )


@mcp.tool()
def mo_query_quantity_evidence(
    pack_id: str,
    module_type: str = "*",
    work_category: str = "",
    item_name: str = "",
    unit: str = "",
    normalized_unit: str = "",
    include_fields: Sequence[str] | None = None,
    group_by: Sequence[str] | None = None,
    sum_field: str = "quantity",
    order_by: Sequence | None = None,
    include_item_regex: str = "",
    exclude_item_regex: str = "",
    exclude_items: Sequence[str] | None = None,
    limit: int = 5000,
) -> str:
    """Query BOQ/quantity evidence rows with common module, item, unit, and grouping options."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    where: dict[str, object] = {}
    if module_type and module_type != "*":
        where["module_type"] = {"wildcard": module_type} if "*" in module_type else {"eq": module_type}
    if work_category:
        where["work_category"] = {"eq": work_category}
    if item_name and item_name != "*":
        where["item_name"] = {"wildcard": item_name} if "*" in item_name else {"eq": item_name}
    if unit:
        where["unit"] = {"eq": unit}
    if normalized_unit:
        where["normalized_unit"] = {"eq": normalized_unit}

    graph, nodes = _filtered_nodes(pack_id, node_type="Element", where=where)
    excluded = set(str(item) for item in _normalize_tool_list(exclude_items))
    if include_item_regex:
        nodes = [node for node in nodes if re.search(include_item_regex, str(_node_field(node, "item_name")))]
    if exclude_item_regex:
        nodes = [node for node in nodes if not re.search(exclude_item_regex, str(_node_field(node, "item_name")))]
    if excluded:
        nodes = [node for node in nodes if str(_node_field(node, "item_name")) not in excluded]

    fields = include_fields or [
        "id",
        "module_type",
        "work_category",
        "item_name",
        "specification",
        "quantity",
        "unit",
        "normalized_unit",
        "source_sheet",
        "source_row",
    ]
    if group_by:
        metric_specs = [
            {"field": sum_field, "agg": "sum", "as": f"{sum_field}_sum"},
            {"field": "source_row", "agg": "collect_set", "as": "source_rows"},
            {"agg": "count", "as": "row_count"},
        ]
        rows, diagnostics = _aggregate_filtered_nodes(nodes, group_by=group_by, metrics=metric_specs)
    else:
        rows = [_project_node(node, fields) for node in nodes]
        diagnostics = {"skipped_rows": {}}
    ordered = _sort_rows(rows, order_by)
    total = 0.0
    skipped_total = 0
    for node in nodes:
        number = _to_number(_node_field(node, sum_field))
        if number is None:
            skipped_total += 1
        else:
            total += number
    max_limit = max(0, limit)
    return _json(
        {
            "pack_id": graph["pack"]["id"],
            "query_type": "quantity_evidence",
            "where": where,
            "count": len(ordered),
            "rows": ordered[:max_limit],
            "truncated": len(ordered) > max_limit,
            "totals": {
                f"{sum_field}_sum": round(total, 6),
                "unit": unit or None,
                "normalized_unit": normalized_unit or None,
                "skipped_rows": skipped_total,
            },
            **diagnostics,
        }
    )


@mcp.tool()
def mo_aggregate_quantity_by_module(
    pack_id: str,
    work_category: str = "",
    unit: str = "",
    normalized_unit: str = "square_meter",
    item_filter: dict | None = None,
    group_by_item: bool = True,
    limit: int = 5000,
) -> str:
    """Aggregate quantity evidence by module, optionally nested by item/specification."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    where: dict[str, object] = {}
    if work_category:
        where["work_category"] = {"eq": work_category}
    if unit:
        where["unit"] = {"eq": unit}
    if normalized_unit:
        where["normalized_unit"] = {"eq": normalized_unit}
    filters = _normalize_tool_dict(item_filter)
    include_regex = str(filters.get("include_regex") or filters.get("includeRegex") or "")
    exclude_regex = str(filters.get("exclude_regex") or filters.get("excludeRegex") or "")
    graph, nodes = _filtered_nodes(pack_id, node_type="Element", where=where)
    if include_regex:
        nodes = [node for node in nodes if re.search(include_regex, str(_node_field(node, "item_name")))]
    if exclude_regex:
        nodes = [node for node in nodes if not re.search(exclude_regex, str(_node_field(node, "item_name")))]

    module_rows: dict[str, dict] = {}
    skipped_rows = 0
    for node in nodes:
        module_id = str(_node_field(node, "module_type") or "").strip()
        if not module_id:
            continue
        quantity = _to_number(_node_field(node, "quantity"))
        if quantity is None:
            skipped_rows += 1
            continue
        row = module_rows.setdefault(
            module_id,
            {**_parse_module_id(module_id), "total_area_m2": 0.0, "source_rows": set(), "items": {}},
        )
        row["total_area_m2"] += quantity
        source_row = _node_field(node, "source_row")
        if source_row is not _MISSING and source_row is not None:
            row["source_rows"].add(source_row)
        if group_by_item:
            item_key = (
                str(_node_field(node, "item_name") or ""),
                str(_node_field(node, "specification") or ""),
            )
            item = row["items"].setdefault(
                item_key,
                {
                    "item_name": item_key[0],
                    "specification": item_key[1],
                    "area_m2": 0.0,
                    "source_rows": set(),
                },
            )
            item["area_m2"] += quantity
            if source_row is not _MISSING and source_row is not None:
                item["source_rows"].add(source_row)

    rows = []
    for module_id, row in module_rows.items():
        items = []
        for item in row.pop("items").values():
            item["area_m2"] = round(item["area_m2"], 6)
            item["source_rows"] = _sort_values(list(item["source_rows"]))
            items.append(item)
        items = _sort_rows(items, [{"field": "item_name", "natural": True}])
        row["total_area_m2"] = round(row["total_area_m2"], 6)
        row["source_rows"] = _sort_values(list(row["source_rows"]))
        if group_by_item:
            row["items"] = items
        rows.append(row)
    rows = _sort_rows(rows, [{"field": "module_id", "natural": True}])
    max_limit = max(0, limit)
    return _json(
        {
            "pack_id": graph["pack"]["id"],
            "query_type": "quantity_by_module",
            "where": where,
            "count": len(rows),
            "rows": rows[:max_limit],
            "truncated": len(rows) > max_limit,
            "skipped_rows": skipped_rows,
        }
    )


@mcp.tool()
def mo_list_project_modules(
    project_id: str = "",
    pack_ids: Sequence[str] | None = None,
    module_pattern: str = _MODULE_PATTERN.pattern,
    include_sources: bool = True,
    limit: int = 1000,
) -> str:
    """List project modules using Module nodes, BOQ module_type, and BIM workset_name fallbacks."""

    if project_id and project_id not in _visible_project_by_id():
        return _not_found_payload("project", project_id)
    projects_by_id = _visible_project_by_id()
    packs_by_id = _visible_pack_by_id()
    if pack_ids:
        active_pack_ids = [pack_id for pack_id in _normalize_tool_list(pack_ids) if str(pack_id) in packs_by_id]
    elif project_id:
        active_pack_ids = [pack_id for pack_id in _project_pack_ids(projects_by_id[project_id]) if pack_id in packs_by_id]
    else:
        active_pack_ids = list(packs_by_id)

    modules: dict[str, dict] = {}
    for pack_id in active_pack_ids:
        for module_id, entry in _module_candidates_from_nodes(
            str(pack_id),
            module_pattern=module_pattern,
            include_sources=include_sources,
        ).items():
            target = modules.setdefault(module_id, {**_parse_module_id(module_id), "sources": []})
            if include_sources:
                for source in entry.get("sources", []):
                    if source not in target["sources"]:
                        target["sources"].append(source)

    rows = _sort_rows(list(modules.values()), [{"field": "module_id", "natural": True}])
    max_limit = max(0, limit)
    return _json(
        {
            "project_id": project_id or None,
            "pack_ids": active_pack_ids,
            "module_count": len(rows),
            "modules": rows[:max_limit],
            "truncated": len(rows) > max_limit,
        }
    )


@mcp.tool()
def mo_list_module_elements(
    pack_id: str,
    module_id: str,
    category: str = "",
    class_name: str = "",
    fields: Sequence[str] | None = None,
    limit: int = 5000,
) -> str:
    """List BIM element nodes that belong to one module via workset_name/module_id."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    where: dict[str, object] = {"workset_name": {"eq": module_id}}
    if category:
        where["category"] = {"eq": category}
    if class_name:
        where["class"] = {"eq": class_name}
    graph, rows = _filtered_rows(
        pack_id,
        node_type="Element",
        where=where,
        fields=fields
        or [
            "id",
            "source_element_id",
            "category",
            "class",
            "family_name",
            "family_and_type",
            "type_name",
            "workset_name",
            "level_id",
            "ifc_guid",
        ],
    )
    ordered = _sort_rows(rows, [{"field": "source_element_id", "natural": True}])
    max_limit = max(0, limit)
    return _json(
        {
            "pack_id": graph["pack"]["id"],
            "module_id": module_id,
            "category": category or None,
            "class": class_name or None,
            "count": len(ordered),
            "rows": ordered[:max_limit],
            "truncated": len(ordered) > max_limit,
        }
    )


def _join_side_rows(side: dict) -> tuple[str, str, list[dict]]:
    pack_id = str(side.get("pack_id") or "")
    if not pack_id:
        raise ValueError("join side requires pack_id")
    if not _pack_is_visible(pack_id):
        raise PermissionError(pack_id)
    join_field = str(side.get("join_field") or "")
    if not join_field:
        raise ValueError("join side requires join_field")
    raw_fields = side.get("fields") if isinstance(side.get("fields"), Sequence) and not isinstance(side.get("fields"), str) else None
    fields = list(raw_fields) if raw_fields else None
    if fields is not None and join_field not in fields:
        fields = [join_field, *fields]
    graph, rows = _filtered_rows(
        pack_id,
        node_type=side.get("node_type"),
        where=side.get("where") if isinstance(side.get("where"), dict) else None,
        fields=fields,
        include_heavy_fields=bool(side.get("include_heavy_fields", False)),
    )
    return graph["pack"]["id"], join_field, rows


def _group_rows_by_key(rows: list[dict], join_field: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        value = row.get(join_field)
        if value is None:
            continue
        grouped.setdefault(str(value), []).append(row)
    return grouped


def _summarize_join_rows(rows: list[dict], *, sample_limit: int = 5) -> dict:
    summary: dict[str, object] = {"count": len(rows), "samples": rows[:sample_limit]}
    for field in ("type_name", "family_and_type", "family_name", "item_name", "specification"):
        values = []
        seen = set()
        for row in rows:
            value = row.get(field)
            if value is None:
                continue
            key = str(value)
            if key not in seen:
                seen.add(key)
                values.append(value)
        if values:
            summary[f"{field}s"] = _sort_values(values)[:20]
    return summary


@mcp.tool()
def mo_join_by_property(
    left: dict,
    right: dict,
    join_type: str = "inner",
    limit: int = 10000,
    summarize_right: bool = True,
) -> str:
    """Join two packs by property value, for example BOQ module_type to BIM workset_name."""

    try:
        left_pack_id, left_join_field, left_rows = _join_side_rows(_normalize_tool_dict(left))
        right_pack_id, right_join_field, right_rows = _join_side_rows(_normalize_tool_dict(right))
    except PermissionError as exc:
        return _forbidden_pack(str(exc))
    except ValueError as exc:
        return _tool_payload_error(str(exc))

    left_groups = _group_rows_by_key(left_rows, left_join_field)
    right_groups = _group_rows_by_key(right_rows, right_join_field)
    left_keys = set(left_groups)
    right_keys = set(right_groups)
    lowered_join_type = str(join_type).lower()
    if lowered_join_type == "left":
        keys = left_keys
    elif lowered_join_type == "right":
        keys = right_keys
    elif lowered_join_type in {"outer", "full"}:
        keys = left_keys | right_keys
    else:
        keys = left_keys & right_keys

    rows = []
    for key in _sort_values(list(keys)):
        left_group = left_groups.get(str(key), [])
        right_group = right_groups.get(str(key), [])
        if not left_group and lowered_join_type in {"inner", "left"}:
            continue
        if not right_group and lowered_join_type == "inner":
            continue
        for left_row in left_group or [None]:
            row = {"join_key": key, "left": left_row}
            if summarize_right:
                row["right_summary"] = _summarize_join_rows(right_group)
            else:
                row["right"] = right_group
            rows.append(row)
            if len(rows) >= max(0, limit):
                break
        if len(rows) >= max(0, limit):
            break

    return _json(
        {
            "left_pack_id": left_pack_id,
            "right_pack_id": right_pack_id,
            "left_count": len(left_rows),
            "right_count": len(right_rows),
            "matched_keys": len(left_keys & right_keys),
            "join_type": lowered_join_type,
            "rows": rows,
            "truncated": len(rows) >= max(0, limit) and len(keys) > len(rows),
            "unmatched_left": _sort_values(list(left_keys - right_keys))[:1000],
            "unmatched_right": _sort_values(list(right_keys - left_keys))[:1000],
        }
    )


@mcp.tool()
def mo_schema_profile(
    pack_id: str,
    sample_size: int = 1000,
    include_property_stats: bool = True,
) -> str:
    """Profile sampled node types, properties, coverage, distinct values, and edge types."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    graph = read_graph(pack_id, max_nodes=max(1, sample_size), max_edges=max(1, sample_size))
    nodes = list(graph.get("nodes", []))
    edges = list(graph.get("edges", []))
    nodes_by_type: dict[str, list[dict]] = {}
    for node in nodes:
        nodes_by_type.setdefault(str(node.get("type") or "unknown"), []).append(node)

    node_types = []
    for node_type, typed_nodes in sorted(nodes_by_type.items(), key=lambda item: (-len(item[1]), item[0])):
        entry: dict[str, object] = {"type": node_type, "count": len(typed_nodes)}
        if include_property_stats:
            property_names = set()
            for node in typed_nodes:
                properties = node.get("properties")
                if isinstance(properties, dict):
                    property_names.update(properties)
            property_stats = {}
            for property_name in sorted(property_names):
                present_values = [
                    _node_field(node, property_name)
                    for node in typed_nodes
                    if _node_has_field(node, property_name) and _node_field(node, property_name) is not None
                ]
                distinct_values = []
                seen = set()
                numeric_count = 0
                for value in present_values:
                    if _to_number(value) is not None:
                        numeric_count += 1
                    sample_value = _compact_sample_value(value)
                    key = json.dumps(sample_value, ensure_ascii=False, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        distinct_values.append(sample_value)
                property_stats[property_name] = {
                    "coverage": round(len(present_values) / len(typed_nodes), 4) if typed_nodes else 0,
                    "distinct_count": len(distinct_values),
                    "sample_values": _sort_values(distinct_values)[:8],
                    "numeric_ratio": round(numeric_count / len(present_values), 4) if present_values else 0,
                }
            entry["properties"] = property_stats
        node_types.append(entry)

    return _json(
        {
            "pack_id": graph["pack"]["id"],
            "sample_size": sample_size,
            "sampled": graph.get("stats", {}),
            "node_types": node_types,
            "edge_types": _sorted_counter(Counter(str(edge.get("relation") or "unknown") for edge in edges), "relation"),
        }
    )


@mcp.tool()
def get_node_context(pack_id: str, node_id: str, limit: int = 50) -> str:
    """Return one node, neighboring nodes, and adjacent edges."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_node_context(pack_id=pack_id, node_id=node_id, limit=limit))


@mcp.tool()
def list_edges(
    pack_id: str,
    source: str | None = None,
    target: str | None = None,
    relation: str | None = None,
    limit: int = 200,
) -> str:
    """List graph edges, optionally filtered by source, target, or relation."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_edges(pack_id=pack_id, source=source, target=target, relation=relation, limit=limit))


@mcp.tool()
def search_pack(pack_id: str, query: str, limit: int = 8) -> str:
    """Search pack evidence documents for a natural language query."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(search_pack_evidence(pack_id, query, limit=limit))


@mcp.tool()
def search_documents(pack_id: str, query: str, limit: int = 8) -> str:
    """OpenCrab-compatible alias for evidence document search."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(search_pack_evidence(pack_id, query, limit=limit))


@mcp.tool()
def ask_pack_question(
    pack_id: str,
    question: str,
    limit: int = 6,
    use_openai: bool = False,
) -> str:
    """Answer a natural language question using pack evidence and graph context."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(answer_pack_question(pack_id, question, limit=limit, use_openai=use_openai))


@mcp.tool()
def query(
    pack_id: str,
    question: str,
    limit: int = 6,
    use_openai: bool = False,
) -> str:
    """OpenCrab-compatible alias for natural language pack question answering."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(answer_pack_question(pack_id, question, limit=limit, use_openai=use_openai))


@mcp.tool()
def list_modules(pack_id: str, limit: int = 300) -> str:
    """Return BIM module summaries parsed directly from documents/modules/*.md."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    payload = read_modules(pack_id=pack_id, limit=limit)
    if int(payload.get("module_count") or payload.get("count") or 0) > 0:
        return _json(payload)
    modules = _sort_rows(
        list(_module_candidates_from_nodes(pack_id, include_sources=True).values()),
        [{"field": "module_id", "natural": True}],
    )
    max_limit = max(0, limit)
    return _json(
        {
            "pack_id": pack_id,
            "source": "property_fallback",
            "module_count": len(modules),
            "modules": modules[:max_limit],
            "truncated": len(modules) > max_limit,
        }
    )


@mcp.tool()
def get_module(pack_id: str, module_id: str, max_chars: int = 18000) -> str:
    """Return one BIM module evidence document and parsed summary fields."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_module(pack_id=pack_id, module_id=module_id, max_chars=max_chars))


@mcp.tool()
def list_assembly_marks(pack_id: str, limit: int = 300) -> str:
    """List Advance Steel assembly mark evidence documents."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_assembly_marks(pack_id=pack_id, limit=limit))


@mcp.tool()
def get_assembly_mark(pack_id: str, mark: str, max_chars: int = 14000) -> str:
    """Return one assembly mark evidence document and parsed summary fields."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_assembly_mark(pack_id=pack_id, mark=mark, max_chars=max_chars))


@mcp.tool()
def get_fasteners(pack_id: str) -> str:
    """Return bolt/anchor quantities and specifications from fastener evidence."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_fasteners(pack_id=pack_id))


@mcp.tool()
def get_section_weight_index(pack_id: str) -> str:
    """Return section count, length, and weight rows from section_weight_index.md."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_section_weight_index(pack_id=pack_id))


@mcp.tool()
def mo_server_status() -> str:
    """Return Modular Ontology MCP server, pack, project, and canonical tool status."""

    return copycrab_status()


@mcp.tool()
def mo_tool_manifest() -> str:
    """Return canonical mo_<domain>_<action> tool names and active exposure profile."""

    return _json(_tool_manifest_payload())


@mcp.tool()
def mo_snapshot_list() -> str:
    """List canonical snapshot releases available to the current MCP scope."""

    try:
        if not _mcp_authorized():
            raise CanonicalSnapshotToolError("unauthorized", "Invalid MCP user URL token.")
        snapshots = []
        for canonical_id, _, _ in _canonical_snapshot_manifests():
            manifest, snapshot = _load_canonical_snapshot(canonical_id)
            if _snapshot_is_visible(snapshot):
                snapshots.append(_snapshot_summary(manifest, snapshot))
        return _json({"status": "ok", "count": len(snapshots), "snapshots": snapshots})
    except Exception as exc:
        return _json(_snapshot_error_payload(exc))


@mcp.tool()
def mo_snapshot_status(canonical_id: str) -> str:
    """Load and SHA-256 verify every included pack in one canonical snapshot."""

    try:
        if not _mcp_authorized():
            raise CanonicalSnapshotToolError("unauthorized", "Invalid MCP user URL token.")
        manifest, snapshot = _load_canonical_snapshot(canonical_id)
        _require_snapshot_access(snapshot)
        verification = _verify_canonical_snapshot(snapshot)
        return _json(
            {
                "status": "ok",
                "snapshot": _snapshot_summary(manifest, snapshot),
                "verification": _snapshot_verification_payload(verification),
            }
        )
    except Exception as exc:
        return _json(_snapshot_error_payload(exc, canonical_id=canonical_id))


@mcp.tool()
def mo_snapshot_query(canonical_id: str, plan: dict) -> str:
    """Execute an exact seven-field plan only after canonical snapshot SHA verification."""

    try:
        if not _mcp_authorized():
            raise CanonicalSnapshotToolError("unauthorized", "Invalid MCP user URL token.")
        canonical_plan = _exact_snapshot_query_plan(plan)
        manifest, snapshot = _load_canonical_snapshot(canonical_id)
        _require_snapshot_access(snapshot)

        # This preflight hashes every included pack.  The orchestrator then
        # verifies the same immutable snapshot again immediately before its
        # deterministic full-scan execution; there is intentionally no MCP
        # argument or internal call here that can disable either check.
        verification = _verify_canonical_snapshot(snapshot)
        execution = execute_structured_project_query(canonical_plan, snapshot)
        if execution.verification.checked_pack_ids != verification.checked_pack_ids:
            raise CanonicalSnapshotToolError(
                "snapshot_verification_changed",
                "Snapshot verification coverage changed before query execution.",
            )

        return _json(
            {
                "status": "ok",
                "snapshot": _snapshot_summary(manifest, execution.snapshot),
                "verification": _snapshot_verification_payload(execution.verification),
                "plan": execution.plan.as_dict(),
                "result": execution.result.as_dict(),
            }
        )
    except Exception as exc:
        return _json(_snapshot_error_payload(exc, canonical_id=canonical_id))


@mcp.tool()
def mo_snapshot_bundle_query(canonical_id: str, bundle: dict) -> str:
    """Execute an exact plan bundle against one fully verified canonical snapshot."""

    try:
        if not _mcp_authorized():
            raise CanonicalSnapshotToolError("unauthorized", "Invalid MCP user URL token.")

        # Validate the complete bundle, including recursively forbidden pack
        # and snapshot selectors, before any snapshot file is opened.
        canonical_bundle = _exact_snapshot_query_bundle(bundle)
        manifest, snapshot = _load_canonical_snapshot(canonical_id)
        _require_snapshot_access(snapshot)

        # Hash every included pack before execution. Each subplan hashes its
        # selected packs again inside execute_query_bundle. A final full pass
        # closes the mutation window before any result is returned.
        before = _verify_canonical_snapshot(snapshot)
        result = execute_query_bundle(canonical_bundle, snapshot)
        after = _verify_canonical_snapshot(snapshot)
        if before.checked_pack_ids != after.checked_pack_ids:
            raise CanonicalSnapshotToolError(
                "snapshot_verification_changed",
                "Snapshot verification coverage changed during bundle execution.",
            )
        bindings = _snapshot_bundle_bindings(manifest, snapshot, result)

        return _json(
            {
                "status": "ok",
                "snapshot": _snapshot_summary(manifest, snapshot),
                "verification": _snapshot_verification_payload(after),
                "bundle": canonical_bundle,
                "bindings": bindings,
                "result": result.as_dict(),
            }
        )
    except Exception as exc:
        return _json(_snapshot_error_payload(exc, canonical_id=canonical_id))


@mcp.tool()
def mo_ontology_manifest(pack_id: str = "", node_limit: int = 1000, edge_limit: int = 2000) -> str:
    """Describe ontology grammar across visible packs or one pack."""

    if pack_id and not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(_ontology_manifest_payload(pack_id=pack_id, node_limit=node_limit, edge_limit=edge_limit))


@mcp.tool()
def mo_project_list() -> str:
    """List BIM projects and the ontology packs attached to each project."""

    return list_projects()


@mcp.tool()
def mo_project_overview(project_id: str = "", schema_node_limit: int = 160, schema_edge_limit: int = 320) -> str:
    """Return project summaries with linked pack summaries and compact schema snapshots."""

    if project_id and project_id not in _visible_project_by_id():
        return _not_found_payload("project", project_id)
    return _json(
        _project_overview_payload(
            project_id=project_id,
            schema_node_limit=schema_node_limit,
            schema_edge_limit=schema_edge_limit,
        )
    )


@mcp.tool()
def mo_project_pack_list(project_id: str = "", include_empty: bool = True) -> str:
    """List packs grouped by project so users choose a project before inspecting individual packs."""

    if project_id and project_id not in _visible_project_by_id():
        return _not_found_payload("project", project_id)
    return _json(_project_pack_list_payload(project_id=project_id, include_empty=include_empty))


@mcp.tool()
def mo_project_search(project_id: str = "", search_text: str = "", limit_per_pack: int = 3) -> str:
    """Search evidence across every visible pack in a project, or across all visible projects."""

    if project_id and project_id not in _visible_project_by_id():
        return _not_found_payload("project", project_id)
    if not search_text.strip():
        return _project_tool_response(
            "mo_project_search",
            project_id,
            {
                "projectId": project_id or None,
                "query": search_text,
                "packCount": 0,
                "matchedPackCount": 0,
                "matchCount": 0,
                "returnedMatchCount": 0,
                "truncated": False,
                "results": [],
                "retrieval": {
                    "mode": "lexical",
                    "scannedPacks": 0,
                    "indexedPacks": 0,
                    "fallbackPacks": 0,
                    "candidateCount": 0,
                    "returnedCandidateCount": 0,
                    "partial": False,
                    "skippedFallbackPacks": 0,
                    "dbMs": 0,
                    "fallbackMs": 0,
                    "elapsedMs": 0,
                },
            },
        )
    payload = _project_search_payload(
        project_id=project_id,
        search_text=search_text,
        limit_per_pack=limit_per_pack,
    )
    return _project_tool_response("mo_project_search", project_id, payload)


@mcp.tool()
def mo_project_ask(project_id: str, question: str, top_k: int = 8) -> str:
    """Retrieve ranked project evidence once; synthesize the final answer in the client without server-side AI calls."""

    if project_id not in _visible_project_by_id():
        return _not_found_payload("project", project_id)
    if not question.strip():
        return _project_tool_response(
            "mo_project_ask",
            project_id,
            {
                "status": "no_answer",
                "projectId": project_id,
                "answerMode": "client_synthesis",
                "evidence": [],
                "retrieval": {
                    "mode": "lexical",
                    "scannedPacks": 0,
                    "indexedPacks": 0,
                    "fallbackPacks": 0,
                    "candidateCount": 0,
                    "returnedCandidateCount": 0,
                    "partial": False,
                    "skippedFallbackPacks": 0,
                    "dbMs": 0,
                    "fallbackMs": 0,
                    "elapsedMs": 0,
                },
            },
        )
    return _project_tool_response(
        "mo_project_ask",
        project_id,
        _project_ask_payload(project_id=project_id, question=question, top_k=top_k),
    )


@mcp.tool()
def mo_pack_list(project_id: str = "", flat: bool = False) -> str:
    """List packs by project first; set flat=true only for low-level pack administration."""

    if flat:
        return list_packs()
    if project_id and project_id not in _visible_project_by_id():
        return _not_found_payload("project", project_id)
    return _json(_project_pack_list_payload(project_id=project_id))


@mcp.tool()
def mo_pack_overview(pack_id: str, schema_node_limit: int = 300, schema_edge_limit: int = 600) -> str:
    """Return a pack summary, schema snapshot, source entrypoints, and recommended next tools."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    overview = _pack_overview_payload(
        pack_id=pack_id,
        schema_node_limit=schema_node_limit,
        schema_edge_limit=schema_edge_limit,
    )
    if overview.get("error") == "not_found":
        return _not_found_payload("pack", pack_id)
    return _json(overview)


@mcp.tool()
def mo_pack_search(search_text: str = "", limit: int = 20) -> str:
    """Search ontology packs by id, title, source, or description."""

    return search_packs(query=search_text, limit=limit)


@mcp.tool()
def mo_pack_schema(pack_id: str, node_limit: int = 1000, edge_limit: int = 2000) -> str:
    """Describe a pack's ontology grammar: node types, relation types, and document prefixes."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(_pack_schema_payload(pack_id=pack_id, node_limit=node_limit, edge_limit=edge_limit))


@mcp.tool()
def mo_source_list(pack_id: str | None = None) -> str:
    """List pack/source summaries and entrypoints."""

    return list_sources(pack_id=pack_id)


@mcp.tool()
def mo_document_list(pack_id: str, prefix: str = "documents/", suffix: str = ".md", limit: int = 200) -> str:
    """List evidence document paths directly from the ZIP pack."""

    return list_pack_documents(pack_id=pack_id, prefix=prefix, suffix=suffix, limit=limit)


@mcp.tool()
def mo_document_read(pack_id: str, path: str, max_chars: int = 12000) -> str:
    """Read a single evidence document directly from the ZIP pack by path."""

    return read_pack_document(pack_id=pack_id, path=path, max_chars=max_chars)


@mcp.tool()
def mo_chunk_read(pack_id: str, chunk_id: str) -> str:
    """Read one cloud/chunks.jsonl evidence chunk by chunk_id."""

    return read_chunk_by_id(pack_id=pack_id, chunk_id=chunk_id)


@mcp.tool()
def mo_evidence_search(pack_id: str, search_text: str, limit: int = 8) -> str:
    """Search pack evidence documents for a natural language query."""

    return search_pack(pack_id=pack_id, query=search_text, limit=limit)


@mcp.tool()
def mo_evidence_trace(pack_id: str, search_text: str, limit: int = 5, include_answer: bool = False) -> str:
    """Search evidence and graph nodes together, with an optional local answer trace."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(
        _evidence_trace_payload(
            pack_id=pack_id,
            search_text=search_text,
            limit=limit,
            include_answer=include_answer,
        )
    )


@mcp.tool()
def mo_anchor_resolve(pack_id: str, chunk_id: str) -> str:
    """Resolve source-native anchors for one evidence chunk so users can reopen the original source."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(_anchor_resolve_payload(pack_id=pack_id, chunk_id=chunk_id))


@mcp.tool()
def mo_anchor_coverage(pack_id: str, limit: int = 200, include_chunks: bool = False) -> str:
    """Summarize source-native anchor coverage across evidence chunks in a pack."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(_anchor_coverage_payload(pack_id=pack_id, limit=limit, include_chunks=include_chunks))


@mcp.tool()
def mo_drawing_text_search(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    query: str = "",
    sheet_no: str = "",
    view_name: str = "",
    text_type: Sequence[str] | str | None = None,
    limit: int = 50,
) -> str:
    """Search drawing_evidence chunks for sheet/view text, annotations, dimensions, and room-tag-like text."""

    return search_drawing_text(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        query=query,
        sheet_no=sheet_no,
        view_name=view_name,
        text_type=text_type,
        limit=limit,
    )


@mcp.tool()
def mo_room_area_tag_extract(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    floor: str = "",
    room_name: str = "",
    sheet_no: str = "",
    limit: int = 50,
) -> str:
    """Extract explicit room area text such as '화장실 3.42㎡' from drawing evidence chunks."""

    return extract_room_area_tags(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        floor=floor,
        room_name=room_name,
        sheet_no=sheet_no,
        limit=limit,
    )


@mcp.tool()
def mo_dimensions_extract(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    view_name: str = "",
    target_label: str = "",
    limit: int = 100,
) -> str:
    """Extract usable dimension text values from drawing evidence for a sheet/view/target label."""

    return extract_dimensions_from_view(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        sheet_no=sheet_no,
        view_name=view_name,
        target_label=target_label,
        limit=limit,
    )


@mcp.tool()
def mo_area_from_dimensions_calculate(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    view_name: str = "",
    target_label: str = "",
    method: str = "rectangular_area",
) -> str:
    """Calculate rectangular area from two extracted dimension values when enough evidence exists."""

    return calculate_area_from_dimensions(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        sheet_no=sheet_no,
        view_name=view_name,
        target_label=target_label,
        method=method,
    )


@mcp.tool()
def mo_schedule_table_extract(
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    project_id: str = "",
    schedule_name: str = "",
    limit: int = 5000,
) -> str:
    """Reconstruct a schedule table from schedule_rows chunks."""

    return extract_schedule_table(
        pack_id=pack_id,
        pack_ids=pack_ids,
        project_id=project_id,
        schedule_name=schedule_name,
        limit=limit,
    )


@mcp.tool()
def mo_dxf_entity_summary(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    limit_layers: int = 50,
) -> str:
    """Summarize parsed DXF entity counts and layers."""

    return dxf_entity_summary(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        sheet_no=sheet_no,
        source_file=source_file,
        limit_layers=limit_layers,
    )


@mcp.tool()
def mo_dxf_entity_search(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    query: str = "",
    sheet_no: str = "",
    source_file: str = "",
    entity_type: Sequence[str] | str | None = None,
    layer: str = "",
    where: dict | None = None,
    fields: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Search raw parsed DXF entities."""

    return dxf_entity_search(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        query=query,
        sheet_no=sheet_no,
        source_file=source_file,
        entity_type=entity_type,
        layer=layer,
        where=where,
        fields=fields,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def mo_dxf_text_search(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    query: str = "",
    sheet_no: str = "",
    source_file: str = "",
    layer: str = "",
    fields: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Search raw DXF TEXT and MTEXT entities."""

    return dxf_text_search(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        query=query,
        sheet_no=sheet_no,
        source_file=source_file,
        layer=layer,
        fields=fields,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def mo_dxf_dimension_search(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    layer: str = "",
    where: dict | None = None,
    fields: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Search raw DXF DIMENSION entities."""

    return dxf_dimension_search(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        sheet_no=sheet_no,
        source_file=source_file,
        layer=layer,
        where=where,
        fields=fields,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def mo_dxf_layer_summary(
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    limit: int = 100,
) -> str:
    """Summarize DXF layers with entity counts."""

    return dxf_layer_summary(
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        sheet_no=sheet_no,
        source_file=source_file,
        limit=limit,
    )


@mcp.tool()
def mo_dxf_bbox_query(
    bbox: Sequence[float],
    project_id: str = "",
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    sheet_no: str = "",
    source_file: str = "",
    entity_type: Sequence[str] | str | None = None,
    layer: str = "",
    mode: str = "intersects",
    fields: Sequence[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Find DXF entities by bounding-box relation."""

    return dxf_bbox_query(
        bbox=bbox,
        project_id=project_id,
        pack_id=pack_id,
        pack_ids=pack_ids,
        sheet_no=sheet_no,
        source_file=source_file,
        entity_type=entity_type,
        layer=layer,
        mode=mode,
        fields=fields,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def mo_quantity_facts_query(
    pack_id: str = "",
    pack_ids: Sequence[str] | None = None,
    project_id: str = "",
    where: dict | None = None,
    select: Sequence[str] | None = None,
    order_by: Sequence | None = None,
    limit: int = 100,
    offset: int = 0,
    include_sibling_shards: bool = True,
) -> str:
    """Query BIM/Revit quantity_facts chunks with where/select/order_by support."""

    return query_quantity_facts(
        pack_id=pack_id,
        pack_ids=pack_ids,
        project_id=project_id,
        where=where,
        select=select,
        order_by=order_by,
        limit=limit,
        offset=offset,
        include_sibling_shards=include_sibling_shards,
    )


@mcp.tool()
def mo_question_answer(
    pack_id: str,
    question: str,
    limit: int = 6,
    use_openai: bool = False,
) -> str:
    """Answer a natural language question using pack evidence and graph context."""

    return ask_pack_question(pack_id=pack_id, question=question, limit=limit, use_openai=use_openai)


@mcp.tool()
def mo_graph_get(pack_id: str, max_nodes: int = 250, max_edges: int = 500) -> str:
    """Return a sampled ontology graph for a pack."""

    return get_graph(pack_id=pack_id, max_nodes=max_nodes, max_edges=max_edges)


@mcp.tool()
def mo_node_type_list(pack_id: str, node_limit: int = 5000) -> str:
    """List node types available in a pack with sampled counts."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    schema = _pack_schema_payload(pack_id=pack_id, node_limit=node_limit, edge_limit=0)
    return _json(
        {
            "packId": schema["packId"],
            "sampled": schema["sampled"],
            "nodeTypes": schema["nodeTypes"],
            "queryableNodeFields": schema["queryableFields"]["nodeFields"],
        }
    )


@mcp.tool()
def mo_node_list(pack_id: str, node_type: str | None = None, limit: int = 100) -> str:
    """List graph nodes, optionally filtered by node type."""

    return list_nodes(pack_id=pack_id, node_type=node_type, limit=limit)


@mcp.tool()
def mo_node_search(
    pack_id: str,
    search_text: str,
    limit: int = 20,
    fields: Sequence[str] | None = None,
    include_heavy_fields: bool = False,
) -> str:
    """Search graph nodes by id, label, and properties."""

    return search_nodes(
        pack_id=pack_id,
        query=search_text,
        limit=limit,
        fields=fields,
        include_heavy_fields=include_heavy_fields,
    )


@mcp.tool()
def mo_node_context(pack_id: str, node_id: str, limit: int = 50) -> str:
    """Return one node, neighboring nodes, and adjacent edges."""

    return get_node_context(pack_id=pack_id, node_id=node_id, limit=limit)


@mcp.tool()
def mo_edge_list(
    pack_id: str,
    source: str | None = None,
    target: str | None = None,
    relation: str | None = None,
    limit: int = 200,
) -> str:
    """List graph edges, optionally filtered by source, target, or relation."""

    return list_edges(pack_id=pack_id, source=source, target=target, relation=relation, limit=limit)


@mcp.tool()
def mo_relation_type_list(pack_id: str, edge_limit: int = 5000) -> str:
    """List relation types available in a pack with sampled counts."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    schema = _pack_schema_payload(pack_id=pack_id, node_limit=max(1000, edge_limit), edge_limit=edge_limit)
    return _json(
        {
            "packId": schema["packId"],
            "sampled": schema["sampled"],
            "relationTypes": schema["relationTypes"],
            "queryableEdgeFields": schema["queryableFields"]["edgeFields"],
        }
    )


@mcp.tool()
def mo_module_list(pack_id: str, limit: int = 300) -> str:
    """Return BIM module summaries parsed directly from documents/modules/*.md."""

    return list_modules(pack_id=pack_id, limit=limit)


@mcp.tool()
def mo_module_get(pack_id: str, module_id: str, max_chars: int = 18000) -> str:
    """Return one BIM module evidence document and parsed summary fields."""

    return get_module(pack_id=pack_id, module_id=module_id, max_chars=max_chars)


@mcp.tool()
def mo_assembly_list(pack_id: str, limit: int = 300) -> str:
    """List Advance Steel assembly mark evidence documents."""

    return list_assembly_marks(pack_id=pack_id, limit=limit)


@mcp.tool()
def mo_assembly_get(pack_id: str, mark: str, max_chars: int = 14000) -> str:
    """Return one assembly mark evidence document and parsed summary fields."""

    return get_assembly_mark(pack_id=pack_id, mark=mark, max_chars=max_chars)


@mcp.tool()
def mo_fastener_summary(pack_id: str) -> str:
    """Return bolt/anchor quantities and specifications from fastener evidence."""

    return get_fasteners(pack_id=pack_id)


@mcp.tool()
def mo_section_weight_index(pack_id: str) -> str:
    """Return section count, length, and weight rows from section_weight_index.md."""

    return get_section_weight_index(pack_id=pack_id)


def _unregister_removed_legacy_tools() -> None:
    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    for name in _REMOVED_LEGACY_TOOL_NAMES:
        if name in registered:
            mcp.remove_tool(name)


_unregister_removed_legacy_tools()


def configure_server(
    *,
    host: str,
    port: int,
    path: str,
    allowed_hosts: Sequence[str],
    allowed_origins: Sequence[str],
    tool_profile: str | None = None,
) -> None:
    if tool_profile is not None:
        mcp.set_tool_profile(tool_profile)
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.streamable_http_path = path
    mcp.settings.stateless_http = True
    mcp.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=list(allowed_hosts),
        allowed_origins=list(allowed_origins),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Modular Ontology MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default=str(env("MODULAR_ONTOLOGY_MCP_TRANSPORT", "stdio")),
        help="Use stdio for local MCP clients or streamable-http for Remote MCP.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="HTTP bind host for remote MCP.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port for remote MCP.")
    parser.add_argument("--path", default=DEFAULT_PATH, help="Remote MCP endpoint path.")
    parser.add_argument(
        "--tool-profile",
        choices=TOOL_PROFILE_NAMES,
        default=str(env("MODULAR_ONTOLOGY_MCP_TOOL_PROFILE", "core")),
        help="Expose core tools (default) or all canonical expert tools.",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        default=None,
        help="Allowed Host header for remote MCP. Repeat for multiple hosts, e.g. --allowed-host graph.example.com.",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        dest="allowed_origins",
        default=None,
        help="Allowed Origin for browser-based MCP clients. Repeat for multiple origins.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    allowed_hosts = _split_many(args.allowed_hosts) if args.allowed_hosts else _split_csv(DEFAULT_ALLOWED_HOSTS)
    allowed_origins = _split_many(args.allowed_origins) if args.allowed_origins else _split_csv(DEFAULT_ALLOWED_ORIGINS)
    configure_server(
        host=args.host,
        port=args.port,
        path=args.path,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        tool_profile=args.tool_profile,
    )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
