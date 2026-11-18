# ZenName: translate the zen name batch (ST options menu)

Date: 2026-10-26
Status: implemented, tested (34 new tests), full suite green

## The bug

"Translating everything collected from the sample game still leaves options
menu entries untranslated" (user screenshot: ゲームクオリティ-高/中/低,
解像度-1280×720 …, while 戻る → Back translated fine).

Census check: the ST extract table contains ZERO of those strings
(解像度 / ゲームクオリティ / フルスクリーン / クオリティ: 0 hits; 戻る: 10).

## Where the strings live

Raw byte search over every decompressed ST payload: the strings exist in
exactly four zen packages' NAME BATCHES (the FName string table):

- `sample_a/Content/common/データ/DT_オプション.uasset` — a DataTable
  whose 23 ROW KEYS are the display labels (BGM-0/中/大/小, SE-…,
  VOICE-…, ウィンドウ, ゲームクオリティ-中/低/高, フルスクリーン,
  ボーダレスウィンドウ, 解像度-1280×720 … 3840×2160).
- `sample_a/Content/common/部屋/WBP_オプションスロット.uasset` — the
  slot widget: GetDataTableRowNames → GetDataTableRowFromName → displays
  the row NAME (FName → FText at runtime). No display FText of its own.
- `sample_a/Content/common/GI_common.uasset` and `BP_save.uasset` —
  the same 10 quality/fullscreen/resolution strings as defaults/save keys.

The widget's category filters compare the row's FString fields
('クオリティ', '解像度', 'ウィンドウ', 'BGM'…) — those are VALUES in the
export data, NOT display text, and stay untouched.

Why the census missed them: scan_text collects FText frames, chained
DataTable FStrings and bytecode strings — never name-batch strings.

## Name batch format (pinned)

`u32 num | u32 num_string_bytes | u64 hash_version (0xC1640000) |
num × u64 hashes | num × BE i16 length headers | concatenated strings`.
ASCII names: header = byte count. Wide names: header = -32768 + char
count (BE i16; 0x8005 = 5 chars). Strings have NO NUL, NO inline length.

**Hash rule (empirically pinned on 23,712 real ST name hashes — 1,355
packages, 4 mismatches, all fullwidth-uppercase lowercase-divergence in
names we never touch):** `stored = CityHash64(lower(name) in storage
charset)` — ascii-stored names hash their ASCII bytes, wide names their
UTF-16LE bytes. Google CityHash v1.0.3 semantics; our port validated
against a reference implementation on 26 known-answer vectors + every
length 1..299 vs the live reference.

## The fix

- `ut/cityhash.py` — from-scratch CityHash64 port (64-bit LE path only).
- `ut/zen.py` — `_read_name_batch` returns per-entry geometry
  (`NameBatchEntry`: encoding, str_offset/bytes, header/hash offsets);
  `ZenPackage.name_entries`. `patch_zen_package` handles name patches:
  rewrite string bytes (splice), length headers, hashes, the batch's
  num_string_bytes, and every summary offset past the batch (header_size,
  cooked_header_size, ipeh, import/export maps, bundles, deps, imported
  names, cell offsets). Export/bulk SerialOffsets are header-RELATIVE, so
  the header growth cancels (`delta(start) - delta(header_size)`); the
  maps THEMSELVES move, so entries are written at
  `map_offset + delta(map_offset)`. Self-check re-parses and pins the
  resulting names. Path names ('/…') are package identity — refused.
- `ut/textscan.py` — ZenName entries: JP-gated collection from the MAIN
  batch only (imported-package names and the script-object store are
  identity). `meta` = {encoding, bytes, name_index, risk}. Risk: red for
  structural patterns (ExecuteUbergraph_/K2Node_/BndEvt__/CallFunc_/asset
  -stem prefixes, _GEN_VARIABLE/_C/_Skeleton/…/_Cue suffixes, 32-hex GUID
  suffix, '/'), red when the name IS a game asset file stem (added after
  user review: BGM cue 1_短くドビュ_Cue had shipped yellow), yellow
  otherwise (collect-first, tag-for-risk — 2,216 yellow + 1,808 red rows
  on ST after the asset-stem rule). Non-name-table rows (FText frames
  like GI_common's 下着_青) stay untagged: FText is display text by
  construction; identifiers surface as name-batch rows.
- `ut/patchtext.py` — ZenName kind: no inline length/NUL; translation
  stores ascii (pure-ASCII) else UTF-16LE; site re-verified against the
  entry's geometry; '/' sources skip with a named reason.
- `ut/scenegraph.py` — rows carry `tag` ("yellow"/"red"/"").
- `ut/exporttext.py` — `_expected_after_patch`: an English-translated
  ZenName vanishes from the verify re-scan (JP-gated), like FString.
- `addon/lib/projectBuilder.js` — row.tag → SLR per-row tags (yellow/red,
  both batch-skipped by default; user promotes by retagging).

## Consumer consistency (why renaming row keys is safe here)

The widget iterates GetDataTableRowNames — names come FROM the patched
DataTable at runtime, lookups stay consistent. GI_common/BP_save carry
the same strings as defaults; patching all packages consistently keeps
save/apply flows coherent. Caveat (documented): existing SAVEGAMEs hold
Japanese setting strings; after patching they no longer match a row, so
options fall back to defaults once. Cross-table name references stay
consistent because the same string is translated identically in every
package's batch.

## Census change

ST: 1,092 → 5,157 rows (992 FText-Base + 91 FText-Ascii + 9 BcString +
4,065 ZenName), 49 → 617 assets with text. EF unchanged (legacy packages
have no zen name batch; no ZenName rows).

NOTE: projects created before this change must RE-CREATE (or re-scan) to
get the ZenName rows — the extract table is captured at create time.

## Tests

- `test_cityhash.py` — 27 known-answer vectors (all length classes).
- `test_zenname.py` — hash rule, batch geometry round-trip, patch_span
  geometry, ST pins: DT_オプション collection (20 yellow + 2 red exact),
  full patch round-trip (names, all 25 hashes, untouched names/exports,
  size delta, summary shifts), path-name refusal, exporter verify chain
  on the rebuilt container.
- Updated pins: test_textscan (kinds set + ZenName assertions + reread
  helper), test_scenegraph (fields/total/assets/golden), test_exporttext
  (DT_オープニング 50 rows), test_patchtext (golden asset filters frames).
