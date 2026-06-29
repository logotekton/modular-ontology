from __future__ import annotations

import argparse
import fnmatch
import json
import re
from collections import Counter
from collections.abc import Sequence

from .auth import get_company_project_access
from .config import env
from .mcp_tokens import get_mcp_token_record
from .pack_index import (
    build_graph as read_graph,
    get_assembly_mark as read_assembly_mark,
    get_fasteners as read_fasteners,
    get_module as read_module,
    get_node_context as read_node_context,
    get_section_weight_index as read_section_weight_index,
    list_assembly_marks as read_assembly_marks,
    list_edges as read_edges,
    list_modules as read_modules,
    list_nodes as read_nodes,
    list_pack_documents as read_pack_documents,
    list_packs as read_packs,
    list_projects as read_projects,
    list_sources as read_sources,
    read_pack_document as read_document,
    search_pack as search_pack_evidence,
    search_packs as search_pack_catalog,
    search_nodes as search_graph_nodes,
)
from .qa import answer_pack_question


try:
    from mcp.server.fastmcp import FastMCP
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

TOOL_ALIASES = {
    "copycrab_status": "mo_server_status",
    "list_projects": "mo_project_list",
    "list_packs": "mo_pack_list",
    "search_packs": "mo_pack_search",
    "list_sources": "mo_source_list",
    "list_pack_documents": "mo_document_list",
    "read_pack_document": "mo_document_read",
    "search_pack": "mo_evidence_search",
    "search_documents": "mo_evidence_search",
    "query": "mo_question_answer",
    "ask_pack_question": "mo_question_answer",
    "get_graph": "mo_graph_get",
    "list_nodes": "mo_node_list",
    "search_nodes": "mo_node_search",
    "get_node_context": "mo_node_context",
    "list_edges": "mo_edge_list",
    "list_modules": "mo_module_list",
    "get_module": "mo_module_get",
    "list_assembly_marks": "mo_assembly_list",
    "get_assembly_mark": "mo_assembly_get",
    "get_fasteners": "mo_fastener_summary",
    "get_section_weight_index": "mo_section_weight_index",
}

TOOL_MANIFEST = [
    {"name": "mo_server_status", "legacy": ["copycrab_status"], "domain": "server", "action": "status"},
    {"name": "mo_tool_manifest", "legacy": [], "domain": "tool", "action": "manifest"},
    {"name": "mo_ontology_manifest", "legacy": [], "domain": "ontology", "action": "manifest"},
    {"name": "mo_project_list", "legacy": ["list_projects"], "domain": "project", "action": "list"},
    {"name": "mo_project_overview", "legacy": [], "domain": "project", "action": "overview"},
    {"name": "mo_project_pack_list", "legacy": [], "domain": "project_pack", "action": "list"},
    {"name": "mo_project_search", "legacy": [], "domain": "project", "action": "search"},
    {"name": "mo_pack_list", "legacy": ["list_packs"], "domain": "pack", "action": "list"},
    {"name": "mo_pack_overview", "legacy": [], "domain": "pack", "action": "overview"},
    {"name": "mo_pack_search", "legacy": ["search_packs"], "domain": "pack", "action": "search"},
    {"name": "mo_pack_schema", "legacy": [], "domain": "pack", "action": "schema"},
    {"name": "mo_source_list", "legacy": ["list_sources"], "domain": "source", "action": "list"},
    {"name": "mo_document_list", "legacy": ["list_pack_documents"], "domain": "document", "action": "list"},
    {"name": "mo_document_read", "legacy": ["read_pack_document"], "domain": "document", "action": "read"},
    {"name": "mo_evidence_search", "legacy": ["search_pack", "search_documents"], "domain": "evidence", "action": "search"},
    {"name": "mo_evidence_trace", "legacy": [], "domain": "evidence", "action": "trace"},
    {"name": "mo_question_answer", "legacy": ["query", "ask_pack_question"], "domain": "question", "action": "answer"},
    {"name": "mo_graph_get", "legacy": ["get_graph"], "domain": "graph", "action": "get"},
    {"name": "mo_node_type_list", "legacy": [], "domain": "node_type", "action": "list"},
    {"name": "mo_node_list", "legacy": ["list_nodes"], "domain": "node", "action": "list"},
    {"name": "mo_node_search", "legacy": ["search_nodes"], "domain": "node", "action": "search"},
    {"name": "mo_filtered_search_nodes", "legacy": [], "domain": "node", "action": "filtered_search"},
    {"name": "mo_distinct_property_values", "legacy": [], "domain": "node", "action": "distinct_property_values"},
    {"name": "mo_aggregate_nodes", "legacy": [], "domain": "node", "action": "aggregate"},
    {"name": "mo_schema_profile", "legacy": [], "domain": "schema", "action": "profile"},
    {"name": "mo_node_context", "legacy": ["get_node_context"], "domain": "node", "action": "context"},
    {"name": "mo_edge_list", "legacy": ["list_edges"], "domain": "edge", "action": "list"},
    {"name": "mo_relation_type_list", "legacy": [], "domain": "relation_type", "action": "list"},
    {"name": "mo_module_list", "legacy": ["list_modules"], "domain": "module", "action": "list"},
    {"name": "mo_list_project_modules", "legacy": [], "domain": "module", "action": "project_list"},
    {"name": "mo_list_module_elements", "legacy": [], "domain": "module", "action": "element_list"},
    {"name": "mo_query_quantity_evidence", "legacy": [], "domain": "quantity", "action": "evidence_query"},
    {"name": "mo_aggregate_quantity_by_module", "legacy": [], "domain": "quantity", "action": "aggregate_by_module"},
    {"name": "mo_join_by_property", "legacy": [], "domain": "node", "action": "join_by_property"},
    {"name": "mo_module_get", "legacy": ["get_module"], "domain": "module", "action": "get"},
    {"name": "mo_assembly_list", "legacy": ["list_assembly_marks"], "domain": "assembly", "action": "list"},
    {"name": "mo_assembly_get", "legacy": ["get_assembly_mark"], "domain": "assembly", "action": "get"},
    {"name": "mo_fastener_summary", "legacy": ["get_fasteners"], "domain": "fastener", "action": "summary"},
    {
        "name": "mo_section_weight_index",
        "legacy": ["get_section_weight_index"],
        "domain": "section",
        "action": "weight_index",
    },
]

TOOL_NAMES = [tool["name"] for tool in TOOL_MANIFEST]
LEGACY_TOOL_NAMES = list(TOOL_ALIASES)


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _split_many(values: Sequence[str]) -> list[str]:
    return [part for value in values for part in _split_csv(value)]


mcp = FastMCP(
    "Modular Ontology",
    instructions=(
        "Use Modular Ontology tools to inspect BIM ontology packs, search evidence, "
        "sample graph nodes/edges, and answer natural language questions about Revit IFC "
        "and Advance Steel model data."
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


def _tool_manifest_payload() -> dict:
    return {
        "naming": {
            "prefix": "mo",
            "pattern": "mo_<domain>_<action>",
            "meaning": "mo is short for Modular Ontology.",
            "canonicalOnlyForNewClients": True,
        },
        "workflow": {
            "primaryUnit": "project",
            "guidance": "Start with project tools. Use pack tools only when the user explicitly wants to inspect a specific pack.",
            "recommendedStart": ["mo_project_list", "mo_project_pack_list", "mo_project_overview", "mo_project_search"],
            "packLevelTools": ["mo_pack_overview", "mo_pack_schema", "mo_document_list", "mo_document_read"],
        },
        "tools": TOOL_MANIFEST,
        "canonicalTools": TOOL_NAMES,
        "legacyTools": LEGACY_TOOL_NAMES,
        "legacyAliases": TOOL_ALIASES,
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


_MISSING = object()
_HEAVY_FIELDS = {
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
_DEFAULT_NODE_FIELDS = [
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
_MODULE_PATTERN = re.compile(r"^[0-9]+-[0-9]{2}-(A|ST|L|G|O)$")


def _normalize_tool_dict(value: dict | None) -> dict:
    return value if isinstance(value, dict) else {}


def _normalize_tool_list(value: Sequence | None) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(value)


def _node_field(node: dict, field: str) -> object:
    if field in node:
        return node.get(field)
    properties = node.get("properties")
    if not isinstance(properties, dict):
        return _MISSING
    if field.startswith("properties."):
        field = field.split(".", 1)[1]
    current: object = properties
    for part in field.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _node_has_field(node: dict, field: str) -> bool:
    return _node_field(node, field) is not _MISSING


def _is_heavy_field(field: str) -> bool:
    normalized = field.split(".", 1)[-1]
    return normalized in _HEAVY_FIELDS


def _to_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _loose_equal(left: object, right: object) -> bool:
    left_number = _to_number(left)
    right_number = _to_number(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return left == right or str(left) == str(right)


def _match_one(value: object, operator: str, expected: object, *, exists: bool) -> bool:
    if operator == "exists":
        return exists if bool(expected) else not exists
    if not exists:
        return operator == "ne" and expected is not None
    if operator == "eq":
        return _loose_equal(value, expected)
    if operator == "ne":
        return not _loose_equal(value, expected)
    if operator == "in":
        return any(_loose_equal(value, item) for item in _normalize_tool_list(expected if isinstance(expected, Sequence) and not isinstance(expected, str) else [expected]))
    if operator == "not_in":
        return not any(_loose_equal(value, item) for item in _normalize_tool_list(expected if isinstance(expected, Sequence) and not isinstance(expected, str) else [expected]))
    if operator == "contains":
        return str(expected).casefold() in str(value).casefold()
    if operator == "regex":
        try:
            return bool(re.search(str(expected), str(value)))
        except re.error:
            return False
    if operator in {"gt", "gte", "lt", "lte"}:
        left_number = _to_number(value)
        right_number = _to_number(expected)
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


def _matches_where(node: dict, where: dict | None) -> bool:
    for field, condition in _normalize_tool_dict(where).items():
        value = _node_field(node, str(field))
        exists = value is not _MISSING
        if isinstance(condition, dict):
            if not condition:
                continue
            if not all(_match_one(value, str(operator), expected, exists=exists) for operator, expected in condition.items()):
                return False
        elif not _match_one(value, "eq", condition, exists=exists):
            return False
    return True


def _natural_key(value: object) -> tuple:
    if value is None or value is _MISSING:
        return (1, "")
    parts = re.split(r"(\d+)", str(value))
    key: list[tuple[int, object]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.casefold()))
    return (0, tuple(key))


def _hashable_value(value: object) -> object:
    if value is _MISSING:
        return None
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _sort_value_key(value: object) -> tuple:
    if value is None or value is _MISSING:
        return (1, 0, "")
    number = _to_number(value)
    if number is not None:
        return (0, 0, number)
    return (0, 1, str(value).casefold())


def _sort_rows(rows: list[dict], order_by: Sequence | None) -> list[dict]:
    specs = _normalize_tool_list(order_by)
    if not specs:
        return rows
    ordered = list(rows)
    for raw_spec in reversed(specs):
        if isinstance(raw_spec, str):
            spec = {"field": raw_spec}
        elif isinstance(raw_spec, dict):
            spec = raw_spec
        else:
            continue
        field = str(spec.get("field") or "")
        if not field:
            continue
        reverse = str(spec.get("direction", "asc")).lower() == "desc"
        natural = bool(spec.get("natural", False))
        ordered.sort(
            key=lambda row: _natural_key(row.get(field)) if natural else _sort_value_key(row.get(field)),
            reverse=reverse,
        )
    return ordered


def _sort_values(values: list[object], order: str = "asc") -> list[object]:
    return sorted(values, key=_natural_key, reverse=str(order).lower() == "desc")


def _compact_sample_value(value: object) -> object:
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    if isinstance(value, dict):
        return {"type": "object", "keys": sorted(str(key) for key in value)[:12]}
    return value


def _project_node(node: dict, fields: Sequence | None = None, *, include_heavy_fields: bool = False) -> dict:
    selected_fields = _normalize_tool_list(fields) or _DEFAULT_NODE_FIELDS
    row: dict[str, object] = {}
    for field_obj in selected_fields:
        field = str(field_obj)
        if not include_heavy_fields and _is_heavy_field(field):
            continue
        value = _node_field(node, field)
        if value is not _MISSING:
            row[field.split(".", 1)[-1]] = value
    return row


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
                    "recommendedNext": "Use mo_project_overview or mo_project_search first; use mo_pack_overview only for pack-level inspection.",
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


def _project_search_payload(project_id: str, search_text: str, limit_per_pack: int = 3) -> dict:
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
    results = [
        {
            "pack": packs_by_id[pack_id],
            "matches": search_pack_evidence(pack_id, search_text, limit=max(0, limit_per_pack)),
        }
        for pack_id in unique_pack_ids
    ]
    return {
        "projectId": project_id or None,
        "query": search_text,
        "packCount": len(unique_pack_ids),
        "matchCount": sum(len(item["matches"]) for item in results),
        "results": results,
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
                "legacyTools": [],
            }
        )
    packs = _filter_pack_list(read_packs())
    projects = _visible_projects()
    scope = _mcp_scope()
    return _json(
        {
            "status": "ok",
            "server": "Modular Ontology MCP",
            "company": _mcp_company() or "all",
            "userEmail": scope.get("userEmail") or "",
            "pack_count": len(packs),
            "project_count": len(projects),
            "tools": TOOL_NAMES,
            "canonicalTools": TOOL_NAMES,
            "legacyTools": LEGACY_TOOL_NAMES,
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
    """Return canonical mo_<domain>_<action> tool names and legacy aliases."""

    return _json(_tool_manifest_payload())


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
        return _json({"projectId": project_id or None, "query": search_text, "packCount": 0, "matchCount": 0, "results": []})
    return _json(_project_search_payload(project_id=project_id, search_text=search_text, limit_per_pack=limit_per_pack))


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


def configure_server(
    *,
    host: str,
    port: int,
    path: str,
    allowed_hosts: Sequence[str],
    allowed_origins: Sequence[str],
) -> None:
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
    )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
