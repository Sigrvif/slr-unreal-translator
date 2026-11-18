# SLRUnreal Phase 4b — Addon Shell, Runtime & Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The shippable SLR Translator addon: New Project entry, scan/extract project flow, translate in SLR, export rebuilt containers through SLR's export dialog, packaged as `dist/SLRUnreal-<os>.zip` releases — mirroring SLRUnity's architecture file-for-file.

**Architecture:** Thin JS shell over the Phase 3/4a backend (the contract already speaks SLRUnity's v2 envelope byte-for-byte — verified in Phase 3 Task 7). JS mirrors SLRUnity's addon (`<dev-root>/SLR Unity Translator\addon\`) with UE adaptations; embedded runtime identical to SLRUnity's (backend is pure stdlib). Release build + verifier per the SLRUnity v2.7 pattern.

**Tech Stack:** NW.js/JS (CommonJS, `node:test`), Python 3.10 embeddable runtime (fetched), PowerShell/zip release packaging.

**Spec:** `<dev-root>/SLR Unreal Translator\docs\specs\2026-10-22-slrunreal-design.md` (+ Appendix B)
**Canonical patterns (read-only references):** `<dev-root>/SLR Unity Translator\addon\` — `UnityAddon.js` (1,690 lines), `lib/{backend,paths,detector,projectBuilder}.js`, `tests/*.test.js` (`node:test`).

## Global Constraints

- Dev home `<dev-root>/SLR Unreal Translator`. No git — ledger `.sdd\2026-10-24-phase4b-addon\progress.md` (controller-created; tasks append). Snapshot at end.
- **SLR Translator is never modified** (standing constraint). Install target during dev: `<dev-root>/SLR Translator\www\addons\SLRUnreal\` (flat layout — addon files at package root).
- Do not modify SLRUnity's addon or dev tree — reference only.
- Zero third-party JS/Python deps (NW.js + Node stdlib + embedded python stdlib only).
- Key-material decision (PREFLIGHT RULING v2, supersedes v1 after user review): **key DISCOVERY is a product feature; no keys ship in the release.** Users' games carry hardcoded keys (the pak is its own oracle) — the addon discovers them offline and caches per game in `backend/keys.json` (user-side, created on first success, survives addon updates only if the updater preserves it — documented). Manual entry (hex/base64 paste) covers obfuscated/derived keys. slr_cli: on encrypted-index pak with no cached/manual key, run the exe candidate scan (bcrypt-validated against index block 0's mount-FString shape); manifest `aes_key` (inline hex) overrides. Dev fixtures/docs still never ship.
- Atomic writes; Windows-first (win runtime packaged; linux payload structural-only like SLRUnity 2.7 if runtime fetch is skipped).
- python 3.10.6 / pytest 8.3.5 / node (check `node --version` ≥ 18) / Git Bash.

## Verified facts (do not re-derive)

- SLRUnity New Project registration: `ui.newProjectDialog.addMenu({icon, descriptionBar, actionBar, goToSlide, at, slides})` queued at load (UnityAddon.js:291-300, guard 302-307); slide HTML via `$jq` (page-global, window.jQuery fallback); dir picking via invisible `nwdirectory` input (unitySelectDir, :93-101).
- Engine handlers: `engines.addHandler("unreal", "createProject", async args => project)` — args `{gameFolder, selectedFile, gameEngine, gameTitle, projectId, options}`; must resolve a project object with `files` (UnityAddon.js:654-700 shows the shape + resolveGameRoot + `common.initStaging` jobs-dir pattern). `engines.addHandler("unreal", "exportHandler", async (targetPath, options) => true|false)` — options.mode "dir"|"zip"; build translations from `proj.files` rows `[jp, translation]`; overlay lifecycle `uiShowLoading/uiLine/uiLoadingEnd` exactly-once (UnityAddon.js:951-1050 documents the overlay discipline — mirror it).
- backend.js: port verbatim (140 lines); the ONLY change: `PATH_KEYS` gains `"out"` and `"aes_key_fixture"` (Phase 3 Task 7 finding — SLRUnity's list lacks them); runtime resolution identical (`runtime/python/python.exe`).
- slr_cli (ours) already implements scan/extract/export with the exact envelope; `export` manifest keys: `game_dir, translations (path to extract-table JSON), out, aes_key_fixture?` — `aes_key_fixture` becomes optional once keys.json exists (Task 4 wires it).
- Extract table shape: `{version, command, game_dir, graph, text_table: [{japanese, english, context, field, file, path_id, entry_index}], stats}` — `exporttext.load_translations` consumes it directly (graph-base index derivation, Phase 4a Task 3 fix round).
- package.json (SLRUnity): `{name, title, description, version, author, autoload: false, load: [...], docs}`; loader scans subfolders of www/addons (Phase 1 survey).
- Runtime: SLRUnity dev tree has `runtime/win` (fetched embeddable python) — `<dev-root>/SLR Unity Translator\runtime\` — our fetch script mirrors SLRUnity's `build/fetch_runtime.py` (read it as the reference).

---

### Task 1: Addon scaffold + lib port

**Files:** Create `addon/package.json`, `addon/lib/backend.js`, `addon/lib/paths.js`, `addon/lib/detector.js`, `addon/tests/backend.test.js`, `addon/tests/detector.test.js`, `addon/tests/paths.test.js`

**Requirements:**
- package.json: name/title SLRUnreal, `load: ["UnrealAddon.js"]`, docs.html, autoload false, version 0.1.0.
- backend.js: port SLRUnity's verbatim EXCEPT (a) PATH_KEYS += `"out"`, `"aes_key_fixture"`; (b) error string "Unity backend runtime not found" → "Unreal backend runtime not found"; (c) comment header noting the contract was byte-verified in Phase 3.
- paths.js: port (siblingDir/toForwardSlashes helpers as SLRUnity's — read its 26 lines and mirror).
- detector.js: `resolveGameRoot(fsh, selectedFile, gameFolder)` UE variant — from a picked path, find the UE root: walk UP until a directory containing `*/Content/Paks` (any game subfolder) or an `Engine/` sibling; accept the shipping exe (`*-Win64-Shipping.exe`) as the picker target; throw with a clear message when no container layout is found. (SLRUnity's detector.js:86 lines is the structural reference; the logic differs for UE.)
- Tests (`node --test addon/tests/`): backend.js (progress parsing, PATH_KEYS resolution incl. the two new keys, ok:false envelope resolution with stubbed spawn); detector (synthetic UE layouts: root picked, exe picked, nested Content/Paks, no-layout error).

### Task 2: scan progress + key discovery (`keyscan.py`)

**Files:** Create `backend/ut/keyscan.py`, `backend/keys.json` placeholder handling; Modify `backend/slr_cli.py`, `backend/ut/textscan.py` (progress hook); Create `backend/tests/test_keyscan.py`; Modify `backend/tests/test_slr_cli.py`

- **keyscan**: `discover_key(game_root, progress=None, max_candidates=None) -> dict` — locate the largest encrypted-index pak + shipping exe(s); parse PE section headers (stdlib `struct`) to enumerate constant-data ranges (.rdata/.data); slide a 32-byte window (step 4, 8/16-aligned first pass) as AES-256 candidates; validate each against the pak's OWN encrypted index block 0 (ECB decrypt via `aes.decrypt_ecb`): plausible mount FString = first i32 length in 1..1024 AND (length >= 4 → next bytes path-like) — a >=32-bit validator; on hit, CONFIRM by decrypting the full index and checking SHA-1 == footer.index_sha1 (the Phase 1 gate — zero false positives). Returns `{key_hex, source: "exe-scan", exe, offset}` or raises `KeyNotFoundError` (with manual-entry guidance). Progress callbacks every ~250k candidates (SLR stdout lines). Perf budget: bcrypt ~24µs/block → ~10-15 min for a 160 MB exe worst-case; rdata-only + alignment-first ordering typically ≪; `max_candidates` caps for tests.
- keys.json cache: `load_keys()` / `save_key(game_tag, key_hex)` — `backend/keys.json` (`{game_tag: {key_hex, source, added}}`); created on first save; tolerant of user edits (missing file = empty).
- slr_cli integration: scan/export with an encrypted-index pak and NO key in the manifest → load keys.json cache → miss → auto-run `discover_key` (manifest `"discover_keys": false` opts out) → on success `save_key` + proceed; manifest `"aes_key"` (inline hex) overrides everything (manual entry path from the UI).
- textscan/slr_cli progress: per-asset stdout lines (SLRUnity format) from scan_game.
- Tests: keyscan finds the EF key from the real game (slow-marked; cap via max_candidates on a targeted slice first, full-scan test optional); validator rejects wrong keys (mutated candidates); keys.json round-trip incl. corrupt-file tolerance; slr_cli auto-discovery subprocess on EF with empty cache (slow) and manual `aes_key` override (fast).

### Task 3: UnrealAddon.js — slide + createProject + exportHandler

**Files:** Create `addon/UnrealAddon.js`, `addon/lib/projectBuilder.js`, `addon/tests/projectBuilder.test.js`, `addon/tests/unrealAddonExport.test.js`; Create `addon/docs.html`, `addon/assets/help.html`, `addon/assets/icon.png` (rebind SLRUnity's icon art or a new simple PNG — no third-party assets).

- New Project slide (mirror UnityAddon.js:104-300 structure, UE wording): pick dir/exe → resolveGameRoot → `common.initStaging` → runBackend scan (progress into overlay) → runBackend extract (table JSON into cache `data/`) → `projectBuilder.buildProject(table, cache)` → resolve project.
- `projectBuilder.buildProject`: map `text_table` rows → SLR `files` shape — group rows by `file` (the asset path), `data: [[japanese, ""]]` per row, stash `entry_index` + `path_id` in a parallel `_slrunreal` meta object stored in `proj.options.slrunreal` (SLRUnity stores engine metadata the same way); write the FULL extract JSON into the cache (self-contained project, e2e-fix7 precedent) — export needs the graph.
- `engines.addHandler("unreal", "createProject", ...)` + `"exportHandler"`: export builds the translated table by loading the cached extract JSON and filling `english` from project rows (match by stored per-row key: asset+entry_index), writes `translations.json` to jobsDir, runs `command: "export"` with `out` = staging dir, then copies the returned container files into `targetPath` game-relative (mode "dir") — for mode "zip" mirror SLRUnity's zip writing (read its export zip section, ~1050-1200, and mirror). Overlay discipline per UnityAddon.js:924-950. Demosaic: export option checkbox "Reveal image-overlay materials ( SampleGameA)" + "…(SampleGameB event image, experimental)" mapping to `demosaic: true` in the manifest (slr_cli export gains `demosaic` passthrough — plan+apply inside exporttext when true; wire `include_medium` from the second checkbox).
- Export-command extension (backend): `export` manifest gains optional `"demosaic": {"sample_a": bool, "sample_b_medium": bool}` — exporttext applies plan_demosaic ops in the same staging pass (the Task 6 combined-export composition, now first-class: demosaic._sub on staged bytes pre-rebuild).
- Tests: projectBuilder (row mapping, meta round-trip); exportHandler logic with stubbed runBackend + real filesystem (targetPath dir write, translations JSON built from project rows); overlay idempotence (mirror SLRUnity's export test approach — read `tests/unityAddonExport.test.js` first).

### Task 4: Runtime fetch + packaging

**Files:** Create `build/fetch_runtime.py` (mirror SLRUnity's `build/fetch_runtime.py` — read it, adapt paths only), keys.json handling per Task 2 (runtime cache, not shipped)

- fetch: win embeddable python → `runtime/win/python/python.exe` (+ linux structural payload optional — mirror SLRUnity's --os flags).
- Smoke: staged runtime runs `slr_cli.py --manifest scan` against the example game (ST) → ok:true.
- keys.json is NOT shipped and NOT build-generated (v2 ruling): it is a user-side cache created at runtime. Add it to snapshot prunes and release-verifier FORBIDDEN?? no — verifier treats it as OPTIONAL (absent in a fresh install).

### Task 5: Release build + verifier

**Files:** Create `build/build_release.py`, `build/verify_package.py`, `build/README` updates; Test: `backend/tests/test_release_packaging.py` (unit-level: verifier rules against a synthetic staged tree)

- Mirror SLRUnity's `build/build_release.py` + `verify_package.py` (read both; adapt): stage `addon/` (prune tests) + `backend/` (slr_cli.py, ut/, requirements.txt if any, **keys.json**) + `runtime/<os>` → zip the stage ROOT (single top-level `SLRUnreal/` folder) → verify: exactly one top-level folder `SLRUnreal/`; required entries (package.json, UnrealAddon.js, lib/*.js, docs.html, assets/*, backend/slr_cli.py, backend/ut/*.py, backend/keys.json, runtime marker); **forbidden**: `backend/tests/`, `docs/`, `.sdd/`, `_snapshots/`, `__pycache__/`, `.pytest_cache/`, `node_modules/`, any `*index_facts.json`/`*demosaic_discovery.json`/`text_discovery.json` (dev fixtures), `*_export_for_ai`; package.json name == folder name; load files present. Win smoke: staged runtime + backend scan over ST → ok:true (mirrors SLRUnity's smoke_scan).
- Deliverables: `dist/SLRUnreal-win.zip` (+ .7z twin if 7za available, SLRUnity 2.7 pattern).

### Task 6: Install + E2E + snapshot + close

- Install into `<dev-root>/SLR Translator\www\addons\SLRUnreal\` (dev copy), restart SLR once, E2E per `docs/E2E-CHECKLIST-PHASE4B.md` (write it): New Project → SLRUnreal slide → pick ST → scan+extract → project created with rows → translate a handful in SLR → export (dir mode) → containers written → apply to a copy → boot → [EN] rows visible (the Phase 4a boot pattern, now through SLR).
- Suite (pytest 132 + new) + node --test all green; snapshot `_snapshots/phase4b-addon.zip` (source-only, keys.json excluded); final ledger entry.

---

## Self-review notes

- Spec coverage: §4 architecture's addon/ tree = Tasks 1-3; runtime = Task 4; release = Task 5 (spec §5 stage 7 delivery + Appendix B amended by the preflight key ruling); §5 stage 6 (translate) is SLR's own flow which the project creation plugs into. Fonts: explicitly Phase 5 (user-led; UE FontFace/Font asset spike) — noted in the ledger and E2E doc.
- The plan relies on mirroring referenced SLRUnity sections; every reference carries exact line numbers from the current SLRUnity 2.7 tree. Where UE logic genuinely differs (detector, projectBuilder mapping, export translation-table build), the shape is specified here.
- Type consistency: `entry_index` is the join key across extract JSON ↔ project meta ↔ export manifest (Phase 3/4a established contract); `file` (payload asset path) groups rows; PATH_KEYS/out/aes_key_fixture carried from the Phase 3 finding.
