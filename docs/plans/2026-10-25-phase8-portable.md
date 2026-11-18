# Phase 8 — Portable projects, standalone demosaic, IoStore patch containers

User goal (2026-10-25): everything required for export lives inside the
slrproj (staging cache), like SLRUnity — without embedding the 3 GB
container. Demosaic becomes a separate operation like Extract Images.
ST export also stops needing the game folder (patch containers).

## Measured facts (2026-10-25, real games)

- EF stage set (text assets, both halves): 541 assets / 70.3 MiB.
- ST stage set: 49 assets / 1.4 MiB. Font/demosaic metadata adds ~0
  (ufont originals are NOT staged — swaps write new bytes; only index
  metadata is needed).
- EF pak: 2.95 GiB total, 17,829 entries; the 3 GB is .ubulk (2.0 GiB)
  + non-text .uexp. ST: 15.7 MB loose pak + 555 MiB .ucas.
- Save Project packs the ENTIRE staging cache (trans-full.js:1647,
  jobs/ NOT excluded) — so `jobs/export-*` leftovers would be packed
  into the slrproj. Fix: remove the export output dir after export
  (SLRUnity precedent UnityAddon.js:1298).

## 8a — staging capture + patch-pak export (EF)

### Backend: `backend/ut/stage.py` (new)

`capture_stage(game_root, aes_key, out_dir, progress)` walks the exact
scan set (scan_game's list: .uasset/.umap; legacy pairs stage BOTH
halves by payload name) plus .ufont files (index metadata only), and
writes:

- `files/<game-rel with / separators>` — raw decompressed bytes of
  every text-bearing asset half + nothing else. Ufonts are NOT staged
  (font swap replaces whole file; only entry metadata needed).
- `stage.json`:
  - `game_root_name`, `containers`: per source container: rel path,
    kind (pak|io), sha1, size — the FINGERPRINT for "game updated?"
    warnings.
  - `aes_key_hex` (EF; null for ST).
  - `pak` (EF): mount_raw bytes hex, footer fields (version, magic,
    encryption_guid, method_names), path_hash_seed, and per staged
    file: entry name (mount-relative), method int, flags int,
    uncompressed_size. Encoded-pos order preserved for the writer.
  - `io` (ST, consumed by 8c): per staged file: utoc rel path,
    file-map name, entry_index.
  - `demosaic_assets`: names of candidate assets (8b staging set —
    staged like text assets so demosaic is portable too).

`stage.json` + files are enough to rebuild a PATCH container with zero
reference to the game folder; the game folder remains the source for
the full-rebuild mode only.

### Backend: `backend/ut/patchpak.py` (new)

`write_patch_pak(stage_dir, entries_data, out_path)`:
synthesize a v11 PakIndex containing ONLY the patched entries (real
entry metadata from stage.json: method shape, flags, per-entry
encryption preserved via the AES key), synthetic Footer with
`index_offset=0` (empty data region — the writer appends
records+payloads at 0, then index, then footer). Reuses `write_pak`
machinery: `_build_replacement` (per-entry Oodle + optional payload
encryption), `_encode_record`, `_encrypt_index`. Encrypted-index
question (does the engine accept an unencrypted patch-pak index?):
write the index encrypted when a key exists — mirrors the source pak,
zero new engine behaviors. Patch name: `<stem>_SLR_P.pak` (the `_P`
suffix grants mount priority over the base pak; the engine mounts
patch paks by suffix, not by numeric slot, so a custom infix is safe).

`verify_patch_pak(out_path, staged_bytes, aes_key)`: re-parse with our
own reader, byte-compare every entry. Necessary but NOT sufficient —
mount precedence is proven by boot test 11 only.

### Backend: export-from-stage

`exporttext.export_translations` gains a stage-dir mode: `_scan_asset`
reads from a `PayloadSources` synthesized over the staging tree
(`stage.sources(stage_dir)` — a dict the payload layer can consume via
a new small indirection: `read_file` on a stage handle returns staged
bytes; `_stage_groups` needs container info from stage.json instead of
parsed indexes). Implementation: build `PayloadSources`-shaped dict
`{"paks": [], "io_pairs": [], "_stage": stage}` and teach payload to
read from it — NO change to the real container paths.

Then `export_patch(stage_dir, translations, out_dir, fonts=None)`:
same patch/verify pipeline as export_translations (rows patched/
skipped identical), but the final write is `write_patch_pak` (EF) or
the 8c patch container (ST), and verify reads back through a
`PayloadSources` over the PATCH container (single-pak handle with
`paks=[patch_path]` — the reader already handles a one-pak game).

- `slr_cli.py`: new `stage` command (create-time capture; also new
  `demosaic_stage` info merged in), `export` gains `stage_dir` +
  `mode: "patch"`; `full` keeps the game-dir path.

### Addon

- create: after extract, run `stage` into
  `<cachePath>/data/unreal-stage/`; store `stageDir` in
  `options.slrunreal`.
- export: default = patch mode (staging, portable). Export dialog gains
  "Full container rebuild (uses the original game folder)" checkbox
  → old path. Fingerprint check when gameDir is present: sha1 vs
  stage.json, warn on mismatch (proceed button).
- CLEANUP BUG FIX: `jobs/export-<ts>` removed after the dir/zip copy
  (prevents 3 GB slrproj bloat).

### Boot folder 11 (user gate)

`<dev-root>/UE\Boot Tests\11-Patch-Pak\`: EF patch pak with Option→Options
+ Credit→Credits + a JP line, ready to drop into `Paks/`. PASS =
launches, labels changed, no serialization error.

## 8b — demosaic as a standalone operation

- create-time: demosaic candidate assets join the staging set
  (ST 8 assets ≈ few hundred KB; EF 3).
- backend: `demosaic_plan_from_stage(stage_dir)` — same fixture ops,
  read targets from staged bytes; `export_demosaic(stage_dir |
  game_dir, out_dir)` → its own patch container
  (`<stem>_SLRDM_P.*`, disjoint files from translation patch —
  co-exist cleanly).
- slr_cli: `demosaic_export` command (mode: stage or game_dir).
- addon: new menu action "Demosaic" (sibling of Extract Images) with
  its own confirm/progress/report ("N of M candidates applied,
  skipped: reasons"); REMOVE `unrealDemosaic`/`unrealDemosaicMedium`
  checkboxes from the translation export dialog. SLRUnity parity
  where the games allow it; EF's baked-pixel EventCG mosaics remain
  the documented wall.
- boot folder 12: ST + EF demosaic patch containers (ST over IoStore
  needs 8c's writer; EF is a patch pak — 12 ships after 8c).

## 8c — IoStore patch containers (ST)

Research first: engine IoStore patch-mount semantics (container id,
store entries, directory index, `_P` suffix priority) against UE 5.4
source / retoc. Then `patchio.py`: synthesize `<stem>_SLR_P.utoc/
.ucas` from stage metadata + patched chunks; verify via re-read. ST
export-from-staging flips to the patch-container path; full rebuild
remains available. Boot folder 13.

## Order

8a (backend stage → patchpak → slr_cli → addon → tests → boot 11),
then 8b (small, EF-side immediately usable; ST side after 8c), then
8c (research-heavy), then release + ledgers.

## Out of scope (unchanged)

- Generic demosaic discovery engine (SLRUnity parity of OPTIONS is
  documented; our per-game fixture remains the mechanism).
- Unkeyed bytecode literals, locres translation.
