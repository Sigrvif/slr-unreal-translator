# -*- coding: utf-8 -*-
"""Phase 5b: font replacement tests.

Synthetic tests cover the .ufont format detector and repackaging; the
real-game tests exercise the scan census on both reference games and a
fonts-only ST export round trip (ST's sidecars live in its small loose
pak, so the container rebuild is seconds — EF's 3GB pak rebuild is
exercised by the Phase 5a round trip and manual smoke, not here).
"""
import os

import pytest

from backend.ut import fonts, payload
from backend.ut.fonts import FontError
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games

# Minimal valid-sfnt stand-ins (magic + size floor only — read_font's
# validation is deliberately shallow, the game's FreeType owns the rest).
_FAKE_DLG = b"\x00\x01\x00\x00" + b"\xaa" * 2048
_FAKE_UI = b"OTTO" + b"\xbb" * 2048

# Census (2026-10-24 discovery): both games ship the 7 engine faces plus
# one game-owned face; ST sidecars are size-prefixed, EF's raw.
_CENSUS = 8


# --------------------------------------------------------------------------
# synthetic: read_font / format detection / repackaging

def test_read_font_validates():
    import tempfile
    for bad, why in [(b"\x00\x01\x00\x00" + b"x" * 10, "too small"),
                     (b"JUNK" + b"x" * 2000, "sfnt magic")]:
        with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as f:
            f.write(bad)
            path = f.name
        try:
            with pytest.raises(FontError, match=why):
                fonts.read_font(path)
        finally:
            os.unlink(path)
    with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as f:
        f.write(_FAKE_DLG)
        path = f.name
    try:
        assert fonts.read_font(path) == _FAKE_DLG
    finally:
        os.unlink(path)


def test_detect_ufont_format():
    assert fonts._detect_ufont_format(_FAKE_UI) == "raw"
    prefixed = (len(_FAKE_UI)).to_bytes(4, "little") + _FAKE_UI + b"\x00" * 4
    assert fonts._detect_ufont_format(prefixed) == "prefixed"
    # wrong prefix value / missing trailer / truncated -> unknown
    assert fonts._detect_ufont_format(
        (len(_FAKE_UI) + 1).to_bytes(4, "little") + _FAKE_UI
        + b"\x00" * 4) is None
    assert fonts._detect_ufont_format(
        (len(_FAKE_UI)).to_bytes(4, "little") + _FAKE_UI) is None
    assert fonts._detect_ufont_format(b"JUNKJUNKJUNK") is None


def test_repackage_preserves_layout():
    assert fonts._repackage("raw", _FAKE_DLG) == _FAKE_DLG
    out = fonts._repackage("prefixed", _FAKE_DLG)
    assert int.from_bytes(out[:4], "little") == len(_FAKE_DLG)
    assert out[4:4 + len(_FAKE_DLG)] == _FAKE_DLG
    assert out[4 + len(_FAKE_DLG):] == b"\x00\x00\x00\x00"


# --------------------------------------------------------------------------
# real games: census + staging

def _st_src():
    return payload.find_sources(
        os.path.join(_GAMES_ROOT, "SampleGameA"))


def _ef_src():
    import pytest
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    if key is None:
        pytest.skip("sample_b AES key not set (tests/keys.py)")
    return payload.find_sources(
        os.path.join(_GAMES_ROOT, "SampleGameB"), aes_key=key)


def _census_checks(rows, game_face_fragment, expected_format):
    assert len(rows) == _CENSUS
    by_role = {}
    for r in rows:
        assert r["format"] == expected_format
        by_role.setdefault(r["role"], []).append(r)
    # dialogue: the game face + the engine's CJK fallback; everything
    # else (Roboto x5 + mono) is UI
    dlg = {r["ufont"] for r in by_role["dialogue"]}
    assert len(dlg) == 2
    assert any(game_face_fragment in f for f in dlg)
    assert any("DroidSansFallback" in f for f in dlg)
    assert len(by_role["ui"]) == 6


def test_st_scan_census():
    _census_checks(fonts.scan_fonts(_st_src()),
                   "sample_a/Content/font/MSGOTHIC", "prefixed")


def test_ef_scan_census():
    _census_checks(fonts.scan_fonts(_ef_src()),
                   "MPLUSRounded1c-ExtraBold2", "raw")


def test_stage_font_swaps_st(tmp_path):
    src = _st_src()
    staged, report = fonts.stage_font_swaps(src, _FAKE_DLG, _FAKE_UI)
    assert len(staged) == 8
    assert len(report["swapped"]) == 8 and report["skipped"] == []
    for row in report["swapped"]:
        data = staged[row["ufont"]]
        font = _FAKE_DLG if row["role"] == "dialogue" else _FAKE_UI
        # every ST sidecar is prefixed: u32 size + font + zero trailer
        assert int.from_bytes(data[:4], "little") == len(font)
        assert data[4:4 + len(font)] == font
        assert data[4 + len(font):] == b"\x00\x00\x00\x00"
    # a role without a font skips its faces, never fatal
    staged_d, report_d = fonts.stage_font_swaps(src, _FAKE_DLG, None)
    assert len(staged_d) == 2
    assert {r["role"] for r in report_d["skipped"]} == {"ui"}


def test_st_fonts_only_export_round_trip(tmp_path):
    """fonts-only export_translations: containers rebuilt from the small
    loose pak, every staged sidecar byte-identical on read-back (the
    export's own verify re-reads them too — this asserts it directly)."""
    from backend.ut import exporttext

    out = tmp_path / "export"
    res = exporttext.export_translations(
        os.path.join(_GAMES_ROOT, "SampleGameA"), {},
        out_dir=str(out), fonts=(_FAKE_DLG, _FAKE_UI))
    assert res["rows_patched"] == 0 and res["fonts_swapped"] == 8
    assert res["verify"] == "ok"
    assert res["containers"], "fonts-only export must still write containers"

    vsrc = {"paks": res["containers"], "io_pairs": [], "aes_key": None,
            "_files": None}
    staged, report = fonts.stage_font_swaps(_st_src(), _FAKE_DLG, _FAKE_UI)
    assert len(report["swapped"]) == 8
    for name, data in staged.items():
        assert payload.read_file(vsrc, name) == data


def test_cli_cmd_export_fonts_job(tmp_path, monkeypatch):
    """The addon's exact job shape: ``cmd_export`` with ``job['fonts']``
    file paths (the CLI layer owns read+validate). slr_cli runs with
    ``backend/`` itself on sys.path (its imports are ``from ut import
    ...``), so it loads as a sibling module next to ``backend.ut``."""
    import importlib.util
    import sys

    dev = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    backend_dir = os.path.join(dev, "backend")
    monkeypatch.syspath_prepend(backend_dir)
    spec = importlib.util.spec_from_file_location(
        "slr_cli_fonts_test", os.path.join(backend_dir, "slr_cli.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    dlg = os.path.join(dev, "..", "SLR Translator", "www", "core", "SEP",
                       "SLRCondensedCJK-Regular.ttf")
    if not os.path.isfile(dlg):        # dev-machine layout fallback
        pytest.skip("SLR Translator install not found")
    if not os.path.isdir(os.path.join(_GAMES_ROOT, "SampleGameA")):
        pytest.skip("sample game not present (SLRUNREAL_GAMES_ROOT)")
    ui = os.path.join(dev, "..", "SLR Translator", "fonts", "sucr.ttf")
    res = mod.cmd_export({
        "version": 2, "command": "export",
        "game_dir": os.path.join(_GAMES_ROOT, "SampleGameA"),
        "translations": {},
        "out": str(tmp_path / "out"),
        "fonts": {"dialogue": os.path.abspath(dlg),
                  "ui": os.path.abspath(ui)}})
    assert res["fonts_swapped"] == 8
    assert res["stats"]["verify"] == "ok"
    assert res["containers"] and res["containers"][0].endswith(".pak")

    # bad font path = named FontError from the shared validation point.
    # slr_cli's imports resolve through ITS sys.path ("ut.fonts"), a second
    # module instance next to backend.ut — catch that tree's class.
    import ut.fonts as cli_fonts  # noqa: PLC0415
    with pytest.raises(cli_fonts.FontError):
        mod.cmd_export({
            "version": 2, "command": "export",
            "game_dir": os.path.join(_GAMES_ROOT, "SampleGameA"),
            "translations": {}, "out": str(tmp_path / "out2"),
            "fonts": {"dialogue": str(tmp_path / "missing.ttf")}})
