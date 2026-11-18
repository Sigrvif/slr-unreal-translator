const test = require("node:test");
const assert = require("node:assert");
const { detectUnreal, isDirectUnreal, resolveGameRoot, isShippingExe } = require("../lib/detector");

// Minimal in-memory fs double matching the injected helper contract.
function memfs(tree) {
  const dirs = new Set();
  const files = new Set();
  const add = (p, isDir) => {
    p = String(p).replace(/\\/g, "/");
    if (isDir) dirs.add(p);
    else files.add(p);
  };
  (function walk(node, base) {
    for (const [name, val] of Object.entries(node)) {
      const p = base ? `${base}/${name}` : name;
      if (val && typeof val === "object") { dirs.add(p); walk(val, p); }
      else add(p, false);
    }
  })(tree, "");
  return {
    isFile: (p) => files.has(String(p).replace(/\\/g, "/")),
    isDir: (p) => dirs.has(String(p).replace(/\\/g, "/")),
    listDir: (p) => {
      p = String(p).replace(/\\/g, "/").replace(/\/$/, "");
      if (!dirs.has(p)) throw new Error("ENOENT: " + p);   // faithful: no "" dir
      const pre = p === "" ? "" : p + "/";
      const kids = new Set();
      for (const d of dirs) if (d.startsWith(pre) && d !== p) kids.add(d.slice(pre.length).split("/")[0]);
      for (const f of files) if (f.startsWith(pre) && f !== p) kids.add(f.slice(pre.length).split("/")[0]);
      return [...kids];
    },
    join: (...parts) => parts.filter((x) => x !== "" && x != null).join("/"),
    dirname: (p) => String(p).replace(/\\/g, "/").split("/").slice(0, -1).join("/"),
  };
}

const ST_GAME = {
  "SampleGameA": {
    "sample_a.exe": "",
    "Engine": { "Config": {} },
    "sample_a": {
      "Binaries": { "Win64": { "sample_a-Win64-Shipping.exe": "" } },
      "Content": { "Paks": { "sample_a-Windows.pak": "", "sample_a-Windows.utoc": "", "sample_a-Windows.ucas": "" } },
    },
  },
};

test("isShippingExe matches both suffixes only", () => {
  assert.equal(isShippingExe("sample_a-Win64-Shipping.exe"), true);
  assert.equal(isShippingExe("Game-Shipping.exe"), true);
  assert.equal(isShippingExe("sample_a.exe"), false);
  assert.equal(isShippingExe("readme.txt"), false);
});

test("isDirectUnreal accepts Content/Paks layout and Binaries exe", () => {
  const fsh = memfs(ST_GAME);
  assert.equal(isDirectUnreal(fsh, "SampleGameA"), true);
});

test("detectUnreal resolves from the folder, the exe, and the parent", () => {
  const fsh = memfs(ST_GAME);
  assert.equal(detectUnreal(fsh, "SampleGameA"), "unreal");
  assert.equal(detectUnreal(fsh, "SampleGameA/sample_a/Binaries/Win64/sample_a-Win64-Shipping.exe"), "unreal");
  assert.equal(detectUnreal(fsh, ""), false);            // empty root
});

test("resolveGameRoot: picking the exe resolves the game root", () => {
  const fsh = memfs(ST_GAME);
  const root = resolveGameRoot(fsh,
    "SampleGameA/sample_a/Binaries/Win64/sample_a-Win64-Shipping.exe",
    "SampleGameA/sample_a/Binaries/Win64");
  assert.equal(root, "SampleGameA");
});

test("resolveGameRoot: picking the game folder itself", () => {
  const fsh = memfs(ST_GAME);
  assert.equal(resolveGameRoot(fsh, "SampleGameA", "wrong-parent"), "SampleGameA");
});

test("resolveGameRoot: one game inside the picked parent", () => {
  const fsh = memfs({
    games: { "SampleGameA": JSON.parse(JSON.stringify(ST_GAME))["SampleGameA"] },
  });
  assert.equal(resolveGameRoot(fsh, "games", ""), "games/SampleGameA");
});

test("resolveGameRoot: nonexistent pick throws with guidance", () => {
  const fsh = memfs({ "empty": {} });
  assert.throws(() => resolveGameRoot(fsh, "", ""), /No Unreal game found/);
});

test("resolveGameRoot: nothing found throws with guidance", () => {
  const fsh = memfs({ "empty": {} });
  assert.throws(() => resolveGameRoot(fsh, "empty", ""), /No Unreal game found/);
});

test("resolveGameRoot: ambiguous children throw (never guess)", () => {
  const tree = { parent: JSON.parse(JSON.stringify(ST_GAME)) };
  const fsh = memfs({
    parent: {
      "gameA": { "a": { "Content": { "Paks": { "a.pak": "" } } } },
      "gameB": { "b": { "Content": { "Paks": { "b.pak": "" } } } },
    },
  });
  assert.throws(() => resolveGameRoot(fsh, "parent", ""), /Multiple Unreal games/);
});

test("CEF-style resource paks do NOT make a folder a UE game", () => {
  const fsh = memfs({
    "cef-app": { "Resources": { "x.pak": "" } },
  });
  assert.equal(isDirectUnreal(fsh, "cef-app"), false);
});
