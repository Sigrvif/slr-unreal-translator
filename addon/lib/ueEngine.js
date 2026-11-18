// Unreal-aware pattern switching for the ONE DSLR engine.
//
// While an Unreal project is the ACTIVE project (trans.project), the DSLR
// engine's four pattern-family options resolve to Unreal sets at translation
// time (processorInit reads option.getValue() per run). For every other
// project they resolve to exactly the user's stored DSLR values. One engine,
// one setup: endpoints, prompts, QC and the stored pattern values are never
// touched, and no state is kept across sessions (nothing to restore after a
// crash).
//
// Mechanism: SLROption.getValue() consults option.options.backend FIRST when
// one is installed (www/js/Option.js), before the wrapper/engine chain. We
// install a backend object on the four pattern options of the live DSLR
// wrapper (reachable as window.slrDSLREngineWrapper — DSLREngine's
// constructor exposes itself there). The backend:
//   getValue(id) -> the user's EDITABLE Unreal copy (addon config, injected
//                   as getPatterns) when an Unreal project is active and the
//                   feature is enabled — falling back to the built-in set —
//                   or the engine's STORED value otherwise, or undefined
//                   (SLROption then falls back to the option's RPG factory
//                   default, mirroring a fresh install).
//   setValue(id, v) -> the engine's normal update(): edits made in the DSLR
//                   options tab persist exactly as before.
// The DSLR options tab keeps showing the stored sets: it renders from the
// engine's own properties, not from getValue() — the switch is behavior-only.
//
// All SLR/NW entry points are injected (window/trans/sys/isEnabled), so the
// module stays unit-testable without an SLR install. Pure-lib rule: node
// builtins + injected references only.
"use strict";

const {
  UE_PATTERN_DEFAULTS,
} = require("./uePatterns.js");

// The wrapper's pattern-family option ids, in TranslationEngineOption ids.
const PATTERN_OPTION_IDS = [
  "protectedPatterns", "splittingPatterns", "isolatePatterns", "splitEndsPatterns",
];

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// An Unreal project is active: created by this addon (options.slrunreal) or
// carrying gameEngine "unreal" — both hold only while a project is open, so
// with no project open the user's own patterns apply.
function isUnrealProject(trans) {
  try {
    const p = trans && trans.project;
    if (!p) return false;
    if (p.gameEngine === "unreal") return true;
    return !!(p.options && p.options.slrunreal);
  } catch (e) { return false; }
}

// Resolves the DSLR wrapper once SLRtrans has constructed it (addons load in
// parallel with core modules, so the global may not exist yet when we run).
async function waitForDslrWrapper(win, deadline, intervalMs) {
  for (;;) {
    let w = null;
    try {
      const cand = win && win.slrDSLREngineWrapper;
      if (cand && cand.storedOptionsHash && typeof cand.getEngine === "function" &&
          cand.storedOptionsHash["protectedPatterns"]) {
        w = cand;
      }
    } catch (e) { w = null; }
    if (w) return w;
    if (Date.now() >= deadline) return null;
    await sleep(intervalMs);
  }
}

/**
 * Install the per-project pattern switch on the live DSLR engine. Every
 * failure path returns {ok:false, reason} instead of throwing — a missing or
 * unusual DSLR build leaves the engine exactly as it was.
 *
 * @param {Object} env injected page surface:
 *   window, trans, sys, isEnabled? () => boolean, getPatterns? (id) => string,
 *   log?, intervalMs?, maxWaitMs?
 *   getPatterns resolves an option id to the USER'S Unreal pattern copy
 *   (addon config); undefined/empty falls back to the built-in set.
 * @returns {Promise<{ok:boolean, reason?:string, wrapper?:Object}>}
 */
async function installUnrealPatternOverride(env) {
  const opts = env || {};
  const log = typeof opts.log === "function" ? opts.log : function () {};
  const intervalMs = opts.intervalMs || 250;
  const maxWaitMs = opts.maxWaitMs || 30000;
  const isEnabled = typeof opts.isEnabled === "function" ? opts.isEnabled : () => true;
  const getUserPatterns = typeof opts.getPatterns === "function" ? opts.getPatterns : () => undefined;
  const trans = opts.trans;
  const sys = opts.sys;

  const wrapper = await waitForDslrWrapper(
    opts.window, Date.now() + maxWaitMs, intervalMs);
  if (!wrapper) {
    return { ok: false, reason: "DSLR engine wrapper not found (SLRtrans core missing?)" };
  }

  const engine = wrapper.getEngine();

  let installed = 0;
  for (const id of PATTERN_OPTION_IDS) {
    const option = wrapper.storedOptionsHash[id];
    if (!option || !option.options) {
      log("pattern option not found:", id);
      continue;
    }
    if (option.options.backend && option.options.backend.__slrunrealUE) {
      installed++;                                  // already ours (re-load guard)
      continue;
    }
    option.options.backend = {
      __slrunrealUE: true,
      getValue() {
        try {
          if (isEnabled() && isUnrealProject(trans)) {
            // the user's editable copy first (live read — edits in the addon
            // options apply to the next translation), then the built-in set
            const custom = getUserPatterns(id);
            if (custom !== undefined && custom !== null) return custom;
            if (UE_PATTERN_DEFAULTS[id] !== undefined) return UE_PATTERN_DEFAULTS[id];
          }
        } catch (e) { /* fall through to the user's value */ }
        // The user's stored value only — NOT the engine property or schema
        // default (those snapshot whatever getValue() last returned and would
        // leak the Unreal set into non-Unreal translations after a rebuild).
        try {
          const store = sys && sys.config && sys.config.translatorEngineOptions;
          const own = store && store[engine.id];
          if (own && own[id] !== undefined) return own[id];
        } catch (e) { /* fall through */ }
        return undefined;   // SLROption.getValue applies the RPG factory default
      },
      setValue(id, value) {                          // SLROption passes (id, value)
        try { engine.update(id, value); }
        catch (e) { log("setValue failed for", id, e && e.message); }
      },
    };
    installed++;
  }

  if (!installed) {
    return { ok: false, reason: "no pattern options found on the DSLR wrapper" };
  }
  return { ok: true, wrapper, installed };
}

module.exports = {
  installUnrealPatternOverride, isUnrealProject, waitForDslrWrapper,
  PATTERN_OPTION_IDS,
};
