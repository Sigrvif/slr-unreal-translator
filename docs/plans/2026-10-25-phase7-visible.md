# Phase 7 — Collect all user-visible text (SLRUnity collect-all parity)

Goal: stop filtering extraction to Japanese. EF's main-menu buttons
(New Game / Continue / Option / Credit / Game End) are ENGLISH and live
in the excluded ASCII-source FText class — the user wants every
user-visible string collected, with SLRUnity's parity behavior: rows
without Japanese carry SLR's gray "cell without Japanese" tag when the
**Japanese source** general option (`sys.config.japanesesource`) is on.

## Discovery (2026-10-25)

- The menu strings are **ASCII-source FText frames** in WG_Title /
  WG_Menu / WG_Credit — the exact class Phase 3 counted as
  `ftext-ascii-source` and skipped (EF 2,796 sites, ST 91).
  `WG_Title` carries New Game / Option / Credit / Game End at the same
  offset pattern (11/17).
- The frame grammar already detects them (flags u32=1 | history u8=0 |
  key | POSITIVE-length ANSI source). Property data — resizable freely,
  the proven splice path (no bytecode constraints).
- BcString non-JP sites (EF 9: ☆ ratings, ▶ glyphs; ST 0) are currently
  gated too — "all user-visible" includes them (gray rows, user skips).
- FString chained cells stay JP-GATED deliberately: DataTable cell
  streams are full of ASCII keys/ids/binary runs; the JP gate is the
  noise filter there (the docstring's junk-string precedent). Visible
  UI text is FText/BcString, not DataTable FStrings.

## Rulings

- New kind **`FText-Ascii`**: ASCII-source FText frames (grammar-identical
  prefix, source = positive-length ANSI FString). Emission rules: body
  must NUL-terminate, length sane, and every source byte < 0x80 (higher
  bytes = codepage-encoded text we can't round-trip safely — counted as
  `ftext-ansi-highbyte`, not emitted). Empty sources (length 1) skipped.
- **Patching FText-Ascii** (geometry len=+42, body=+46, 1 byte/char):
  ASCII translation → ANSI body (positive length, grown/shrunk freely —
  property splice, export-map fixups handle shift); non-ASCII
  translation → UTF-16 body with NEGATIVE length (the FTextHistory_Base
  source FString is sign-encoded; the game's FString reader is
  encoding-agnostic). The verify's expected-kind therefore switches:
  a non-ASCII-translated row re-appears as `FText-Base` in the re-scan.
- **BcString JP gate dropped**: all grammar-valid rows emitted (symbols
  included). The vanish rule already covers them.
- Warnings retired: `ftext-ascii-source` and `bcstring-nonjp` (the rows
  are the count now). `shape-W-sites` stays.
- **Gray tagging** (addon side): `buildProject(table, {japaneseSource})`
  — when SLR's `japanesesource` config is on, rows whose source fails
  the JP_CHAR class get SLR's `["gray"]` tag; off = untagged (SLRUnity
  v2.2 e2e-fix11 semantics). Read at project creation from
  `sys.config.japanesesource` (same key SLRUnity uses).
- Census totals change again (ASCII FText rows + non-JP BcStrings);
  all pins re-measured.

## Implementation

- textscan: `_FRAME` ASCII sites → validated FText-Ascii emission;
  BcString gate dropped; docstring updated (the two retired exclusion
  classes now document WHY they are emitted).
- patchtext: FText-Ascii branch (verify positive length + NUL; ANSI or
  UTF-16 write by translation content; splice).
- exporttext: expected-kind switch for translated FText-Ascii.
- addon: projectBuilder gray tags; UnrealAddon passes
  `sys.config.japanesesource` at createProject.
- Tests: synthetic FText-Ascii patch (ASCII + UTF-16-translation paths),
  real EF round trip ("Option" → "Options" on WG_Title), census pins,
  projectBuilder gray-tag unit test.
- Boot test 10: EF title-menu strings (Option → Options, Credit →
  Credits) — property-data path, but boots are cheap confidence.
