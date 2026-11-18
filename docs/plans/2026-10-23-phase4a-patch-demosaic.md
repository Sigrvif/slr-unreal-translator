# SLRUnreal Phase 4a — Translation Patching & Demosaic Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Phase 3 text tables into game-accepted translated containers, and find + implement the demosaic surface — end-to-end: translations → length-aware asset patches → container rebuilds via the Phase 2 writers → verified output + user boot artifacts.

**Architecture:** Pure-Python `ut/` continues. New: `patchtext.py` (length-aware FText/FString rewrites with per-asset offset re-basing), payload write-back + export orchestration over the `rewrite.py` kernel, demosaic discovery + patching (same asset-patch layer), `slr_cli export` command. The JS addon, embedded runtime, and release packaging are Phase 4b.

**Tech Stack:** Python 3.10+, pytest. References: retoc (zen structures), Phase 1-3 modules (all review-hardened), Phase 3 census + ledger carry items.

**Spec:** `<dev-root>/SLR Unreal Translator\docs\specs\2026-10-22-slrunreal-design.md`
**Phase 3 ledger (carry items):** `.sdd\2026-10-23-phase3-text\progress.md`

## Global Constraints

- Dev home `<dev-root>/SLR Unreal Translator`. No git — ledger `.sdd\2026-10-24-phase4a-patch\progress.md` (controller-created; tasks append). Source-only snapshot at the end.
- Games READ-ONLY; all rebuilt output to temp or the boot-tests area; real-game tests skip cleanly.
- Zero third-party code; key/DLL hygiene per spec Appendix B; patched magic `0x5A6F12E1` on all pak outputs (the Phase 2 writers already do this).
- **Length-aware patching only** — no same-length hacks: translations change string lengths; the patch layer re-bases offsets and updates sizes up the chain (asset → container), exactly like the SLRUnity export did.
- **Blank-English = not-patched** (Phase 3 final-review note): the patch contract skips rows whose translation is empty/None.
- Structure-aware IoStore patches only (boot-test T3 lesson): container-header (type 6) edits — needed when a zen package's size changes — go through a dedicated, tested path; the Phase 2 type-6 guard is relaxed only for that path.
- Atomic writes everywhere; python 3.10.6/pytest 8.3.5/Git Bash; `PYTHONIOENCODING=utf-8`.
- RESOLVED (user, 2026-10-24): boot variants 3a (chunk relocated, content identical) and 3d (wrong hash) BOTH LAUNCH WITHOUT ERROR — append/relocation mechanics confirmed engine-accepted; the engine does NOT verify per-chunk hashes at mount (we recompute them anyway). No fallback strategy needed; test 3's original crash was the blind content patch, not the writer.

## Verified facts (Phases 1-3, do not re-derive)

- TextEntry (asset, offset, kind, source): FText-Base patchable range `[offset+42, offset+46+2*(len(source)+1))` — length i32 at offset+42 (store `-(chars+1)`), UTF-16LE+NUL at offset+46; FString range `[offset, offset+4+2*(len(source)+1))`. ST offsets are zen-chunk-absolute; EF offsets are .uexp-relative (asset named after the .uexp).
- Census: ST 992 FText-Base (36 assets); EF 6,359 FText-Base + 898 FString (466 assets). W-shape (601 JP strings) excluded — permanent gap unless revisited. EF .uasset halves are metadata (Shape-B) — never patched.
- Zen package internals (ST): summary at chunk TAIL + trailing tag 0x9E2A83C1; export bundles carry per-export (command, local index, serial offset/size) — tier-grouped interleaved; FExportMapEntry stride 72B; the container-header chunk (type 6) holds per-package store entries incl. export_bundles_size — MUST be updated when a package chunk's length changes; ScriptObjects chunk (type 5, global.utoc) maps package ids.
- Legacy internals (EF): .uasset export table holds SerialOffset/SerialSize per export (serial data lives in the .uexp at those offsets); total_header_size == len(uasset); .uexp ends with the package tag.
- Writers (Phase 2, byte-identity goldens): `pak.write_pak(src, idx, out, entries_data, aes_key, magic)` — modified entries re-Oodled + re-encrypted as needed, all hash domains pinned; `iostore.write_iostore(utoc, ucas, out_u, out_c, chunk_patches)` — block-aligned appends, blake3 metas recomputed, type-6 guard (to be relaxed for structure-aware container-header patches); `rewrite.plan_rewrite/execute_rewrite` orchestrates and returns game-relative output paths.
- slr_cli contract v2 (byte-verified vs SLRUnity): manifest/result envelope; scan/extract implemented; Phase 4 adds `export`.

---

### Task 1: Length-aware text patching layer

**Files:** Create `backend/ut/patchtext.py`, `backend/tests/test_patchtext.py`

**Interfaces:**
- `patch_payload(payload: bytes, entries: list, translations: dict[int, str]) -> tuple[bytes, dict]` — entries = TextEntry list for ONE asset (only that asset's rows); translations maps entry_index → translated text (None/"" = skip). For each patched entry, in DESCENDING offset order: rewrite the length field + UTF-16 body; splice; record `shift` per patch. Result payload + report `{patched: [{entry_index, old_len, new_len, shift}], skipped: [...]}`. Multiple entries per asset supported; offsets re-based per shift.
- `rebase_entries(entries, report) -> list` — same TextEntry list with offsets adjusted for future re-scans.
- Unit tests on synthetic payloads (FText-Base + FString, multi-entry with interleaved offsets, shrinking + growing translations, empty-translation skip, invalid UTF-16 surrogate rejection).

**Real-data test:** patch DT_学校会話's payload with 3 hand-translations (e.g. numbers→"TEST1/2/3" of different lengths) → re-scan the patched payload via textscan's frame detector → exactly 3 changed sources, all other frames byte-identical; rebase check: original entry list rebased by the report re-finds all 992/473 frames on the patched payload.

### Task 2: Package-size propagation (zen store + legacy export tables)

**Files:** Modify `backend/ut/zen.py`, `backend/ut/uasset.py`, `backend/ut/iostore.py` (additive); Create `backend/tests/test_patchprop.py`

**Interfaces / requirements:**
- zen: `patch_zen_package(pkg_payload, entries, translations) -> bytes` — Task 1 splice + update the export bundle's serial sizes/offsets for shifted exports + summary size fields (padded to 4/alignment per the format as parsed) + re-emit with correct trailing tag. All 1,359 packages must survive a null-patch round-trip byte-identically (golden: patch with {} == original bytes).
- iostore: `write_iostore(..., container_header_patches=...)` — structure-aware chunk-6 edit path: parse the container header (Phase 1 survey + retoc container_header.rs), update `export_bundles_size` for packages whose chunk length changed, re-serialize; the old type-6 guard now permits ONLY this path (still rejects raw chunk_patches on type 6). Null-patch golden: rebuild with container_header_patches={} == byte-identical.
- legacy: `patch_legacy_pair(uasset, uexp, entries, translations) -> (uasset, uexp)` — splice .uexp, update each shifted export's SerialSize (+ SerialOffset if export order/shifts cross export boundaries), keep the tail tag. Golden: null-patch round-trip byte-identical on all 7,656 pairs (sample >=500 if slow).

### Task 3: Export orchestration end-to-end

**Files:** Create `backend/ut/exporttext.py`, `backend/tests/test_exporttext.py`; Modify `backend/slr_cli.py` (+`export` command)

**Interfaces:**
- `export_translations(game_root, translations_by_asset, aes_key=None, out_dir=None) -> dict` — for each asset with translations: read payload → patch (Task 1/2) → stage the patched file; group staged files by container; call the Phase 2 writers (pak entries_data / iostore chunk_patches + container_header_patches) → write containers to out_dir (or temp); verify: re-parse each output container, extract the patched assets, re-scan → every translated row present, every untranslated row byte-identical; return {containers: [paths], rows_patched, rows_skipped, verify: ok}.
- slr_cli `export`: manifest {version 2, command "export", game_dir, translations (path to the extracted table JSON with english filled), aes_key_fixture?, out} → envelope with the verify stats. Blank english = skipped (contract line).
- Tests: both games, real translations on 10+ rows each (synthetic "TEST..." strings of varied lengths incl. length GROWTH) → containers rebuilt → full verify chain green; EF end-to-end includes AES re-encryption paths (reuse slow-marker conventions).

### Task 4: Demosaic discovery (exploratory)

**Files:** Create `docs/discovery/demosaic-surface.md`, `backend/tests/fixtures/demosaic_discovery.json`, `backend/tests/test_demosaic_fixture.py` (schema gate)

- Method (discovery-driven, same as Phase 3's text spike): scan both games' readable assets for mosaic/censor surfaces: ST — VRM4U MToon materials (names/params), any material/texture named *mosaic*/*censor*/*hide*, decal/post-process candidates; EF — same sweeps over its pak assets. Identify HOW the mosaic is drawn (material param? masked texture? opacity?) and WHICH minimal asset edit disables/reveals it. Record candidates with asset paths + evidence; the fixture schema: {"sample_a": {"candidates": [{asset, object, why, edit_hint, confidence}]}, "sample_b": {...}}.
- No patching yet — observation + a chosen approach per game with byte-level evidence (what field, what value flips it).

### Task 5: Demosaic patch implementation

**Files:** Create `backend/ut/demosaic.py`, `backend/tests/test_demosaic.py`

- `plan_demosaic(game_root, aes_key=None, selection=None) -> list[PatchOp]` (candidates from Task 4, user-selection filter ready for 4b UI); `apply_demosaic(ops, ...) -> staged files` reusing the Task 3 export machinery; slr_cli `export` gains an optional demosaic section in the manifest.
- Tests per game: apply the chosen edit → payload re-parse shows the flipped field/asset; container rebuild verify chain (as Task 3).

### Task 6: Boot-test artifacts + E2E + snapshot + close

- Build user boot folders under `<dev-root>/UE\Boot Tests\` (the established pattern): per game, one folder with REAL translated entries (10-20 rows of sensible English — you may draft simple literal translations) + demosaic applied if Task 5 landed a confident edit; READMEs per the established format; verification before placement (re-parse + magic + translated rows present).
- `docs/E2E-CHECKLIST-PHASE4A.md`: automated section (suite) + manual section (the boot folders: game boots, translated strings visible in-game where identifiable, mosaic behavior change).
- Full suite + snapshot `_snapshots/phase4a-patch-demosaic.zip` + final ledger entry.

---

## Self-review notes

- Spec coverage: §5 stages 5 (demosaic scan) + 7 (export) backend halves; stage 6 (translate) stays SLR's JS-side flow (4b wires it). Approach B invariants inherited from the Phase 2 writers.
- The two hardest unknowns are gated by discovery protocols again: package-size propagation (Task 2 null-patch goldens on ALL packages) and demosaic surface (Task 4 before Task 5).
- Open dependency: boot variants 3a/3d (user) — plan carries the fallback strategy inline (Global Constraints).
- Type consistency: patch_payload/rebase_entries (T1) consumed by T2's per-format wrappers; export_translations (T3) consumed by slr_cli + T5's apply_demosaic; PatchOp list shape defined in T4's fixture.
