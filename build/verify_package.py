"""Structural verifier for dist/SLRUnreal-<os>.* release packages.

Mirrors SLR Unity Translator's build/verify_package.py (v2.7): a release is
a zip with EXACTLY ONE top-level folder (SLRUnreal/ — the AddonLoader scans
subfolders of www/addons for package.json), the required payload inside, and
nothing from the dev tree. SLRUnreal-specific rules: the embedded runtime
payload is PURE STDLIB (no pip), keys.json is a user-side runtime cache that
must NOT ship (fresh installs discover their own keys), and no dev fixture
or IDA-derived artifact may leak.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zipfile

ADDON_FOLDER = "SLRUnreal"

REQUIRED_ENTRIES = [
    "package.json",
    "UnrealAddon.js",
    "docs.html",
    "assets/icon.png",
    "assets/help.html",
    "lib/backend.js",
    "lib/paths.js",
    "lib/detector.js",
    "lib/projectBuilder.js",
    "backend/slr_cli.py",
    "backend/ut/__init__.py",
    "backend/ut/iostore.py",
    "backend/ut/pak.py",
    "backend/ut/payload.py",
    "backend/ut/textscan.py",
    "backend/ut/scenegraph.py",
    "backend/ut/zen.py",
    "backend/ut/uasset.py",
    "backend/ut/patchtext.py",
    "backend/ut/exporttext.py",
    "backend/ut/demosaic.py",
    "backend/ut/keyscan.py",
    "backend/ut/aes.py",
    "backend/ut/blake3.py",
    "backend/ut/oodle.py",
    "backend/ut/manifest.py",
    "backend/ut/protection.py",
    "backend/ut/rewrite.py",
    "backend/ut/inspect.py",
]

RUNTIME_MARKER = {"win": "runtime/python/python.exe", "linux": "runtime/bin/python3"}

FORBIDDEN_ANCHORED = ["tests/", "backend/tests/"]
FORBIDDEN_ANYWHERE = ["__pycache__/", ".pytest_cache/", "node_modules/",
                      ".sdd/", "_snapshots/", "docs/specs/", "docs/plans/",
                      "docs/ida/", "docs/discovery/"]
# Dev-side fixtures and caches that must NEVER ride along (spec Appendix B):
FORBIDDEN_FILE_SUFFIXES = [
    "index_facts.json", "demosaic_discovery.json", "text_discovery.json",
    "demosaic_leads.json",
]
FORBIDDEN_FILES = ["keys.json", "build_boot_tests.py", "build_boot_variants.py"]
FORBIDDEN_SUBSTRINGS = ["_export_for_ai"]


def rules_errors(names: "list[str]", read_file, os_name: str, has_runtime: bool) -> "list[str]":
    errors: "list[str]" = []
    prefix = ADDON_FOLDER + "/"

    # Rule 1: exactly one top-level folder, no root files.
    tops = sorted({n.split("/", 1)[0].rstrip("/") for n in names if n.strip("/")})
    if tops != [ADDON_FOLDER]:
        errors.append(f"top-level entries must be exactly [{ADDON_FOLDER}/], found: {tops}")
    root_files = [n for n in names if "/" not in n.rstrip("/")]
    if root_files:
        errors.append(f"entries at the zip ROOT are forbidden: {root_files}")

    # Rule 2: required entries.
    required = list(REQUIRED_ENTRIES)
    if has_runtime:
        required.append(RUNTIME_MARKER[os_name])
    missing = [prefix + e for e in required if prefix + e not in names]
    if missing:
        errors.append(f"missing required entries: {missing}")

    # Rule 3: runtime payload correctness (stdlib runtime: pure payload).
    runtime_entries = [n for n in names if n.startswith(prefix + "runtime/")]
    if has_runtime and not runtime_entries:
        errors.append("runtime claimed but no runtime/ entries found")
    if has_runtime and os_name == "win" and (
            any("linux" in n for n in runtime_entries)
            or any(n.startswith(prefix + "runtime/bin/") for n in runtime_entries)):
        errors.append("win package must not carry a linux runtime payload")
    if has_runtime and os_name == "linux" and any(
            n.startswith(prefix + "runtime/python/") for n in runtime_entries):
        errors.append("linux package must not carry the win runtime (runtime/python/)")
    # Oodle must NOT ride any package: Epic's UE EULA permits redistributing
    # oo2core only inside UE products. The backend discovers the library at
    # scan time from the user's game folder / local UE install / env
    # (backend/ut/oodle.py); packages carrying it are rejected outright.
    for n in names:
        low = n.lower()
        if "oo2core" in low or "liboo2core" in low:
            errors.append(f"forbidden entry: {n} (Oodle Core is never bundled - "
                          "Epic EULA; discovered at scan time instead)")

    # Rule 4: no dev leakage.
    for n in names:
        for pat in FORBIDDEN_ANCHORED:
            if n == prefix + pat or n.startswith(prefix + pat):
                errors.append(f"forbidden entry: {n} (matches {pat})")
        for pat in FORBIDDEN_ANYWHERE:
            if pat in n:
                errors.append(f"forbidden entry: {n} (matches {pat})")
        base = os.path.basename(n)
        for suf in FORBIDDEN_FILE_SUFFIXES:
            if base.endswith(suf):
                errors.append(f"forbidden dev fixture: {n}")
        if base in FORBIDDEN_FILES:
            errors.append(f"forbidden file: {n} (runtime cache / dev script)")
        for sub in FORBIDDEN_SUBSTRINGS:
            if sub in n:
                errors.append(f"forbidden entry: {n} (contains {sub})")

    # Rule 5: package.json name matches the folder; load files exist.
    try:
        pkg = json.loads(read_file(prefix + "package.json").decode("utf-8"))
        if pkg.get("name") != ADDON_FOLDER:
            errors.append(f"package.json name {pkg.get('name')!r} != {ADDON_FOLDER!r}")
        for load_file in pkg.get("load", []):
            if prefix + load_file not in names:
                errors.append(f"package.json load file not present: {load_file}")
    except (KeyError, ValueError) as e:
        errors.append(f"cannot read/parse {prefix}package.json: {e}")

    return errors


def verify_zip(package: str, os_name: str, has_runtime: bool = True) -> bool:
    print(f"[verify] {package} (os={os_name}, runtime={'yes' if has_runtime else 'no'})", flush=True)
    if not os.path.isfile(package):
        print("ERROR: package not found", file=sys.stderr)
        return False
    with zipfile.ZipFile(package) as zf:
        names = zf.namelist()
        bad = zf.testzip()
        if bad is not None:
            print(f"ERROR: corrupt entry: {bad}", file=sys.stderr)
            return False
        errors = rules_errors(names, zf.read, os_name, has_runtime)
    if errors:
        for e in errors:
            print("ERROR: " + e, file=sys.stderr)
        return False
    print(f"[verify] OK: {os.path.getsize(package):,} bytes, {len(names)} entries, "
          f"single top-level folder '{ADDON_FOLDER}/'", flush=True)
    return True


def find_sevenzip(extra_candidates=None) -> "str | None":
    import shutil
    if os.environ.get("SLRUNREAL_7ZA"):
        cand = os.environ["SLRUNREAL_7ZA"]
        if os.path.isfile(cand):
            return cand
    # candidates aim at the TARGET os; a win 7za.exe is useless on a Linux
    # build host — prefer a host-executable binary from PATH first
    candidates = [shutil.which("7z"), shutil.which("7za")]
    candidates += [c for c in (extra_candidates or [])
                   if not (c and c.lower().endswith(".exe") and os.name != "nt")]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def make_7z(package_root_stem: str, stage_root: str, sevenzip: str) -> bool:
    r = subprocess.run([sevenzip, "a", "-t7z", "-mx=9", package_root_stem + ".7z",
                        ADDON_FOLDER], capture_output=True, text=True, cwd=stage_root)
    return r.returncode == 0
