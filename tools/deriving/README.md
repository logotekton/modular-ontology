# Deriving

`deriving` is the isolated BIMGraph AI experiment layer for turning Revit raw
JSONL exports into project-specific derived ontology artifacts.

The Revit add-in should stay a raw fact extractor. This tool reads those raw
files, profiles the project, assigns bootstrap classifications, writes coverage
logs, and prepares LocalCrab/OpenCrab-compatible derived data.

This folder intentionally does not replace `tools/ontology-builder`. It gives us
a separate test surface for BIMGraph AI before any LocalCrab fork is vendored or
deeply customized.

## Pipeline

```text
Revit Add-in raw export
-> deriving profile/build
-> derived JSONL
-> BIMGraph AI ontology pack/retrieval feature
```

## Input

An export folder containing some or all of:

```text
elements.jsonl
materials.jsonl
quantities.jsonl
sheets.jsonl
sheet_pdfs.jsonl
sheet_dxfs.jsonl
view_dxfs.jsonl
drawing_entities.jsonl
views.jsonl
annotations.jsonl
schedules.jsonl
schedule_cells.jsonl
graph_edges.jsonl
manifest.json
```

## Output

```text
derived/
  manifest.json
  project_profile.json
  bim_objects.jsonl
  material_facts.jsonl
  quantity_facts.jsonl
  drawing_documents.jsonl
  schedule_tables.jsonl
  schedule_rows.jsonl
  drawing_evidence.jsonl  # PDF/DXF export index plus DXF entity evidence
  relationships.jsonl
  classification_decisions.jsonl
  source_coverage.jsonl
```

The bootstrap classifier is deliberately conservative. It is not the final AI
judgment layer. Future BIMGraph AI runs should replace or override the
classification decisions while preserving the same output contract:

- every derived row has `source_refs`
- every raw row has coverage
- every keep/exclude decision has a reason and confidence
- raw files remain the lossless source of truth

## Usage

```powershell
python tools/deriving/deriving.py `
  --raw-dir "C:\Users\...\Documents\OpenCrab\RevitProjectGraphExports\..." `
  --out-dir "C:\Users\...\Documents\OpenCrab\RevitProjectGraphExports\...\derived"
```

Use `--dry-run` to print the profile without writing derived files.

## Build OpenCrab Pack Suite

After `derived/` exists, build separate OpenCrab Cloud Pack outputs per derived
JSONL domain:

```powershell
python tools/deriving/build_opencrab_pack_suite.py `
  --derived-dir "C:\Users\...\Documents\OpenCrab\RevitProjectGraphExports\...\derived" `
  --validate-zips `
  --zip
```

This writes:

```text
derived/opencrab_packs/
  pack_catalog.json
  pack_descriptions.md
  build_summary.json
  zips/
    bim_objects-ontology-pack.zip
    quantity_facts-01-ontology-pack.zip
    relationships-01-ontology-pack.zip
  bim_objects/
    ...-ontology-pack/
      README.md
      pack.json
      manifest.json
      cloud/documents.jsonl
      cloud/chunks.jsonl
      graph/nodes.jsonl
      graph/edges.jsonl
      00_index/
      01_sources/
      02_schema/
      03_graph/
      04_mappings/
      05_hypotheses/
      06_reports/
      07_examples/
      opencrab/
  material_facts/
  quantity_facts/
  drawing_documents/
  schedule_tables/
  schedule_rows/
  drawing_evidence/
  relationships/
```

The suite builder follows the local `ontology-pack-builder` skill output shape:
each ZIP exposes `manifest.json`, `cloud/documents.jsonl`,
`cloud/chunks.jsonl`, `graph/nodes.jsonl`, and `graph/edges.jsonl`, while the
pack folder also includes `00_index` through `07_examples` and `opencrab`.

Large derived domains are sharded by semantic group and row count so one huge
Revit graph does not become one oversized pack. Audit logs
(`classification_decisions.jsonl`, `source_coverage.jsonl`) are excluded by
default and can be built with `--include-audit-packs`.

The legacy `build_opencrab_pack.py` script is retained only as a debugging
adapter for a single combined pack and now requires `--legacy-single-pack`.
BIMGraph AI product flow should use `build_opencrab_pack_suite.py`.
