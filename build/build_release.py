"""Assemble dist/SLRUnreal-<version>-<os>.zip (+ .7z twin) release packages.

Mirrors SLR Unity Translator's build/build_release.py (v2.7). Stage layout
(binding): EVERYTHING inside one top-level SLRUnreal/ folder.

    SLRUnreal/
      package.json, UnrealAddon.js, docs.html, assets/, lib/
      backend/slr_cli.py, backend/ut/*.py          (NO tests, NO caches,
                                                    NO keys.json, NO fixtures)
      runtime/python/python.exe                    (win payload)

Pipeline per OS: stage under dist/stage-<os>/SLRUnreal/ -> zip the STAGE
ROOT -> verify (build/verify_package.py rules) -> (win) smoke scan through
the STAGED runtime + backend against the example game (ok:true required).

Archive names carry the addon version (addon/package.json), e.g.
SLRUnreal-1.0.0-win.zip, so releases are trackable by filename alone.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_package as vp  # noqa: E402

ADDON_DIR = os.path.join(ROOT, "addon")
BACKEND_DIR = os.path.join(ROOT, "backend")
DIST_DIR = os.path.join(ROOT, "dist")
EXAMPLE_GAME = os.environ.get("SLRUNREAL_EXAMPLE_GAME", "")  # local dev convenience; smoke skips when unset
ADDON_FOLDER = vp.ADDON_FOLDER

PRUNE_DIRS = {"__pycache__", ".pytest_cache"}
ADDON_ROOT_PRUNE = {"tests"}


def addon_version() -> str:
    with open(os.path.join(ADDON_DIR, "package.json"), encoding="utf-8") as f:
        return json.load(f)["version"]


def stage_release(stage: str) -> None:
    def ignore(dirpath: str, names: list) -> set:
        drop = {n for n in names if n in PRUNE_DIRS}
        if os.path.abspath(dirpath) == os.path.abspath(ADDON_DIR):
            drop |= ADDON_ROOT_PRUNE
        return drop

    shutil.copytree(ADDON_DIR, stage, dirs_exist_ok=True, ignore=ignore)
    # backend: the module tree only (slr_cli + ut) — no tests, no caches,
    # no keys.json (user-side runtime cache), no dev fixtures.
    dst = os.path.join(stage, "backend")
    os.makedirs(dst, exist_ok=True)
    shutil.copy2(os.path.join(BACKEND_DIR, "slr_cli.py"), os.path.join(dst, "slr_cli.py"))
    shutil.copytree(os.path.join(BACKEND_DIR, "ut"), os.path.join(dst, "ut"),
                    ignore=lambda d, names: {n for n in names if n in PRUNE_DIRS})


# NOTE: the Oodle Core library is deliberately NOT staged into packages.
# Epic's UE EULA only permits redistributing it inside UE products, so the
# packaged addon DISCOVERS it at scan time instead (backend/ut/oodle.py:
# game folder -> local UE install -> SLRUNREAL_OODLE_DLL). The runtime/
# oodle/ layout populated by build/fetch_oodle.py stays a local-dev
# convenience and is skipped by the ignore rule below.
def stage_runtime(stage: str, os_name: str) -> bool:
    src = os.path.join(ROOT, "runtime", os_name)
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(stage, "runtime"),
                        ignore=lambda d, names: {n for n in names
                                                 if n in PRUNE_DIRS or n == "oodle"})
    else:
        print(f"ERROR: runtime/{os_name} missing (run build/fetch_runtime.py first)",
              file=sys.stderr, flush=True)
        return False
    return True


def zip_stage(stage_root: str, package: str) -> None:
    if os.path.exists(package):
        os.remove(package)
    print(f"[zip] {stage_root}{os.sep}{ADDON_FOLDER}{os.sep} -> {package}", flush=True)
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, dirs, files in os.walk(stage_root):
            dirs[:] = sorted(d for d in dirs if d not in PRUNE_DIRS)
            for f in sorted(files):
                full = os.path.join(root, f)
                zf.write(full, os.path.relpath(full, stage_root).replace(os.sep, "/"))


def smoke_scan(stage: str, os_name: str = "win") -> bool:
    """Run the STAGED runtime + backend scan against the example game; the
    envelope must be ok:true with a non-empty census."""
    exe = os.path.join(stage, "runtime", "python", "python.exe")         if os_name == "win" else os.path.join(stage, "runtime", "bin", "python3")
    cli = os.path.join(stage, "backend", "slr_cli.py")
    if not os.path.isfile(exe):
        print(f"[smoke] no staged runtime ({exe}) - skipped", flush=True)
        return True
    if os_name == "linux":
        # the linux interpreter cannot run on a Windows build host — the
        # functional gate is the first run on a Linux machine (SLRUnity
        # build_release.py:180-181 precedent)
        print("[smoke] skipped: the linux interpreter cannot run on a "
              "Windows host (functional gate = first Linux run)", flush=True)
        return True
    if not os.path.isdir(EXAMPLE_GAME):
        print(f"WARNING: example game not found - smoke skipped: {EXAMPLE_GAME}",
              file=sys.stderr, flush=True)
        return True
    with tempfile.TemporaryDirectory(prefix="slrunreal-smoke-") as tmp:
        manifest = os.path.join(tmp, "scan.json")
        result = os.path.join(tmp, "scan.result.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "command": "scan", "game_dir": EXAMPLE_GAME}, f)
        r = subprocess.run([exe, cli, "--manifest", manifest, "--result", result],
                           capture_output=True, text=True, cwd=tmp, timeout=1800,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode != 0:
            print(f"ERROR: smoke scan exited {r.returncode}\n{r.stderr[-2000:]}",
                  file=sys.stderr, flush=True)
            return False
        with open(result, encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("ok") is not True:
            print("ERROR: smoke scan ok:false - " + str(payload.get("error")),
                  file=sys.stderr, flush=True)
            return False
        stats = payload.get("stats") or {}
        print(f"[smoke] scan ok:true - text_entries={stats.get('text_entries')} "
              f"files_scanned={stats.get('files_scanned')}", flush=True)
        if (stats.get("text_entries") or 0) < 900:
            print(f"ERROR: smoke scan census too low: {stats.get('text_entries')} "
                  f"entries (the example game carries 992 FText frames; a low "
                  f"count means the staged runtime is broken - Oodle DLL, "
                  f"paths, encoding)", file=sys.stderr, flush=True)
            return False
    return True


def build_release(os_name: str) -> bool:
    stage_root = os.path.join(DIST_DIR, f"stage-{os_name}")
    stage = os.path.join(stage_root, ADDON_FOLDER)
    shutil.rmtree(stage_root, ignore_errors=True)
    os.makedirs(stage)
    stage_release(stage)
    # License paperwork rides every package: GPLv3 + third-party notices
    # (CPython runtime, Oodle Core, CityHash/DirectXTex-derived codecs).
    for fname in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        shutil.copy2(os.path.join(ROOT, fname), os.path.join(stage, fname))
    if not stage_runtime(stage, os_name):
        return False
    package = os.path.join(DIST_DIR, f"SLRUnreal-{addon_version()}-{os_name}.zip")
    zip_stage(stage_root, package)
    if not vp.verify_zip(package, os_name, has_runtime=True):
        return False
    if not smoke_scan(stage, os_name):
        return False
    sevenzip = vp.find_sevenzip(extra_candidates=[
        os.path.join(os.path.dirname(ROOT), "SLR Translator",
                     "node_modules", "7zip-bin", "win", "x64", "7za.exe")])
    if sevenzip:
        r = subprocess.run([sevenzip, "a", "-t7z", "-mx=9",
                            package[:-4] + ".7z", ADDON_FOLDER],
                           capture_output=True, text=True, cwd=stage_root)
        if r.returncode != 0:
            print("WARNING: 7z twin failed - zip-only release", file=sys.stderr, flush=True)
        else:
            print(f"[7z] {package[:-4]}.7z", flush=True)
    else:
        print("WARNING: no 7z CLI found - zip-only release", file=sys.stderr, flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--os", dest="target", choices=["win", "linux", "all"],
                    default="all")
    args = ap.parse_args()
    os.makedirs(DIST_DIR, exist_ok=True)
    targets = ["win", "linux"] if args.target == "all" else [args.target]
    for t in targets:
        if not build_release(t):
            print(f"FAILED ({t})", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
