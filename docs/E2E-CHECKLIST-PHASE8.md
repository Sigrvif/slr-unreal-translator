# E2E checklist — Phase 8 (portable projects, standalone demosaic, IoStore patch containers)

Build under test: `dist/SLRUnreal-win.zip` (9,075,628B, 73 entries, smoke
census text_entries=1092 — ST scan through the packaged runtime).
Installed to `<dev-root>/SLR Translator\www\addons\SLRUnreal` (md5 match).

## Offline gates (all PASS, 2026-10-26)

- [x] Full backend suite: 207 tests (incl. 8 new Phase 8a pins in
      test_stage.py/test_patchpak.py, 4 Phase 8b/8c pins in
      test_patchio_demosaic.py — real-EF portable export round trip,
      real-ST portable export round trip, real-ST/real-EF standalone
      demosaic).
- [x] Addon suite: 27/27 against dist/stage-win (fake-jQ loader
      contract, create-click regression, builder).
- [x] Release build: verify OK, single top-level folder, runtime
      python, smoke scan green.
- [x] Real-game smoke beyond tests: EF CLI export_patch (3 rows →
      10 KB patch pak, verify ok); ST export_patch (3 rows → 300 B
      utoc + 1.8 KB ucas, verify ok); demosaic CLI (EF 1 op medium,
      ST 7 ops, both verify ok).

## User boot gates (prepared, awaiting results)

- [x] **Boot 11 — EF patch pak: PASS (2026-10-26, user)** — "launches,
      the edits show, and deleting the SampleGameB-Windows_SLR_P.pak
      reverts it back." Pak mount precedence, the encrypted patch
      index, the synthesized FDI, and delete-to-uninstall all
      confirmed in-game.
- [~] **Boot 12 — standalone demosaic** (12-Demosaic-Standalone) —
      PARTIAL (2026-10-26, user): both games LAUNCH clean — both patch
      families mount, including the first-ever IoStore patch-pair
      boot. Visual outcome unconfirmed: ST's 7 models (mosaic gone in
      adult scenes) and EF's WG_H_Event02 clean event image not yet
      checked.
- [~] **Boot 13 — ST IoStore patch** (13-ST-Patch-IoStore) —
      PARTIAL (2026-10-26, user): LAUNCHES clean — package-store
      registration with an empty directory index is ACCEPTED (no
      missing-package errors; the riskiest mount question of the
      phase). The EN_ステータス "Test line." rows not yet located
      in-game; visual confirmation pending.
- [ ] In-app: create a project for EF and ST with the new build —
      create now runs the one-pass `stage` command (expect a
      "Staging set: N files (X MiB)" console line) and Save Project
      should produce a .slrproj carrying the staging tree (EF ~90 MB
      compressed less — 7z solid should shrink it further).
- [ ] In-app: export a translated row WITHOUT the game folder
      present (rename/move it temporarily) — patch mode must succeed
      purely from the slrproj's staging set.
- [ ] In-app: "Demosaic (standalone)" section with the project open →
      pick output folder → patch container + applied/skipped report.

## Failure triage

- Boot 11 fails (serialization error): the patch-pak index or FDI
  grammar differs from what the engine's mount path accepts — capture
  the exact message; compare the patch pak's footer/index against a
  real _P.pak from any UE game if available.
- Boot 13 fails (missing package / chunk): the empty directory index
  or the store-entry tail is wrong — the container header's minimal
  tail (all sections empty) may need the source's soft-references
  serial info instead. Compare against global.utoc's header (which
  the game does mount).
- Boot 12 ST fails but 13 passes: the demosaic patch container's
  registration works for text but not texture-bearing packages —
  check whether the engine needs the shader-map store entries
  (we preserve them; the failing shape would say otherwise).
