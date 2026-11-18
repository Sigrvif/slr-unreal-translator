# E2E checklist — Phase 6 (widget-blueprint bytecode strings)

Build under test: `dist/SLRUnreal-win.zip` (9,052,404B, 69 entries, smoke
census text_entries=1001) — installed to
`<dev-root>/SLR Translator\www\addons\SLRUnreal` (md5-verified against stage).

## Prepared for you (no building needed)

`<dev-root>/UE\Boot Tests\9-Widget-Strings\` — README + two sample-translation
patches built through the addon's own export path:

- `SampleGameA_widget_strings\` — 3 rows (save toast, save-dialog line,
  hypnotism-slot hint) via the IoStore patch path (.utoc+.ucas).
- `SampleGameB_widget_strings\` — 49 rows (guild registration scene, menu
  labels, catacombs quest dialogue, hotel fast-travel prompt — includes a
  .umap-hosted row) via the pak path.

## Boot test (user)

1. Copy the patch over a COPY of each game (keep layout; paths in README).
2. Look for: the translated lines IN ENGLISH in the UI/dialogue, Japanese
   everywhere else (sample patch), no crashes/garbling/layout breaks.

## What changed for users

- The former "≈600 widget strings reported as warnings" limitation is
  CLOSED: those strings are now normal translatable rows (EF +583, ST +9).
- Translations longer than a script string's original slot are skipped
  with a reported reason (rows_skipped / backend skipped_detail) — never
  truncated, never corrupting.

## Covered by automated tests

- backend `tests/test_bcstrings.py` (10) + updated census pins across
  test_textscan / test_scenegraph / test_slr_cli / test_exporttext —
  full suite 189 passed. Addon suite 26/26 (addon untouched this phase).
