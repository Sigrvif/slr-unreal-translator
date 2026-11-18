const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs");
const { runBackend, parseProgressLines, resolveRuntime } = require("../lib/backend");
const { siblingDir, toForwardSlashes } = require("../lib/paths");

test("parseProgressLines buffers partial lines and ignores noise", () => {
  const st = { buf: "" };
  const out1 = parseProgressLines('{"type":"progress","done":1,"t', st);
  assert.deepEqual(out1, []);
  const out2 = parseProgressLines('otal":74,"file":"level0"}\nnot json\n', st);
  assert.deepEqual(out2, [{ type: "progress", done: 1, total: 74, file: "level0" }]);
});

test("parseProgressLines handles multiple lines in one chunk", () => {
  const st = { buf: "" };
  const out = parseProgressLines('{"type":"progress","done":1,"total":2,"file":"a"}\n{"type":"progress","done":2,"total":2,"file":"b"}\n', st);
  assert.equal(out.length, 2);
});

test("paths helpers", () => {
  assert.equal(siblingDir("C:/Games/SomeGame", "_unreal_img"), "C:/Games/SomeGame_unreal_img");
  assert.equal(toForwardSlashes("a\\b\\c"), "a/b/c");
});

test("PATH_KEYS absolutizes out and aes_key_fixture (SLRUnreal additions)", async () => {
  const jobsDir = fs.mkdtempSync(path.join(os.tmpdir(), "slrunreal-"));
  const seen = {};
  const fakeResult = { ok: true, version: 2 };
  const spawnFn = (cmd, args, opts) => {
    const manifest = args[args.indexOf("--manifest") + 1];
    seen.job = JSON.parse(fs.readFileSync(manifest, "utf-8"));
    seen.cwd = opts.cwd;
    fs.writeFileSync(args[args.indexOf("--result") + 1], JSON.stringify(fakeResult));
    return { stdout: { on() {} }, stderr: { on() {} }, on(ev, cb) { if (ev === "close") setImmediate(() => cb(0)); } };
  };
  await runBackend(
    { version: 2, command: "export", game_dir: "rel/game", translations: "rel/t.json", out: "rel/out", aes_key_fixture: "rel/k.json" },
    { jobsDir, spawnFn, python: { cmd: "py", baseArgs: ["cli"] } });
  assert.equal(path.isAbsolute(seen.job.out), true);
  assert.equal(path.isAbsolute(seen.job.aes_key_fixture), true);
  assert.equal(path.isAbsolute(seen.job.game_dir), true);
  assert.equal(seen.job.command, "export");            // non-path keys verbatim
  assert.equal(seen.job.version, 2);
});

test("runBackend resolves ok:false envelopes without rejecting (stubbed spawn)", async () => {
  const jobsDir = fs.mkdtempSync(path.join(os.tmpdir(), "slrunreal-"));
  const fakeResult = { ok: false, version: 2, error: "boom", traceback: "tb" };
  const spawnFn = (cmd, args) => {
    fs.writeFileSync(args[args.indexOf("--result") + 1], JSON.stringify(fakeResult));
    return { stdout: { on() {} }, stderr: { on() {} }, on(ev, cb) { if (ev === "close") setImmediate(() => cb(0)); } };
  };
  const res = await runBackend({ version: 2, command: "scan", game_dir: "x" }, { jobsDir, spawnFn, python: { cmd: "py", baseArgs: ["cli"] } });
  assert.equal(res.ok, false);
  assert.equal(res.error, "boom");
});

test("runBackend rejects when the child writes no result (stubbed spawn)", async () => {
  const jobsDir = fs.mkdtempSync(path.join(os.tmpdir(), "slrunreal-"));
  const spawnFn = () => ({ stdout: { on() {} }, stderr: { on() {} }, on(ev, cb) { if (ev === "close") setImmediate(() => cb(1)); } });
  await assert.rejects(() => runBackend({ version: 2, command: "scan", game_dir: "x" }, { jobsDir, spawnFn, python: { cmd: "py", baseArgs: ["cli"] } }),
    /without writing a result file/);
});

test("resolveRuntime finds the win payload layout", () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "slrunreal-rt-"));
  fs.mkdirSync(path.join(base, "runtime", "python"), { recursive: true });
  fs.mkdirSync(path.join(base, "backend"), { recursive: true });
  const exe = path.join(base, "runtime", "python", "python.exe");
  fs.writeFileSync(exe, "");
  fs.writeFileSync(path.join(base, "backend", "slr_cli.py"), "");
  const rt = resolveRuntime(base);
  assert.equal(rt.cmd, exe);
  assert.equal(rt.baseArgs.length, 1);
  assert.equal(rt.baseArgs[0].endsWith("slr_cli.py"), true);
});

test("resolveRuntime returns null without the payload", () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "slrunreal-rt0-"));
  assert.equal(resolveRuntime(base), null);
});
