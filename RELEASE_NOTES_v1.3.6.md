# Manus Harness v1.3.6 Patch Notes

v1.3.6 is a small fidelity patch from the third pilot test: a bright pastel search/SEO portal reconstruction. The generic reconstruction contract already worked well for MES dashboards, but portal screens often express density through named arrays such as shortcuts, shopping cards, places, feeds, and keyword chips instead of table rows.

## Changes

- Bumped package metadata from `1.3.5` to `1.3.6`.
- Updated the FastAPI app metadata version to `1.3.6`.
- Extended `DesignLinter._count_data_rows()` to count portal-style arrays:
  - `keywords`
  - `shortcuts`
  - `shopping`
  - `places`
  - `trends`
  - `music`
  - `feed`
  - `tabs`
  - `sidebar`
- Added a regression test proving that search portal reconstructions can satisfy `min_data_rows` without fake table markup.
- Added `.gitignore` and `.tarignore` entries so cache/build artifacts do not leak into patch archives.

## Why

The v1.3.5 reconstruction contract closed the largest drift risk: prompt-only image reproduction could become a visually nicer but less faithful redesign. The third pilot showed the next smaller gap: non-table UIs need density checks too. Search portals, ecommerce surfaces, and content feeds should be measured by their real repeated structures rather than being pushed toward tables.

## Verification

- Harness test suite: `22 passed`.
- Search portal pilot:
  - React/Vite build passed.
  - Generic `DesignLinter`: safe.
  - Reconstruction contract: safe.
  - Browser QA showed no console errors and no horizontal overflow at desktop/mobile widths.

## Compatibility

This is a patch release. No public API signatures changed.
