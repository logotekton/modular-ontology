from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence

from .auth import get_company_project_access
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


DEFAULT_HOST = os.environ.get("MODULAR_GRAPH_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("MODULAR_GRAPH_MCP_PORT", "8011"))
DEFAULT_PATH = os.environ.get("MODULAR_GRAPH_MCP_PATH", "/mcp/{mcp_token}")
DEFAULT_ALLOWED_HOSTS = os.environ.get(
    "MODULAR_GRAPH_MCP_ALLOWED_HOSTS",
    "127.0.0.1:*,localhost:*,[::1]:*",
)
DEFAULT_ALLOWED_ORIGINS = os.environ.get(
    "MODULAR_GRAPH_MCP_ALLOWED_ORIGINS",
    "http://127.0.0.1:*,http://localhost:*,http://[::1]:*",
)

TOOL_NAMES = [
    "copycrab_status",
    "list_projects",
    "list_packs",
    "search_packs",
    "list_sources",
    "list_pack_documents",
    "read_pack_document",
    "search_pack",
    "search_documents",
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
]


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _split_many(values: Sequence[str]) -> list[str]:
    return [part for value in values for part in _split_csv(value)]


mcp = FastMCP(
    "Modular Graph",
    instructions=(
        "Use Modular Graph tools to inspect BIM ontology packs, search evidence, "
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
    return os.environ.get("MODULAR_GRAPH_MCP_COMPANY", "").strip()


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
        if not record:
            return {"authorized": False, "token": token, "company": "", "userEmail": ""}
        return {"authorized": True, **record}
    return {"authorized": True, "company": _mcp_company_env(), "userEmail": "", "token": ""}


def _mcp_authorized() -> bool:
    return bool(_mcp_scope().get("authorized"))


def _mcp_company() -> str:
    scope = _mcp_scope()
    if not scope.get("authorized"):
        return ""
    return str(scope.get("company") or "").strip()


def _is_internal_company(company: str) -> bool:
    lowered = company.casefold()
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


@mcp.tool()
def copycrab_status() -> str:
    """Return MCP server, pack, project, and tool status for CopyCrab/Modular Graph."""

    if not _mcp_authorized():
        return _json(
            {
                "status": "unauthorized",
                "server": "Modular Graph MCP",
                "detail": "Invalid MCP user URL token.",
                "tools": [],
            }
        )
    packs = _filter_pack_list(read_packs())
    projects = _visible_projects()
    scope = _mcp_scope()
    return _json(
        {
            "status": "ok",
            "server": "Modular Graph MCP",
            "company": _mcp_company() or "all",
            "userEmail": scope.get("userEmail") or "",
            "pack_count": len(packs),
            "project_count": len(projects),
            "tools": TOOL_NAMES,
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
def search_nodes(pack_id: str, query: str, limit: int = 20) -> str:
    """Search graph nodes by id, label, and properties."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(search_graph_nodes(pack_id=pack_id, query=query, limit=limit))


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
    use_ollama: bool = False,
) -> str:
    """Answer a natural language question using pack evidence and graph context."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(answer_pack_question(pack_id, question, limit=limit, use_openai=use_openai, use_ollama=use_ollama))


@mcp.tool()
def query(
    pack_id: str,
    question: str,
    limit: int = 6,
    use_openai: bool = False,
    use_ollama: bool = False,
) -> str:
    """OpenCrab-compatible alias for natural language pack question answering."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(answer_pack_question(pack_id, question, limit=limit, use_openai=use_openai, use_ollama=use_ollama))


@mcp.tool()
def list_modules(pack_id: str, limit: int = 300) -> str:
    """Return BIM module summaries parsed directly from documents/modules/*.md."""

    if not _pack_is_visible(pack_id):
        return _forbidden_pack(pack_id)
    return _json(read_modules(pack_id=pack_id, limit=limit))


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
    parser = argparse.ArgumentParser(description="Run the Modular Graph MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default=os.environ.get("MODULAR_GRAPH_MCP_TRANSPORT", "stdio"),
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
