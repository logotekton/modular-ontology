# Manus Harness v1.3.7 Patch Notes

v1.3.7 adds a stricter visual-quality gate for image-to-UI reconstruction. Earlier patches blocked large structural drift, but the third pilot showed that a screen could still pass while looking more like a polished mockup than a close reconstruction. This patch lets the harness enforce post-render similarity, source-region alignment, required asset usage, typography cues, and spacing rhythm.

## Changes

- Bumped package metadata from `1.3.6` to `1.3.7`.
- Added `DesignLinter.lint_visual_qa_report(qa_report, contract)`.
- Added post-render QA checks:
  - `min_visual_fidelity_score`
  - `max_pixel_diff_ratio`
  - `required_region_bboxes`
  - `min_region_iou`
- Extended `lint_reconstruction_contract()` with stricter static reconstruction cues:
  - `required_assets`
  - `typography_contract`
  - `spacing_contract`
  - `strict_assets`
  - `strict_typography`
  - `strict_spacing`
- Added three regression tests:
  - strict visual cue enforcement
  - low screenshot similarity and bbox drift blocking
  - passing visual QA metrics acceptance

## Why

Prompt-only control is too soft, and structural contracts alone do not fully protect visual fidelity. A search portal can preserve the right sections and still lose the source image's asset treatment, spacing rhythm, and pixel-level similarity. v1.3.7 gives the harness a stronger second gate after Playwright/Pixelmatch screenshots are produced.

## Example Visual QA Contract

```json
{
  "min_visual_fidelity_score": 0.82,
  "max_pixel_diff_ratio": 0.12,
  "required_region_bboxes": ["search-hero", "shopping", "seo-insight"],
  "min_region_iou": 0.65
}
```

## Verification

- Harness test suite: `25 passed`.
- This is a patch release. No public API signatures were removed.
