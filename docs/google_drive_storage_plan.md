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
    moddular_graph.sqlite3
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

- `01_Database/moddular_graph.sqlite3`: SQLite graph/document index
- `00_Admin/users.json`: companies, users, approvals, roles
- `00_Admin/mcp_remote.json`: public MCP endpoint settings
- `02_Ontology_Packs/indexed/*.zip`: ingested ontology pack ZIP files

Important:

SQLite should not be edited concurrently from multiple PCs through Google Drive sync.
For production multi-user access, move runtime storage to PostgreSQL or a server disk.
Use Google Drive as a document, pack, and backup store unless a single server is the only writer.
