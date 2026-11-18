# Phase 6 — Widget-blueprint bytecode strings (the "shape-W" limitation)

Goal: close the last documented extraction gap — the ~570 EF (+9 ST)
UI strings inside compiled blueprint functions that Phase 3 excluded as
"unpinned". Both halves of the old exclusion are now solved: the byte
grammar is identified (Kismet string constants) and the patch strategy
needs no length field at all.

## Discovery (2026-10-25, probes in /tmp/shapew_*.py)

- All 579 EF regex sites (`shape-W-unpinned`) sit in **`Function`**
  exports = compiled Kismet bytecode of widget blueprints. Strict grammar
  validates **579/579 EF + 9/9 ST**: 570 JP strings in EF; the other 9
  are symbol glyphs (▶ arrows, ☆ rating stars) — excluded by the JP gate,
  counted.
- PREFIX RULE (added after the first implementation round): every real
  site carries ``29 01`` before the ``34`` opcode (591/592 census; the
  one exception was a FALSE POSITIVE anchored at a ``4`` byte inside a
  hex key whose mojibake "text" passed the JP gate — and whose greedy
  match had been SWALLOWING a real row). Requiring the prefix makes
  key-internal anchors structurally impossible and fixed genuine
  under-extraction.
- Grammar (UE 5.6 bytecode, NUL-terminated — no length prefixes):
  ```
  34 | UTF-16LE text | 00 00 | 1f | 32-hex UPPERCASE ANSI key | 00  [+ 1f 00 empty-ANSI const]
  ```
  `0x34` = EX_UnicodeStringConst, `0x1f` = EX_StringConst (this engine's
  numbering — verified against observed `1c` FinalFunction+ i32 FPI,
  `1d` IntConst+i32, `1e` FloatConst+f32, `07` JumpIfNot+i32, `35`
  StructMemberContext+i32, `16` EndFunctionParms). Sources decode as real
  Japanese (武器 大剣, シュティーア, ウマ乗り…); median 8 chars, max 156.
  The key travels WITH the source (an FText-ish construct built at
  runtime); it is part of the frame validation and is never patched.
- NUL-terminated means **the old "no length field → cannot patch"
  reasoning is obsolete in our favor**: a same-size in-place rewrite
  needs no field updates and moves no bytes.

## Rulings

- **Same-size in-place patching only — zero bytecode relocation.** The
  patchable region is `[opcode, text+NUL end)`. A translation is written
  + NUL-terminated early; the dead tail is zero-padded (the VM reads a
  string const only to its terminator; nothing after it executes).
- **Two write modes, chosen per row:**
  1. *UTF-16 in place* (`34` op preserved): translation bytes
     (UTF-16LE + NUL) fit the original text budget → safest, default.
  2. *ANSI + opcode swap* (`34` → `1f`): UTF-16 does not fit but the
     translation is pure ASCII and fits the same region at 1 byte/char —
     this engine's own EX_StringConst form (the key const right after is
     literally one). English is always ASCII. Boot test validates the VM
     path; until then the swap is only used when UTF-16 does not fit.
- Neither mode resizes, splices, or moves anything → **no jump/skip
  relocations**, the Phase 3 show-stopper never arises. `shift` is
  always 0.
- **JP gate at emission** (`_has_jp`): symbol-only literals are counted
  (`bcstring-nonjp: N` warning), never emitted.
- Kind name: **`BcString`** (bytecode string const). TextEntry.offset =
  the opcode byte (patch address); `key` carries the 32-hex key; the
  patchable region includes the opcode byte (op-swap rewrites it).
- **Vanish rule** (exporttext._expected_after_patch): translated BcString
  rows whose translation is not JP disappear from the re-scan — UTF-16
  mode leaves an English body behind the anchor, ANSI mode removes the
  anchor entirely; both are non-extraction-targets (same semantics as
  FString rows), byte-verified separately.
- Census totals change DELIBERATELY (measured): ST 992 → 1,001
  (assets_with_text 36 → 38); EF 7,257 → 7,840 with BcString = 583
  (assets_with_text 466 → 475; 592 framed sites = 583 JP rows + 9
  symbol-glyph sites; 13 of the sites live in .umap packages). All
  pinned tests updated to the measured numbers.
- Unkeyed bytecode literals (a `34` string const without the key
  framing) are NOT extracted — unanchored, unknown consumers; not
  counted (the with-key grammar is the safe surface).

## Implementation

- `textscan`: `_scan_bc_strings(data)` strict forward parser (validated
  588/588 against the regex census); emission for `class_name ==
  "Function"`; docstring decision note updated (Phase 3 exclusion →
  Phase 6 pin); `bcstring-nonjp` warning.
- `patchtext`: `BcString` kind — site verification (op byte, UTF-16 text
  == entry.source, key framing), two write modes, zero-shift splice,
  overlap check on the full region, `mode` in the report.
- `exporttext`: BcString joins the FString vanish rule.
- Tests: synthetic frame round-trips (both modes + too-long skip +
  overlap rejection), real-game EF patch_payload round trip, ST/EF scan
  census updates, container-rebuild re-read.
- Boot test 9: EF + ST exports with a handful of translations (short
  UTF-16 mode + long ANSI-swap mode visible), user confirms English UI
  text renders and nothing else broke.
