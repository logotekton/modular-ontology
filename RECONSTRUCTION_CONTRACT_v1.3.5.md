# Visual Fidelity Contract v1.3.5

## Purpose

The Visual Fidelity Contract is the blocking rule set for image-to-React reconstruction. It prevents an agent from passing a visually attractive redesign when the task is to reproduce a source image.

## Minimal Example

```json
{
  "theme_mode": "light",
  "required_regions": [
    "topbar",
    "sidebar",
    "kpi_strip",
    "process_flow",
    "production_table",
    "alerts",
    "quality_queue",
    "equipment_table",
    "work_order_table"
  ],
  "required_text": [
    "OEE",
    "Throughput",
    "Defect Rate",
    "Downtime",
    "Work Orders"
  ],
  "required_kpi_labels": [
    "OEE",
    "Throughput",
    "Defect Rate",
    "Downtime",
    "Work Orders"
  ],
  "min_table_count": 4,
  "min_kpi_count": 5,
  "min_data_rows": 25,
  "strict_density": true
}
```

## Enforcement Rules

- `theme_mode_mismatch`: source theme mode changed.
- `required_region_missing`: a required source-image region is absent.
- `required_text_missing`: required source text is absent.
- `minimum_table_count_missing`: table density is too low.
- `minimum_kpi_count_missing`: KPI count is too low.
- `minimum_data_rows_missing`: data density is too low.

## Recommended Use

Use this contract only when the user asked for screenshot/image reconstruction. For open-ended UI generation, keep using the generic DesignLinter only.

## MES Pilot Baseline

The MES dashboard source image requires:

- Light content surface.
- Dark global topbar and left navigation.
- Five KPI cards.
- Process flow strip.
- Production line status table.
- OEE trend panel.
- Active alerts list.
- Quality inspection queue table.
- Equipment health table.
- Batch/work order schedule table.

v1.3.5 blocks the v1 dark dashboard drift and passes the v2 table-dense reconstruction.
