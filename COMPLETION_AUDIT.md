# Modular Graph Completion Audit

Date: 2026-06-04

## Objective

Build an OpenCrab-like UI + server website for BIM ontology packs:

- Ingest ontology pack ZIP files.
- Visualize ontology packs as a graph.
- Replace Marketplace with Projects.
- Restrict pack creation/upload to administrators.
- Remove workflow, billing/payment, community, question, and contact surfaces.
- Support a Revit/Advance Steel pipeline where modeled data becomes JSON, JSON becomes ontology packs, packs are uploaded, managed by project, explored as graphs, and queried by GPT/Codex through MCP.

## Evidence Checklist

| Requirement | Artifact evidence | Verification evidence | Status |
| --- | --- | --- | --- |
| Uses local Manus-style harness workspace | `manus_harness/` extracted; FastAPI project coexists with harness package | `python -m pytest -q` includes existing harness tests | Implemented |
| Ontology ZIP discovery | `moddular_graph/pack_index.py::discover_pack_files`, `list_packs` | `tests/test_moddular_graph.py::test_discovers_existing_revit_and_advance_steel_packs` | Implemented |
| Revit pack graph ingestion | `build_graph_from_pack` reads `graph/nodes.jsonl`, `graph/edges.jsonl` | `test_builds_graph_from_both_pack_shapes` | Implemented |
| Advance Steel pack graph ingestion | `_build_producer_graph` reads `backdata/jsonl/*.jsonl` | `test_builds_graph_from_both_pack_shapes` | Implemented |
| Admin upload immediately ingests | `/api/packs/upload`, `save_uploaded_pack`, `index_pack` | `test_pack_upload_is_session_admin_only_and_accepts_valid_zip` checks `ingest.status`, docs, nodes, edges | Implemented |
| Persistent server index | `moddular_graph/store.py`, SQLite `packs/documents/nodes/edges` | `test_sqlite_index_persists_pack_documents_and_graph` | Implemented |
| Graph visualization UI | `src/App.tsx::GraphCanvas`, `src/styles.css` | Browser smoke verified canvas rendering and no console errors | Implemented |
| Project tab instead of Marketplace | `src/App.tsx` nav includes `Projects`; no Marketplace UI | `test_projects_replace_marketplace_with_project_pack_grouping`; grep found Marketplace only in test name | Implemented |
| Admin-only pack permissions | `moddular_graph/auth.py`, upload/reindex authorization checks | `test_auth_sessions_distinguish_admin_and_member`, upload/reindex RBAC tests, browser admin/member UI smoke | Implemented for local RBAC |
| Configurable employee users | `MODDULAR_GRAPH_USERS_FILE`, `data/users.example.json` | `test_auth_can_load_users_from_json_config` | Implemented |
| Removed workflow/payment/community/contact UI | No routes/nav/components for these features | `rg` search found no runtime UI/server occurrences | Implemented |
| MCP tools for GPT/Codex | `moddular_graph/mcp_server.py`, `mcp-config.example.json` | `scripts/verify_mcp_stdio.py`, `test_mcp_stdio_server_lists_and_calls_tools` | Implemented |
| Natural-language model query | `moddular_graph/qa.py`, `/api/query`, MCP `ask_pack_question` | `test_local_graph_rag_answer_uses_evidence_and_graph_context`, browser query smoke | Implemented |
| Optional OpenAI synthesis | `qa.py::use_openai`, env `MODDULAR_GRAPH_OPENAI_MODEL` | `test_openai_graph_rag_path_uses_injected_client` | Implemented with fallback |
| UI + server website | FastAPI serves API and built React UI from `dist/` | `test_fastapi_serves_web_shell_or_build_hint`, manual `GET /` and `/api/health` 200 | Implemented |
| One-command production-like run | `scripts/run_production.ps1` | Script is documented; build/server smoke was run manually | Implemented |

## Latest Verification

- `python -m pytest -q`: 40 passed, 1 warning.
- `npm run build`: successful.
- `python scripts\verify_mcp_stdio.py`: listed `ask_pack_question`, `get_graph`, `list_packs`, `list_projects`, `search_pack`; returned `local-graph-rag`.
- Browser smoke checks confirmed graph rendering, admin/member RBAC UI, reindex UI, query result, and no console errors.
- FastAPI single-server smoke confirmed `/` and `/api/health` return 200.

## Residual Gaps

These are production-hardening gaps, not blockers for the requested local working implementation:

- Authentication is local/configurable RBAC; enterprise SSO can be added later if required by deployment policy.
- OpenAI synthesis requires user-provided `OPENAI_API_KEY` and `MODDULAR_GRAPH_OPENAI_MODEL`.
- SQLite index is suitable for local/small-team deployment; large enterprise deployment should add migrations, backup, and stronger search.
- Deployment packaging is a PowerShell local runner, not a cloud/Windows service installer.
