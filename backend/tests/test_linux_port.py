# -*- coding: utf-8 -*-
"""Phase 9 (Linux support) tests: Oodle library resolution, fetch pins,
and the Linux build rules. The functional Linux gate (running the
interpreter, dlopen the .so) needs a Linux host — these pin everything
verifiable offline plus the release-layout contract."""
import os
import stat
import sys
import zipfile

import pytest

from backend.ut import oodle

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def test_platform_map_covers_both():
    assert "win32" in oodle._PLATFORM_LIBS
    assert "linux" in oodle._PLATFORM_LIBS
    win_dir, win_name = oodle._PLATFORM_LIBS["win32"]
    linux_dir, linux_name = oodle._PLATFORM_LIBS["linux"]
    assert win_name.endswith(".dll") and win_dir == "win"
    assert linux_name.endswith(".so.9") and linux_dir == "linux"


def test_find_dll_this_platform():
    # the current host's payload must resolve (win on this host; linux
    # on a Linux host — the same assertion either way)
    path = oodle.find_dll()
    assert os.path.isfile(path)
    if sys.platform == "win32":
        assert path.endswith("oo2core_9_win64.dll")
    else:
        assert path.endswith("liboo2corelinux64.so.9")


def test_find_dll_unmapped_platform(monkeypatch):
    monkeypatch.setattr(oodle.sys, "platform", "sunos")
    with pytest.raises(FileNotFoundError, match="no Oodle library mapped"):
        oodle.find_dll()


def test_candidates_cover_dev_and_release_layouts():
    # the dev tree (runtime/ 3 levels up from backend/ut) and the
    # packaged addon (4 levels up) both resolve
    cands = oodle._candidates()
    assert len(cands) == 2
    assert any(n.endswith(oodle._PLATFORM_LIBS[sys.platform][1]) for n in cands)


def test_oodle_roundtrip_this_platform():
    dll = oodle.get_dll()
    data = os.urandom(5000) + b"japanese text" * 50
    comp = oodle.compress(dll, data)
    assert oodle.decompress(dll, comp, len(data)) == data


# --------------------------------------------------------------------------
# fetch pins (build scripts)

def test_fetch_oodle_pins():
    sys.path.insert(0, os.path.join(ROOT, "build"))
    import fetch_oodle
    assert set(fetch_oodle.SOURCES) == {"win", "linux"}
    for t, spec in fetch_oodle.SOURCES.items():
        assert spec["url"].startswith("https://github.com/WorkingRobot/OodleUE/")
        assert len(spec["sha256"]) == 64
        assert "2.9.10" in spec["url"]
    # both payloads are fetched on this dev tree
    for t, spec in fetch_oodle.SOURCES.items():
        dest = os.path.join(ROOT, spec["dest"])
        assert os.path.isfile(dest), f"{t} oodle payload missing: {dest}"


def test_linux_oodle_is_elf_x64():
    path = os.path.join(ROOT, "runtime", "oodle", "linux",
                        "liboo2corelinux64.so.9")
    if not os.path.isfile(path):
        pytest.skip("linux oodle payload not fetched")
    with open(path, "rb") as f:
        head = f.read(20)
    assert head[:4] == b"\x7fELF"           # ELF
    assert head[4] == 2 and head[5] == 1     # 64-bit, little-endian
    # the three ctypes symbols ride the dynsym (string-level check)
    with open(path, "rb") as f:
        blob = f.read()
    for sym in (b"OodleLZ_Decompress", b"OodleLZ_Compress",
                b"OodleLZ_GetCompressedBufferSizeNeeded"):
        assert sym + b"\x00" in blob, sym


def test_fetch_runtime_linux_structural():
    sys.path.insert(0, os.path.join(ROOT, "build"))
    import fetch_runtime
    py = os.path.join(ROOT, "runtime", "linux", "bin", "python3")
    if not os.path.isfile(py):
        pytest.skip("linux runtime payload not fetched")
    assert fetch_runtime._verify_linux_structural(py)


# --------------------------------------------------------------------------
# release rules (build/verify_package)

def test_verify_rules_reject_linux_package_without_so(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "build"))
    import verify_package as vp
    # names with the linux runtime but NO oodle .so -> must error
    names = [vp.ADDON_FOLDER + "/" + n for n in [
        "package.json", "UnrealAddon.js", "docs.html",
        "assets/icon.png", "assets/help.html",
        "lib/backend.js", "lib/paths.js", "lib/detector.js",
        "lib/projectBuilder.js",
        "backend/slr_cli.py", "backend/ut/__init__.py",
        "backend/ut/pak.py", "backend/ut/oodle.py",
        "runtime/bin/python3",
    ]]
    read = {n: b"" for n in names}

    def rf(n):
        return read[n]

    errs = vp.rules_errors(names, rf, "linux", has_runtime=True)
    assert any("liboo2corelinux64" in e for e in errs)
    # win package: must not carry the linux runtime
    errs2 = vp.rules_errors(names, rf, "win", has_runtime=True)
    assert any("linux" in e for e in errs2)
