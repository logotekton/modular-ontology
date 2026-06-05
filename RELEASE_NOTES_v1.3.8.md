# Manus Harness v1.3.8 Patch Notes

v1.3.8 is a small follow-up from the blog UI pilot. The visual QA gate from v1.3.7 worked, but the static reconstruction density counter still favored dashboard/portal arrays. Blog screens express density through categories, posts, topics, moods, and writing challenges, so those structures now count toward `min_data_rows`.

## Changes

- Bumped package metadata from `1.3.7` to `1.3.8`.
- Extended `DesignLinter._count_data_rows()` with blog-style arrays:
  - `categories`
  - `posts`
  - `postCards`
  - `creators`
  - `topics`
  - `moods`
  - `challenges`
- Added a regression test proving that blog reconstructions can satisfy density contracts without fake tables.

## Verification

- Harness test suite: `26 passed`.

## Compatibility

This is a patch release. No public API signatures changed.
