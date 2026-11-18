# SLRUnreal Phase 4b E2E checklist

## Automated (all green at build time)

- Python suite: 137 passed (backend formats, patching, export, demosaic,
  keyscan incl. real-game discovery; slr_cli contract subprocess tests).
- JS suite: 23 passed (backend transport + PATH_KEYS additions, UE detector
  layouts, projectBuilder row mapping + export fill-back).
- Release: dist/SLRUnreal-win.zip + .7z verified (single SLRUnreal/ folder,
  required payload, no dev leakage — keys.json and fixtures excluded) and
  the STAGED runtime+backend smoke scan returns the exact 992-entry census
  on the example game.

## Manual (user-run, once)

1. Restart SLR Translator (the addon was installed to
   www/addons/SLRUnreal/ while SLR was running).
2. New Project → the SLRUnreal slide should appear (after SLRUnity's).
3. Pick the SampleGameA folder (or its shipping exe) → Create project:
   scan + extract run with progress lines; the project opens with ~992
   rows across 36 files.
4. Repeat for SampleGameB if installed: the scan must PASS the key step
   with no key supplied (offline discovery; first run caches
   backend/keys.json inside the addon folder) and produce ~7,257 rows.
5. Translate a handful of rows (anything you like).
6. Export (folder mode) → rebuilt containers appear in the target folder.
7. Apply to a COPY of the game → boot → translated lines visible
   (the Phase 4a boot folders demonstrated this pipeline manually).

## Known gaps (documented, not bugs)

- SampleGameB widget-blueprint text (postfix shape) not extracted.
- Baked-in CG overlays cannot be revealed (pixels destroyed at draw time).
- Fonts: Phase 5 lead (UE font assets; SLRUnity-parity font swap).
