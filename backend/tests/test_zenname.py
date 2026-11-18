# -*- coding: utf-8 -*-
"""ZenName: zen name-batch collection + patching (the ST options-menu
fix). The options labels are DataTable ROW KEYS stored as name-batch
strings — displayed by WBP_オプションスロット via GetDataTableRowNames.
Patching rewrites the string bytes, the BE i16 length header, the u64
CityHash64(lowercased) hash, the batch's string-bytes counter, and every
summary offset past the batch; the self-check re-parses and pins the
resulting names.

Real-game pins (skipped when the ST tree is absent) use DT_オプション —
20 yellow display keys + 2 red self-referential names.
"""
import os
import struct

import pytest

from backend.ut import cityhash, payload, textscan, zen
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games

ST_ROOT = os.path.join(_GAMES_ROOT, "SampleGameA")
_DT_OPTIONS = "sample_a/Content/common/データ/DT_オプション.uasset"

_ST_AVAILABLE = os.path.isdir(ST_ROOT)


def _sources():
    return payload.find_sources(ST_ROOT)


def _script_objects(src):
    return textscan._script_objects(src)


# ---------------------------------------------------------------- cityhash

def test_name_batch_hash_rule_pure_unit():
    """The hash rule the patcher relies on: the batch stores CityHash64
    of the LOWERCASED string in its storage charset — 'BGM-0' and
    'bgm-0' share one hash, case-sensitive bytes do not."""
    low = cityhash.cityhash64("bgm-0".encode("ascii"))
    assert cityhash.cityhash64("BGM-0".lower().encode("ascii")) == low
    assert low != cityhash.cityhash64("BGM-0".encode("ascii"))
    # the exact value ST stores for the FName 'BGM-0'
    assert low == 0xFFA912C2D7D0EA26


# --------------------------------------------------- synthetic round-trip

def test_name_batch_geometry_roundtrip():
    """_read_name_batch returns per-entry geometry that re-decodes to the
    names and re-verifies the hash rule, on a hand-built batch:
    1 ascii + 1 wide name."""  # batch: num, string_bytes, hash_version,
    # 2 hashes, 2 BE i16 length headers, then the strings
    s0 = b"BGM-0"
    s1 = "中".encode("utf-16-le")
    h0 = cityhash.cityhash64(s0.lower())          # rule: hash(lowercase)
    h1 = cityhash.cityhash64("中".encode("utf-16-le"))
    batch = struct.pack("<IIQ", 2, len(s0) + len(s1), 0xC1640000) \
        + struct.pack("<QQ", h0, h1) \
        + struct.pack(">hh", len(s0), -32768 + 1) + s0 + s1
    names, end, entries = zen._read_name_batch(batch, 0)
    assert names == ["BGM-0", "中"]
    assert end == len(batch)
    assert [(e.encoding, e.str_bytes) for e in entries] == \
        [("ascii", 5), ("wide", 2)]
    for e in entries:
        raw = batch[e.str_offset:e.str_offset + e.str_bytes]
        dec = raw.decode("ascii" if e.encoding == "ascii" else "utf-16-le")
        assert dec == e.text
        (stored_h,) = struct.unpack_from("<Q", batch, e.hash_offset)
        want = cityhash.cityhash64(
            e.text.lower().encode("ascii" if e.encoding == "ascii"
                                  else "utf-16-le"))
        assert stored_h == want
        (hdr,) = struct.unpack_from(">h", batch, e.header_offset)
        assert hdr == (len(s0) if e.encoding == "ascii" else -32768 + 1)


def test_patch_span_uses_entry_geometry():
    """patch_span for ZenName reads the byte length from entry.meta — a
    wide source has str_bytes == 2 * chars, an ascii one == chars."""
    from backend.ut import patchtext
    e = textscan.TextEntry(
        asset="a.uasset", export_index=-1, class_name=None,
        property_path=["<name>", "1"], kind="ZenName", namespace="", key="",
        source="ゲーム", offset=40, meta={"encoding": "wide", "bytes": 4,
                                          "name_index": 1, "risk": "yellow"})
    span = patchtext.patch_span(e, {"offset": 40, "old_chars": 2,
                                    "new_chars": 9, "shift": 14})
    assert span == (40, 44, 14)


# ------------------------------------------------- real-game pins (ST)

@pytest.mark.skipif(not _ST_AVAILABLE, reason="ST game tree not present")
def test_st_options_names_collected():
    src = _sources()
    so = _script_objects(src)
    data = payload.read_file(src, _DT_OPTIONS)
    pkg = zen.parse_zen(data, _DT_OPTIONS, script_objects=so)
    entries, warnings = textscan.scan_text(pkg)
    zn = [e for e in entries if e.kind == "ZenName"]
    yellow = {e.source for e in zn if e.meta["risk"] == "yellow"}
    red = {e.source for e in zn if e.meta["risk"] == "red"}
    # the options menu displays exactly these row keys
    assert {"ゲームクオリティ-高", "ゲームクオリティ-中", "ゲームクオリティ-低",
            "解像度-1280×720", "解像度-1600×900", "解像度-1920×1080",
            "解像度-2560×1440", "解像度-3840×2160",
            "フルスクリーン", "ボーダレスウィンドウ",
            "ウィンドウ", "BGM-中", "SE-大", "VOICE-小"} <= yellow
    assert red == {"DT_オプション", "/Game/common/データ/DT_オプション"}
    assert not warnings or all(w.startswith(("shape-W", "ftext-"))
                               for w in warnings)
    # ascii names (BGM-0 etc.) are not collected: JP-gated
    assert "BGM-0" not in yellow


@pytest.mark.skipif(not _ST_AVAILABLE, reason="ST game tree not present")
def test_st_asset_stem_names_are_red():
    """A name-batch string that IS a game asset file is structural: the
    BGM cue '1_短くドビュ_Cue' names common/BGM/1_短くドビュ_Cue.uasset —
    renaming it would break every sound reference. With the asset-stem
    set passed (the production callers do), the row is RED. (User-
    reported misclassification: this row shipped yellow in the first
    tagging pass.)"""
    src = _sources()
    stems = textscan.asset_stems(src)
    assert "1_短くドビュ_Cue" in stems
    cue = "sample_a/Content/common/BGM/1_短くドビュ_Cue.uasset"
    pkg = zen.parse_zen(payload.read_file(src, cue), cue,
                        script_objects=_script_objects(src))
    entries, _w = textscan.scan_text(pkg, stems)
    cue_rows = [e for e in entries if e.kind == "ZenName"
                and e.source == "1_短くドビュ_Cue"]
    assert len(cue_rows) == 1
    assert cue_rows[0].meta["risk"] == "red"
    # the _Cue suffix rule reaches the same verdict even without the
    # stems set — this name is doubly covered; the asset-stem rule
    # exists for structural stems WITHOUT conventional suffixes
    entries2, _w2 = textscan.scan_text(pkg)
    row2 = next(e for e in entries2 if e.kind == "ZenName"
                and e.source == "1_短くドビュ_Cue")
    assert row2.meta["risk"] == "red"

@pytest.mark.skipif(not _ST_AVAILABLE, reason="ST game tree not present")
def test_st_options_name_patch_roundtrip():
    """Patch every yellow name with an ASCII translation, then:
    re-parse must show the new names, every stored hash must equal
    CityHash64(lower(name) in storage charset), untouched names byte
    stay, the export's data must be byte-identical, and the package
    must grow by exactly the name-region delta."""
    src = _sources()
    so = _script_objects(src)
    data = payload.read_file(src, _DT_OPTIONS)
    pkg = zen.parse_zen(data, _DT_OPTIONS, script_objects=so)
    entries, _w = textscan.scan_text(pkg)
    trans = {}
    for i, e in enumerate(entries):
        if e.kind == "ZenName" and e.meta["risk"] == "yellow":
            trans[i] = "[EN] " + e.source
    assert len(trans) == 20
    patched = zen.patch_zen_package(data, entries, trans)
    pkg2 = zen.parse_zen(patched, _DT_OPTIONS, script_objects=so)

    # names substituted, red names and ascii names untouched
    for i, e in enumerate(entries):
        if e.kind != "ZenName":
            continue
        got = pkg2.names[e.meta["name_index"]]
        if e.meta["risk"] == "yellow":
            assert got == trans[i]
        else:
            assert got == e.source
    assert pkg2.names[0] == "BGM-0"          # ascii, never collected

    # every hash in the patched batch verifies
    for ne in pkg2.name_entries:
        enc = "ascii" if ne.encoding == "ascii" else "utf-16-le"
        want = cityhash.cityhash64(ne.text.lower().encode(enc))
        (stored,) = struct.unpack_from("<Q", patched, ne.hash_offset)
        assert stored == want, ne.text

    # exports unchanged (patch was names-only): same size, same bytes
    assert len(pkg2.exports) == len(pkg.exports)
    for a, b in zip(pkg.exports, pkg2.exports):
        assert a.serial_offset == b.serial_offset
        assert a.serial_size == b.serial_size
        assert a.data == b.data
    # payload growth == the string-region delta (mixed-script
    # translations store wide: 2 B/char over "[EN] " + source)
    delta = sum(len(trans[i].encode("utf-16-le")) - entries[i].meta["bytes"]
                for i in trans)
    assert len(patched) - len(data) == delta
    # summary offsets moved with the batch
    assert pkg2.header_size == pkg.header_size + (len(patched) - len(data))


@pytest.mark.skipif(not _ST_AVAILABLE, reason="ST game tree not present")
def test_st_path_names_never_patched():
    """The '/'-containing self-name is never patched: patchtext reports
    it skipped with the named reason (the established too-long/too-risky
    contract), and the payload stays byte-identical."""
    from backend.ut import patchtext
    src = _sources()
    so = _script_objects(src)
    data = payload.read_file(src, _DT_OPTIONS)
    pkg = zen.parse_zen(data, _DT_OPTIONS, script_objects=so)
    entries, _w = textscan.scan_text(pkg)
    path_idx = next(i for i, e in enumerate(entries)
                    if e.kind == "ZenName" and "/" in e.source)
    patched, report = patchtext.patch_payload(data, entries,
                                              {path_idx: "X"})
    assert patched == data
    assert report["skipped"] == [
        {"entry_index": path_idx,
         "reason": "name contains a path separator — package identity, "
                   "never patched"}]


@pytest.mark.skipif(not _ST_AVAILABLE, reason="ST game tree not present")
def test_st_options_export_verify_chain():
    """The exporter's full chain on the options asset: translate 3 yellow
    names, verify 'ok' — the rebuilt container's re-scan drops the
    English names (JP-gated collection) and keeps the rest."""
    from backend.ut import exporttext
    src = _sources()
    so = _script_objects(src)
    data = payload.read_file(src, _DT_OPTIONS)
    pkg = zen.parse_zen(data, _DT_OPTIONS, script_objects=so)
    entries, _w = textscan.scan_text(pkg)
    yellow = [i for i, e in enumerate(entries)
              if e.kind == "ZenName" and e.meta["risk"] == "yellow"][:3]
    translations = {_DT_OPTIONS: {i: f"Name {entries[i].source}"
                                  for i in yellow}}
    # export_translations directly (the stage route needs a stage tree;
    # this pins the payload math, which the two routes share)
    out = exporttext.export_translations(ST_ROOT, translations,
                                         out_dir=None)
    assert out["verify"] == "ok"
    assert out["rows_patched"] == 3
    utoc_out = next(p for p in out["containers"] if p.endswith(".utoc"))
    ucas_out = next(p for p in out["containers"] if p.endswith(".ucas"))
    vsrc = {"paks": [], "io_pairs": [(utoc_out, ucas_out)],
            "aes_key": None, "_files": None}
    pkg2 = zen.parse_zen(payload.read_file(vsrc, _DT_OPTIONS), _DT_OPTIONS,
                         script_objects=so)
    for i in yellow:
        assert pkg2.names[entries[i].meta["name_index"]] \
            == f"Name {entries[i].source}"
