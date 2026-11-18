// Unreal text-protection patterns for the "DSLR Unreal" translation engine
// (lib/ueEngine.js registers a second DSLR engine instance under UE_ENGINE_ID
// with these defaults instead of the RPG Maker ones).
//
// Format contract: DSLRTranslationEngineWrapper.getPatterns() wraps the stored
// string in "[\n" + str + "\n]" and evals it — so each value here must be a
// comma-separated list of REGEX LITERAL sources, one per line, with // comment
// lines allowed between items (exactly how the RPG defaults in
// www/core/SLRtrans/lib/TranslationEngine/DSLRTranslationEngineWrapper.js are
// authored). ueEngine.test.js pins that every string parses back into regexes.
//
// Pure-lib rule: only node builtins; no NW.js / SLR globals.
"use strict";

const UE_ENGINE_ID = "dslr-unreal";
const UE_ENGINE_NAME = "DSLR Unreal (Unreal Engine projects)";

// --- Protected Patterns ----------------------------------------------------
// What Unreal text can contain that must survive an LLM round trip:
//   - rich-text markup (<Em>...</Em>, <font color=...>, <br/>, <img id=x/>)
//   - FText format arguments ({0}, {PlayerName}, {Gender|masculine}) — Format()
//     looks these up by name at runtime, so a translated-away argument breaks
//     the string
//   - DSLR's own full-batch dividers (дiv markers) — engine-internal, kept
//     verbatim from the DSLR defaults so plain-text batching still recovers
// Wolf-style placeholder tokens ({PERSON1}, {V1}, ...) are covered by the
// generic brace pattern.
const PROTECTED_PATTERNS = [
  "// SLRUnreal — Unreal Engine text protection",
  "// Rich-text markup: <b>, </Em>, <font color=...>, <br/>, <img id=x/>",
  /<\/?[A-Za-z][^<>\n]*>/g.toString() + ',',
  "// FText format arguments: {0}, {PlayerName}, {Gender| masculine|feminine}",
  /\{[^{}\n]+\}/g.toString() + ',',
  "// DSLR full-batch dividers (engine-internal; verbatim from the DSLR defaults)",
  /[\n \-{]*дiv\d*[}\n \-]*/g.toString() + ',',
  /дiv/g.toString() + ',',
  /д/g.toString() + ',',
  /\n?--- {div\d+} ---\n?/g.toString() + ',',
].join("\n");

// --- Aggressive Splitting --------------------------------------------------
// Kept from the DSLR defaults: empty-line handling and whole cells without
// Japanese (already-Latin UI labels cost tokens and risk re-translation; the
// @ in the class keeps DSLR's internal line markers out of the match).
// Dropped: every RPG Maker-specific split (system-message translations,
// dialogue plugin, CommonPopup/OSW/PSM/sac/FFF script shapes).
const SPLITTING_PATTERNS = [
  "// SLRUnreal — Unreal Engine splits",
  "// Empty lines",
  /^[ 　\s]*$/gm.toString() + ',',
  /^\s*[\n\r]+/gm.toString() + ',',
  /[\n\r]+\s*$/gm.toString() + ',',
  "// Cells that do not include Japanese or placeholders",
  /^[ 　]*(?!.*\\+[niv]w?\[\d+\])[^一-龠ぁ-ゔァ-ヴー々〆〤ヶﾚﾃﾞｨﾌｧｲｯ@]+[ 　]*$/gi.toString() + ',',
].join("\n");

// --- Isolation Patterns ----------------------------------------------------
// Nothing isolated by default: UE dialogue has no nameplate-escape structure
// to peel off, and the RPG escape-driven isolations never match UE text.
const ISOLATE_PATTERNS = [
  "// SLRUnreal — nothing isolated by default for Unreal text",
].join("\n");

// --- Cutting Corners Patterns ----------------------------------------------
// Kept from the DSLR defaults: filename-looking rows (extended with UE asset
// extensions), lone whitespace, whitespace+linebreak corners. Dropped: RPG
// plugin/script corners and the decorative-symbol corner classes (paste them
// from the main DSLR engine's panel if you want them).
const SPLIT_ENDS_PATTERNS = [
  "// SLRUnreal — Unreal Engine corners",
  "// files (UE asset extensions added)",
  /^.+\.(png|jpg|jpeg|ttf|otf|ttc|woff|woff2|txt|ini|cfg|json|js|wav|ogg|mp3|mp4|m4a|bmp|exe|pak|utoc|ucas|uasset|uexp|umap|umaterial|ufont).*$/gi.toString() + ',',
  "//Lone Whitespace",
  /^[ 　\s]+$/gim.toString() + ',',
  "// Whitespace with linebreak at start",
  /^[\"\'\`\’]?\s*\n\s*/gi.toString() + ',',
  "// Whitespace with linebreak at end",
  /\s*\n\s*[\"\'\`\’]?$/gi.toString() + ',',
].join("\n");

module.exports = {
  UE_ENGINE_ID, UE_ENGINE_NAME,
  UE_PATTERN_DEFAULTS: {
    protectedPatterns: PROTECTED_PATTERNS,
    splittingPatterns: SPLITTING_PATTERNS,
    isolatePatterns: ISOLATE_PATTERNS,
    splitEndsPatterns: SPLIT_ENDS_PATTERNS,
  },
};
