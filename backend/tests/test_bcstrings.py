# -*- coding: utf-8 -*-
"""Phase 6: Kismet bytecode string constants (BcString) — scan grammar,
same-size patching (UTF-16 in place + ANSI opcode swap), and a real-game
EF round trip.

Synthetic pins are exact-byte; the frame builder mirrors the discovered
grammar ``[29 01][34][UTF-16 text][00 00][1f][32hex][00][1f 00]``. The
real-game pin patches two WG_* rows through patch_payload (same-size
rule), checks the key framing and untouched rows byte-exactly, and
exercises the Phase 6 vanish rule via exporttext.
"""
import json
import os

import pytest

from backend.ut import exporttext, payload, patchtext, textscan, uasset
from backend.ut.textscan import TextEntry

from backend.tests.keys import sample_b_key
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games
EF_KEY = sample_b_key()  # None -> game-gated tests skip (see tests/keys.py)

# --------------------------------------------------------------------------
# synthetic frame builder (local mirror of the scan grammar)

KEY1 = "6B4F2E9A1C0D5837F0A2B4C6D8E0F273"  # synthetic frame key
KEY2 = "5516C6DB40D8970A746D00BB1FC0258B"
BC_OP = 2                                # [29 01][34]…  — the op byte


def _bcframe(text: str, key: str = KEY1) -> bytes:
    return (b"\x29\x01\x34" + text.encode("utf-16-le") + b"\x00\x00"
            + b"\x1f" + key.encode("ascii") + b"\x00"
            + b"\x1f\x00")                       # the empty-ANSI const


def _bcentry(offset: int, source: str, key: str = KEY1) -> TextEntry:
    return TextEntry(asset="Synthetic/WG_Test.uexp", export_index=0,
                     class_name="Function", property_path=["<bcstring>", "0"],
                     kind="BcString", namespace="", key=key, source=source,
                     offset=offset)


# --------------------------------------------------------------------------
# scanner

def test_scan_bc_strings_grammar():
    data = b"\x17" + _bcframe("武器 大剣") + b"\x16"
    rows = textscan._scan_bc_strings(data)
    assert len(rows) == 1
    op, txt, key, units = rows[0]
    assert op == 1 + BC_OP and txt == "武器 大剣" and key == KEY1 and units == 5
    # empty text / missing prefix / lowercase key / truncated framing: none
    assert textscan._scan_bc_strings(b"\x29\x01\x34\x00\x00") == []
    assert textscan._scan_bc_strings(_bcframe("武器 大剣")[1:]) == []
    assert textscan._scan_bc_strings(
        _bcframe("テスト", KEY1.lower())) == []
    assert textscan._scan_bc_strings(_bcframe("テスト")[:-3]) == []


def test_bc_collect_all(st_scan, ef_scan):
    """Phase 7: ALL grammar-valid BcString rows are emitted (the former
    JP gate is gone — symbol glyphs like the ☆ ratings are rows now,
    gray-tagged client-side). Census: ST 9, EF 592 = 583 JP + 9 glyphs;
    the 29 01 prefix rule replaced one key-anchored mojibake row with the
    real row the old greedy match had swallowed."""
    assert st_scan["stats"]["by_kind"]["BcString"] == 9
    assert ef_scan["stats"]["by_kind"]["BcString"] == 592
    assert "bcstring-nonjp" not in ef_scan["stats"]["warnings"]
    for scan in (st_scan, ef_scan):
        for e in scan["entries"]:
            if e.kind == "BcString":
                assert len(e.key) == 32
    glyphs = [e for e in ef_scan["entries"]
              if e.kind == "BcString" and not textscan._has_jp(e.source)]
    assert len(glyphs) == 9


# --------------------------------------------------------------------------
# patching: same-size, two modes

def test_patch_utf16_mode_and_zero_shift():
    src = "武器 大剣"                          # 5 chars -> region 1+12 = 13
    frame = _bcframe(src)
    data = b"\x00" * 4 + frame + b"\xaa" * 8
    entry = _bcentry(4 + BC_OP, src)
    result, report = patchtext.patch_payload(data, [entry], {0: "Sword"})
    assert len(data) == 4 + len(frame) + 8
    assert report["patched"][0]["mode"] == "utf16"
    assert report["patched"][0]["shift"] == 0
    assert len(result) == len(data)
    # exact fit: prefix + op + "Sword" + NUL fills the region
    assert result[4:4 + len(frame)] == (b"\x29\x01\x34"
                                        + "Sword".encode("utf-16-le")
                                        + b"\x00\x00"
                                        + b"\x1f" + KEY1.encode() + b"\x00"
                                        + b"\x1f\x00")
    assert result[4 + len(frame):] == b"\xaa" * 8
    # the site still parses; its text is now the translation (the JP gate
    # in scan_text is what stops it becoming a row again)
    rows = textscan._scan_bc_strings(result)
    assert [(op, txt, key) for op, txt, key, _u in rows] \
        == [(4 + BC_OP, "Sword", KEY1)]
    rebased = patchtext.rebase_entries([entry], report)
    assert rebased[0].offset == 4 + BC_OP and rebased[0].source == "Sword"


def test_patch_utf16_space_pad():
    """Shorter translation = SPACE-padded to the original unit count (the
    terminator must land on the original position — bytes after it are
    executed bytecode, there is no dead tail)."""
    src = "シュティーア"                       # 6 chars -> region 15 bytes
    frame = _bcframe(src)
    entry = _bcentry(BC_OP, src)
    result, report = patchtext.patch_payload(
        frame, [entry], {0: "Hello"})          # 5 chars -> "Hello " padded
    assert report["patched"][0]["mode"] == "utf16"
    assert len(result) == len(frame)
    assert result[BC_OP:BC_OP + 1 + 14] == (b"\x34"
        + "Hello ".encode("utf-16-le") + b"\x00\x00")
    # the following bytecode (key const) keeps its exact address
    assert result[BC_OP + 15:] == (b"\x1f" + KEY1.encode() + b"\x00"
                                   + b"\x1f\x00")
    # the site re-parses with the PADDED text (raw parser; the JP gate in
    # scan_text is what stops it becoming a row again)
    rows = textscan._scan_bc_strings(result)
    assert [(op, txt, key) for op, txt, key, _u in rows] \
        == [(BC_OP, "Hello ", KEY1)]


def test_patch_ansi_swap_mode():
    src = "大剣"                               # 2 chars -> region 7 bytes
    frame = _bcframe(src)
    entry = _bcentry(BC_OP, src)
    result, report = patchtext.patch_payload(
        frame, [entry], {0: "Sword"})          # utf16 needs 13 > 7; ANSI 7
    assert report["patched"][0]["mode"] == "ansi"
    assert report["patched"][0]["shift"] == 0
    assert len(result) == len(frame)
    assert result[:BC_OP] == b"\x29\x01"       # prefix preserved
    assert result[BC_OP:BC_OP + 7] == b"\x1f" + b"Sword" + b"\x00"
    assert result[BC_OP + 7:] == b"\x1f" + KEY1.encode() + b"\x00\x1f\x00"
    assert textscan._scan_bc_strings(result) == []   # 0x34 anchor is gone


def test_patch_ansi_space_pad():
    src = "防具"                               # 2 chars -> region 7 bytes
    frame = _bcframe(src)
    entry = _bcentry(BC_OP, src)
    result, report = patchtext.patch_payload(
        frame, [entry], {0: "Arm2"})           # 4 chars -> "Arm2 " (5)
    assert report["patched"][0]["mode"] == "ansi"
    assert result[BC_OP:BC_OP + 7] == b"\x1f" + b"Arm2 " + b"\x00"
    assert result[BC_OP + 7:] == b"\x1f" + KEY1.encode() + b"\x00\x1f\x00"


def test_patch_too_long_skips_never_truncates():
    frame = _bcframe("大剣")                   # region 7 bytes
    entry = _bcentry(BC_OP, "大剣")
    long_jp = "とても長い日本語の訳文は入らない"  # non-ASCII, 14 chars
    result, report = patchtext.patch_payload(frame, [entry], {0: long_jp})
    assert result == frame                     # untouched
    assert report["patched"] == []
    assert report["skipped"][0]["entry_index"] == 0
    assert "too long" in report["skipped"][0]["reason"]


def test_patch_bc_two_sites():
    f1 = _bcframe("武器 槍", KEY1)             # 4 chars -> region 11 bytes
    f2 = _bcframe("防具", KEY2)
    data = f1 + f2
    e1, e2 = (_bcentry(BC_OP, "武器 槍", KEY1),
              _bcentry(len(f1) + BC_OP, "防具", KEY2))
    result, report = patchtext.patch_payload(
        data, [e1, e2], {0: "Arm1", 1: "Arm2"})   # e1 utf16-fit, e2 ansi-swap
    assert [p["entry_index"] for p in report["patched"]] == [0, 1]
    modes = {p["entry_index"]: p.get("mode") for p in report["patched"]}
    assert modes == {0: "utf16", 1: "ansi"}
    assert all(p["shift"] == 0 for p in report["patched"])
    assert len(result) == len(data)
    rows = textscan._scan_bc_strings(result)
    assert [(op, txt, key) for op, txt, key, _u in rows] \
        == [(BC_OP, "Arm1", KEY1)]          # only the utf16 site re-parses


def test_patch_bc_duplicate_entries_overlap():
    """The same region claimed twice = hard error (double-write is
    corruption by construction — same rule as the FText kinds)."""
    frame = _bcframe("武器 大剣")
    with pytest.raises(ValueError, match="overlap"):
        patchtext.patch_payload(frame, [_bcentry(BC_OP, "武器 大剣"),
                                        _bcentry(BC_OP, "武器 大剣")],
                                {0: "Sword", 1: "Blade"})


def test_patch_bc_stale_entries_rejected():
    with pytest.raises(ValueError, match="opcode byte"):
        patchtext.patch_payload(_bcframe("武器 大剣")[1:],
                                [_bcentry(BC_OP, "武器 大剣")], {0: "Sword"})
    with pytest.raises(ValueError, match="key framing"):
        patchtext.patch_payload(_bcframe("武器 大剣", KEY2),
                                [_bcentry(BC_OP, "武器 大剣")], {0: "Sword"})
    with pytest.raises(ValueError, match="text mismatch"):
        patchtext.patch_payload(_bcframe("防具"),
                                [_bcentry(BC_OP, "武器")], {0: "Sword"})


# --------------------------------------------------------------------------
# real-game round trip: EF WG_* package patched + re-scanned

def test_st_export_too_long_bcstring_skips_cleanly(tmp_path):
    """Export-level: a BcString translation that fits no write mode is
    SKIPPED with a reason (rows_skipped + skipped_detail) — the export
    still verifies, because the row is excluded from the patch AND from
    the expected re-scan set."""
    from backend.ut import exporttext

    st = os.path.join(_GAMES_ROOT, "SampleGameA")
    src = payload.find_sources(st)
    # locate one real BcString row and craft a non-fitting translation
    name = next(f for f in payload.list_files(src)
                if f.endswith("WBP_部屋.uasset"))
    pkg = textscan.zen.parse_zen(payload.read_file(src, name), name,
                                 script_objects=textscan._script_objects(src))
    entries, _w = textscan.scan_text(pkg)
    bc = [e for e in entries if e.kind == "BcString"]
    assert bc, "expected BcString rows in the save widget"
    row = bc[0]
    too_long = "あ" * (len(row.source) * 3 + 40)   # non-ASCII, no fit ever
    idx = entries.index(row)
    res = exporttext.export_translations(
        st, {name: {idx: too_long}}, out_dir=str(tmp_path / "out"))
    assert res["verify"] == "ok"
    assert res["rows_patched"] == 0
    assert res["rows_skipped"] == 1
    assert res["skipped_detail"] == [{
        "asset": name, "entry_index": idx,
        "reason": "translation too long for the bytecode string slot"}]


def test_ef_real_bcstring_round_trip():
    src = payload.find_sources(
        os.path.join(_GAMES_ROOT, "SampleGameB"), aes_key=EF_KEY)
    name = next(f for f in payload.list_files(src)
                if f.endswith("WG_SubQuest09_NPC00_00.uasset"))
    stem = name[:-len(".uasset")]
    pkg = uasset.parse_legacy(payload.read_file(src, name),
                              payload.read_file(src, stem + ".uexp"),
                              stem + ".uexp")
    entries, _warn = textscan.scan_text(pkg)
    bc = sorted((e for e in entries if e.kind == "BcString"),
                key=lambda e: len(e.source))
    assert len(bc) > 20
    short = next(e for e in bc if len(e.source) >= 5)      # "Hello" fits utf16
    mid = next(e for e in bc if 13 <= len(e.source) < 26)  # forces the swap
    long_ = max(bc, key=lambda e: len(e.source))

    uexp = payload.read_file(src, stem + ".uexp")
    translations = {}
    for i, e in enumerate(entries):
        if e is short:
            translations[i] = "Hello"
        elif e is mid:
            translations[i] = "Opened the treasure chest."
    result, report = patchtext.patch_payload(uexp, entries, translations)
    assert len(result) == len(uexp), "same-size rule"
    modes = {p["entry_index"]: p.get("mode") for p in report["patched"]}
    assert modes == {entries.index(short): "utf16",
                     entries.index(mid): "ansi"}
    assert len(report["skipped"]) == 0

    # re-scan the patched .uexp bytes: exact-fill keeps both sites
    # parseable — the utf16 row reads back SPACE-PADDED to the original
    # unit count, the ansi-swapped row lost its 0x34 anchor entirely, and
    # every untouched row re-appears byte-identically
    new_map = {key: txt for _op, txt, key, _u
               in textscan._scan_bc_strings(result)}
    assert new_map[short.key] == "Hello".ljust(len(short.source))
    assert mid.key not in new_map
    assert new_map[long_.key] == long_.source
    untouched = [e for e in bc if e is not short and e is not mid]
    assert all(new_map[e.key] == e.source for e in untouched)

    # vanish rule: exactly the two translated rows leave the expected set
    assert len(exporttext._expected_after_patch(entries, translations)) \
        == len(entries) - 2
