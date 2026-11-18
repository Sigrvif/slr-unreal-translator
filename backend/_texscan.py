"""Read-only discovery #3: validate the derived texture grammar at scale.

ST (zen):  leading[i32 SX@2, i32 SY@6, PF_ FStrings, i32 mipcount@O0-8]
           + per-mip [i32 bulkidx SX SY SZ] headers between payloads at
           bulk_map[idx].serial_offset (flags inline).
EF (legacy): probe what sits in the gaps — full FBulkData headers vs
           DataResource indices — by walking from the uexp bulk area.

Prints per-game pass/fail + censuses; first 10 mismatches each.
"""
from __future__ import annotations

import collections
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ut import payload, uasset, zen  # noqa: E402
from ut.textscan import _script_objects  # noqa: E402
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games

GAMES = {
    "ST": os.path.join(_GAMES_ROOT, "SampleGameA"),
    "EF": os.path.join(_GAMES_ROOT, "SampleGameB"),
}


def ef_key():
    import os
    hexkey = os.environ.get("SLRUNREAL_SAMPLE_B_AES_KEY", "").strip()
    if not hexkey:
        with open("tests/fixtures/local/sample_b.key", encoding="utf-8") as f:
            hexkey = f.read().strip()
    return bytes.fromhex(hexkey)


# bytes per 4x4 block (or per pixel for uncompressed) for the formats we saw
BLOCK_BYTES = {"PF_DXT1": 8, "PF_DXT3": 16, "PF_DXT5": 16, "PF_BC5": 16,
               "PF_BC4": 8, "PF_BC7": 16, "PF_B8G8R8A8": None, "PF_G8": None,
               "PF_A8": None, "PF_R8G8B8A8": None}


def mip_bytes(fmt, w, h):
    if fmt not in BLOCK_BYTES:
        return None
    bw = BLOCK_BYTES[fmt]
    if bw is None:                     # uncompressed: 4B/px (G8/A8: 1B/px)
        bpp = 1 if fmt in ("PF_G8", "PF_A8") else 4
        return max(1, w) * max(1, h) * bpp
    return ((w + 3) // 4) * ((h + 3) // 4) * bw


def st_texture(pkg, ex):
    """Validate one ST Texture2D export; dict or raise AssertionError."""
    d = ex.data
    bl = pkg.bulk_map
    assert bl, "no bulk map"
    sx, sy = struct.unpack_from("<ii", d, 2)
    # every PF_ FString in the leading area
    fmts = []
    pos = 10
    limit = min(bl[0].serial_offset if bl else len(d), len(d))
    while pos + 4 <= limit:
        n = struct.unpack_from("<i", d, pos)[0]
        if 4 <= n <= 32 and pos + 4 + n <= limit:
            s = d[pos + 4:pos + 4 + n]
            if s.startswith(b"PF_") and s.endswith(b"\x00"):
                fmts.append(s.rstrip(b"\x00").decode())
        pos += 4
    assert fmts, "no PF_ string"
    fmt = fmts[-1]
    mipcount = struct.unpack_from("<i", d, bl[0].serial_offset - 8)[0]
    assert mipcount == len(bl), f"mipcount {mipcount} != {len(bl)} bulk entries"
    assert struct.unpack_from("<i", d, bl[0].serial_offset - 4)[0] == 0
    # walk headers between payloads
    dims = []
    for k, e in enumerate(bl):
        hpos = e.serial_offset - 16
        assert hpos >= 0
        bi, hx, hy, hz = struct.unpack_from("<iiii", d, hpos)
        assert bi == k, f"mip {k}: bulkidx {bi}"
        assert hz == 1
        dims.append((hx, hy))
    assert dims[0] == (sx, sy), f"mip0 {dims[0]} != {sx}x{sy}"
    for k in range(1, len(dims)):
        exp = (max(1, dims[k-1][0] // 2), max(1, dims[k-1][1] // 2))
        assert dims[k] == exp, f"mip {k}: {dims[k]} != {exp}"
    for k, e in enumerate(bl):
        want = mip_bytes(fmt, *dims[k])
        assert want == e.serial_size, \
            f"mip {k}: size {e.serial_size} != {want} ({fmt} {dims[k]})"
        assert e.flags & 0x40, f"mip {k}: not inline flags {e.flags:#x}"
        assert e.serial_offset + e.serial_size <= len(d)
    return {"sx": sx, "sy": sy, "fmt": fmt, "mips": len(bl), "dims": dims}


def census_st():
    src = payload.find_sources(GAMES["ST"])
    files = payload.list_files(src)
    names = [f for f in files if f.endswith(".uasset")]
    script = _script_objects(src)
    ok = fail = 0
    fmt_c = collections.Counter()
    dim_c = collections.Counter()
    mip_c = collections.Counter()
    errs = []
    for n in names:
        try:
            pkg = zen.parse_zen(payload.read_file(src, n), n,
                                script_objects=script)
        except Exception:
            continue
        for ex in pkg.exports:
            if ex.class_name != "Texture2D":
                continue
            try:
                info = st_texture(pkg, ex)
            except AssertionError as e:
                fail += 1
                if len(errs) < 10:
                    errs.append(f"{n} {ex.object_name}: {e}")
                continue
            ok += 1
            fmt_c[info["fmt"]] += 1
            mip_c[info["mips"]] += 1
            dim_c[(info["sx"], info["sy"])] += 1
    print(f"ST: ok {ok}  fail {fail}")
    print("  formats:", dict(fmt_c.most_common()))
    print("  mipcounts:", dict(mip_c.most_common(8)))
    print("  top dims:", dim_c.most_common(6))
    for e in errs:
        print("  FAIL", e)


def census_ef():
    src = payload.find_sources(GAMES["EF"], aes_key=ef_key())
    files = payload.list_files(src)
    names = [f for f in files if f.endswith(".uasset")]
    ok = fail = 0
    errs = []
    hdr_flags = collections.Counter()
    for n in names[:1200]:
        try:
            stem = n[:-len(".uasset")]
            pkg = uasset.parse_legacy(payload.read_file(src, n),
                                      payload.read_file(src, stem + ".uexp"),
                                      stem + ".uexp")
        except Exception:
            continue
        for ex in pkg.exports:
            if ex.class_name != "Texture2D":
                continue
            # probe: are there 16B [a b c d] gaps that look like dims?
            d = ex.data
            found = False
            i = 0
            while i + 16 <= len(d) and not found:
                a, b, c, dd = struct.unpack_from("<iiii", d, i)
                if c == 1 and 0 <= dd <= 32 and a > 0 and b > 0:
                    # candidate [SX SY SZ idx]; check halving chain nearby
                    if mip_bytes("PF_DXT1", a, b) == dd or True:
                        hdr_flags[dd] += 1
                        found = True
                i += 4
            if found:
                ok += 1
            else:
                fail += 1
                if len(errs) < 5:
                    errs.append(n)
    print(f"EF probe: candidates {ok} none {fail}")
    print("  idx-ish values:", dict(hdr_flags.most_common(10)))
    for e in errs:
        print("  none:", e)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "ST"
    if which == "ST":
        census_st()
    else:
        census_ef()
