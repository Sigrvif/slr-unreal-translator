# SLRUnreal Phase 3 E2E checklist

Automated (pytest, real games; Phase 3 is READ-ONLY — no game file is written
anywhere in the phase-3 code paths, all output to temp) — DONE:
`python -m pytest backend/tests -q` → **88 passed** in 317.57 s (5:17)
(88 = 42 Phase 1-2 regression tests + 46 Phase 3; dominated by the full-game
EF sweeps at ~63 s each — top four durations are all EF: slr_cli extract
round-trip 67.3 s, slr_cli scan 65.9 s, the in-process session scan
(conftest.py, one per game per run) 65.5 s, the 7,656-pair uasset parse
sweep 53.8 s; ST scans are 2.7-2.8 s each).

What the 46 Phase 3 tests pin (Tasks 1-7):

- Payload layer (6): ST list_files 3,377 names (pak 1,669 + utoc 1,708,
  disjoint) · EF 17,829 · EF pairing rule — every .uasset (7,555) and .umap
  (101) carries its export stream as an exact-stem .uexp pak entry (7,656,
  zero orphans both directions) · DefaultEngine.ini byte pin · ST zen
  payloads end with package tag 0x9E2A83C1 (51/51 sampled).
- Discovery census gate (4): `fixtures/text_discovery.json` schema ==
  scanner output — the ground truth every downstream count is pinned against.
- Zen parser (8): all 1,359 ST packages parse with ZERO exceptions (24,170
  package names, 6,762 exports / 16,104 imports; 6,762/6,762 class refs
  resolve via the ScriptObjects chunk — no CityHash needed); trailing-tag
  0x9E2A83C1 validated across all 1,359; grammar detector finds exactly 992
  FText frames ST-wide, ALL inside export slices.
- Legacy parser (8): all 7,656 EF pairs parse with ZERO exceptions (233,733
  exports, every slice exact-length, all .uexp tail tags verified); golden
  DT_EroEventTalkTable 1,143/1,143 frames in-slice; EF-wide census 6,359
  frames (6,191 JP) exact; 4,606 wide Japanese name entries pinned; shape-B
  .uasset halves stay metadata-only (the u32 stays unread).
- textscan (10): census-exact BOTH games — **ST 992 FText-Base (991 JP + 1
  non-JP "→") / 0 FString**, **EF 6,359 FText-Base (6,191 JP) + 898 FString
  = 7,257 entries**; errors 0 on both; round-trip re-reads reproduce every
  sampled frame's source exactly; shape-W excluded with named warning class
  (unpinnable from bytes); warnings never abort (fuzz-probed); 6 synthetic
  branch pins (property walk, tagless fallback, chained FString, shape-W,
  ascii skip, junk).
- scenegraph (6): text_table rows == entries on BOTH games (ST 992 / EF
  7,257); every entry reaches exactly one leaf (verified game-wide); columns
  mirror SLRUnity's strings.csv contract + the `entry_index` write-back
  extension; ST 36 assets-with-text (nodes 1/36/88/992/992), EF 466
  (1/466/1,569/7,257/7,257).
- slr_cli (4): contract v2 envelope byte-verified against SLRUnity's CLI
  (version gate, unknown-command gate, error/traceback shape, utf-8
  ensure_ascii=False, exit 0 in every outcome); subprocess scan ST 2.8 s /
  EF 62.9 s with all census numbers reproduced; extract writes the Task-6
  graph ATOMICALLY (mkstemp + os.replace, no .tmp leftovers) and parses
  back intact; nonexistent game_dir → hard error (never silent zero-file
  success).

Plus the 42 carried Phase 1-2 tests (AES/blake3/pak+utoc/ucas readers,
writers, rewrite orchestration, manifest, protection, inspect, IDA fixture
gates) — unchanged this phase.

Per-game headline numbers:

| | SampleGameA (IoStore/Zen, UE 5.6.1) | SampleGameB (pak-only, AES, legacy) |
|---|---|---|
| files listed | 3,377 | 17,829 |
| packages parsed | 1,359/1,359 (0 errors) | 7,656/7,656 pairs (0 errors) |
| text entries | 992 FText-Base | 7,257 = 6,359 FText-Base + 898 FString |
| JP entries | 991 (+1 non-JP "→") | 6,191 JP FText + 898 JP FString |
| assets with text | 36 | 466 |
| golden asset | DT_学校会話 473 | DT_EroEventTalkTable 1,143 |
| full-game scan | 2.7-2.8 s | ~63 s |
| warnings | ascii-source 91, shape-W 9 | ascii-source 2,796, shape-W 592 |
| errors | 0 | 0 |

Manual (user-run) — NONE this phase. Phase 3 only reads; nothing to boot or
eyeball. The write loop's E2E belongs to Phase 4's export flow (its rebuilt
containers will need boot tests on COPIED game dirs, same protocol as
Phase 2's checklist). Phase 2's four manual boot tests already exercised the
writers Phase 4 reuses: 3 PASS; T3 (blind iostore chunk patch) FAIL with the
ruling recorded — Phase 4 IoStore patches must be structure-aware
(FText-level), never blind byte overwrites.

## Phase 4 handoff — ledger carry items

- **offset+42** (Task 5): FText-Base source sub-field sits at frame
  offset+42 (the frame prefix); `TextEntry` (asset, offset, kind, len) fully
  determines the patchable range — the write-back address.
- **PATH_KEYS** (Task 7): manifest path keys `out` and `aes_key_fixture` are
  NOT in SLRUnity backend.js's PATH_KEYS convention — the Phase 4 runner
  must absolutize them (or reuse out_dir).
- **W-gap** (Task 5): 601 JP UI strings live in shape-W sites (9 ST + 592
  EF, WG_* function bytecode) excluded as unpinnable from bytes — widget
  text coverage backlog.
- **shape-B u32** (Tasks 2/4): EF .uasset halves carry [FString name |
  u32 UNKNOWN | i32 -N | UTF-16]; provenance must be pinned before any
  rewrite touches them (metadata-only today).
- **key-UX seam** (Task 7): manifest `aes_key_fixture` (dev-side JSON) is
  the placeholder for the shipped key-management UX; the seam lives entirely
  in `slr_cli._sources()`; a bad/missing fixture is a hard error, never a
  silent plaintext fallthrough.
- Also carried (Task 7 minors): EF scan is ~63 s silent — progress stream
  needs a `scan_game` hook; foreign-pak dir picks need a UX gate;
  `entry_index` (not context) is the write-back key (context contains "/"
  and is not parseable).
