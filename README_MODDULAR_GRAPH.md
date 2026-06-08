# Modular Graph

Bright-tone BIM ontology graph web app for Revit IFC and Advance Steel ontology packs.

## Run

```powershell
pip install -e .
npm install
python -m uvicorn moddular_graph.app:app --host 127.0.0.1 --port 8010
npm run dev -- --port 5173
```

Open `http://127.0.0.1:5173`.

Production-like single-server mode:

```powershell
.\scripts\run_production.ps1
```

After `npm run build`, FastAPI serves both the React UI and API at `http://127.0.0.1:8010`.

## Google Drive storage for Vercel

Vercel has no bundled local SQLite database or ontology ZIP packs. To hydrate the deployment from Google Drive, set these environment variables:

```powershell
MODDULAR_GRAPH_GOOGLE_DRIVE_FOLDER_ID=1qBTaqsuRbKL-DdUG8EHtwpdpIo7EO2en
MODDULAR_GRAPH_STRUCTURED_DATA_DIR=1
MODDULAR_GRAPH_GOOGLE_SERVICE_ACCOUNT_JSON={...}
```

Share the `Modular Ontology` Google Drive folder with the service account email as Editor. On cold start, the API downloads:

- `00_Admin/users.json`
- `00_Admin/mcp_remote.json`
- `01_Database/moddular_graph.sqlite3`
- `02_Ontology_Packs/indexed/*.zip`

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
- project and pack-link changes update `01_Database/moddular_graph.sqlite3`
- ontology pack uploads create or update ZIPs in `02_Ontology_Packs/indexed/`

Force a full write-back as an admin:

```powershell
POST /api/admin/storage/google-drive/write-back
POST /api/admin/storage/google-drive/write-back?include_packs=true
```

For local development with an authenticated Google Cloud SDK, start the server with:

```powershell
$env:MODDULAR_GRAPH_GOOGLE_DRIVE_FOLDER_ID="1qBTaqsuRbKL-DdUG8EHtwpdpIo7EO2en"
$env:MODDULAR_GRAPH_GOOGLE_DRIVE_USE_GCLOUD_AUTH="1"
$env:MODDULAR_GRAPH_GCLOUD_COMMAND="C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
python -m uvicorn moddular_graph.app:app --host 127.0.0.1 --port 8000
```

Demo sessions:

- Admin: `admin@moddular.local` / `admin123!`
- Member: `member@moddular.local` / `member123!`

For a configurable local user file, copy `data/users.example.json`, edit users and password hashes, then set:

```powershell
$env:MODDULAR_GRAPH_USERS_FILE="D:\path\to\users.json"
```

Only admin sessions can upload ontology ZIP packs. Successful uploads are immediately ingested into the SQLite ontology index.

Admin sessions can also rebuild the persistent SQLite ontology index from the current ZIP packs. The index is stored at `data/moddular_graph.sqlite3` and caches pack summaries, Markdown evidence documents, sampled graph nodes, and sampled graph edges.

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

Create a temporary public HTTPS URL for ChatGPT with Cloudflare Tunnel:

```powershell
.\scripts\start_chatgpt_mcp_tunnel.ps1
```

The script writes the generated URL to `data/mcp_remote.json`, restarts the Remote MCP server with the tunnel host allowed, and the MCP URL panel displays the usable `https://...trycloudflare.com/mcp` address. This quick-tunnel URL is temporary; run the script again if the tunnel process stops.

For a public domain, allow that host before starting the server:

```powershell
$env:MODULAR_GRAPH_MCP_HOST="0.0.0.0"
$env:MODULAR_GRAPH_MCP_ALLOWED_HOSTS="graph.your-company.com,127.0.0.1:*,localhost:*"
$env:MODULAR_GRAPH_MCP_ALLOWED_ORIGINS="https://chatgpt.com,https://chat.openai.com"
.\scripts\run_remote_mcp.ps1
```

Verify the HTTP Remote MCP endpoint:

```powershell
python scripts\verify_mcp_http.py
```

Optional OpenAI synthesis:

```powershell
$env:OPENAI_API_KEY="..."
$env:MODDULAR_GRAPH_OPENAI_MODEL="your-model"
```

Then call `/api/query` or `ask_pack_question` with `use_openai: true`. Without those settings the system stays on local Graph RAG mode.

Optional local Gemma 4 synthesis through a secure tunnel:

```powershell
ollama list
ollama ps
Invoke-RestMethod `
  -Uri http://127.0.0.1:11434/api/chat `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"model":"gemma4:12b-it-q4_K_M","messages":[{"role":"user","content":"안녕. 한 문장으로 답해줘."}],"stream":false}'
```

Start the token-protected local proxy:

```powershell
.\scripts\start_local_ai_proxy.ps1
```

Start the Cloudflare Quick Tunnel:

```powershell
.\scripts\start_local_ai_tunnel.ps1
```

Copy `publicBaseUrl` from `data/local_ai_remote.json` and `token` from `data/local_ai_proxy.json` into Vercel production env:

```powershell
MODDULAR_GRAPH_USE_OLLAMA=1
MODDULAR_GRAPH_OLLAMA_URL=https://example.trycloudflare.com
MODDULAR_GRAPH_OLLAMA_MODEL=gemma4:12b-it-q4_K_M
MODDULAR_GRAPH_OLLAMA_TOKEN=<token from data/local_ai_proxy.json>
MODDULAR_GRAPH_OLLAMA_TIMEOUT=180
```

The proxy only exposes `/api/tags` and `/api/chat`, requires `X-Modular-AI-Token`, and forwards requests to local Ollama at `http://127.0.0.1:11434`.
