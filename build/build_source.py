"""Build the minimal SLRUnreal source archive.

Mirrors the SLRUnity convention: a readable, buildable source tree WITHOUT
fetched payloads, build outputs, game-derived fixtures, working ledgers or
snapshots. The archive name carries the addon version
(dist/SLRUnreal-<version>-source.zip) so releases are trackable by filename.

  README.md              project overview, layout, build, test, constraints
  pytest.ini             so `python -m pytest backend/tests` runs from the root
  addon/                 the JS addon, complete but payload-free (the runtime
                         payloads are fetched at build time, not shipped here)
  backend/               Python backend: slr_cli.py, ut/, tests/ — test CODE
                         only (tests/fixtures holds game-derived extracts;
                         keys.json holds this machine's discovered pak keys)
  build/                 the build scripts themselves (incl. this one)
  docs/E2E-CHECKLIST.md  the master release-validation checklist

Everything else (runtime/, dist/, _snapshots/, .sdd/, the rest of docs/)
stays out on purpose.
"""

import argparse
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(ROOT, "dist")

# Top-level trees that never enter the archive.
SKIP_TOP = {"runtime", "dist", "_snapshots", ".sdd", ".pytest_cache"}
# docs/ contributes only the master checklist; its subfolders (discovery,
# ida, plans, specs) are development material.
DOCS_KEEP = {"E2E-CHECKLIST.md"}
SKIP_FILES = {"keys.json", "backend.log"}


def collect():
    files = []
    for dp, dns, fns in os.walk(ROOT):
        rel_dp = os.path.relpath(dp, ROOT)
        parts = rel_dp.split(os.sep)
        if "__pycache__" in parts:
            continue
        if rel_dp == ".":
            dns[:] = [d for d in dns if d not in SKIP_TOP]
        elif parts[0] == "docs":
            if len(parts) == 1:                     # docs/ itself: filter files
                fns[:] = [f for f in fns if f in DOCS_KEEP]
            dns[:] = []                             # no docs subfolders
        elif parts[0] == "backend" and rel_dp == os.path.join("backend", "tests"):
            dns[:] = [d for d in dns if d != "fixtures"]
        for fn in fns:
            if fn in SKIP_FILES or fn.endswith((".pyc", ".log")):
                continue
            files.append(os.path.join(dp, fn))
    return sorted(files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args = ap.parse_args()
    os.makedirs(DIST_DIR, exist_ok=True)
    with open(os.path.join(ROOT, "addon", "package.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    out_zip = os.path.join(DIST_DIR, f"SLRUnreal-{version}-source.zip")
    files = collect()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, os.path.relpath(p, ROOT))
    size = os.path.getsize(out_zip)
    print(f"[source] {out_zip}")
    print(f"[source] OK: {size:,} bytes, {len(files)} entries")

    # verify: no payload/log/key/ledger leakage, version present
    with zipfile.ZipFile(out_zip) as z:
        names = z.namelist()
        leaked = [n for n in names
                  if "keys.json" in n or n.endswith((".log", ".pyc"))
                  or n.split("/")[0] in SKIP_TOP
                  or n.startswith(("docs/discovery", "docs/ida",
                                   "docs/plans", "docs/specs",
                                   "backend/tests/fixtures"))]
        if leaked:
            print("FAILED — leaked entries:", leaked, file=sys.stderr)
            return 1
        pkg = json.loads(z.read("addon/package.json").decode("utf-8"))
        print(f"[source] addon version: {pkg['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
