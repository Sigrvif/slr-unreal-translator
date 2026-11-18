// Unreal game detection + game-root resolution — pure functions over
// injected fs helpers.
//
// Pure-lib rule: no NW.js / SLR globals and no direct fs use; the wiring
// file (UnrealAddon.js) injects {isFile, isDir, listDir, join, dirname}.
// The same helpers are doubled in tests/detector.test.js.
//
// Root marker (the one that wins): a directory containing
// `<child>/Content/Paks` with at least one .pak/.utoc file — the UE
// container layout (Chromium's resource .paks live under Engine/Binaries/
// ThirdParty and never under Content/Paks). A *-Win64-Shipping.exe is NOT
// by itself a root marker: it sits in <Game>/Binaries/Win64, and treating
// any exe-bearing folder as a root would resolve the Binaries dir as the
// game. Exe picks instead WALK UP from the exe's folder to the nearest
// ancestor that has the Content/Paks marker (bounded — see resolveGameRoot).
"use strict";

function isShippingExe(name) {
  const low = String(name).toLowerCase();
  return low.endsWith("-win64-shipping.exe") || low.endsWith("-shipping.exe");
}

function _paksHasContainers(fsh, paksDir) {
  try {
    for (const f of fsh.listDir(paksDir)) {
      const lowf = String(f).toLowerCase();
      if (lowf.endsWith(".pak") || lowf.endsWith(".utoc")) return true;
    }
  } catch (e) { /* unreadable dir: not a marker */ }
  return false;
}

// Primitive: does `dir` ITSELF look like a UE game root? (No parent/sibling
// scanning — callers decide how far to search.) Shared by detectUnreal
// (engine registration) and resolveGameRoot (scan-target resolution) so the
// two can never disagree about what counts as a UE game.
function isDirectUnreal(fsh, dir) {
  if (!fsh.isDir(dir)) return false;
  let entries;
  try {
    entries = fsh.listDir(dir);
  } catch (e) {
    return false;
  }
  for (const sub of entries) {
    const sd = fsh.join(dir, sub);
    if (fsh.isDir(sd) && fsh.isDir(fsh.join(sd, "Content", "Paks")) &&
        _paksHasContainers(fsh, fsh.join(sd, "Content", "Paks"))) return true;
  }
  return false;
}

// Engine detection for Engines.registerDetector: boolean-ish "is this UE?".
// The search covers the given folder itself and, when the picker handed an
// executable, its parent directory's siblings (one level) so launcher-style
// layouts still resolve.
function detectUnreal(fsh, gamePath) {
  const looksLikeFile = fsh.isFile(gamePath) ||
    (!fsh.isDir(gamePath) && /\.[^./\\]+$/.test(gamePath));
  const base = looksLikeFile ? fsh.dirname(gamePath) : gamePath;
  // A file pick (the shipping exe) lives in Binaries/Win64 — walk up to the
  // nearest Content/Paks root before scanning siblings.
  if (looksLikeFile) {
    if (_nearestRoot(fsh, base, 6)) return "unreal";
  } else if (isDirectUnreal(fsh, base)) {
    return "unreal";
  }
  let children;
  try {
    children = fsh.listDir(base);
  } catch (e) {
    return false;
  }
  for (const child of children) {
    if (isDirectUnreal(fsh, fsh.join(base, child))) return "unreal";
  }
  return false;
}

// Walk UP from `start` to the nearest ancestor (inclusive) that is a UE
// root, at most `maxLevels` steps. Returns null when none qualifies.
function _nearestRoot(fsh, start, maxLevels) {
  let cur = start;
  for (let i = 0; i <= maxLevels; i++) {
    if (isDirectUnreal(fsh, cur)) return cur;
    const parent = fsh.dirname(cur);
    if (!parent || parent === cur) return null;
    cur = parent;
  }
  return null;
}

// Scan-target resolution: SLR hands the createProject handler
// {gameFolder: DIRNAME(picked), selectedFile: picked}. resolveGameRoot
// returns the actual UE game root for a picked path:
//   - picked is a file (typically the shipping exe) -> walk UP from its
//     folder to the nearest Content/Paks root (Binaries/Win64 is 3 levels
//     below the root; 6 covers launcher nesting without crossing games);
//   - picked is a directory                        -> the folder itself if
//     it is a root, else its DIRECT children (exactly one -> that child;
//     several -> throw, ambiguous — never guess; none -> fall through);
//   - gameFolder as a last resort when IT is a root; else throw.
function resolveGameRoot(fsh, picked, gameFolder) {
  const looksLikeFile = fsh.isFile(picked) ||
    (!fsh.isDir(picked) && /\.[^./\\]+$/.test(picked));
  const base = looksLikeFile ? fsh.dirname(picked) : String(picked || "");
  if (isDirectUnreal(fsh, base)) return base;
  if (looksLikeFile) {
    const up = _nearestRoot(fsh, base, 6);
    if (up) return up;
  }
  let children = [];
  try {
    children = fsh.listDir(base)
      .map((c) => fsh.join(base, c))
      .filter((c) => isDirectUnreal(fsh, c));
  } catch (e) { /* nonexistent base: fall through to the error */ }
  if (children.length === 1) return children[0];
  if (children.length > 1) {
    throw new Error("Multiple Unreal games found inside:\n  " + base + "\n\nPick one of them directly:\n  " +
      children.join("\n  "));
  }
  if (gameFolder && isDirectUnreal(fsh, gameFolder)) return gameFolder;
  throw new Error("No Unreal game found at:\n  " + base +
    "\n\nAn Unreal game folder contains <Project>/Content/Paks with .pak/.utoc files and a *-Win64-Shipping.exe under <Project>/Binaries/Win64 — pick the game folder or the shipping exe.");
}

module.exports = { detectUnreal, isDirectUnreal, resolveGameRoot, isShippingExe };
