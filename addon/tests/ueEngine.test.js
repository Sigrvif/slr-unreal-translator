const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { UE_PATTERN_DEFAULTS } = require("../lib/uePatterns.js");
const {
  installUnrealPatternOverride, isUnrealProject, PATTERN_OPTION_IDS,
} = require("../lib/ueEngine.js");

// --- fake SLR surface -------------------------------------------------------
// MiniOption mirrors SLROption's resolution EXACTLY as documented in
// www/js/Option.js: backend first, then the wrapper/engine chain, then the
// factory default when undefined. When a real SLR install is present, the
// cross-check test below runs the same assertions through the genuine class.
class MiniOption {
  constructor(o) { this.options = o; }
  getId() { return this.options.id; }
  getDefault() { return this.options.default; }
  getValue() {
    let v;
    if (this.options.backend) v = this.options.backend.getValue(this.options.id);
    else if (this.options.wrapper) v = this.options.wrapper.getEngine().getOptions(this.options.id);
    if (v === undefined) v = this.getDefault();
    return v;
  }
  setValue(value) {
    if (this.options.backend) this.options.backend.setValue(this.options.id, value);
    else if (this.options.wrapper) this.options.wrapper.getEngine().update(this.options.id, value);
  }
}

// Engine.getOptions mirrors the stored ?? defaultConfig ?? this[key] ?? schema
// chain of TranslatorEngine.prototype.getOptions; update() mirrors update().
function makeEngine(sys, id) {
  const eng = {
    id,
    getOptions(key) {
      const store = sys.config.translatorEngineOptions || {};
      const own = store[eng.id] || eng.defaultConfig || {};
      if (!key) return own;
      return own[key] ?? eng.defaultConfig?.[key] ?? eng[key]
        ?? eng.optionsForm?.schema?.[key]?.default;
    },
    update(key, value) {
      eng[key] = value;
      sys.config.translatorEngineOptions = sys.config.translatorEngineOptions || {};
      sys.config.translatorEngineOptions[eng.id] =
        sys.config.translatorEngineOptions[eng.id] || {};
      sys.config.translatorEngineOptions[eng.id][key] = value;
      sys.saved = (sys.saved || 0) + 1;
    },
    defaultConfig: {},
    optionsForm: { schema: {}, form: [] },
  };
  return eng;
}

function makeWorld() {
  const sys = {
    config: { translatorEngineOptions: { dslr: {
      protectedPatterns: "USER-protected",
      splittingPatterns: "USER-split",
      isolatePatterns: "USER-isolate",
      // splitEndsPatterns intentionally NOT stored (fresh-install case)
    } } },
    saved: 0,
  };
  const engine = makeEngine(sys, "dslr");
  const wrapper = {
    getEngine: () => engine,
    storedOptionsHash: {},
  };
  for (const id of PATTERN_OPTION_IDS) {
    wrapper.storedOptionsHash[id] = new MiniOption({
      wrapper, id, default: "RPG-DEFAULT-" + id, backend: undefined,
    });
  }
  const window = { slrDSLREngineWrapper: wrapper };
  const trans = { project: null };
  return { sys, engine, wrapper, window, trans };
}

const INSTALL = (world, extra = {}) => installUnrealPatternOverride(Object.assign({
  window: world.window, trans: world.trans, sys: world.sys,
  intervalMs: 1, maxWaitMs: 200,
}, extra));

test("installs tagged backends on the four pattern options, idempotently", async () => {
  const world = makeWorld();
  const res = await INSTALL(world);
  assert.equal(res.ok, true);
  assert.equal(res.installed, 4);
  for (const id of PATTERN_OPTION_IDS) {
    const backend = world.wrapper.storedOptionsHash[id].options.backend;
    assert.ok(backend && backend.__slrunrealUE === true, id);
  }
  // the DSLR engine registration itself is untouched
  assert.equal(world.engine.id, "dslr");
  // a second install (addon reload) does not stack backends
  const again = await INSTALL(world);
  assert.equal(again.ok, true);
  assert.equal(again.installed, 4);
  assert.equal(world.wrapper.storedOptionsHash.protectedPatterns.options.backend
    .__slrunrealUE, true);
});

test("Unreal projects get the Unreal sets; everything else the user's values", async () => {
  const world = makeWorld();
  await INSTALL(world);
  const get = (id) => world.wrapper.storedOptionsHash[id].getValue();

  // no project open -> user's stored values
  world.trans.project = null;
  assert.equal(get("protectedPatterns"), "USER-protected");
  assert.equal(get("splittingPatterns"), "USER-split");

  // RPG Maker project -> user's stored values
  world.trans.project = { gameEngine: "rmmv", options: {} };
  assert.equal(get("protectedPatterns"), "USER-protected");

  // Unreal project (either marker) -> Unreal sets, all four options
  world.trans.project = { gameEngine: "unreal", options: {} };
  assert.equal(get("protectedPatterns"), UE_PATTERN_DEFAULTS.protectedPatterns);
  assert.equal(get("splittingPatterns"), UE_PATTERN_DEFAULTS.splittingPatterns);
  assert.equal(get("isolatePatterns"), UE_PATTERN_DEFAULTS.isolatePatterns);
  assert.equal(get("splitEndsPatterns"), UE_PATTERN_DEFAULTS.splitEndsPatterns);

  world.trans.project = { gameEngine: "", options: { slrunreal: { gameDir: "E:/g" } } };
  assert.equal(get("protectedPatterns"), UE_PATTERN_DEFAULTS.protectedPatterns);
});

test("fresh-install keys fall back to the RPG factory default, never to a leaked Unreal set", async () => {
  const world = makeWorld();
  await INSTALL(world);
  // splitEndsPatterns has no stored value. Simulate the options-form build
  // having written the CURRENT getValue() onto the engine property while a
  // Unreal project was open (OptionFormBuilder._emitEntries does this): the
  // override must NOT let that leak into a non-Unreal translation.
  world.trans.project = { gameEngine: "unreal", options: {} };
  const leaked = world.wrapper.storedOptionsHash.splitEndsPatterns.getValue();
  assert.equal(leaked, UE_PATTERN_DEFAULTS.splitEndsPatterns);
  world.engine.splitEndsPatterns = leaked;            // _emitEntries effect
  world.engine.optionsForm.schema.splitEndsPatterns = { default: leaked };

  world.trans.project = { gameEngine: "rmmv", options: {} };
  assert.equal(world.wrapper.storedOptionsHash.splitEndsPatterns.getValue(),
    "RPG-DEFAULT-splitEndsPatterns");
});

test("user edits in the DSLR options tab persist through the backend", async () => {
  const world = makeWorld();
  await INSTALL(world);
  world.trans.project = { gameEngine: "rmmv", options: {} };
  world.wrapper.storedOptionsHash.protectedPatterns.setValue("MY-new-patterns");
  assert.equal(
    world.sys.config.translatorEngineOptions.dslr.protectedPatterns,
    "MY-new-patterns");
  assert.equal(world.wrapper.storedOptionsHash.protectedPatterns.getValue(),
    "MY-new-patterns");
});

test("the addon toggle disables the switch without touching stored values", async () => {
  const world = makeWorld();
  await INSTALL(world, { isEnabled: () => false });
  world.trans.project = { gameEngine: "unreal", options: {} };
  assert.equal(world.wrapper.storedOptionsHash.protectedPatterns.getValue(),
    "USER-protected");
});

test("editable user copies win, verbatim — empty means no patterns (DSL semantics)", async () => {
  const world = makeWorld();
  const copies = { protectedPatterns: "/<MyTag>/g,", isolatePatterns: "" };
  await INSTALL(world, { getPatterns: (id) => copies[id] });
  world.trans.project = { gameEngine: "unreal", options: {} };
  const get = (id) => world.wrapper.storedOptionsHash[id].getValue();
  // user's edited copy, live-read on every getValue call
  assert.equal(get("protectedPatterns"), "/<MyTag>/g,");
  copies.protectedPatterns = "/<Edited>/g,";
  assert.equal(get("protectedPatterns"), "/<Edited>/g,");
  // empty copy passes through as "" (cleared box = no patterns), undefined
  // copy (never configured) falls back to the built-in set
  assert.equal(get("isolatePatterns"), "");
  assert.equal(get("splitEndsPatterns"), UE_PATTERN_DEFAULTS.splitEndsPatterns);
  // non-Unreal project ignores the copies entirely
  world.trans.project = { gameEngine: "rmmv", options: {} };
  assert.equal(get("protectedPatterns"), "USER-protected");
});

test("fails softly when the DSLR wrapper never appears", async () => {
  const world = makeWorld();
  world.window.slrDSLREngineWrapper = undefined;
  const res = await INSTALL(world, { intervalMs: 1, maxWaitMs: 20 });
  assert.equal(res.ok, false);
  assert.match(res.reason, /wrapper not found/);
});

test("isUnrealProject recognizes both markers", () => {
  const trans = { project: null };
  assert.equal(isUnrealProject(trans), false);
  assert.equal(isUnrealProject({ project: { gameEngine: "unreal" } }), true);
  assert.equal(isUnrealProject({ project: { options: { slrunreal: {} } } }), true);
  assert.equal(isUnrealProject({ project: { gameEngine: "rmmv" } }), false);
});

// Cross-check against the GENUINE SLROption class when an SLR install is
// present: the whole override rides on SLROption's backend-first contract,
// so pin that the real class resolves exactly like MiniOption above.
const SLR_INSTALL = process.env.SLRUNREAL_SLROPTION_JS || "";  // local SLR install; test skips without it
test("real SLROption resolves the installed backend the same way", async () => {
  if (!fs.existsSync(SLR_INSTALL)) return;            // no SLR install here
  globalThis.window = globalThis.window || {};        // Option.js assigns window.SLROption
  const SLROption = require(SLR_INSTALL);
  const world = makeWorld();
  const res = await INSTALL(world);
  assert.equal(res.ok, true);

  const real = new SLROption({ id: "protectedPatterns", default: "RPG-X" });
  // graft our backend + wrapper exactly as the live engine holds them
  real.options.backend = world.wrapper.storedOptionsHash.protectedPatterns.options.backend;
  world.trans.project = { gameEngine: "unreal", options: {} };
  assert.equal(real.getValue(), UE_PATTERN_DEFAULTS.protectedPatterns);
  world.trans.project = { gameEngine: "rmmv", options: {} };
  assert.equal(real.getValue(), "USER-protected");
  world.trans.project = null;
  real.setValue("EDITED-via-real-class");
  assert.equal(
    world.sys.config.translatorEngineOptions.dslr.protectedPatterns,
    "EDITED-via-real-class");
  assert.equal(real.getValue(), "EDITED-via-real-class");
});

// --- pattern strings parse under DSLR's eval contract -----------------------
// getPatterns() does eval("[\n" + str + "\n]"): every shipped string must be
// a valid array literal of regexes (comment lines allowed between items).
test("every Unreal pattern string evals to an array of RegExp", () => {
  for (const id of Object.keys(UE_PATTERN_DEFAULTS)) {
    const str = UE_PATTERN_DEFAULTS[id];
    let arr;
    try {
      arr = eval("[\n" + str + "\n]");             // same call DSLR makes
    } catch (e) {
      assert.fail(id + " does not parse: " + e.message);
    }
    assert.ok(Array.isArray(arr), id);
    for (const item of arr) {
      assert.ok(item instanceof RegExp, id + " produced non-regex " + String(item));
    }
    assert.ok(arr.length > 0 || id === "isolatePatterns",
      id + " should carry at least one pattern (isolate may be empty)");
  }
});

test("the Unreal protections actually match Unreal markup and miss plain text", () => {
  const protectedPatterns = eval("[\n" + UE_PATTERN_DEFAULTS.protectedPatterns + "\n]");
  const hit = (s) => protectedPatterns.some((re) => {
    const m = s.match(re);
    return m && m[0].length > 0;
  });
  assert.equal(hit("<Em>彼女</Em>"), true);          // rich text
  assert.equal(hit("<font color=\"#ff0\">警告</font>"), true);
  assert.equal(hit("続ける<br/>"), true);
  assert.equal(hit("{PlayerName}の冒険"), true);      // FText format arg
  assert.equal(hit("{0}個手に入れた！"), true);
  assert.equal(hit("セーブしますか？"), false);        // plain dialogue untouched
  assert.equal(hit("ゲームクオリティ-高"), false);
});

test("no-Japanese split cells pass through but Japanese cells do not", () => {
  const splits = eval("[\n" + UE_PATTERN_DEFAULTS.splittingPatterns + "\n]");
  const fullMatch = (s) => splits.some((re) => {
    re.lastIndex = 0;
    const m = re.exec(s);
    return m && m[0] === s;
  });
  assert.equal(fullMatch("New Game"), true);         // already-Latin label: not sent
  assert.equal(fullMatch("OPTIONS"), true);
  assert.equal(fullMatch("はじめる"), false);          // Japanese cell: translated
  assert.equal(fullMatch("Settings\nはじめる"), false);
});
