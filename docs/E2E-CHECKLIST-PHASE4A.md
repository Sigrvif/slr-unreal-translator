# SLRUnreal Phase 4a E2E checklist

## Automated (pytest, real games, read-only on originals)

- `test_patchtext` (11): length-aware splice geometry — grow/shrink/multi-entry,
  skip-on-blank, overlap + surrogate/NUL rejection; real-data 3-patch on
  DT_学校会話 (3 changed / 470 identical / rebase refinds 473).
- `test_patchprop` (16): null-patch byte-identity goldens — ALL 1,359 ST zen
  packages + ALL 7,656 EF pairs + container-header path through write_iostore;
  propagation arithmetic verified on real assets (serial sizes, TOC lengths,
  cross-boundary shifts).
- `test_exporttext` (7): translations → rebuilt containers → full verify chain on
  both games (incl. EF AES rebuild, .umap-hosted pairs, pruned non-prefix tables);
  CLI `export` subprocess both games.
- `test_demosaic` (6): plan (HIGH default / MEDIUM opt-in / LOW refused) +
  apply+verify on both games (ST 7 material-slot swaps; EF widget texture-ref).

Suite: **132 passed** (~8 min; slow tail = EF sweeps).

## Manual (user-run, on the READY-MADE folders — originals untouched)

- `<dev-root>/UE\Boot Tests\5-SampleGameA-Translated` — launch `sample_a.exe`.
  12 school-dialogue lines carry `[EN1]`..`[EN12]` placeholders; 7 character
  models have their overlay material slot swapped to the clean material that
  ships beside it. EXPECT: game boots; some early school dialogue shows [ENn]
  lines; characters render normally (no missing-material checkerboards).
- `<dev-root>/UE\Boot Tests\6-SampleGameB-Translated` — launch `SampleGameB.exe`.
  12 event-dialogue lines carry `[ENn]`; one event image (WG_H_Event02's hand
  image) swapped to its clean variant. EXPECT: game boots; dialogue shows [ENn]
  lines. NOTE: most CG images keep their overlay — it is baked into the pixels
  (information destroyed); only the one swapped reference can change.

Report per folder: boots yes/no; if yes, whether [ENn] lines are visible and
whether anything looks broken (missing textures, corrupted models).

## Phase 4b handoff (carried in the ledger)

- `offset+42` FText geometry; PATH_KEYS additions (`out`, `aes_key_fixture`);
  progress hook for the 60s EF scan; key-UX seam; W-shape gap (601 strings);
  shape-B u32; **combined export**: text + demosaic must rebuild in ONE pass
  (composed manually in the Task 6 boot script via shared primitives — the
  addon's export command needs this as a first-class path; last-writer-wins
  otherwise).
