# E2E checklist — Phase 7 (collect all user-visible text)

Build under test: `dist/SLRUnreal-win.zip` (9,054,076B, 69 entries, smoke
census text_entries=1092) — installed to
`<dev-root>/SLR Translator\www\addons\SLRUnreal` (md5-verified against stage).

## Prepared for you (no building needed)

`<dev-root>/UE\Boot Tests\10-Menu-Labels\` — README + EF title-menu patch
(Option → Options, Credit → Credits; verify ok, built through the addon's
own export path). Boot: copy `SampleGameB\Content\Paks\` over a game
COPY, launch, check the title screen.

## What changed for users

- **All user-visible text is collected now**, not just Japanese:
  EF 7,840 → 10,645 rows; ST 1,001 → 1,092. The main-menu buttons the
  user reported are in (WG_Title: New Game / Load Game / Option /
  Credit / Game End / Ver 1.0.5).
- **Gray tagging (SLRUnity parity)**: rows whose source has no Japanese
  carry SLR's gray "cell without Japanese" tag when SLR's
  **Japanese source** general option is on; off = untagged. Read at
  project creation — existing projects are unaffected.
- Symbol-glyph strings (☆ ratings, ▶) are collected too (gray rows;
  leave them untranslated if you don't want them touched).
- Officially-English FText can be translated to any language: ASCII
  translations stay ANSI; non-ASCII translations flip the stored FString
  to UTF-16 automatically (both load fine in-game).
- DataTable ASCII cells (row keys/ids) are still NOT collected — the
  Japanese gate stays there as a noise filter.

## Boot test 10 (user): PASS — EF launches, Options/Credits render
correctly on the title screen. The FText-Ascii patch path is confirmed
in-game.

## Covered by automated tests

- backend `test_ftext_ascii.py` (4: exact-byte ANSI grow/shrink, UTF-16
  flip, stale rejection, EF WG_Title real round trip) + `test_bcstrings`
  collect-all + gray-tag unit test + census pins everywhere — full suite
  **195 passed**; addon suite 27 passed.
