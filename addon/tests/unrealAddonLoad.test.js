const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

const ADDON_DIR = path.join(__dirname, "..");
const STAGED = path.join(ADDON_DIR, "..", "dist", "stage-win", "SLRUnreal");

// Evaluate UnrealAddon.js under SLR's exact loader shape (Addon.loadJS):
// new AsyncFunction(body).call(thisAddon) with page globals as TRUE globals
// (AsyncFunction bodies resolve free variables through global scope).
async function evalAddon(addonDir, stubs, overrides = {}) {
  const saved = {};
  const env = {
    require,                                    // NW.js exposes it as a page global
    thisAddon: {
      fullPath: addonDir,
      package: { version: "0.1.0" },
      getWebLocation: () => "file:///" + addonDir.split(path.sep).join("/"),
    },
    $: null,
    ui: { newProjectDialog: null, showLoading() {}, loadingEnd() {}, loadingProgress() {} },
    trans: { project: null },
    engines: {
      add: (id, opts) => stubs.engines.push([id, opts]),
      addHandler: (id, kind, fn) => { stubs.handlers[id + ":" + kind] = fn; },
    },
    Engines: { registerDetector: (fn) => stubs.detectors.push(fn) },
    common: {},
    fsh: {},
    alert: (m) => { throw new Error("alert called: " + m); },
    nw: undefined,
    ...overrides,
  };
  for (const [k, v] of Object.entries(env)) { saved[k] = globalThis[k]; globalThis[k] = v; }
  const dispose = () => {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete globalThis[k]; else globalThis[k] = v;
    }
  };
  try {
    const script = fs.readFileSync(path.join(ADDON_DIR, "UnrealAddon.js"), "utf-8");
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const ret = await new AsyncFunction(script).call(env.thisAddon);
    // Globals stay installed until dispose(): interaction tests drive the
    // registered handlers afterwards, and the body's free variables
    // (trans, ui, $, …) resolve through global scope at CALL time.
    return { ret, env, dispose };
  } catch (e) {
    dispose();
    throw e;
  }
}

test("evals under the SLR loader contract and registers engine + handlers", async () => {
  if (!fs.existsSync(path.join(STAGED, "runtime", "python", "python.exe")))
    return;                                     // needs a built stage (build_release)
  const stubs = { engines: [], handlers: {}, detectors: [] };
  const { ret, env, dispose } = await evalAddon(STAGED, stubs);
  try {
    assert.equal(stubs.engines.length, 1);
    assert.equal(stubs.engines[0][0], "unreal");
    assert.equal(stubs.detectors.length, 1);
    assert.ok(stubs.handlers["unreal:createProject"]);
    assert.ok(stubs.handlers["unreal:exportHandler"]);
    // The addon options form: the pattern-switch toggle followed by the four
    // editable Unreal pattern sets, defaults from lib/uePatterns.js.
    const form = env.thisAddon.optionsForm;
    assert.ok(form, "optionsForm should be assigned on the addon");
    assert.deepEqual(Object.keys(form), [
      "uePatternOverride", "ueProtectedPatterns", "ueSplittingPatterns",
      "ueIsolatePatterns", "ueSplitEndsPatterns",
    ]);
    assert.equal(form.uePatternOverride.default, true);
    const { UE_PATTERN_DEFAULTS } = require(path.join(STAGED, "lib", "uePatterns.js"));
    assert.equal(form.ueProtectedPatterns.default, UE_PATTERN_DEFAULTS.protectedPatterns);
    assert.equal(form.ueSplitEndsPatterns.default, UE_PATTERN_DEFAULTS.splitEndsPatterns);
  } finally {
    dispose();
  }
});

test("bails silently (no throw, no registration) without the runtime payload", async () => {
  // Realistic package minus the runtime payload: lib/ ships, runtime/ absent.
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "slrunreal-nort-"));
  fs.mkdirSync(path.join(tmp, "lib"), { recursive: true });
  for (const f of fs.readdirSync(path.join(ADDON_DIR, "lib")))
    fs.copyFileSync(path.join(ADDON_DIR, "lib", f), path.join(tmp, "lib", f));
  const stubs = { engines: [], handlers: {}, detectors: [] };
  const { ret, dispose } = await evalAddon(tmp, stubs);
  dispose();
  assert.equal(ret, undefined);
  assert.equal(stubs.engines.length, 0);
  assert.equal(stubs.detectors.length, 0);
});

// Minimal jQuery stand-in: just what drawProjectCreator touches — element
// lookup by selector (cached per root), handler registration, trigger with
// a settable `this.value`, prop/text/attr no-ops. Every created element
// lands in $.created so tests can reach detached ones (selectDir's input).
function makeFakeJQ() {
  const makeEl = (jq, html) => {
    const el = {
      html, handlers: {}, kids: {}, props: {}, value: "",
      find(sel) { if (!this.kids[sel]) this.kids[sel] = makeEl(jq, sel); return this.kids[sel]; },
      on(ev, fn) { this.handlers[ev] = fn; return this; },
      one(ev, fn) { this.handlers[ev] = fn; return this; },
      trigger(ev) { const h = this.handlers[ev]; if (h) h.call(this); return this; },
      prop(k, v) { if (v === undefined) return this.props[k]; this.props[k] = v; return this; },
      text() { return this; }, attr() { return this; },
      append() { return this; }, val() { return this; },
    };
    jq.created.push(el);
    return el;
  };
  const jq = (html) => makeEl(jq, html);
  jq.created = [];
  return jq;
}

// A game tree resolveGameRoot accepts: <root>/<Project>/Content/Paks with a
// container + the shipping exe.
function makeFakeGame() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "slrunreal-game-"));
  const proj = path.join(root, "MyGame");
  fs.mkdirSync(path.join(proj, "Content", "Paks"), { recursive: true });
  fs.mkdirSync(path.join(proj, "Binaries", "Win64"), { recursive: true });
  fs.writeFileSync(path.join(proj, "Content", "Paks", "MyGame-Windows.pak"), "pak");
  fs.writeFileSync(path.join(proj, "Binaries", "Win64",
    "MyGame-Win64-Shipping.exe"), "MZ");
  return root;
}

test("create-project slide click reaches createProjectFromEngine (no stale option refs)", async () => {
  if (!fs.existsSync(path.join(STAGED, "runtime", "python", "python.exe")))
    return;                                     // needs a built stage (build_release)
  const gameRoot = makeFakeGame();
  const created = [];
  const stubs = { engines: [], handlers: {}, detectors: [] };
  const fakeJQ = makeFakeJQ();
  const { env } = await evalAddon(STAGED, stubs, {
    $: fakeJQ,
    ui: {
      newProjectDialog: {
        addMenu(menu) {
          // SLR invokes drawProjectCreator() right here (slides map values)
          for (const slide of Object.values(menu.slides || {})) created.push(slide);
        },
        close() {},
      },
      showLoading() {}, loadingEnd() {}, loadingProgress() {},
    },
    trans: {
      project: null,
      createProjectFromEngine: async (engineId, payload) => {
        env.lastCreate = { engineId, payload };
        return { project: true };
      },
    },
  });
  const slide = created[0];
  assert.ok(slide, "drawProjectCreator should run via addMenu");

  // the pick-dir flow: the click invokes selectDir synchronously (its
  // detached nwdirectory input is created right there); resolve that input
  // via its input handler (this.value). The shipping exe sits INSIDE the
  // project folder, so resolveGameRoot walks up to <root>/MyGame.
  slide.find(".slrunrealPickDir").handlers.click();
  const input = fakeJQ.created.find((e) =>
    String(e.html || "").includes('type="file" nwdirectory'));
  assert.ok(input, "selectDir input should have been created");
  input.value = path.join(gameRoot, "MyGame", "Binaries", "Win64",
    "MyGame-Win64-Shipping.exe");
  input.handlers.input.call(input);                   // resolve the pick
  await new Promise((r) => setImmediate(r));          // let pick finish

  // the create-project click — the Phase-5b regression: a stale free
  // variable here threw ReferenceError (unhandled rejection) BEFORE
  // createProjectFromEngine was reached
  await slide.find(".slrunrealCreate").handlers.click();

  assert.ok(env.lastCreate, "createProjectFromEngine must be called");
  assert.equal(env.lastCreate.engineId, "unreal");
  assert.equal(env.lastCreate.payload.gameEngine, "unreal");
  // detector convention: the root is the folder CONTAINING
  // <Project>/Content/Paks — the exe pick walks up to it
  assert.equal(env.lastCreate.payload.gameFolder, gameRoot);
  assert.equal(env.lastCreate.payload.gameTitle, path.basename(gameRoot));
  assert.equal(env.lastCreate.payload.options, undefined,
    "create payload must not carry options — demosaic lives in the export dialog");
});
