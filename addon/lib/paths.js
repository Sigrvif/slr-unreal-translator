// Pure path helpers for the SLRUnreal addon.
// Pure-lib rule: no NW.js / SLR globals here — plain CommonJS, requireable
// from both `node --test` and the NW addon wiring files (via absolute path).
"use strict";

const path = require("path");

// dirname(gameDir) + "_" + basename(gameDir) + suffix, forward slashes.
// Forward slashes on purpose: the backend contract's path convention, and
// Node fs accepts them on Windows.
function siblingDir(gameDir, suffix) {
  const abs = path.resolve(gameDir);
  return toForwardSlashes(path.join(path.dirname(abs), path.basename(abs) + suffix));
}

function toForwardSlashes(p) {
  return String(p).split("\\").join("/");
}

function fileStem(name) {
  const s = String(name);
  const i = s.lastIndexOf(".");
  return i > 0 ? s.slice(0, i) : s;
}

module.exports = { siblingDir, toForwardSlashes, fileStem };
