"""Fetch the pinned Oodle SDK decompression libraries for LOCAL DEVELOPMENT
only (populates runtime/oodle/, which backend/ut/oodle.py finds as a search
candidate). Never committed, never staged into release packages: Epic's UE
EULA does not permit redistributing these binaries outside UE products, so
the shipped addon discovers the library at scan time from the user's game
folder or local UE install instead (see backend/ut/oodle.py).

Two platform payloads, both from the WorkingRobot/OodleUE mirror of the
distributable SDK 2.9.10:
  win:   .../win/redist/oo2core_9_win64.dll
  linux: .../linux/lib/liboo2corelinux64.so.9   (ELF x86-64)
"""
import argparse
import hashlib
import os
import sys
import urllib.request

SOURCES = {
    "win": {
        "url": ("https://github.com/WorkingRobot/OodleUE/raw/refs/heads/main/"
                "Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/2.9.10/"
                "win/redist/oo2core_9_win64.dll"),
        "sha256": "6f5d41a7892ea6b2db420f2458dad2f84a63901c9a93ce9497337b16c195f457",
        "dest": os.path.join("runtime", "oodle", "win", "oo2core_9_win64.dll"),
    },
    "linux": {
        "url": ("https://github.com/WorkingRobot/OodleUE/raw/refs/heads/main/"
                "Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/2.9.10/"
                "linux/lib/liboo2corelinux64.so.9"),
        "sha256": "ed7e98f70be1254a80644efd3ae442ff61f854a2fe9debb0b978b95289884e9c",
        "dest": os.path.join("runtime", "oodle", "linux",
                             "liboo2corelinux64.so.9"),
    },
}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fetch(target: str) -> int:
    spec = SOURCES[target]
    dest = os.path.join(ROOT, spec["dest"])
    if os.path.isfile(dest):
        with open(dest, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() == spec["sha256"]:
                print(f"[oodle/{target}] already present: {dest}")
                return 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"[oodle/{target}] downloading {spec['url']}")
    with urllib.request.urlopen(spec["url"]) as r, open(dest + ".part", "wb") as f:
        f.write(r.read())
    with open(dest + ".part", "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != spec["sha256"]:
        os.remove(dest + ".part")
        print(f"ERROR: sha256 mismatch {digest}", file=sys.stderr)
        return 1
    os.replace(dest + ".part", dest)
    # the linux .so must be executable to dlopen
    if target == "linux":
        os.chmod(dest, 0o755)
    print(f"[oodle/{target}] ok: {dest}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--os", dest="target", choices=["win", "linux", "all"],
                    default="all")
    args = ap.parse_args()
    targets = ["win", "linux"] if args.target == "all" else [args.target]
    for t in targets:
        if fetch(t) != 0:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
