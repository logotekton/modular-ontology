# OpenCrab Upstream

Deriving uses OpenCrab/LocalCrab as an upstream reference for the BIMGraph AI
ontology feature.

## Source

- Repository: https://github.com/AlexAI-MCP/OpenCrab
- Local analysis clone: `tools/deriving/upstream/opencrab`
- Checked commit: `d34352cec9d99c755c1e891f811911461a460280`
- License declared by upstream `pyproject.toml`: `MIT`

The local analysis clone is intentionally ignored by Git. Keep it as a source
reference while we design the BIMGraph-specific fork/adapter boundary.

## Current Integration Stance

We are not importing OpenCrab wholesale into BIMGraph AI yet.

Current split:

```text
Revit Add-in
  raw JSONL extraction

tools/deriving
  raw -> derived JSONL
  source coverage
  drawing evidence
  bootstrap classification

OpenCrab upstream
  MetaOntology grammar reference
  Pack v1 ZIP contract
  graph/evidence/quality layout reference
  MCP/query/store design reference
```

Next adapter target:

```text
derived/
  bim_objects.jsonl
  material_facts.jsonl
  quantity_facts.jsonl
  drawing_documents.jsonl
  schedule_tables.jsonl
  schedule_rows.jsonl
  drawing_evidence.jsonl
  relationships.jsonl
  source_coverage.jsonl

-> OpenCrab Pack v1
  manifest.json
  graph/nodes.jsonl
  graph/edges.jsonl
  evidence/index.jsonl
  quality/report.json
  README.md
  sample_queries.json
```

## License Handling

Before vendoring or publishing a modified fork, preserve the upstream copyright
and license notice. If the upstream repository later adds a standalone
`LICENSE` or `NOTICE` file, copy it into the fork/vendor notice package.

