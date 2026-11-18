# -*- coding: utf-8 -*-
"""Phase 7: FText-Ascii — collect-all-visible ASCII-source FText frames.

Synthetic exact-byte pins for the patch (ASCII grow/shrink stays ANSI,
non-ASCII translation flips the source FString to UTF-16), a real-game
EF round trip on WG_Title ("Option" -> "Options" — the user's actual
request), and the export-level expected-kind switch.
"""
import json
import os
import struct

import pytest

from backend.ut import exporttext, payload, patchtext, textscan, uasset
from backend.ut.textscan import TextEntry

from backend.tests.keys import sample_b_key
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games
EF_KEY = sample_b_key()  # None -> game-gated tests skip (see tests/keys.py)

KEY1 = "6B4F2E9A1C0D5837F0A2B4C6D8E0F273"  # synthetic FText frame key


def _asciiframe(text: str, key: str = KEY1) -> bytes:
    """flags u32=1 | history u8=0 | FString key | ANSI source FString."""
    return (b"\x01\x00\x00\x00\x00"
            + struct.pack("<i", 33) + key.encode("ascii") + b"\x00"
            + struct.pack("<i", len(text) + 1) + text.encode("ascii")
            + b"\x00")


def _asciientry(offset: int, source: str, key: str = KEY1) -> TextEntry:
    return TextEntry(asset="Synthetic/WG_Title.uexp", export_index=0,
                     class_name="WidgetBlueprint",
                     property_path=["<frame>", "0"],
                     kind="FText-Ascii", namespace="", key=key,
                     source=source, offset=offset)


# --------------------------------------------------------------------------
# synthetic patch pins

def test_patch_ascii_grow_and_shrink():
    frame = _asciiframe("Option") + _asciiframe("Credit", KEY2 := KEY1)
    e1 = _asciientry(0, "Option")
    e2 = _asciientry(len(_asciiframe("Option")), "Credit")
    result, report = patchtext.patch_payload(
        frame, [e1, e2], {0: "Options", 1: "Credits"})
    assert [p["shift"] for p in report["patched"]] == [1, 1]
    assert len(result) == len(frame) + 2
    # both bodies ANSI, lengths positive (7 chars + NUL = 8), terminated
    assert struct.pack("<i", 8) + b"Options\x00" in result
    assert struct.pack("<i", 8) + b"Credits\x00" in result
    # re-parse: the frames re-appear with the new sources
    rows = []
    import re
    for m in textscan._FRAME.finditer(result):
        (ln,) = struct.unpack_from("<i", m.group(2), 0)
        if ln > 0:
            rows.append(result[m.end():m.end() + ln - 1].decode("ascii"))
    assert rows == ["Options", "Credits"]


def test_patch_ascii_to_utf16_flip():
    """A non-ASCII translation flips the source FString to UTF-16
    (negative length) — the game's FString reader takes either."""
    frame = _asciiframe("Start")
    entry = _asciientry(0, "Start")
    result, report = patchtext.patch_payload(
        frame, [entry], {0: "開始"})
    (ln,) = struct.unpack_from("<i", result, 42)
    assert ln == -(len("開始") + 1)
    assert result[46:46 + 2 * (len("開始") + 1)] \
        == "開始".encode("utf-16-le") + b"\x00\x00"
    # the frame detector sees it as a plain UTF-16 FText frame now
    frames = textscan._detect_frames(result)
    assert [(f[2], f[3]) for f in frames if f[1] is not None] \
        == [(KEY1, "開始")]


def test_patch_ascii_stale_entry_rejected():
    with pytest.raises(ValueError, match="stored length"):
        # same region, wrong claimed source: stored 7 != claimed 8
        patchtext.patch_payload(_asciiframe("Option"),
                                [_asciientry(0, "Options")], {0: "Options"})


# --------------------------------------------------------------------------
# real-game round trip: the user's actual request on WG_Title

def test_ef_wg_title_options_round_trip(tmp_path):
    from backend.ut.textscan import scan_text

    src = payload.find_sources(
        os.path.join(_GAMES_ROOT, "SampleGameB"), aes_key=EF_KEY)
    name = next(f for f in payload.list_files(src)
                if f.endswith("WG_Title.uexp"))
    stem = name[:-len(".uexp")]
    pkg = uasset.parse_legacy(payload.read_file(src, stem + ".uasset"),
                              payload.read_file(src, name),
                              name)
    entries, _w = textscan.scan_text(pkg)
    picks = {i: e for i, e in enumerate(entries) if e.kind == "FText-Ascii"}
    by_src = {e.source: i for i, e in picks.items()}
    assert {"Option", "Credit"} <= set(by_src)
    translations = {by_src["Option"]: "Options",
                    by_src["Credit"]: "Credits"}
    res = exporttext.export_translations(
        os.path.join(_GAMES_ROOT, "SampleGameB"), {name: translations},
        aes_key=EF_KEY, out_dir=str(tmp_path / "out"))
    assert res["verify"] == "ok" and res["rows_patched"] == 2

    # independent re-parse from the rebuilt pak: the title menu carries
    # the new labels; everything else identical
    from backend.ut.textscan import scan_text as _scan
    vsrc = {"paks": res["containers"], "io_pairs": [], "aes_key": EF_KEY,
            "_files": None}
    ux = payload.read_file(vsrc, name)
    ua = payload.read_file(vsrc, stem + ".uasset")
    pkg2 = uasset.parse_legacy(ua, ux, name)
    ventries, _w2 = _scan(pkg2)
    vsrc_map = {e.source for e in ventries if e.kind == "FText-Ascii"}
    assert {"Options", "Credits"} <= vsrc_map
    assert "Option" not in vsrc_map and "Credit" not in vsrc_map
