# E2E checklist — Phase 5b (font replacement)

Build under test: `dist/SLRUnreal-win.zip` (9,049,615B, 69 entries, smoke
census 992 exact) — installed to `<dev-root>/SLR Translator\www\addons\SLRUnreal`
(md5-verified against the stage).

## Prepared for you (no building needed)

`<dev-root>/UE\Boot Tests\8-Fonts\` — README.txt + two fonts-only patches
produced by the addon's own export path:

- `SampleGameA_fonts\sample_a\Content\Paks\sample_a-Windows.pak`
  (21.9MB — 8 faces swapped, prefix format preserved, verify ok)
- `SampleGameB_fonts\SampleGameB\Content\Paks\SampleGameB-Windows.pak`
  (3.19GB — 8 faces swapped, raw format preserved, verify ok)

## Boot test (user)

1. Copy the patch contents over a COPY of each game (keep layout).
2. Launch. Look for: all text still renders (JP included — no tofu), the
   typefaces visibly changed (condensed CJK dialogue, narrow Latin UI).
3. Report anything missing/garbled or a crash with what preceded it.

**RESULT (2026-10-25): PASS — both games launch and the font renders.
The TTC subface fallback never appeared; that lever is closed.**

## In-app checks (optional, after boot passes)

- New Project → SLRUnreal → create a project → Export dialog shows
  "Unreal Export Options" with demosaic (2 fields) + **Replace game fonts**
  + two custom font fields with Browse… buttons (remembered across runs).
- Export with fonts ticked → console shows "Fonts swapped: N face(s)";
  a fonts-only export (no translated rows) is allowed and produces the
  font-bearing containers.
- New Project → SLRUnreal → Insert translated images: checkbox "Replace
  game fonts" above the run button; insert summary reports "fonts swapped:
  N face(s)".

## Covered by automated tests

- backend: `tests/test_fonts.py` (8) — read_font validation, .ufont format
  detector (raw / prefixed / unknown), repackage layout, both games' census
  (8 faces: 2 dialogue + 6 UI), ST staging format preservation, ST
  fonts-only export round trip with byte read-back, CLI `cmd_export` job
  shape with font paths + named FontError on a bad path. Full suite 179
  passed (178 + this file's CLI test).
- addon: 25/25 (`node --test`), `node --check` clean.
