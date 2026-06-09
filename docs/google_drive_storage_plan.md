# Modular Ontology Google Drive Storage Plan

Target Google Drive folder:

https://drive.google.com/drive/folders/1qBTaqsuRbKL-DdUG8EHtwpdpIo7EO2en

Recommended structure:

```text
Modular Ontology/
  00_Admin/
    users.json
    mcp_remote.json
    access-policy.md
  01_Database/
    modular_ontology.sqlite3
    snapshots/
      YYYY-MM-DD_HH-mm-ss/
  02_Ontology_Packs/
    incoming/
    indexed/
    archive/
  03_Projects/
    Samcheok Building B/
      source-json/
      ontology-packs/
      reports/
    Yeoju Modular Dormitory/
      source-json/
      ontology-packs/
      reports/
  04_MCP/
    server-config/
    connection-guides/
    tool-manifests/
  05_Backups/
    daily/
    manual/
  99_Archive/
```

Runtime data files:

- `01_Database/modular_ontology.sqlite3`: SQLite graph/document index
- `00_Admin/users.json`: companies, users, approvals, roles
- `00_Admin/mcp_remote.json`: public MCP endpoint settings
- `02_Ontology_Packs/indexed/*.zip`: ingested ontology pack ZIP files

Vercel runtime sync:

- Set `MODULAR_ONTOLOGY_GOOGLE_DRIVE_FOLDER_ID=1qBTaqsuRbKL-DdUG8EHtwpdpIo7EO2en`.
- Set `MODULAR_ONTOLOGY_STRUCTURED_DATA_DIR=1`.
- Preferred auth: set `MODULAR_ONTOLOGY_GOOGLE_SERVICE_ACCOUNT_JSON` to a Google service-account JSON value and share the Drive folder with that service account email as Editor.
- Alternatives: `MODULAR_ONTOLOGY_GOOGLE_DRIVE_ACCESS_TOKEN` for a short-lived OAuth token, or `MODULAR_ONTOLOGY_GOOGLE_DRIVE_API_KEY` only if the folder/files are publicly readable.
- Optional cache control: `MODULAR_ONTOLOGY_GOOGLE_DRIVE_SYNC_TTL_SECONDS`, default `300`.

The Vercel function downloads Drive files into `/tmp/modular-ontology` on startup, then the existing SQLite and ZIP-pack code reads that local copy.

Runtime write-back:

- Auth/admin writes update `00_Admin/users.json`.
- Project and pack-link writes update `01_Database/modular_ontology.sqlite3`.
- Pack uploads update or create ZIP files in `02_Ontology_Packs/indexed/`.
- `POST /api/admin/storage/google-drive/write-back` pushes the current users file and SQLite DB.
- `POST /api/admin/storage/google-drive/write-back?include_packs=true` also pushes indexed ZIP packs.

Important:

SQLite should not be edited concurrently from multiple PCs through Google Drive sync.
For production multi-user access, move runtime storage to PostgreSQL or a server disk.
Use Google Drive as the runtime backing store only while one server is the active writer.
