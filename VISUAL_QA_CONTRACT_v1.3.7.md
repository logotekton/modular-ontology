# Visual QA Contract v1.3.7

This contract is the post-render companion to the static reconstruction contract. Use it after the generated React app has been rendered by Playwright and compared against the source image.

## Static Reconstruction Additions

`lint_reconstruction_contract(code, contract)` now accepts:

```json
{
  "required_assets": [
    {
      "label": "hero mascot crop",
      "aliases": ["assets/hero-mascot.png", "hero-mascot"]
    }
  ],
  "typography_contract": {
    "required_tokens": ["font-weight", "line-height"],
    "min_typography_tokens": 3
  },
  "spacing_contract": {
    "required_tokens": ["gap:", "padding:"],
    "min_spacing_tokens": 3
  },
  "strict_assets": true,
  "strict_typography": true,
  "strict_spacing": true
}
```

## Post-Render Visual QA

`lint_visual_qa_report(qa_report, contract)` accepts QA output like:

```json
{
  "visual_fidelity_score": 0.87,
  "pixel_diff_ratio": 0.08,
  "region_bboxes": [
    { "name": "search-hero", "iou": 0.82 },
    { "name": "shopping", "iou": 0.75 }
  ]
}
```

And a blocking contract like:

```json
{
  "min_visual_fidelity_score": 0.82,
  "max_pixel_diff_ratio": 0.12,
  "required_region_bboxes": ["search-hero", "shopping"],
  "min_region_iou": 0.65
}
```

## Recommended Pipeline Placement

1. Analyze source image and produce a static reconstruction contract.
2. Generate React code.
3. Run `lint_reconstruction_contract()` before writing or accepting code.
4. Build and render with Playwright.
5. Produce Pixelmatch and region bbox metrics.
6. Run `lint_visual_qa_report()` before delivery.
7. If blocked, feed the findings into the next revision loop.
