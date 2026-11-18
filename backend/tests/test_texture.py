# -*- coding: utf-8 -*-
"""Phase 5a: Texture2D walk-parser + SEP round-trip tests.

Synthetic tests build export data in the byte layout documented in
ut/texture.py (leading region -> PF_ FString -> [0][M][0] -> payload/entry
chain -> terminator). Real-game tests exercise ST (IoStore) end to end.
"""
import json
import os
from types import SimpleNamespace

import pytest

from backend.ut import bcdec, payload, pngio, texture, uasset, zen
from backend.ut.textscan import _script_objects
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games

GAMES = {
    "sample_a": os.path.join(_GAMES_ROOT, "SampleGameA"),
}


# --------------------------------------------------------------------------
# synthetic exports

def _bulk(offset, size, flags=0x48):
    return SimpleNamespace(serial_offset=offset, serial_size=size,
                           flags=flags, cooked_index=0)


def _fstring(s: bytes) -> bytes:
    return (len(s) + 1).to_bytes(4, "little") + s + b"\x00"


def _build_export(fmt_name: bytes, mips_rgb, srgbjunk=b"\x04\x03" + b"\x11" * 16):
    """Export data + bulk_map for one Texture2D in the walk layout.
    mips_rgb: [(w, h, rgba_bytes)] big-to-small, all inline; each mip's
    payload precedes its [SX SY SZ][nxt] entry."""
    data = bytearray(srgbjunk)
    data += _fstring(fmt_name)
    data += (0).to_bytes(4, "little")
    data += len(mips_rgb).to_bytes(4, "little")
    data += (0).to_bytes(4, "little")
    bulk = []
    for i, (w, h, rgba) in enumerate(mips_rgb):
        enc = bcdec.encode_texture(fmt_name.decode(), w, h, rgba)
        off = len(data)
        data += enc
        bulk.append(_bulk(off, len(enc)))
        nxt = i + 1 if i + 1 < len(mips_rgb) else 0
        data += (w).to_bytes(4, "little")
        data += (h).to_bytes(4, "little")
        data += (1).to_bytes(4, "little")
        data += (nxt).to_bytes(4, "little")
    data += (0).to_bytes(4, "little")
    data += (0).to_bytes(4, "little")
    return bytes(data), bulk


def _flat(w, h, rgba4):
    return bytes(rgba4) * (w * h)


def test_walk_parses_synthetic_dxt1():
    m0 = _flat(64, 64, (100, 150, 200, 255))
    m1 = _flat(32, 32, (100, 150, 200, 255))
    data, bulk = _build_export(b"PF_DXT1",
                               [(64, 64, m0), (32, 32, m1)])
    ex = SimpleNamespace(object_name="T_Test", data=data)
    info = texture.parse_texture(ex, bulk)
    assert (info["w"], info["h"]) == (64, 64)
    assert info["fmt"] == "PF_DXT1"
    assert len(info["mips"]) == 2
    assert info["mips"][0]["storage"] == "inline"
    assert info["mips"][0]["offset"] == bulk[0].serial_offset
    rgba = bcdec.decode_texture("PF_DXT1", 64, 64,
                                data[bulk[0].serial_offset:
                                     bulk[0].serial_offset + bulk[0].serial_size])
    assert all(abs(rgba[i] - (100, 150, 200, 255)[i % 4]) <= 8 for i in range(64))


def test_walk_rejects_garbage_and_vt():
    data, bulk = _build_export(b"PF_DXT1",
                               [(64, 64, _flat(64, 64, (1, 2, 3, 255)))])
    ex = SimpleNamespace(object_name="T", data=data)
    assert texture.parse_texture(ex, bulk)["w"] == 64
    # zero-mip layout (virtual texture) -> honest skip reason
    vt = bytearray(b"\x04\x03" + _fstring(b"PF_DXT1")
                   + (0).to_bytes(4, "little") + (0).to_bytes(4, "little")
                   + (1).to_bytes(4, "little") + (0).to_bytes(4, "little")
                   + (0).to_bytes(4, "little"))
    with pytest.raises(texture.TextureError, match="virtual texture"):
        texture.parse_texture(SimpleNamespace(object_name="T", data=bytes(vt)), bulk)


def test_box_down_odd_edges():
    # 3x2 -> 1x1: every pixel averaged
    px = bytearray()
    for y in range(2):
        for x in range(3):
            px += bytes((x * 10, y * 10, 0, 255))
    out = texture._box_down(bytes(px), 3, 2)
    assert len(out) == 4
    assert out[0] == 5 and out[1] == 5 and out[2] == 0 and out[3] == 255
    # 4x2 -> 2x1: column averages
    px = bytearray()
    for y in range(2):
        for x in range(4):
            px += bytes((x * 10, y * 10, 0, 255))
    out = texture._box_down(bytes(px), 4, 2)
    assert len(out) == 8
    assert out[0] == 5 and out[1] == 5 and out[4] == 25 and out[5] == 5
    assert all(out[i + 3] == 255 for i in (0, 4))


def test_png_name_sanitizes():
    name = texture.png_name("sample_a/Content/画像類/cursorA.uasset",
                            "カーソル", 3)
    assert name.endswith("__3.png")
    assert "/" not in name and "\\" not in name


# --------------------------------------------------------------------------
# real-game (SampleGameA, IoStore)

def _st_src():
    return payload.find_sources(GAMES["sample_a"])


def test_st_texture_census():
    src = _st_src()
    names = [f for f in payload.list_files(src) if f.endswith(".uasset")]
    script = _script_objects(src)
    ok = 0
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
                texture.parse_texture(ex, pkg.bulk_map)
                ok += 1
            except texture.TextureError:
                pass
    assert ok >= 230, ok


def test_st_extract_manifest_and_pngs(tmp_path):
    res = texture.extract_images(GAMES["sample_a"], str(tmp_path),
                                 max_area=256 * 256)
    assert res["count"] >= 10
    man = json.load(open(os.path.join(str(tmp_path), "manifest.json"),
                         encoding="utf-8"))
    assert len(man) == res["count"]
    for row in man:
        p = os.path.join(str(tmp_path), row["png"])
        assert os.path.isfile(p)
        w, h, rgba = pngio.decode_png(open(p, "rb").read())
        assert (w, h) == (row["width"], row["height"])


def test_st_round_trip_container_rebuild(tmp_path):
    """One small texture: extract -> re-import identical pixels -> rebuild
    its container -> re-parse the OUTPUT container -> decoded mip0 equals
    the staged encode. Proves the byte-swap survives the container writer."""
    import tempfile

    from backend.ut import iostore
    src = _st_src()
    pkgname = ("Engine/Content/EditorLandscapeResources/"
               "WhiteSquareTexture.uasset")
    raw = payload.read_file(src, pkgname)
    script = _script_objects(src)
    pkg = zen.parse_zen(raw, pkgname, script_objects=script)
    ex = next(e for e in pkg.exports if e.class_name == "Texture2D")
    info = texture.parse_texture(ex, pkg.bulk_map)
    rgba = texture.read_mip(src, pkgname, ex, info, 0)

    # import path: same pixels back in (idempotent manifest+render dir)
    md = _extract_manifest_dir(tmp_path, pkgname, info)
    staged = texture.import_images(GAMES["sample_a"], md, md)
    assert pkgname in staged["staged"]
    out_dir = tempfile.mkdtemp(dir=str(tmp_path))
    containers = texture.write_containers(GAMES["sample_a"],
                                          staged["staged"], out_dir)
    assert containers
    # re-parse the OUTPUT utoc/ucas and decode the texture again
    utoc_out = next(c for c in containers if c.endswith(".utoc"))
    with open(utoc_out, "rb") as f:
        toc = iostore.parse_toc(f.read())
    ucas_out = utoc_out[:-len(".utoc")] + ".ucas"
    entry = next(i for n, i in toc.file_map.items()
                 if n.lower().endswith(pkgname.lower()))
    raw2 = iostore.read_entry(toc, ucas_out, entry)
    pkg2 = zen.parse_zen(raw2, pkgname, script_objects=script)
    ex2 = next(e for e in pkg2.exports if e.class_name == "Texture2D")
    info2 = texture.parse_texture(ex2, pkg2.bulk_map)
    assert (info2["w"], info2["h"], info2["fmt"]) == (info["w"], info["h"],
                                                      info["fmt"])
    rgba2 = bcdec.decode_texture(info2["fmt"], info2["w"], info2["h"],
                                 ex2.data[info2["mips"][0]["offset"]:
                                          info2["mips"][0]["offset"]
                                          + info2["mips"][0]["size"]])
    assert bytes(rgba2) == bytes(rgba)


def _extract_manifest_dir(tmp_path, pkgname, info):
    """Build a minimal extract dir (manifest.json + rendered PNG) whose
    'rendered' pixels are the ORIGINAL texture pixels — the idempotency
    round-trip."""
    import tempfile

    src = _st_src()
    script = _script_objects(src)
    raw = payload.read_file(src, pkgname)
    pkg = zen.parse_zen(raw, pkgname, script_objects=script)
    ex = next(e for e in pkg.exports if e.class_name == "Texture2D")
    info = texture.parse_texture(ex, pkg.bulk_map)
    rgba = texture.read_mip(src, pkgname, ex, info, 0)
    d = tempfile.mkdtemp(dir=str(tmp_path))
    pname = texture.png_name(pkgname, info["name"], 0)
    with open(os.path.join(d, pname), "wb") as f:
        f.write(pngio.encode_png(info["w"], info["h"], bytes(rgba)))
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump([{"pkg": pkgname, "export_index": 0, "name": info["name"],
                    "type": "Texture2D", "width": info["w"],
                    "height": info["h"], "format": info["fmt"],
                    "mips": len(info["mips"]), "png": pname,
                    "candidate": False}], f)
    return d


# --------------------------------------------------------------------------
# real-game (SampleGameB, legacy pak packaging)

EF_GAME = os.path.join(_GAMES_ROOT, "SampleGameB")
EF_KEY_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "fixtures", "sample_b_index_facts.json")


def _ef_src():
    import pytest
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    if key is None:
        pytest.skip("sample_b AES key not set (tests/keys.py)")
    return payload.find_sources(EF_GAME, aes_key=key)


def test_ef_legacy_parse_maskrgb():
    src = _ef_src()
    files = payload.list_files(src)
    name = next(f for f in files if f.endswith("T_ky_maskRGB3.uasset"))
    stem = name[:-len(".uasset")]
    pkg = uasset.parse_legacy(payload.read_file(src, name),
                              payload.read_file(src, stem + ".uexp"),
                              stem + ".uexp")
    ex = next(e for e in pkg.exports if e.class_name == "Texture2D")
    ub_size = payload.file_size(src, stem + ".ubulk")
    info = texture.parse_texture_legacy(ex, ub_size)
    assert (info["w"], info["h"]) == (1024, 1024)
    assert info["fmt"] == "PF_DXT1"
    assert len(info["mips"]) == 11
    # mips 0..3 sequential in .ubulk (cumulative offsets), tail inline
    assert [m["storage"] for m in info["mips"]] == \
        ["ubulk"] * 4 + ["inline"] * 7
    assert info["mips"][1]["offset"] == info["mips"][0]["size"]
    rgba = texture.read_mip(src, name, ex, info, 0)
    assert len(rgba) == 1024 * 1024 * 4


def test_ef_legacy_round_trip_pak_rebuild(tmp_path):
    """EF: extract mip0 -> re-import the same pixels -> rebuild the PAK ->
    re-read from the OUTPUT pak -> decode and compare (PSNR, since the
    BC1 encoder is an approximation, not bit-exact)."""
    import math
    import tempfile

    from backend.ut import pak as pak_mod
    from backend.ut.manifest import normalize_pak_name

    def psnr(a, b):
        se = sum((a[i] - b[i]) ** 2 for i in range(len(a)))
        return 99.0 if not se else 10 * math.log10(255 * 255 / (se / len(a)))

    src = _ef_src()
    files = payload.list_files(src)
    name = next(f for f in files if f.endswith("T_ky_maskRGB3.uasset"))
    stem = name[:-len(".uasset")]
    pkg = uasset.parse_legacy(payload.read_file(src, name),
                              payload.read_file(src, stem + ".uexp"),
                              stem + ".uexp")
    ex = next(e for e in pkg.exports if e.class_name == "Texture2D")
    info = texture.parse_texture_legacy(
        ex, payload.file_size(src, stem + ".ubulk"))
    rgba = texture.read_mip(src, name, ex, info, 0)

    d = tempfile.mkdtemp(dir=str(tmp_path))
    pname = texture.png_name(name, info["name"], 0)
    with open(os.path.join(d, pname), "wb") as f:
        f.write(pngio.encode_png(info["w"], info["h"], bytes(rgba)))
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump([{"pkg": name, "export_index": 0, "name": info["name"],
                    "type": "Texture2D", "width": info["w"],
                    "height": info["h"], "format": info["fmt"],
                    "mips": len(info["mips"]), "png": pname,
                    "candidate": False}], f)

    staged = texture.import_images(EF_GAME, d, d,
                                   aes_key=src["aes_key"])
    assert stem + ".uexp" in staged["staged"]
    assert stem + ".ubulk" in staged["staged"]
    for k in ("uexp", "ubulk"):
        old = payload.read_file(src, f"{stem}.{k}")
        assert len(staged["staged"][f"{stem}.{k}"]) == len(old)

    out_dir = tempfile.mkdtemp(dir=str(tmp_path))
    containers = texture.write_containers(EF_GAME, staged["staged"], out_dir,
                                          aes_key=src["aes_key"])
    pak_out = next(c for c in containers if c.endswith(".pak"))
    idx = pak_mod.read_index(pak_out, aes_key=src["aes_key"])
    by_name = {normalize_pak_name(idx.mount_point, e.name): e
               for e in idx.entries}
    uexp2 = pak_mod.read_entry_data(pak_out, by_name[stem + ".uexp"],
                                    aes_key=src["aes_key"])
    ubulk2 = pak_mod.read_entry_data(pak_out, by_name[stem + ".ubulk"],
                                     aes_key=src["aes_key"])
    pkg2 = uasset.parse_legacy(payload.read_file(src, name), uexp2,
                               stem + ".uexp")
    ex2 = next(e for e in pkg2.exports if e.class_name == "Texture2D")
    info2 = texture.parse_texture_legacy(ex2, len(ubulk2))
    assert (info2["w"], info2["h"], info2["fmt"]) == (info["w"], info["h"],
                                                      info["fmt"])
    rgba2 = bcdec.decode_texture(
        info2["fmt"], info2["w"], info2["h"],
        ubulk2[info2["mips"][0]["offset"]:
               info2["mips"][0]["offset"] + info2["mips"][0]["size"]])
    assert psnr(rgba, rgba2) > 30.0
