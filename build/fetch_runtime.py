"""Fetch the runtime payload for the SLRUnreal release packages.

win: CPython 3.10.11 embeddable (amd64) -> runtime/win/python/
linux: python-build-standalone CPython 3.10.13 install-only tar
       (x86_64-unknown-linux-gnu) -> runtime/linux/ (tar root python/
       hoisted one level so runtime/linux/bin/python3 exists — the
       layout lib/backend.js resolveRuntime expects, SLRUnity 2.7
       pattern; the pinned release is 20240107, same as SLRUnity's).
The SLRUnreal backend is pure stdlib (Phase 1-3 constraint: zero third-party
code), so unlike SLRUnity's runtime there is NO pip stage — unpack, verify,
done. Idempotent: a target whose verification passes is skipped. The ._pth
from the embeddable zip is kept as-is (isolated mode);
backend/slr_cli.py inserts its own directory on sys.path.

Linux verification on a Linux host runs the interpreter; on a Windows
host the ELF cannot execute, so verification is STRUCTURAL only (the
bin/python3 entry exists and the stdlib zip is present) — same class
of cross-build fallback SLRUnity uses (its cross-install stage is pip-
specific; ours has no pip stage at all).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(ROOT, "runtime")
WIN_PY_ZIP = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip"
LINUX_PY_TAR = ("https://github.com/indygreg/python-build-standalone/releases/"
                "download/20240107/"
                "cpython-3.10.13+20240107-x86_64-unknown-linux-gnu-install_only.tar.gz")
LINUX_PY_TAR_MIRROR = ("https://github.com/astral-sh/python-build-standalone/"
                       "releases/download/20240107/"
                       "cpython-3.10.13+20240107-x86_64-unknown-linux-gnu-install_only.tar.gz")


def _verify_win(python_exe: str) -> bool:
    try:
        r = subprocess.run([python_exe, "-c", "import json,struct,ctypes,urllib.request; print('runtime ok')"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[verify] could not run {python_exe}: {e}", file=sys.stderr, flush=True)
        return False
    if r.returncode != 0:
        print(f"[verify] FAILED for {python_exe} (exit {r.returncode})\n{r.stdout}\n{r.stderr}",
              file=sys.stderr, flush=True)
        return False
    print(f"[verify] {python_exe}: runtime ok", flush=True)
    return True


def fetch_win() -> int:
    py_dir = os.path.join(RUNTIME_DIR, "win", "python")
    exe = os.path.join(py_dir, "python.exe")
    if os.path.isfile(exe) and _verify_win(exe):
        print("[fetch] win runtime already present and verified", flush=True)
        return 0
    os.makedirs(py_dir, exist_ok=True)
    print(f"[fetch] downloading {WIN_PY_ZIP}", flush=True)
    zip_path = os.path.join(RUNTIME_DIR, "win", "python-embed.zip")
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with urllib.request.urlopen(WIN_PY_ZIP) as r, open(zip_path, "wb") as f:
        f.write(r.read())
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(py_dir)
    os.remove(zip_path)
    if not _verify_win(exe):
        print("ERROR: win runtime verification failed after fetch", file=sys.stderr, flush=True)
        return 1
    return 0


def _verify_linux_structural(py: str) -> bool:
    """Cross-host verification: the interpreter entry exists, is a real
    ELF, and the stdlib is unpacked beside it. (Running it is impossible
    on a Windows host; the first Linux run is the functional gate.)"""
    linux_root = os.path.dirname(os.path.dirname(py))
    stdlib = os.path.join(linux_root, "lib", "python3.10")
    checks = [
        os.path.isfile(py),
        os.path.isfile(os.path.join(os.path.dirname(py), "python3.10")),
        # install-only layout: real .py stdlib files (json/struct are the
        # modules slr_cli's own verification imports)
        os.path.isfile(os.path.join(stdlib, "json", "__init__.py")),
        os.path.isfile(os.path.join(stdlib, "struct.py")),
    ]
    if all(checks):
        print(f"[verify] {py}: structural check ok (cross-host)", flush=True)
        return True
    print(f"[verify] FAILED structural check for {py}: {checks}",
          file=sys.stderr, flush=True)
    return False


def _verify_linux(py: str) -> bool:
    try:
        r = subprocess.run([py, "-c",
                            "import json,struct,ctypes,urllib.request; print('runtime ok')"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        # Windows host: the ELF interpreter cannot run — structural only
        return _verify_linux_structural(py)
    if r.returncode != 0:
        print(f"[verify] FAILED for {py} (exit {r.returncode})\n{r.stdout}\n{r.stderr}",
              file=sys.stderr, flush=True)
        return False
    print(f"[verify] {py}: runtime ok", flush=True)
    return True


def fetch_linux() -> int:
    linux_root = os.path.join(RUNTIME_DIR, "linux")
    py = os.path.join(linux_root, "bin", "python3")
    if os.path.isfile(py) and _verify_linux(py):
        print("[fetch] linux runtime already present and verified", flush=True)
        return 0
    if os.path.isdir(linux_root):
        shutil.rmtree(linux_root)
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=RUNTIME_DIR)   # same volume -> cheap rename
    try:
        tgz = os.path.join(tmp, "cpython.tar.gz")
        for url in (LINUX_PY_TAR, LINUX_PY_TAR_MIRROR):
            print(f"[fetch] downloading {url}", flush=True)
            try:
                with urllib.request.urlopen(url) as r, open(tgz, "wb") as f:
                    f.write(r.read())
                break
            except Exception as e:              # noqa: BLE001 - try mirror
                print(f"[fetch] {url} failed: {e}", file=sys.stderr, flush=True)
        else:
            print("ERROR: both linux runtime URLs failed", file=sys.stderr)
            return 1
        with tarfile.open(tgz, "r:gz") as tf:
            tf.extractall(tmp)                # tar root is python/
        if not os.path.isdir(os.path.join(tmp, "python")):
            print("ERROR: linux tar did not contain a python/ root",
                  file=sys.stderr, flush=True)
            return 1
        # Hoist python/* one level so runtime/linux/bin/python3 exists
        os.rename(os.path.join(tmp, "python"), linux_root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # python-build-standalone ships bin/python3.10 with a python3 symlink
    if not os.path.isfile(py):
        print(f"ERROR: {py} missing after extraction", file=sys.stderr, flush=True)
        return 1
    if not _verify_linux(py):
        print("ERROR: linux runtime verification failed after fetch",
              file=sys.stderr, flush=True)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--os", dest="target", choices=["win", "linux", "all"],
                    default="all",
                    help="which runtime payload to fetch")
    args = ap.parse_args()
    targets = ["win", "linux"] if args.target == "all" else [args.target]
    for t in targets:
        if t == "win":
            if fetch_win() != 0:
                return 1
        elif fetch_linux() != 0:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
