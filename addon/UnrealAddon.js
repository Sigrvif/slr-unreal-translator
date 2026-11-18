// SLRUnreal addon wiring: New Project entry, createProject, exportHandler.
// Mirrors SLRUnity's UnityAddon.js (v2.7) integration points exactly.
//
// LOADER CONTRACT (www/js/addon.js, Addon.loadJS): this file's body is eval'd
// as an AsyncFunction with `this` = the Addon instance — top-level statements
// only, NO IIFE. Top-level `return` bails out of the body. The NW page has
// full Node access, and the page globals (engines, Engines, common, trans,
// ui, alert) are all in scope — the same surface SEP.js uses.
const thisAddon = this;                       // loader contract (SLRUnity:18)
const path = require("path");
const fs = require("fs");
const addonDir = path.resolve(thisAddon.fullPath);
const L = (f) => require(path.join(addonDir, f));
const { runBackend } = L("lib/backend.js");
const { buildProject, applyTranslations } = L("lib/projectBuilder.js");
const { detectUnreal, resolveGameRoot } = L("lib/detector.js");

// Bundled Python runtime for this OS (payloads fetched at BUILD time).
// Absent payload => the addon stays invisible: no engine, no handlers.
const RUNTIME = (() => {
  try {
    const { resolveRuntime } = L("lib/backend.js");
    return resolveRuntime(addonDir);
  } catch (e) {
    console.warn("unreal: runtime resolution failed:", e);
    return null;
  }
})();
if (!RUNTIME) return;

const fsh = {
  isFile: (p) => { try { return fs.statSync(p).isFile(); } catch (e) { return false; } },
  isDir: (p) => { try { return fs.statSync(p).isDirectory(); } catch (e) { return false; } },
  listDir: (p) => { try { return fs.readdirSync(p); } catch (e) { return []; } },
  join: (...a) => path.join(...a), dirname: (p) => path.dirname(p),
};

// $ is a page global in NW; the window.jQuery fallback keeps loader-eval
// test harnesses (which inject page globals as parameters) working.
const $jq = (typeof $ === "function") ? $ :
  ((typeof window !== "undefined" && window && typeof window.jQuery === "function") ? window.jQuery : null);

function openHelp() {
  // SLRUnity's opener (UnityAddon.js:346-362): prefer SLR's documentation
  // system addon page (package.json docs field integrates docs.html into
  // the Help window); fall back to assets/help.html in the system browser
  // for SLR builds without the documentation system.
  try {
    if (typeof docsHelp !== "undefined" && docsHelp && typeof docsHelp.open === "function") {
      docsHelp.open("addon-SLRUnreal");
      return;
    }
  } catch (e) {}
  const url = "file:///" + path.join(addonDir, "assets", "help.html").split(path.sep).join("/");
  try {
    if (typeof nw !== "undefined" && nw && nw.Shell && typeof nw.Shell.openExternal === "function") {
      nw.Shell.openExternal(url);
      return;
    }
  } catch (e) {}
  try { if (typeof window !== "undefined" && window.open) { window.open(url); return; } } catch (e) {}
  alert("SLRUnreal help page (open in a browser):\n" + path.join(addonDir, "assets", "help.html"));
}

// Overlay helpers (SLRUnity's exactly-once discipline, UnityAddon.js:924-950):
// ui.showLoading() first, per-step lines through ui.loadingProgress, an
// idempotent end that runs BEFORE every alert on every exit path.
function uiLine(title, line) {
  try {
    ui.loadingProgress(title, line, { consoleOnly: false, mode: "consoleOutput" });
  } catch (e) { console.warn(title + ": " + line); }
}
function showProgress(p) { uiLine("Unreal scan", p.done + "/" + p.total + "  " + p.file); }

function selectDir() {
  // customParser selectDir pattern: invisible nwdirectory input resolves
  // with the picked folder on "input" (this.value, not $(this).val()).
  return new Promise((resolve) => {
    const $elm = $jq("<input type=\"file\" nwdirectory />");
    $elm.one("input", function () { resolve(this.value || ""); });
    $elm.trigger("click");
  });
}

// --- SEP image round-trip (standalone tools, SLRUnity e2e-fix6 shape) ----
// Decoupled from any translation project: extract dumps every Texture2D as
// PNG (<Game>_unreal_img + manifest.json), the user translates the images in
// an SEP project over that folder, and insert writes the rendered PNGs back
// into rebuilt containers. Both drive the overlay lifecycle themselves and
// summarize on the console; only failures alert (SLRUnity e2e-fix10).
function tempJobsDir(prefix) {
  const os = require("os");
  try { return fs.mkdtempSync(path.join(os.tmpdir(), prefix)); } catch (e) {}
  return path.join(os.tmpdir(), prefix + "-" + Date.now()); // runBackend mkdirs it
}

function siblingDir(gameDir, suffix) {
  return path.join(path.dirname(gameDir), path.basename(gameDir) + suffix);
}

async function unrealRunExtractImages(gameDir) {
  const outDir = siblingDir(gameDir, "_unreal_img");
  const jobsDir = tempJobsDir("slrunreal-img-");
  try { ui.showLoading(); } catch (e) {}
  uiLine("Unreal images", "Extracting images to " + outDir + " — progress in this console.");
  let res = null, err = null;
  try {
    res = await runBackend(
      { version: 2, command: "extract_images", game_dir: gameDir, out_dir: outDir },
      { jobsDir, python: RUNTIME, basePath: addonDir,
        onProgress: (p) => uiLine("Unreal images", p.done + "/" + p.total + "  " + path.basename(p.file || "")) });
  } catch (e) { err = e; }
  let ended = false;
  const endLoading = () => { if (ended) return; ended = true; ui.loadingEnd(); };
  try {
    if (err || !res.ok) {
      endLoading(); // failures end the overlay FIRST — no alert under it
      alert("Image extraction failed: " + (err ? (err.message || String(err)) : (res.error || "unknown")) +
            "\nLog: " + path.join(jobsDir, "backend.log"));
      return;
    }
    uiLine("Unreal images", "Extraction complete — " + (res.stats ? res.stats.images : "?") + " images");
    uiLine("Unreal images", "output: " + outDir);
    uiLine("Unreal images", "next: 1) Delete the images you don't want translated (in that folder).");
    uiLine("Unreal images", "next: 2) Create an SEP project on that folder (New Project \u2192 SEP).");
    uiLine("Unreal images", "next: 3) Translate the images in SEP, then export SEP's rendered images to a folder (same file names, same sizes).");
    uiLine("Unreal images", "next: 4) Come back here (New Project \u2192 SLRUnreal) and use \"Insert translated images\".");
  } finally {
    endLoading(); // the TRUE end — last on success, exactly once on every path
  }
}

// Font swap resolution (Phase 5b, SLRUnity parity): the two roles are
// SLR Translator's own faces unless the user supplied overrides — the
// dialogue role MUST be CJK-capable (untranslated Japanese has to keep
// rendering), the UI role is the narrow Latin face. A missing file logs a
// WARNING and skips ONLY the font stage (strings/images still patched).
function unrealResolveFonts(dialoguePath, uiPath) {
  const cfg = (p) => String(p || "").trim();
  let root = "";
  try { root = (typeof common.getRootDir === "function") ? common.getRootDir() : ""; } catch (e) {}
  const dlg = cfg(dialoguePath) || path.join(root, "www", "core", "SEP", "SLRCondensedCJK-Regular.ttf");
  const ui = cfg(uiPath) || path.join(root, "fonts", "sucr.ttf");
  const missing = [dlg, ui].filter((p) => !fsh.isFile(p));
  if (missing.length) {
    uiLine("Unreal fonts", "WARNING: font file missing (" + missing.join("; ") +
           ") — font swap skipped, other patches unaffected");
    return null;
  }
  return { dialogue: dlg, ui: ui };
}

// --- standalone demosaic (Phase 8b) ---------------------------------------
// One backend command (demosaic_patch): the OPEN project's staging set in,
// a demosaic patch container out. Portable like export_patch (no game
// folder), disjoint output name (_SLRDM_P) so it coexists with the
// translation patch in Paks/.
async function unrealRunDemosaic(o) {
  const jobsDir = tempJobsDir("slrunreal-dm-");
  try { ui.showLoading(); } catch (e) {}
  uiLine("Unreal demosaic", "Building the demosaic patch from the project's staging set…");
  let res = null, err = null;
  try {
    res = await runBackend(
      { version: 2, command: "demosaic_patch", stage_dir: o.stageDir,
        out: o.outDir, include_medium: o.includeMedium ? true : undefined },
      { jobsDir, python: RUNTIME, basePath: addonDir });
  } catch (e) { err = e; }
  let ended = false;
  const endLoading = () => { if (ended) return; ended = true; ui.loadingEnd(); };
  try {
    if (err || !res.ok) {
      endLoading();
      alert("Demosaic failed: " + (err ? (err.message || String(err)) : (res.error || "unknown")) +
            "\nLog: " + path.join(jobsDir, "backend.log"));
      return;
    }
    if (!res.ops_applied && !res.ops_skipped) {
      uiLine("Unreal demosaic", "No demosaic candidates for this game — nothing to do.");
      endLoading();
      return;
    }
    uiLine("Unreal demosaic", "Applied " + res.ops_applied + " of " +
      (res.ops_applied + res.ops_skipped) + " candidate(s), verify: " + res.verify);
    for (const s of (res.skipped_detail || []).slice(0, 20)) {
      uiLine("Unreal demosaic", "  skipped: " + (s.asset || "?") + " — " + (s.reason || "?"));
    }
    for (const c of (res.containers || [])) {
      uiLine("Unreal demosaic", "Wrote " + path.basename(c));
    }
    if ((res.containers || []).length) {
      uiLine("Unreal demosaic", "Copy the output into the game's Content\\Paks folder (beside the base containers). Delete it to uninstall.");
    }
  } finally {
    endLoading();
  }
}

async function unrealRunInsertImages(o) {
  const jobsDir = tempJobsDir("slrunreal-insert-");
  try { ui.showLoading(); } catch (e) {}
  uiLine("Unreal images", "Inserting translated images — rebuilding containers into " + o.outDir + " (progress in this console).");
  let res = null, err = null;
  try {
    res = await runBackend(
      { version: 2, command: "insert_images", game_dir: o.gameDir, out_dir: o.outDir,
        textures_manifest_dir: o.extractDir, textures_dir: o.renderedDir,
        fonts: o.fonts || undefined },
      { jobsDir, python: RUNTIME, basePath: addonDir,
        onProgress: (p) => uiLine("Unreal images", p.done + "/" + p.total + "  " + path.basename(p.file || "")) });
  } catch (e) { err = e; }
  let ended = false;
  const endLoading = () => { if (ended) return; ended = true; ui.loadingEnd(); };
  try {
    if (err || !res.ok) {
      endLoading();
      alert("Inserting translated images failed: " + (err ? (err.message || String(err)) : (res.error || "unknown")) +
            "\nLog: " + path.join(jobsDir, "backend.log"));
      return;
    }
    uiLine("Unreal images", "Insert complete");
    uiLine("Unreal images", "images imported: " + (res.stats ? res.stats.imported : "?"));
    if (res.fonts_swapped) {
      uiLine("Unreal images", "fonts swapped: " + res.fonts_swapped + " face(s)");
    }
    if (res.stats && res.stats.errors && res.stats.errors.length) {
      uiLine("Unreal images", "skipped entries: " + res.stats.errors.length + " (see backend.log)");
    }
    uiLine("Unreal images", "output: " + o.outDir);
    uiLine("Unreal images", "apply: copy the rebuilt containers over the game's own files (keep the folder layout), then run the game.");
  } finally {
    endLoading();
  }
}

// --- New Project slide ---------------------------------------------------
function drawProjectCreator() {
  const version = (thisAddon.package && thisAddon.package.version)
    ? (" Ver. " + thisAddon.package.version) : "";
  const $slide = $jq(`<div>
    <h1><i class="icon-plus-circled"></i><span>SLRUnreal${version}</span></h1>
    <div class="dialogSectionBlock fieldgroup">
        <h2>Create a translation project</h2>
        <div class="info">Pick the game folder (or its *-Win64-Shipping.exe). The scan reads the game's UE containers (pak / IoStore), extracts every user-visible string with its asset context, and — for games whose pak index is encrypted — discovers the pak key offline from the game's own executable (no key is bundled with the addon). A full scan takes a few minutes; it also captures the portable staging set that later exports build from (no game folder needed at export). The game itself is never modified.</div>
        <div class="actionButtons">
            <button type="button" class="slrunrealPickDir"><i class="icon-folder-open"></i>Select Folder</button>
        </div>
        <div class="actionButtons">
            <button type="button" class="slrunrealCreate" disabled>Create project</button>
        </div>
    </div>
    <div class="dialogSectionBlock fieldgroup">
        <h2>Extract images for image translation (SEP)</h2>
        <div class="info">Standalone tool — no translation project needed. Dumps every Texture2D as PNG into &lt;Game&gt;_unreal_img next to the game (plus a manifest). Start here when text inside images needs translating; nothing in the game is modified. An SEP project is created on that folder separately (New Project &rarr; SEP).</div>
        <div class="actionButtons">
            <button class="slrunrealExtractImages"><i class="icon-picture"></i>Select Game Folder &amp; Extract Images</button>
        </div>
    </div>
    <div class="dialogSectionBlock fieldgroup">
        <h2>Insert translated images into rebuilt containers</h2>
        <div class="info">Standalone tool — the last step of the image round-trip. Writes SEP's rendered images back into the game's UE containers (same-size PNGs only) and rebuilds the affected containers into the output folder. Strings are translated separately, from the strings project's own export.</div>
        <div class="slrunrealPickRow" style="max-width:34em;margin:8px auto;text-align:center">
            <div style="margin-bottom:2px">1. Game folder</div>
            <label><input type="dvSelectPath" nwdirectory class="fullWidth slrunrealPick slrunrealPickGame" data-slrunreal-pick="game" value="" /></label>
            <div class="slrunrealPickGamePath" style="opacity:.75;font-size:.9em;margin-top:2px">(not selected)</div>
        </div>
        <div class="slrunrealPickRow" style="max-width:34em;margin:8px auto;text-align:center">
            <div style="margin-bottom:2px">2. Extracted-images folder</div>
            <label><input type="dvSelectPath" nwdirectory class="fullWidth slrunrealPick slrunrealPickExtract" data-slrunreal-pick="extract" value="" /></label>
            <div class="slrunrealPickExtractPath" style="opacity:.75;font-size:.9em;margin-top:2px">(with manifest.json)</div>
        </div>
        <div class="slrunrealPickRow" style="max-width:34em;margin:8px auto;text-align:center">
            <div style="margin-bottom:2px">3. SEP rendered-images folder</div>
            <label><input type="dvSelectPath" nwdirectory class="fullWidth slrunrealPick slrunrealPickRendered" data-slrunreal-pick="rendered" value="" /></label>
            <div class="slrunrealPickRenderedPath" style="opacity:.75;font-size:.9em;margin-top:2px">(SEP's export output)</div>
        </div>
        <div class="slrunrealPickRow" style="max-width:34em;margin:8px auto;text-align:center">
            <div style="margin-bottom:2px">4. Output folder (rebuilt containers)</div>
            <label><input type="dvSelectPath" nwdirectory class="fullWidth slrunrealPick slrunrealPickOut" data-slrunreal-pick="out" value="" /></label>
            <div class="slrunrealPickOutPath" style="opacity:.75;font-size:.9em;margin-top:2px">(game-relative container copies land here)</div>
        </div>
        <label style="display:block;margin:10px auto 0;max-width:34em;text-align:left"><input type="checkbox" class="slrunrealInsertFont"> Replace game fonts (SLRCondensedCJK for dialogue, Stint Ultra Condensed for UI — see documentation)</label>
        <div class="actionButtons">
            <button class="slrunrealInsertRun"><i class="icon-ok"></i>Insert translated images</button>
        </div>
    </div>
    <div class="dialogSectionBlock fieldgroup">
        <h2>Demosaic (standalone)</h2>
        <div class="info">Standalone tool — separate from translation export. Builds a demosaic PATCH container from the OPEN project's saved staging set (portable: no game folder needed). Where a model binds the game's overlay (mosaic) material, the clean material that ships in the same package is written into that slot instead. The candidate list is per-game and automatic — there is nothing to configure. Copy the output into the game's Content\Paks folder beside the base containers; delete it to uninstall.</div>
        <label style="display:block;margin:8px auto 0;max-width:34em;text-align:left"><input type="checkbox" class="slrunrealDmMedium"> Include experimental candidates (lower confidence — test on a copy)</label>
        <div class="actionButtons">
            <button class="slrunrealDemosaic"><i class="icon-ok"></i>Build demosaic patch (open project)</button>
        </div>
    </div>
    <div class="dialogSectionBlock"><a href="#" class="slrunrealOpenDocs">Open documentation</a></div>
  </div>`);
  let picked = "";
  $slide.find(".slrunrealPickDir").on("click", async function () {
    picked = await selectDir();
    if (picked) {
      $slide.find(".slrunrealCreate").prop("disabled", false);
    }
  });
  $slide.find(".slrunrealOpenDocs").on("click", function (ev) {
    try { if (ev && typeof ev.preventDefault === "function") ev.preventDefault(); } catch (e0) {}
    openHelp();
  });
  // (d) standalone demosaic — the OPEN project's staging set builds a
  // demosaic patch container (Phase 8b: separate operation, like the
  // image tools; the export dialog no longer carries demosaic options).
  $slide.find(".slrunrealDemosaic").on("click", async function () {
    const proj = (typeof trans !== "undefined" && trans && trans.project) || null;
    const meta = proj && proj.options && proj.options.slrunreal;
    if (!meta || !meta.stageDir) {
      alert("No Unreal project with a staging set is open.\n\nOpen (or create) an SLRUnreal translation project first — the demosaic candidates were captured when the project was created.");
      return;
    }
    const outDir = await selectDir();
    if (!outDir) return;
    await ui.newProjectDialog.close();
    await unrealRunDemosaic({
      stageDir: meta.stageDir,
      outDir: path.resolve(outDir),
      includeMedium: $slide.find(".slrunrealDmMedium").prop("checked") === true,
    });
  });
  $slide.find(".slrunrealCreate").on("click", async function () {
    if (!picked) return;
    let gameDir;
    try {
      gameDir = resolveGameRoot(fsh, picked, "");
    } catch (e) {
      alert(e && e.message ? e.message : String(e));
      return;
    }
    try { await ui.newProjectDialog.close(); } catch (e1) {}
    const title = String(gameDir).split(/[\\/]/).filter(Boolean).pop() || "Unreal game";
    // No options here: demosaic lives in the EXPORT dialog now (Phase 4b),
    // and buildProject constructs options.slrunreal (gameDir, rowMap) itself.
    await trans.createProjectFromEngine("unreal", {
      gameFolder: gameDir, selectedFile: picked, gameEngine: "unreal",
      gameTitle: title,
    });
  });
  // (b) standalone image extraction — one button, one folder pick; the
  // overlay lifecycle is driven by unrealRunExtractImages itself.
  $slide.find(".slrunrealExtractImages").on("click", async function () {
    const target = await selectDir();
    if (!target) return;
    let gameDir;
    try {
      gameDir = resolveGameRoot(fsh, target, "");
    } catch (e) {
      alert(e && e.message ? e.message : String(e));
      return;
    }
    await ui.newProjectDialog.close();
    await unrealRunExtractImages(gameDir);
  });
  // (c) insert translated images — four folder picks through SLR's native
  // dvSelectPath widget (SLRUnity e2e-fix9 pattern: ui.dvField.renderSelectPath
  // renders the "..." buttons; values read here on change). Plain text inputs
  // remain usable when DVField is unavailable.
  const picks = { game: "", extract: "", rendered: "", out: "" };
  const pickHints = {
    game: "(not selected)",
    extract: "(with manifest.json)",
    rendered: "(SEP's export output)",
    out: "(game-relative container copies land here)",
  };
  const wirePick = (inputCls, pathCls, key) => {
    $slide.find(inputCls).on("change", function () {
      const dir = String((this && this.value) || "");
      picks[key] = dir;
      try { $slide.find(pathCls).text(dir || pickHints[key]); } catch (e) {}
    });
  };
  wirePick(".slrunrealPickGame", ".slrunrealPickGamePath", "game");
  wirePick(".slrunrealPickExtract", ".slrunrealPickExtractPath", "extract");
  wirePick(".slrunrealPickRendered", ".slrunrealPickRenderedPath", "rendered");
  wirePick(".slrunrealPickOut", ".slrunrealPickOutPath", "out");
  try {
    const dv = (ui && ui.dvField && typeof ui.dvField.renderSelectPath === "function") ? ui.dvField
      : ((typeof DVField === "function") ? new DVField() : null);
    if (dv) dv.renderSelectPath($slide.find("[type=dvSelectPath]"));
    else console.warn("unreal: DVField unavailable — insert-tool path inputs stay plain text fields");
  } catch (e) { console.warn("unreal dvSelectPath render failed:", e); }
  $slide.find(".slrunrealInsertRun").on("click", async function () {
    if (!picks.game || !picks.extract || !picks.rendered || !picks.out) {
      alert("Pick all four folders first:\n  1. the game folder,\n  2. the extracted-images folder (from \"Extract images...\"),\n  3. SEP's rendered-images folder,\n  4. the output folder for the rebuilt containers.");
      return;
    }
    if (!fsh.isFile(fsh.join(picks.extract, "manifest.json"))) {
      alert("No manifest.json found in:\n" + picks.extract +
            "\n\nPick the folder created by \"Extract images for image translation (SEP)\".");
      return;
    }
    let gameDir;
    try {
      gameDir = resolveGameRoot(fsh, picks.game, "");
    } catch (e) {
      alert(e && e.message ? e.message : String(e));
      return;
    }
    await ui.newProjectDialog.close();
    const insertFonts = $slide.find(".slrunrealInsertFont").prop("checked")
      ? unrealResolveFonts("", "") : null;
    await unrealRunInsertImages({ gameDir, extractDir: picks.extract,
                                  renderedDir: picks.rendered, outDir: picks.out,
                                  fonts: insertFonts });
  });
  return $slide;
}

try {
  if ($jq && ui && ui.newProjectDialog && typeof ui.newProjectDialog.addMenu === "function") {
    ui.newProjectDialog.addMenu({
      icon: thisAddon.getWebLocation() + "/assets/icon.png",
      descriptionBar: `<h2>Translate an Unreal Engine game (UE4 / UE5)</h2>
                   <p>Scan a UE game's containers for Japanese strings and translate them with the DSLR engine — rebuilt containers on export, with optional image-overlay material swaps.</p>`,
      actionBar: "",
      goToSlide: "unrealTranslator",
      at: 4,
      slides: { "unrealTranslator": drawProjectCreator() },
    });
  } else {
    console.warn("unreal: New Project dialog entry NOT registered (no $ / ui.newProjectDialog.addMenu)");
  }
} catch (e) { console.warn("unreal New Project dialog entry failed:", e); }

// --- engine registration + detection ------------------------------------
try {
  engines.add("unreal", { title: "Unreal Engine (UE4 / UE5)" });
  Engines.registerDetector(async (gamePath) => detectUnreal(fsh, gamePath));
} catch (e) { console.warn("unreal engine registration failed:", e); }

// --- Unreal-aware DSLR patterns -------------------------------------------
// While an Unreal project is the ACTIVE project, the one DSLR engine's
// pattern options resolve to Unreal sets at translation time (rich-text
// markup, FText format arguments — the RPG Maker patterns never apply to UE
// text). Non-Unreal projects get exactly the user's stored DSLR values; the
// stored config and the DSLR options tab are never modified. No second
// engine, no second setup. Toggle + the four Unreal pattern sets (editable
// copies of lib/uePatterns.js, empty = built-in default): Options →
// General → SLRUnreal. Edits apply to the next translation, no restart.
const { UE_PATTERN_DEFAULTS } = L("lib/uePatterns.js");
const UE_PATTERN_FIELD = (key, title, description) => ({
  type: "string", default: UE_PATTERN_DEFAULTS[key],
  formType: "ace",
  formOptions: { aceMode: "javascript", aceTheme: "twilight", height: "200px" },
  title, description,
});
const UE_PATTERN_FORMAT = "Same format as DSLR's pattern boxes: comma-separated regular expression literals (e.g. /<Em>/gi,), one per line; // lines are comments. Clear completely for no patterns at all — same semantics as DSLR's own pattern options; the addon's built-in defaults ship in lib/uePatterns.js inside the addon folder if you want them back. Takes effect on the next translation.";
thisAddon.optionsForm = {
  uePatternOverride: {
    type: "boolean", default: true, formType: "checkbox",
    title: "Unreal text-protection patterns",
    inlinetitle: "Use Unreal patterns while an Unreal project is open",
    description: "While an Unreal Engine project is the active project, translate with the Unreal pattern sets below instead of DSLR's RPG Maker pattern sets. This is a runtime switch only — your DSLR settings and stored patterns are never modified, and non-Unreal projects always use your own patterns. Uncheck to translate Unreal projects with your DSLR patterns unchanged.",
  },
  ueProtectedPatterns: UE_PATTERN_FIELD("protectedPatterns",
    "Unreal Protected Patterns",
    "Matched text is replaced by a placeholder before translation and restored after — this is where markup survives the LLM: rich-text tags (<Em>…</Em>, <font color=…>, <br/>), FText format arguments ({0}, {PlayerName}) and DSLR's batch dividers.\n" + UE_PATTERN_FORMAT),
  ueSplittingPatterns: UE_PATTERN_FIELD("splittingPatterns",
    "Unreal Aggressive Splitting",
    "Matched text is split out and never sent to the translator: empty lines, and whole cells without Japanese (already-Latin UI labels are not re-translated).\n" + UE_PATTERN_FORMAT),
  ueIsolatePatterns: UE_PATTERN_FIELD("isolatePatterns",
    "Unreal Isolation Patterns",
    "Matched sequences are translated separately (deduplicated). Empty by default — Unreal text has no nameplate-escape structure to peel off.\n" + UE_PATTERN_FORMAT),
  ueSplitEndsPatterns: UE_PATTERN_FIELD("splitEndsPatterns",
    "Unreal Cutting Corners Patterns",
    "Matched symbols at a sentence's start/end are removed before translation and re-attached after: filename-looking rows, stray whitespace and linebreak padding.\n" + UE_PATTERN_FORMAT),
};
(async () => {
  try {
    const { installUnrealPatternOverride } = L("lib/ueEngine.js");
    const cfgKey = (id) => "ue" + id.charAt(0).toUpperCase() + id.slice(1);
    const res = await installUnrealPatternOverride({
      window: (typeof window !== "undefined") ? window : undefined,
      trans: (typeof trans !== "undefined") ? trans : undefined,
      sys: (typeof sys !== "undefined") ? sys : undefined,
      maxWaitMs: 10000,
      isEnabled: () => {
        try { return thisAddon.getConfig("uePatternOverride") !== false; }
        catch (e) { return true; }
      },
      // Live config read: the editable copies above, verbatim — an empty box
      // means NO patterns (same semantics as DSLR's own pattern options).
      // undefined (key never stored) falls back to lib/uePatterns.js.
      getPatterns: (id) => {
        try {
          const v = thisAddon.getConfig(cfgKey(id));
          if (typeof v === "string") return v;
        } catch (e) {}
        return undefined;                       // ueEngine falls back to uePatterns.js
      },
      log: (...a) => console.warn("unreal patterns:", ...a),
    });
    if (!res.ok) console.info("unreal: DSLR pattern override not installed:", res.reason);
  } catch (e) { console.warn("unreal: pattern override setup failed:", e); }
})();

// --- export options (SLRUnity parity: options live in the EXPORT dialog) --
// Declared like the custom parser's (CustomParserLoader.js:444-496):
// engines.getEngine(id).addProperty("exportOptionFields", () => jsonForm).
// jsonform maps "type":"boolean" to a checkbox; the form lists both keys by
// hand (its "*" wildcard would duplicate them). The onOpenExportDialogReady
// hook then moves SLR's hardcoded addon-fields wrapper (trans.html:899, the
// LAST dialog section) to the TOP so the options are seen — the SLRUnity
// e2e-fix15 pattern, pure DOM reorder, no SLR file touched.
const SLRUNREAL_EXPORT_SCHEMA = {
  "unrealFullRebuild": {
    "title": "Full container rebuild (uses the original game folder)",
    "description": "Off (default): the export is a small PATCH container built entirely from the files saved inside this project — no game folder needed. Legacy-pak games (SampleGameB) get one <name>_SLR_P.pak; IoStore games (SampleGameA) get a <name>_SLR_P.utoc/.ucas pair. Copy them into the game's Content\\Paks folder beside the base containers; delete those files to uninstall. On: rebuild the COMPLETE game containers from the original game folder (needs the game installed at its create-time path).",
    "type": "boolean",
    "default": false
  },
  "unrealSwapFonts": {
    "title": "Replace game fonts",
    "description": "Swap the game's font files with SLR's two faces: dialogue/Japanese text gets SLRCondensedCJK (narrow Latin glyphs so translated English fits its boxes, CJK coverage so untranslated Japanese keeps rendering), menus/UI text gets Stint Ultra Condensed (narrow Latin — pure UI text never needs Japanese). The game re-rasterizes its text from the new fonts, so this works with no frozen atlases. Use the two font fields below to supply your own fonts instead.",
    "type": "boolean",
    "default": false
  },
  "unrealFontDialogue": {
    "title": "Custom dialogue font (optional)",
    "description": "Absolute path to your own .ttf/.otf/.ttc for DIALOGUE/JAPANESE text: the game-owned font faces (and the engine's CJK fallback) are swapped for it. Empty = SLR's SLRCondensedCJK — chosen here because untranslated Japanese must keep rendering (a Latin-only face would leave tofu). A proportional font works but dialogue may overflow its boxes; a condensed face fits best.",
    "type": "string",
    "default": ""
  },
  "unrealFontUi": {
    "title": "Custom UI font (optional)",
    "description": "Absolute path to your own .ttf/.otf/.ttc for MENU/UI text: the engine's default faces (Roboto family, mono) are swapped for it. Empty = SLR's Stint Ultra Condensed — a narrower Latin-only face is used here (instead of the dialogue font for everything) because pure UI text never needs Japanese glyphs and this face fits translated English best. Leave both empty to use both SLR faces; put the same font in both fields to force one font everywhere.",
    "type": "string",
    "default": ""
  },
};
const SLRUNREAL_EXPORT_FORM = [
  { "type": "msg", "msg": "<h2>Unreal Export Options</h2>" },
  { "key": "unrealFullRebuild" },
  { "key": "unrealSwapFonts" },
  { "key": "unrealFontDialogue" },
  { "key": "unrealFontUi" },
];

// Font-file picker (SLRUnity unitySelectFontFile pattern): a detached NW.js
// file input resolves to the absolute path via its input event.
function unrealSelectFontFile() {
  return new Promise((resolve) => {
    const $elm = $jq("<input type=\"file\" accept=\".ttf,.otf,.ttc\" />");
    $elm.one("input", function () { resolve(this.value || ""); });
    $elm.trigger("click");
  });
}

try {
  engines.getEngine("unreal").addProperty("exportOptionFields", () => ({
    jsonForm: {
      schema: JSON.parse(JSON.stringify(SLRUNREAL_EXPORT_SCHEMA)),
      form: SLRUNREAL_EXPORT_FORM.map((f) => Object.assign({}, f)),
    },
  }));
} catch (e) { console.warn("unreal exportOptionFields failed:", e); }

try {
  engines.addHandler("unreal", "onOpenExportDialogReady", function ($dialogExport) {
    try {
      if (!$dialogExport || typeof $dialogExport.find !== "function" ||
          typeof $dialogExport.prepend !== "function") return;
      const $fields = $dialogExport.find("form.customFields");
      if ($fields && $fields.length) $dialogExport.prepend($fields);
      // Custom fonts: prefill the persisted paths and attach a Browse…
      // button to each font input (jsonform renders them as plain text
      // inputs). Best-effort enhancement — never a dialog blocker.
      if (!$fields || !$fields.length || typeof $jq !== "function") return;
      const cfg = (k) => {
        try { return String(thisAddon.getConfig(k) || ""); } catch (e) { return ""; }
      };
      const fontFields = [
        ["unrealFontDialogue", cfg("fontDialogue"), "empty = SLR's Japanese-capable face (SLRCondensedCJK)"],
        ["unrealFontUi", cfg("fontUi"), "empty = SLR's narrow Latin face (Stint Ultra Condensed)"],
      ];
      for (const [name, saved, placeholder] of fontFields) {
        const $inp = $fields.find('[name="' + name + '"]');
        if (!$inp || !$inp.length) continue;
        if (saved && !$inp.val()) $inp.val(saved);
        if (typeof $inp.attr === "function") $inp.attr("placeholder", placeholder);
        const $btn = $jq('<button type="button" class="slrunrealFontBrowse">Browse…</button>');
        $btn.on("click", async function () {
          const picked = await unrealSelectFontFile();
          if (picked) $inp.val(picked);
        });
        ($inp.parent && $inp.parent().length ? $inp.parent() : $inp).append($btn);
      }
    } catch (e) { console.warn("unreal export options hook failed:", e); }
  });
} catch (e) { console.warn("unreal onOpenExportDialogReady registration failed:", e); }

// --- createProject -------------------------------------------------------
// Called by Trans.createProjectFromEngine with {gameFolder, selectedFile,
// gameEngine, gameTitle, projectId, options}; must resolve the project
// object (buildProject shape). A throw is reported by SLR as engine failure.
engines.addHandler("unreal", "createProject", async (args) => {
  let gameDir;
  try {
    gameDir = resolveGameRoot(fsh, args.selectedFile || args.gameFolder, args.gameFolder);
  } catch (e) {
    alert(e && e.message ? e.message : String(e));
    throw e;
  }
  const cache = await common.initStaging(args.projectId, args.gameTitle || "Unreal game");
  const jobsDir = path.join(cache.cachePath, "jobs");
  const stageDir = path.join(cache.cachePath, "data", "unreal-stage");
  const extractPath = path.join(cache.cachePath, "data", "extract.json");
  uiLine("Unreal scan", "Scanning containers + capturing the portable staging set (key discovery runs automatically if needed)…");
  let stage;
  let ended = false;
  const endLoading = () => { if (!ended) { ended = true; ui.loadingEnd(); } };
  try {
    // Phase 8a: ONE pass — scan + extract table + the staging tree the
    // patch export builds from (the project becomes portable: Save
    // Project packs the staging cache, so everything export needs rides
    // inside the .slrproj like SLRUnity's staged game files).
    stage = await runBackend({ version: 2, command: "stage", game_dir: gameDir,
                               stage_dir: stageDir, out: extractPath },
      { jobsDir, basePath: addonDir, python: RUNTIME, onProgress: showProgress });
  } catch (e) {
    endLoading();
    alert("Unreal scan backend failed: " + (e.message || e));
    throw e;
  } finally {
    endLoading();
  }
  if (!stage.ok) {
    alert("Unreal scan failed: " + (stage.error || "unknown") + "\nLog: " + path.join(jobsDir, "backend.log"));
    throw new Error(stage.error || "scan failed");
  }
  const table = JSON.parse(fs.readFileSync(extractPath, "utf-8"));
  const st = stage.stage || {};
  uiLine("Unreal stage", "Staging set: " + (st.files || "?") + " files (" +
    (((st.bytes || 0) / 1048576).toFixed(1)) + " MiB) — portable export ready.");
  // Phase 7 collect-all-visible: read SLR's "Japanese source" general
  // option at creation so rows without Japanese get SLR's gray no-Japanese
  // tag (SLRUnity parity, same config key). Off -> every row untagged.
  const japaneseSource = (typeof sys !== "undefined" && sys && sys.config)
    ? sys.config.japanesesource == true : false;
  const project = buildProject(table, {
    gameDir, gameTitle: args.gameTitle, cache, japaneseSource, stageDir,
  });
  uiLine("Unreal extract", "Project created — " + (table.text_table || []).length + " rows.");
  return project;
});

// --- exportHandler -------------------------------------------------------
// targetPath + options.mode "dir"|"zip" (SLR's export dialog); returns true
// when SLR should treat the export as done. Overlay discipline: the overlay
// ends BEFORE every alert; endLoading is idempotent and runs on every path.
engines.addHandler("unreal", "exportHandler", async function (targetPath, options) {
  if (!options || (options.mode !== "dir" && options.mode !== "zip")) return false;

  const proj = trans.project;
  const meta = proj && proj.options && proj.options.slrunreal;
  if (!meta || !meta.gameDir) {
    alert("Unreal project metadata missing — re-create the project.");
    return true;
  }
  ui.showLoading();
  uiLine("Unreal export", "Preparing translated containers…");
  let overlayClosed = false;
  const endLoading = () => { if (!overlayClosed) { overlayClosed = true; ui.loadingEnd(); } };
  try {
    const jobsDir = path.join(proj.cache.cachePath, "jobs");

    // Rebuild the translated extract table from the project rows.
    const extractPath = path.join(proj.cache.cachePath, "data", "extract.json");
    const table = JSON.parse(fs.readFileSync(extractPath, "utf-8"));
    const filled = applyTranslations(table, proj.files || {}, meta.rowMap || {});
    const custom = options.custom || {};
    const swapFonts = custom.unrealSwapFonts === true;
    // A fonts-only export is valid (SLRUnity parity: tick the box, get the
    // font-bearing containers) — the no-rows guard applies only when the
    // font swap is off too.
    if (!filled && !swapFonts) {
      endLoading();
      alert("No translated rows to export — translate some lines first.");
      return true;
    }
    // SLR's cachePath starts RELATIVE to the app cwd (e2e-fix8) — resolve
    // every path we write or hand to the manifest here too (backend.js
    // PATH_KEYS absolutizes as well: belt and braces).
    const translationsPath = path.resolve(path.join(jobsDir, "translations.json"));
    fs.writeFileSync(translationsPath, JSON.stringify(table), "utf-8");

    // Phase 8a export modes:
    //   patch (default) — portable, built purely from the staging tree
    //     the project carries inside the .slrproj; EF writes a mount-
    //     priority _SLR_P.pak. ST (IoStore) needs the Phase 8c writer —
    //     until then it silently falls back to the full rebuild.
    //   full — the pre-Phase-8a behavior: rebuild whole containers from
    //     the ORIGINAL game folder (user opt-in via the export dialog).
    const stageDirOk = !!(meta.stageDir && fs.existsSync(meta.stageDir) &&
      fs.statSync(meta.stageDir).isDirectory() &&
      fs.existsSync(path.join(meta.stageDir, "stage.json")));
    const wantFull = custom.unrealFullRebuild === true;
    let patchMode = stageDirOk && !wantFull;

    // Fingerprint warning (patch mode + a live game folder): a game that
    // changed since create still exports (the patch targets the STAGED
    // bytes), but the user should know the base game moved on.
    if (patchMode && meta.gameDir && fs.existsSync(meta.gameDir)) {
      try {
        const fp = await runBackend({ version: 2, "command": "stage_fingerprint",
          "stage_dir": meta.stageDir, "game_dir": meta.gameDir },
          { jobsDir, basePath: addonDir, python: RUNTIME });
        if (fp.ok && (fp.changed || []).length) {
          uiLine("Unreal export", "WARNING: the game folder changed since the project was created (" +
            fp.changed.slice(0, 3).join(", ") + (fp.changed.length > 3 ? ", …" : "") +
            ") — the patch is built from the captured staging set.");
        }
      } catch (e) { /* best-effort warning only */ }
    }

    const outDir = path.resolve(path.join(jobsDir, "export-" + Date.now()));
    let job;
    if (patchMode) {
      job = { version: 2, command: "export_patch", stage_dir: meta.stageDir,
              translations: translationsPath, out: outDir };
      uiLine("Unreal export", "Patch export (portable — built from the project's staging set).");
    } else {
      if (wantFull || stageDirOk) {
        uiLine("Unreal export", stageDirOk
          ? "Full container rebuild from the original game folder…"
          : "Full container rebuild (no staging set found — re-create the project for portable exports).");
      }
      job = { version: 2, command: "export", game_dir: meta.gameDir,
              translations: translationsPath, out: outDir };
      // Demosaic no longer rides the translation export (Phase 8b): it
      // is a standalone operation with its own patch container, run from
      // the project-creation dialog's "Demosaic (standalone)" section.
    }
    // Font swap (Phase 5b): SLR's two faces unless custom paths are set;
    // custom paths persist to the addon config (SLRUnity fontCjk/fontLatin
    // pattern). A missing file logs a WARNING inside unrealResolveFonts and
    // skips only the font stage.
    if (swapFonts) {
      const customDlg = String(custom.unrealFontDialogue || "").trim();
      const customUi = String(custom.unrealFontUi || "").trim();
      try {
        if (customDlg && thisAddon.getConfig("fontDialogue") !== customDlg)
          thisAddon.setConfig("fontDialogue", customDlg);
        if (customUi && thisAddon.getConfig("fontUi") !== customUi)
          thisAddon.setConfig("fontUi", customUi);
      } catch (e) { console.warn("unreal: font config persist failed:", e); }
      const fonts = unrealResolveFonts(customDlg, customUi);
      if (fonts) job.fonts = fonts;
    }
    let res;
    try {
      res = await runBackend(job, { jobsDir, basePath: addonDir, python: RUNTIME,
        onProgress: (p) => uiLine("Unreal export", p.done + "/" + p.total + "  " + p.file) });
    } catch (e) {
      endLoading();
      alert("Unreal export backend failed: " + (e.message || e));
      return true;
    }
    if (!res.ok) {
      endLoading();
      alert("Unreal export failed: " + (res.error || "unknown") + "\nLog: " + path.join(jobsDir, "backend.log"));
      return true;
    }
    uiLine("Unreal export", "Rows patched: " + res.stats.rows_patched +
      "  skipped: " + res.stats.rows_skipped + "  verify: " + res.stats.verify);
    if (res.fonts_swapped) {
      uiLine("Unreal export", "Fonts swapped: " + res.fonts_swapped + " face(s)");
    }

    // Collect the rebuilt containers game-relative from the output dir.
    const files = [];
    (function walk(dir, rel) {
      for (const name of fs.readdirSync(dir)) {
        const full = path.join(dir, name);
        const relName = rel ? rel + "/" + name : name;
        if (fs.statSync(full).isDirectory()) walk(full, relName);
        else files.push([full, relName]);
      }
    })(outDir, "");
    if (options.mode === "dir") {
      for (const [full, relName] of files) {
        const dst = path.join(targetPath, ...relName.split("/"));
        fs.mkdirSync(path.dirname(dst), { recursive: true });
        fs.copyFileSync(full, dst);
        uiLine("Unreal export", "Wrote " + relName);
      }
    } else if (typeof common.aSpawn === "function" && typeof common.get7zipBin === "function") {
      // Zip the CONTENTS of the output dir so containers land at the archive
      // root (SLRUnity Task 5 finding: common.pack wraps the folder itself;
      // spawn 7z with cwd = outDir and a bare "*" — rmmv.js precedent).
      uiLine("Unreal export", "Packing archive…");
      let zipCode = -1;
      await common.aSpawn(common.get7zipBin(), ["a", "-tzip", targetPath, "*"], {
        cwd: outDir,
        onDone: function (result, code) { zipCode = code; },
        onError: function (data) { uiLine("Unreal export", String(data || "")); },
      });
      if (zipCode !== 0) {
        endLoading();
        alert("Failed to create the zip archive (7z exited with code " + zipCode + ").");
        return true;
      }
    } else {
      endLoading();
      alert("Zip mode needs SLR's 7z helper (common.aSpawn) — export as folder instead.");
      return true;
    }
    uiLine("Unreal export", patchMode
      ? "Export complete — a PATCH container. Copy " +
        res.containers.map((c) => path.basename(c)).join(" + ") +
        " into the game's Content\\Paks folder (beside the base containers). Delete those files to uninstall."
      : "Export complete — apply the rebuilt containers to a COPY of the game.");
    // SLR's Save Project (.slrproj) packs the ENTIRE staging cache and
    // jobs/ is NOT excluded (trans-full.js:1649-1652) — a leftover EF
    // export tree would pack ~3 GB of rebuilt containers into the next
    // project save. Remove it once the dir/zip copy consumed it (the
    // SLRUnity export removes its staging dir for the same reason).
    try {
      if (outDir && outDir.startsWith(path.resolve(jobsDir)) && fs.statSync(outDir).isDirectory()) {
        fs.rmSync(outDir, { recursive: true, force: true });
      }
    } catch (eCleanup) {
      console.warn("unreal export: could not remove the export output dir (left in place):", outDir, eCleanup && eCleanup.message ? eCleanup.message : eCleanup);
    }
  } catch (e) {
    endLoading();
    alert("Unreal export error: " + (e.message || e));
    return true;
  }
  endLoading();
  return true;
});
