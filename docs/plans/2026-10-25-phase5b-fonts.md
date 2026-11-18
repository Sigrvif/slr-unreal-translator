# Phase 5b — Font replacement (SLRUnity parity) + text-box sizing

Goal: the SLRUnity "Replace game fonts" contract on UE games. Two-role
font swap — dialogue/Japanese role gets SLRCondensedCJK (narrow Latin +
CJK coverage so untranslated Japanese keeps rendering), UI/Latin role
gets Stint Ultra Condensed (narrow Latin for translated English) — plus
optional custom-font overrides per role, integrated into BOTH flows that
already rebuild containers (strings export, image insert).

## SLRUnity contract (parity target)

- Export dialog checkbox **Replace game fonts** + two custom font fields
  (dialogue/CJK, UI/Latin; absolute .ttf/.otf paths, remembered via addon
  config, Browse… button). Insert dialog: checkbox only.
- Swap rewrites the font data embedded in the game's own font files;
  object names/references preserved (the game keeps using "its" fonts).
- Default faces resolved from the SLR installation:
  `www/core/SEP/SLRCondensedCJK-Regular.ttf` (dialogue),
  `fonts/sucr.ttf` (UI). Missing file → WARNING + that stage is skipped
  (strings still patched).
- TMP atlas re-bake: NOT needed on UE — see discovery.

## UE-side discovery (2026-10-24, both reference games)

- Both games cook **tagless** (no property tags — known since Phase 2);
  FontFace/UFont exports carry raw script-order values only.
- **No FontFace embeds font data**: ST MSGOTHIC FontFace export = 48B
  (editor path "C:\Windows\Fonts\MSGOTHIC.TTC" + flags); EF
  MPLUSRounded1c-ExtraBold2 = 190B (dev-project .ttf path). No RuntimeData
  properties anywhere → **the game rasterizes glyphs at runtime via
  FreeType** (SlateCore ships in cooked builds). No TMP-style frozen
  atlas exists to re-bake; the SLRUnity atlas re-render has NO UE
  counterpart — a data-only swap is the whole feature.
- Actual font bytes live in **`.ufont` sidecars** (UE 5.4+ packaged font
  files), one per FontFace, keyed by package name:
  ST `sample_a/Content/font/MSGOTHIC.ufont` (8,990,168B = u32 LE
  size 8,990,160 + TTC bytes); EF `.../MPLUSRounded1c-ExtraBold2.ufont`
  (3,619,000B = RAW TTF, no header). Both games also ship the 7 stock
  engine faces (Roboto×5, DroidSansMono, DroidSansFallback) as .ufont.
  Format detection: first 4B sfnt magic (00 01 00 00 / OTTO / ttcf /
  true) = raw; else u32LE == size-8 and bytes[8:12] is sfnt magic =
  size-prefixed; anything else (e.g. compressed future formats) = skip
  with a logged reason.
- Census (both games): 4 Font + 8 FontFace exports; the game-owned face
  is the ONLY content-side font (ST /Game/font/MSGOTHIC, EF
  /Game/SampleGameB/Iroiro/MPLUSRounded1c-ExtraBold2); everything else
  is /Engine/ stock. 30 (ST) / 407 (EF) WidgetBlueprint exports consume
  fonts through FSlateFontInfo (tagless — per-widget patching would need
  schemas for every widget class; ruled out).

## Rulings

- **Swap targets = .ufont entries only.** The .uasset/.ucas package data
  of FontFace/Font assets is never touched (names, references, subface
  keys stay original) — byte-for-byte the SLRUnity "nothing is added,
  nothing renamed" contract.
- **Role mapping**: game-owned faces (package path not under /Engine/)
  → dialogue font; engine faces → UI font, EXCEPT
  DroidSansFallback → dialogue font (it is the engine's CJK fallback;
  a Latin-only face there would tofu untranslated Japanese rendered via
  the fallback chain). Result: untranslated JP renders through the game
  face AND the engine fallback; all default-UI Latin goes narrow.
- **Format-preserving replacement**: the new .ufont bytes are written in
  the format detected on the ORIGINAL sidecar (raw vs u32-size-prefixed).
  Entry sizes change freely — `pak.write_pak` re-stores appended entries
  and `iostore.write_iostore` appends block-aligned chunks (both already
  used for text/texture patches).
- **Merge into ONE rebuild**: font entries join the same `staged_files`
  dict as text patches / texture imports before the single container
  rebuild (Phase 4a ruling: separate rebuilds of one container overwrite
  each other).
- **Subface caveat (logged, boot-test)**: ST's game face is a 3-face TTC
  (MS Gothic/PGothic/UI Gothic). If the composite pins a subface index
  and the 1-face replacement misses it, UE falls back along the
  composite chain (readable, wrong font) — not tofu. Escalation lever if
  the boot test shows fallback: hand-roll a K-copy TTC wrapper (12-byte
  header + offsets; FreeType does not verify table checksums). Deferred
  until needed.
- **Custom font validation**: sfnt magic check + >1KB size, else named
  error (addon alert), matching SLRUnity's missing-font WARNING behavior
  when a default face is absent from the installation.

## Text-box sizing (user question: "in case that's a thing in UE")

Answer: no per-widget recalibration is implemented, deliberately. UMG
TextBlocks auto-size to their text (unlike Unity's fixed RectTransforms):
width comes from the layout slot, height grows with wrapping. The overflow
SLRUnity fixes with narrow fonts does not have a UE twin that we can patch
blindly (tagless WidgetBlueprints would need per-class schemas for 400+
widget types). The narrow-font swap IS the calibration; a user whose
specific game still clips can pick a narrower face in the custom UI-font
field. Documented honestly in docs/help.

## Implementation

- `backend/ut/fonts.py` (new):
  - `scan_fonts(src)` → per-face rows {package, role, ufont, size, format}.
  - `stage_font_swaps(src, dialogue_font, ui_font)` → (staged, report);
    format-preserving repack, role mapping as ruled.
  - `read_font(path)` → validated font bytes (sfnt magic).
- CLI: `cmd_export` + `cmd_insert_images` accept `fonts: {dialogue, ui}`
  (paths; absent key = off). Staging merged before the single rebuild.
- Addon: export-dialog fields `unrealSwapFonts` (bool) +
  `unrealFontDialogue` / `unrealFontUi` (string paths, config-persisted,
  Browse… via NW.js file picker — SLRUnity e2e pattern); insert section
  checkbox `unrealInsertFont`. Default faces resolved from
  `common.getRootDir()` like SLRUnity; missing default = console WARNING
  line, stage skipped.
- Tests: fixture-driven (synthetic sfnt bytes) + both games (scan census,
  staged format preservation, export-merge round trip re-read with key,
  verify semantics inherited from exporttext).
