# Modular Ontology

Bright-tone BIM ontology graph web app for Revit IFC and Advance Steel ontology packs.

## Run

```powershell
pip install -e .
npm install
python -m uvicorn modular_ontology.app:app --host 127.0.0.1 --port 8010
npm run dev -- --port 5173
```

Open `http://127.0.0.1:5173`.

Production-like single-server mode:

```powershell
.\scripts\run_production.ps1
```

After `npm run build`, FastAPI serves both the React UI and API at `http://127.0.0.1:8010`.

## Workspace routes

The React workspace is a single-page app, but each major feature has a stable browser path. Keep these routes distinct so deep links, refreshes, screenshots, and issue reports point to the right workspace area.

| Path | UI area | Main ownership |
| --- | --- | --- |
| `/dashboard` | Dashboard | operational summary and assigned project overview |
| `/projects` | Projects | project metadata, project-pack links, and access context |
| `/upload` | Upload | ontology ZIP upload, IFC file staging, and pack list |
| `/graph` | Graph Explorer | project-level graph view, pack filters, node inspector, and AI Query |
| `/mcp-connection` | MCP Connections | per-user MCP URL and connector setup |
| `/admin` | Admin | user approval, company management, roles, and project access |

`/` currently opens the dashboard. Unknown non-API paths fall back to the React app so the client route can decide what to show.

## API namespaces

All backend routes stay under `/api` except the streamable HTTP MCP endpoint under `/mcp`. Prefer extending the existing namespace that matches the feature instead of adding new top-level paths.

| Namespace | Purpose |
| --- | --- |
| `/api/auth/*` | login, signup, session lookup, logout |
| `/api/admin/*` | admin-only users, companies, project access, storage sync, and reindex actions |
| `/api/projects*` | visible projects, project suggestions, and project graph data |
| `/api/packs*` | visible ontology packs and ontology ZIP upload |
| `/api/graph/{pack_id}` | legacy single-pack graph inspection |
| `/api/ifc/upload` | IFC, IFCZIP, or ZIP model file staging by project |

### IFC to XKT conversion

Model Explorer loads `.xkt` assets. When `@xeokit/xeokit-convert` is installed, the backend uses the local converter by default:

```powershell
node node_modules\@xeokit\xeokit-convert\convert2xkt.js -s {ifc} -f ifc -o {xkt}
```

Override it with `MODULAR_ONTOLOGY_XKT_CONVERTER_CMD` when you need a custom converter command. The command may use `{ifc}` and `{xkt}` placeholders. Set `MODULAR_ONTOLOGY_DISABLE_DEFAULT_XKT_CONVERTER=1` to disable the built-in default.
Long conversions use `MODULAR_ONTOLOGY_XKT_CONVERTER_TIMEOUT_SECONDS` and default to 900 seconds.
| `/api/query` | Graph RAG and user-key OpenAI AI Query |
| `/api/llm/openai/validate` | user-provided OpenAI API key validation |
| `/api/mcp/*` | per-user MCP URL status and token regeneration |
| `/mcp/{token}` | streamable HTTP MCP endpoint for AI clients |

The default graph experience should use `/api/projects/{project_id}/graph` because one project can contain multiple packs. Keep `/api/graph/{pack_id}` for focused pack inspection and compatibility.

## Local verification

Run these before handing off a local change:

```powershell
npm run build
python -m pytest tests/test_modular_ontology.py -q
```

For route fallback checks without starting a full browser session:

```powershell
@'
from fastapi.testclient import TestClient
from modular_ontology.app import app

client = TestClient(app)
for path in ["/dashboard", "/projects", "/upload", "/graph", "/mcp-connection", "/admin"]:
    response = client.get(path)
    print(path, response.status_code, response.headers.get("content-type", ""))
'@ | python -
```

Do not deploy or push as part of local verification unless that is explicitly requested.

## Google Drive storage for Vercel

Vercel has no bundled local SQLite database or ontology ZIP packs. To hydrate the deployment from Google Drive, set these environment variables:

```powershell
MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID=1qBTaqsuRbKL-DdUG8EHtwpdpIo7EO2en
MODULAR_ONTOLOGY_STRUCTURED_DATA_DIR=1
MODULAR_ONTOLOGY_GOOGLE_SERVICE_ACCOUNT_JSON={...}
```

Share the `Modular Ontology` Google Drive folder with the service account email as Editor. On cold start, the API downloads:

- `00_Admin/users.json`
- `00_Admin/mcp_remote.json`
- `01_Database/modular_ontology.sqlite3`
- `02_Projects/<project-id>/ifc-models/*.{ifc,ifczip,zip,xkt}`
- `02_Projects/<project-id>/ontology-packs/*.zip`
- legacy compatibility: `03_IFC_Models/` and `04_Ontology_Packs/indexed/*.zip`
- `05_MCP/`
- `07_Backups/`

Check sync status:

```powershell
GET /api/storage/google-drive/status
```

Force a refresh as an admin:

```powershell
POST /api/admin/storage/google-drive/sync
```

Runtime write-back is enabled for admin/user changes:

- user, company, approval, role, and company-project access changes update `00_Admin/users.json`
- project and pack-link changes update `01_Database/modular_ontology.sqlite3`
- ontology pack ZIPs are synchronized from each project's `ontology-packs/` folder

Model and pack files are uploaded by an administrator directly in Google Drive. The app does not receive model or pack file uploads from the browser. Use the Sync tab after placing files in Drive:

- Create the project in the app first. The app creates `02_Projects/<project-id>/` and its default upload folders in Drive.
- IFC/XKT files: `02_Projects/<project-id>/ifc-models/`
- ontology pack ZIP files: `02_Projects/<project-id>/ontology-packs/`

During sync, IFC/XKT files without metadata are registered automatically as model metadata, pack ZIPs are reindexed into the project knowledge graph, and project-scoped packs are linked to the matching project. A project's `ontology-packs/` folder is authoritative for that project's pack links, so removing a ZIP from Drive and syncing removes that pack link from the project. Project-scoped ZIPs are cached locally with the project ID prefixed to avoid filename collisions. Legacy `03_IFC_Models/<project-id>/files/` and `04_Ontology_Packs/indexed/` folders are still read for backward compatibility.

Force a full write-back as an admin:

```powershell
POST /api/admin/storage/google-drive/write-back
POST /api/admin/storage/google-drive/write-back?include_packs=true
```

For local development with an authenticated Google Cloud SDK, start the server with:

```powershell
$env:MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID="1qBTaqsuRbKL-DdUG8EHtwpdpIo7EO2en"
$env:MODULAR_ONTOLOGY_GOOGLE_DRIVE_USE_GCLOUD_AUTH="1"
$env:MODULAR_ONTOLOGY_GCLOUD_COMMAND="C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
python -m uvicorn modular_ontology.app:app --host 127.0.0.1 --port 8000
```

Demo sessions:

- Admin: `admin@modular-ontology.local` / `admin123!`
- Member: `member@modular-ontology.local` / `member123!`

For a configurable local user file, copy `data/users.example.json`, edit users and password hashes, then set:

```powershell
$env:MODULAR_ONTOLOGY_USERS_FILE="D:\path\to\users.json"
```

Only admin sessions can upload ontology ZIP packs. Successful uploads are immediately ingested into the SQLite ontology index.

Admin sessions can also rebuild the persistent SQLite ontology index from the current ZIP packs. The index is stored at `data/modular_ontology.sqlite3` and caches pack summaries, Markdown evidence documents, sampled graph nodes, and sampled graph edges.

## MCP

Use `mcp-config.example.json` as the Codex/GPT MCP server template.

Available tools:

- `list_projects`
- `list_packs`
- `get_graph`
- `search_pack`
- `ask_pack_question`

Verify the stdio MCP server:

```powershell
python scripts\verify_mcp_stdio.py
```

Run a Remote MCP server for ChatGPT-compatible HTTP access:

```powershell
.\scripts\run_remote_mcp.ps1
```

The local endpoint is `http://127.0.0.1:8011/mcp`. ChatGPT needs this same MCP endpoint on a public HTTPS URL, for example `https://graph.your-company.com/mcp` after deploying the server or putting an HTTPS tunnel/reverse proxy in front of port `8011`.

The production Vercel deployment exposes the Remote MCP endpoint directly at:

```text
https://modular-ontology.xyz/mcp/{token}
```

The fallback Vercel project alias is:

```text
https://modular-ontology.vercel.app/mcp/{token}
```

Create a temporary public HTTPS URL for ChatGPT with Cloudflare Tunnel:

```powershell
.\scripts\start_chatgpt_mcp_tunnel.ps1
```

The script writes the generated URL to `data/mcp_remote.json`, restarts the Remote MCP server with the tunnel host allowed, and the MCP URL panel displays the usable `https://...trycloudflare.com/mcp` address. This quick-tunnel URL is temporary; run the script again if the tunnel process stops.

Create a fixed public HTTPS URL without buying a domain by using Tailscale Funnel:

```powershell
.\scripts\login_tailscale.ps1 -Install
.\scripts\login_tailscale.ps1
.\scripts\start_tailscale_mcp_funnel.ps1
```

Tailscale Funnel uses the device DNS name under your tailnet, for example `https://device.tailnet.ts.net/mcp`. If the installer reports error `1603` with a pending reboot, restart Windows and run `.\scripts\login_tailscale.ps1 -Install` again. The first Funnel run can open a Tailscale admin approval page; approve Funnel there, then rerun `.\scripts\start_tailscale_mcp_funnel.ps1`.

For a public domain, allow that host before starting the server:

```powershell
$env:MODULAR_ONTOLOGY_MCP_HOST="0.0.0.0"
$env:MODULAR_ONTOLOGY_MCP_ALLOWED_HOSTS="graph.your-company.com,127.0.0.1:*,localhost:*"
$env:MODULAR_ONTOLOGY_MCP_ALLOWED_ORIGINS="https://chatgpt.com,https://chat.openai.com"
.\scripts\run_remote_mcp.ps1
```

Verify the HTTP Remote MCP endpoint:

```powershell
python scripts\verify_mcp_http.py
```

Optional OpenAI synthesis:

```powershell
$env:OPENAI_API_KEY="..."
$env:MODULAR_ONTOLOGY_OPENAI_MODEL="gpt-4.1-mini"
```

Then call `/api/query` or `ask_pack_question` with `use_openai: true`. If `MODULAR_ONTOLOGY_OPENAI_MODEL` is unset, the default model is `gpt-4.1-mini`.

For the hosted web UI, production does not need a shared `OPENAI_API_KEY`. Users can enter their own OpenAI API key in the AI Query panel; the key is sent only with that request and is not written to server storage.

