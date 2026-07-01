# Deriving Adapter Plan

Goal: adapt OpenCrab's pack contract to BIMGraph AI Revit data without
hardcoding project-specific categories.

## Direction

1. Keep Revit exporter as a raw extractor.
2. Let Deriving build project-specific derived JSONL.
3. Convert each user-facing derived JSONL domain into its own OpenCrab Cloud Pack.
4. Keep BIMGraph-specific query routing separate from upstream OpenCrab internals.

## Mapping

| Revit raw source | Deriving derived output | Pack target |
| --- | --- |
| `elements.jsonl` | `bim_objects.jsonl` | `opencrab_packs/bim_objects/*-ontology-pack.zip` |
| `materials.jsonl` | `material_facts.jsonl` | `opencrab_packs/material_facts/*-ontology-pack.zip` |
| `quantities.jsonl` | `quantity_facts.jsonl` | `opencrab_packs/quantity_facts/*-ontology-pack.zip` |
| `sheets.jsonl` | `drawing_documents.jsonl` | `opencrab_packs/drawing_documents/*-ontology-pack.zip` |
| `sheet_pdfs.jsonl` | `drawing_evidence.jsonl` | drawing evidence packs |
| `sheet_dxfs.jsonl` | `drawing_evidence.jsonl` | drawing evidence packs |
| `view_dxfs.jsonl` | `drawing_evidence.jsonl`, `relationships.jsonl` | drawing evidence and relationship packs |
| `schedules.jsonl` | `schedule_tables.jsonl` | `opencrab_packs/schedule_tables/*-ontology-pack.zip` |
| `schedule_cells.jsonl` | `schedule_rows.jsonl`, `drawing_evidence.jsonl` | `opencrab_packs/schedule_rows/*`, `opencrab_packs/drawing_evidence/*` |
| `graph_edges.jsonl` | `relationships.jsonl` | `opencrab_packs/relationships/*-ontology-pack.zip` |
| `views.jsonl` | `drawing_evidence.jsonl`, `relationships.jsonl` | drawing evidence and relationship packs |
| `annotations.jsonl` | `drawing_evidence.jsonl` | drawing evidence packs |
| PDF/DWG extraction | `drawing_evidence.jsonl` | drawing evidence packs |

`classification_decisions.jsonl` and `source_coverage.jsonl` are audit outputs,
not default retrieval packs. They can be emitted as optional audit packs when
the workflow needs classification review or raw-to-derived coverage validation.

Each generated pack follows the local `ontology-pack-builder` skill shape:

```text
pack-folder/
|-- README.md
|-- pack.json
|-- manifest.json
|-- cloud/documents.jsonl
|-- cloud/chunks.jsonl
|-- graph/nodes.jsonl
|-- graph/edges.jsonl
|-- 00_index/
|-- 01_sources/
|-- 02_schema/
|-- 03_graph/
|-- 04_mappings/
|-- 05_hypotheses/
|-- 06_reports/
|-- 07_examples/
`-- opencrab/
```

Large domains such as `quantity_facts.jsonl` and `relationships.jsonl` may be
sharded by semantic key and row count. This keeps individual packs smaller and
keeps graph edge counts below LocalCrab/OpenCrab validation limits.

## BIMGraph Query Modes

The adapter should preserve enough structure for these natural-language modes:

- `inventory_query`: object/category/type lists and counts
- `spec_query`: object specs with materials, quantities, and drawing evidence
- `quantity_query`: material/object quantity aggregation
- `drawing_evidence_query`: sheet, view, annotation, schedule, PDF/DWG evidence
- `schedule_query`: schedule tables and reconstructed rows
- `quality_check_query`: missing links, low-confidence rows, capped edges
- `export_query`: CSV/Excel/report-ready tabular output

## Quality Gates

- Every raw row appears in `source_coverage.jsonl`.
- Every generated pack has Cloud Pack ingest files.
- Every generated ZIP follows the OpenCrab Data ZIP allowlist.
- Every graph edge endpoint exists within its pack.
- Excluded rows preserve decision, reason, and confidence.
- Capped relationships such as view-element references are marked in quality.
- Low-confidence schedule/annotation records remain searchable but reviewable.

## Adapter v0 Decision

The first suite adapter intentionally maps BIMGraph domain nodes to OpenCrab's
base `concept/Entity` grammar. This avoids introducing a BIM-specific grammar
extension before the query behavior is tested. Revit/BIM semantics are preserved
inside node and edge/chunk properties:

- `properties.bimgraph_node_kind`
- `properties.bimgraph_relation`
- `properties.importance`
- `properties.source_refs`

Later, once the query modes stabilize, we can promote frequently used BIMGraph
types into an explicit grammar extension such as `BIMObject`, `MaterialFact`,
`QuantityFact`, `DrawingDocument`, and `ScheduleRow`.

## Adopted Drawing Dual-Parsing Direction

Use Revit API as the semantic source of truth and DXF as final 2D drawing
evidence. Do not try to rebuild BIM from DXF alone.

Adopt now:

- Export sheet DXF for the final plotted sheet context.
- Export placed-view DXF to isolate graphics by Revit view before matching.
- Keep PDF as human-readable evidence and downstream visual parsing input.
- Treat `FilteredElementCollector(document, viewId)` as candidate visibility,
  not proof that an element is plotted; proof comes from matched DXF fragments.
- Route `sheet_pdfs.jsonl`, `sheet_dxfs.jsonl`, `view_dxfs.jsonl`,
  `views.jsonl`, `annotations.jsonl`, and `schedule_cells.jsonl` into
  `drawing_evidence.jsonl`.

Defer until the parser stage:

- ACadSharp DXF entity parsing.
- NetTopologySuite geometry operations and affine transform fitting.
- Graphic fragment grouping from raw DXF entities.
- Confidence scoring and unmatched model/graphic reports.
- 2D review UI and highlight workflow.

Do not adopt yet:

- SQLite as a required dependency; JSONL remains the interchange format.
- Project-specific hardcoded DXF layer names as a core assumption.
- Automatic Revit model edits from drawing mismatches.
- Full BIM reconstruction from DXF-only evidence.
