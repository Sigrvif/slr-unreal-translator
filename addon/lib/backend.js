// Backend runner: executes slr_cli.py jobs via manifest/result JSON files
// (Contract v2 — SLRUnreal port of SLRUnity's lib/backend.js. The wire
// contract was byte-verified against SLRUnity's runner in Phase 3 Task 7:
// same argv, same envelope, same error strings. Differences from the
// SLRUnity original are exactly two: PATH_KEYS gains "out" and
// "aes_key_fixture" (our export/discovery commands carry them — the
// SLRUnity list predates them), and the runtime-not-found message says
// "Unreal". Everything else is the verbatim transport.)
//
// Contract recap driving this module:
//   <runtime-python> slr_cli.py --manifest job.json --result result.json
//   - All paths travel inside job.json (non-ASCII-safe; never argv).
//   - Exit code is always 0; outcomes live in result.json.
//   - stdout is EXACTLY zero or more progress lines, each one JSON object:
//     {"type":"progress","done":<int>,"total":<int>,"file":"<rel>"}.
//   - Result envelope: {"ok":true,"version":2,...payload} or
//     {"ok":false,"version":2,"error":"...","traceback":"..."}.
//
// Pure-lib rule: no NW.js / SLR globals here. The spawn function is injected
// (opts.spawnFn) or defaults to require("child_process").spawn, which works
// both in plain Node tests and inside the NW page (full Node access).
"use strict";

const fs = require("fs");
const path = require("path");

// {cmd, baseArgs:[slrCliPath]} for this OS's bundled runtime, or null when
// the payload is absent (runtime not fetched / wrong-OS payload).
function resolveRuntime(basePath) {
  basePath = path.resolve(basePath);   // tolerate relative basePaths (caller cwd)
  const cli = path.join(basePath, "backend", "slr_cli.py");
  if (process.platform === "win32") {
    const exe = path.join(basePath, "runtime", "python", "python.exe");
    return fs.existsSync(exe) ? { cmd: exe, baseArgs: [cli] } : null;
  }
  const exe = path.join(basePath, "runtime", "bin", "python3");
  return fs.existsSync(exe) ? { cmd: exe, baseArgs: [cli] } : null;
}

// Line-buffered progress parsing: split `chunk` on newlines across calls,
// JSON.parse each complete line, keep only progress objects, ignore noise.
// `state` is a caller-owned {buf: ""} that carries the partial tail.
function parseProgressLines(chunk, state) {
  state.buf += chunk.toString("utf-8");
  const out = [];
  let idx;
  while ((idx = state.buf.indexOf("\n")) >= 0) {
    const line = state.buf.slice(0, idx).trim();
    state.buf = state.buf.slice(idx + 1);
    if (!line) continue;
    try {
      const o = JSON.parse(line);
      if (o && o.type === "progress") out.push(o);
    } catch (e) {
      // non-JSON noise on stdout — backend logs its own stderr into backend.log
    }
  }
  return out;
}

let _jobCounter = 0;

// Manifest keys whose values are filesystem paths (union over our commands:
// scan/extract carry game_dir (+ aes_key_fixture on EF-with-fixture jobs);
// export carries game_dir, translations, out).
const PATH_KEYS = new Set([
  "game_dir", "out_dir", "translations_path", "index_path",
  // SLRUnreal: our export command's output dir key is "out" (not SLRUnity's
  // "out_dir"), its translations table rides "translations" (NOT SLRUnity's
  // "translations_path" — the e2e-fix8 class of bug: SLR's cache paths start
  // RELATIVE to the app cwd and the child resolves them against the jobs
  // dir), and the dev-side key fixture path rides "aes_key_fixture".
  "out", "translations", "aes_key_fixture",
]);

// Every path-valued manifest entry is made absolute before the manifest is
// written (SLR's cache paths start out relative to the app cwd). Nested
// objects/arrays are walked so the resolution holds wherever a path key may
// appear; non-string and empty values stay verbatim.
function resolveManifestPaths(value, key) {
  if (Array.isArray(value)) return value.map((v) => resolveManifestPaths(v));
  if (value && typeof value === "object")
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolveManifestPaths(v, k)]));
  if (key !== undefined && PATH_KEYS.has(key) && typeof value === "string" && value)
    return path.resolve(value);
  return value;
}

// runBackend(job, opts) -> Promise<result envelope>
//   job  = {version:2, command, ...} (contract manifest body)
//   opts = {jobsDir, python?: {cmd, baseArgs}, spawnFn?, onProgress?, basePath?}
// Backend failures (ok:false) RESOLVE with the full envelope; only harness
// errors (missing runtime payload, spawn failure, no result file) reject.
async function runBackend(job, opts) {
  const spawnFn = opts.spawnFn || require("child_process").spawn;
  const python = opts.python || resolveRuntime(opts.basePath || ".");
  if (!python) throw new Error("Unreal backend runtime not found (payload missing for this OS)");
  const jobsDir = path.resolve(opts.jobsDir);
  const logPath = path.join(jobsDir, "backend.log");
  fs.mkdirSync(jobsDir, { recursive: true });
  const id = `job-${Date.now()}-${++_jobCounter}`;
  const manifest = path.join(jobsDir, id + ".json");
  const result = path.join(jobsDir, id + ".result.json");
  fs.writeFileSync(manifest, JSON.stringify(resolveManifestPaths(job)), "utf-8");
  return await new Promise((resolve, reject) => {
    const child = spawnFn(python.cmd, [...python.baseArgs, "--manifest", manifest, "--result", result],
      { windowsHide: true, cwd: jobsDir });
    const state = { buf: "" };
    let errTail = "";   // last 2 KB of stderr, for diagnosing a child that
                        // died before slr_cli could write its own backend.log
    child.stdout.on("data", (d) => {
      for (const p of parseProgressLines(d, state)) opts.onProgress && opts.onProgress(p);
    });
    child.stderr.on("data", (d) => { errTail = (errTail + d.toString("utf-8")).slice(-2048); });
    child.on("error", reject);
    child.on("close", (code) => {
      try {
        const payload = JSON.parse(fs.readFileSync(result, "utf-8"));
        resolve(payload);
      } catch (e) {
        try {   // never leave the log pointer a dead end
          fs.appendFileSync(logPath,
            `\n[runner] backend exited (code ${code}) without writing ${result}\n[runner] stderr tail:\n${errTail}\n`);
        } catch (e2) { /* best effort */ }
        reject(new Error(
          `backend exited (code ${code}) without writing a result file; see ${logPath}` +
          (errTail ? `\nstderr: ${errTail.split("\n").slice(-3).join(" | ").slice(0, 400)}` : "")));
      }
    });
  });
}

module.exports = { runBackend, parseProgressLines, resolveRuntime };
