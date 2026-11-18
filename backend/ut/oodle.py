"""ctypes binding for the user-side Oodle SDK libraries.

The Oodle Core library (oo2core_9_win64.dll / liboo2corelinux64.so.9) is
proprietary Epic Games code: the UE EULA only permits redistributing it
inside Unreal Engine products, so this project never bundles it. Instead
the library is DISCOVERED at runtime, in this order:

  1. explicit argument / SLRUNREAL_OODLE_DLL env var
  2. the game being processed (most UE games ship oo2core next to their
     binaries; version-matched to the very containers being read)
  3. the dev/release ``runtime/oodle/`` layout (populated by
     build/fetch_oodle.py for LOCAL DEVELOPMENT only - never staged into
     packages by build/build_release.py)
  4. a local Unreal Engine install (Engine/Binaries/ThirdParty/Oodle/
     <ver>/<plat>/redist/), available to anyone with the free Epic Games
     Launcher
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading

_LOCK = threading.Lock()
_DLL = None

_DEV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "runtime", "oodle")
# layout inside the packaged addon: backend/ut/oodle.py sits one level
# deeper than the dev tree, so runtime/oodle may be either 3 or 4 levels up
_RELEASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "runtime", "oodle")

_PLATFORM_LIBS = {
    "win32": ("win", "oo2core_9_win64.dll"),
    "linux": ("linux", "liboo2corelinux64.so.9"),
}


_SEARCH_ROOTS: "list[str]" = []      # game roots, set by the entry points

_OODLE_FILENAMES = ("oo2core_9_win64.dll", "liboo2corelinux64.so.9",
                    "oo2core_8_win64.dll", "liboo2corelinux64.so.8",
                    "oo2core_7_win64.dll", "liboo2corelinux64.so.7")


def add_search_root(path: "str | None") -> None:
    """Register a game root to scan for the library (idempotent)."""
    if path and os.path.isdir(path) and path not in _SEARCH_ROOTS:
        _SEARCH_ROOTS.append(path)


def _candidates() -> "list[str]":
    entry = _PLATFORM_LIBS.get(sys.platform)
    if entry is None:
        raise FileNotFoundError(
            f"no Oodle library mapped for platform {sys.platform!r}")
    subdir, name = entry
    out = [os.path.join(d, subdir, name) for d in (_DEV_DIR, _RELEASE_DIR)]
    return out


def _game_dir_candidates() -> "list[str]":
    import glob
    out: "list[str]" = []
    for root in _SEARCH_ROOTS:
        for n in _OODLE_FILENAMES:
            out.append(os.path.join(root, n))                       # beside the exe
        out += glob.glob(os.path.join(root, "*", "Binaries", "*", "oo2core_*"))  # <Name>/Binaries/<Plat>
        out += glob.glob(os.path.join(root, "Binaries", "*", "oo2core_*"))
        out += glob.glob(os.path.join(root, "*", "Binaries", "*", "liboo2core*"))
        out += glob.glob(os.path.join(root, "Binaries", "*", "liboo2core*"))
    return out


def _ue_install_candidates() -> "list[str]":
    import glob
    bases = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.path.join(os.environ.get("SystemDrive", r"C:"), os.sep,
                          "Program Files")]
    out: "list[str]" = []
    for base in bases:
        out += glob.glob(os.path.join(base, "Epic Games", "UE_*", "Engine",
                                      "Binaries", "ThirdParty", "Oodle", "*",
                                      "win", "redist", "oo2core_*_win64.dll"))
    return out


def find_dll(explicit: "str | None" = None) -> str:
    tried = []
    for cand in [explicit, os.environ.get("SLRUNREAL_OODLE_DLL"),
                 *_game_dir_candidates(), *_candidates(),
                 *_ue_install_candidates()]:
        if not cand:
            continue
        tried.append(cand)
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        "Oodle library not found. Epic's UE EULA does not allow bundling "
        "it, so it is discovered at runtime. Provide one of: the env var "
        "SLRUNREAL_OODLE_DLL, the game folder (most UE games ship "
        "oo2core beside their binaries - the game root is searched "
        "automatically), or a local Unreal Engine install. Tried: "
        + "; ".join(tried))


def get_dll() -> ctypes.CDLL:
    global _DLL
    with _LOCK:
        if _DLL is not None:
            return _DLL
        path = find_dll()
        dll = ctypes.CDLL(path)
        # OodleLZ_Decompress 2.9.x signature (15 args); returns decoded byte count
        # or 0 on failure.
        fn = dll.OodleLZ_Decompress
        fn.restype = ctypes.c_longlong
        fn.argtypes = [ctypes.c_char_p, ctypes.c_longlong, ctypes.c_char_p,
                       ctypes.c_longlong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p,
                       ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_ulonglong, ctypes.c_int]
        # OodleLZ_Compress 2.9.x signature (13 args, incl. the lrm/lrmBytes
        # slots); returns compressed byte count, 0 on failure, -1 on invalid
        # arguments. Verified against this DLL: exact deterministic roundtrips.
        fn = dll.OodleLZ_Compress
        fn.restype = ctypes.c_longlong
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_longlong,
                       ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p,
                       ctypes.c_ulonglong, ctypes.c_void_p, ctypes.c_ulonglong,
                       ctypes.c_void_p]
        # Max size a valid compressor run may need for the output buffer
        # (2.9 takes the compressor id AND the raw size).
        sz = dll.OodleLZ_GetCompressedBufferSizeNeeded
        sz.restype = ctypes.c_ulonglong
        sz.argtypes = [ctypes.c_int, ctypes.c_longlong]
        _DLL = dll
        return _DLL


def decompress(dll: ctypes.CDLL, src: bytes, dst_len: int) -> bytes:
    dst = ctypes.create_string_buffer(dst_len)
    n = dll.OodleLZ_Decompress(src, len(src), dst, dst_len, 1, 0, 0,
                               None, 0, None, 0, None, None, 0, 0)
    if n != dst_len:
        raise ValueError(f"oodle decompress returned {n}, expected {dst_len}")
    return dst.raw[:dst_len]


def compress(dll: ctypes.CDLL, data: bytes, compressor: int = 8,
             level: int = 4) -> bytes:
    """Compress ``data`` with Oodle; defaults are Kraken (8) / Normal (4).

    The output buffer is sized via OodleLZ_GetCompressedBufferSizeNeeded (the
    only guaranteed-safe size for arbitrary inputs). Returns the raw Oodle
    stream; raises ValueError when the codec rejects the call (return <= 0).
    Empty input compresses to the empty stream (the DLL returns 0 for it).
    """
    if not data:
        return b""
    need = dll.OodleLZ_GetCompressedBufferSizeNeeded(compressor, len(data))
    comp = ctypes.create_string_buffer(need)
    n = dll.OodleLZ_Compress(compressor, data, len(data), comp, level,
                             None, None, 0, None, 0, None, 0, None)
    if n <= 0:
        raise ValueError(f"oodle compress returned {n} (compressor={compressor}, "
                         f"level={level}, {len(data)} bytes)")
    return comp.raw[:n]
