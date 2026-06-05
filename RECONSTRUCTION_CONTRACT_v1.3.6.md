# Visual Fidelity Contract v1.3.6

v1.3.6 keeps the v1.3.5 reconstruction contract and adds better support for portal-style screens.

## Contract Fields

```json
{
  "theme_mode": "light",
  "required_regions": ["portal-sidebar", "top-tabs", "search-hero"],
  "required_text": ["Mingle Search", "SEO", "Beta"],
  "required_kpi_labels": [],
  "min_table_count": 0,
  "min_kpi_count": 0,
  "min_data_rows": 18,
  "strict_density": false
}
```

## Data Density

`min_data_rows` now recognizes both dashboard/table structures and portal/content structures.

Dashboard-style arrays:

- `rows`
- `machines`
- `alerts`
- `inspections`
- `schedule`
- `orders`
- `workOrders`
- `productionRows`
- `equipmentRows`
- `lines`

Portal-style arrays:

- `keywords`
- `shortcuts`
- `shopping`
- `places`
- `trends`
- `music`
- `feed`
- `tabs`
- `sidebar`

## Intended Use

Use this contract when the task is image-to-UI reconstruction. For open-ended UI generation, keep using the generic `DesignLinter`.
