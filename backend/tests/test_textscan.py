"""Task 5: shared text extraction — TextEntry over both games' packages.

Invariants pinned against the real games and the Task 2 discovery fixture
(``text_discovery.json`` is ground truth):
  - ST scan_game: FText-Base entries == 992 (exact frame census);
    DT_学校会話 contributes 473.
  - EF scan_game: FText-Base entries == 6,359 (6,191 JP + 168 non-JP —
    the golden DT_EroEventTalkTable alone: 1,143 = 1,065 JP + 78
    censorship-bar ■ frames; non-JP FText still counts).
  - Round-trip: re-reading the payload at entry.offset reproduces the
    frame's source string exactly (20 sampled entries per game).
  - Zero-abort: full sweeps complete; warnings listed in stats as
    class->count (shape-W site counter; Phase 7 collect-all-visible).

Synthetic tests (game-independent) pin the property-tag walk, the honest
["<frame>", N] fallback path, the chained-FString emission, the shape-W
exclusion warning, and zero-abort on junk data.
"""
import re
import struct
from types import SimpleNamespace

from backend.ut import payload, textscan

ST_TOTAL = 992                    # fixture: ftext_frames_jp (frame census)
EF_TOTAL = 6359                    # fixture: ftext_frames_total
EF_JP = 6191                       # fixture: ftext_frames_jp
ST_GOLDEN = "sample_a/Content/common/データ/DT_学校会話.uasset"
ST_GOLDEN_FRAMES = 473             # discovery: 225 runs = 473 frames
EF_GOLDEN_EXP = ("SampleGameB/Content/SampleGameB/DataTable/EroEvent/"
                 "DT_EroEventTalkTable.uexp")
EF_GOLDEN_FRAMES = 1143            # 1,065 JP + 78 ■ (Task 4 golden)
ITEM04_EXP = ("SampleGameB/Content/SampleGameB/DataTable/Item/"
              "EF_Item04_Food.uexp")


# flags u32=1 | history u8=0 | FString len 33 | 32 UPPER hex | NUL | i32 len
_FRAME = re.compile(
    rb"\x01\x00\x00\x00\x00\x21\x00\x00\x00([0-9A-F]{32})\x00(....)", re.S)


def _has_jp(s: str) -> bool:
    return any(0x3040 <= ord(c) <= 0x30FF or 0x4E00 <= ord(c) <= 0x9FFF
               or 0xFF01 <= ord(c) <= 0xFF60 or 0x3000 <= ord(c) <= 0x303F
               or 0xFF61 <= ord(c) <= 0xFF9F for c in s)


def _frame_bytes(key: str, text: str) -> bytes:
    units = text.encode("utf-16-le") + b"\x00\x00"
    return (b"\x01\x00\x00\x00\x00"
            + struct.pack("<i", 33) + key.encode("ascii") + b"\x00"
            + struct.pack("<i", -(len(text) + 1)) + units)


def _fstring_bytes(text: str) -> bytes:
    units = text.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<i", -(len(text) + 1)) + units


def _legacy_pkg(data: bytes, names: list[str], class_name="DataTable"
                ) -> SimpleNamespace:
    """A duck-typed legacy package with one export carrying ``data``."""
    ex = SimpleNamespace(object_name="Ex", class_index=-1,
                         class_name=class_name, super_index=0,
                         template_index=0, outer_index=0, object_flags=0,
                         serial_size=len(data), serial_offset=100,
                         is_not_for_client=False, is_not_for_server=False,
                         is_asset=True, data=data)
    return SimpleNamespace(name="Synthetic/S.uexp", package_name="/Game/S",
                           package_flags=0, total_header_size=100,
                           names=names, imports=[], exports=[ex],
                           version_info=SimpleNamespace())


# --------------------------------------------------------------------------
# game sweeps (scan fixtures are session-scoped in conftest.py: each game
# is scanned exactly once per run and shared with the other scan tests)


def test_st_scan_census_exact(st_scan):
    stats = st_scan["stats"]
    assert stats["errors"] == []
    assert stats["assets_scanned"] == 1359          # fixture: packages_scanned
    assert stats["by_kind"]["FText-Base"] == ST_TOTAL
    golden = [e for e in st_scan["entries"]
              if e.asset == ST_GOLDEN and e.kind == "FText-Base"]
    assert len(golden) == ST_GOLDEN_FRAMES
    # ST frames = 991 JP + 1 non-JP ("→" — Task 3 ledger)
    assert stats["by_kind_jp"]["FText-Base"] == 991
    # sample sanity: JP entries carry Japanese, classes resolve, paths honest
    jp = [e for e in st_scan["entries"] if _has_jp(e.source)]
    sample = jp[::max(1, len(jp) // 20)][:20]
    assert sample and all(_has_jp(e.source) for e in sample)
    assert any(e.class_name == "DataTable" for e in golden)
    assert all(isinstance(e.property_path, list) and e.property_path
               and all(isinstance(p, str) for p in e.property_path)
               for e in st_scan["entries"])


def test_ef_scan_census_exact(ef_scan):
    stats = ef_scan["stats"]
    assert stats["errors"] == []
    assert stats["assets_scanned"] == 7656          # fixture: uasset_uexp_pairs
    assert stats["by_kind"]["FText-Base"] == EF_TOTAL
    assert stats["by_kind_jp"]["FText-Base"] == EF_JP
    golden = [e for e in ef_scan["entries"]
              if e.asset == EF_GOLDEN_EXP and e.kind == "FText-Base"]
    assert len(golden) == EF_GOLDEN_FRAMES          # 1,065 JP + 78 ■
    assert sum(1 for e in golden if _has_jp(e.source)) == 1065
    assert any(e.class_name == "DataTable" for e in golden)
    # plain-FString class is real: the fixture's "~110 in EF_Item04_Food"
    # (reviewer census) undercounted by string parity — the pinned row
    # layout is two cells per row, each [table][X_Name][name] /
    # [table][X_Text][description] => 4 JP chained strings x 57 rows = 228
    item04 = [e for e in ef_scan["entries"]
              if e.asset == ITEM04_EXP and e.kind == "FString"]
    assert len(item04) == 228


def test_roundtrip_reproduces_sources(st_src, ef_src, st_scan, ef_scan):
    """Re-reading the payload at entry.offset reproduces the source string
    exactly for 20 sampled entries per game (both kinds)."""
    def reread(src, entry):
        data = payload.read_file(src, entry.asset)
        if entry.kind == "FText-Base":
            m = _FRAME.match(data, entry.offset)
            assert m, f"frame grammar missing at {entry.offset}"
            (ln,) = struct.unpack_from("<i", m.group(2), 0)
            n = -ln
            assert n > 0
            units = struct.unpack_from(f"<{n}H", data, m.end())
            assert units[-1] == 0
            assert m.group(1).decode() == entry.key
            return "".join(map(chr, units[:-1]))
        if entry.kind == "FText-Ascii":
            ln_pos = entry.offset + 42
            (ln,) = struct.unpack_from("<i", data, ln_pos)
            assert ln == len(entry.source) + 1
            body = data[ln_pos + 4:ln_pos + 4 + ln]
            assert body[-1:] == b"\x00"
            return body[:-1].decode("ascii")
        if entry.kind == "BcString":
            op = entry.offset
            assert data[op - 2:op] == b"\x29\x01" and data[op] == 0x34
            units = len(entry.source)
            body = data[op + 1:op + 1 + 2 * units]
            assert data[op + 1 + 2 * units:op + 3 + 2 * units] == b"\x00\x00"
            return body.decode("utf-16-le")
        if entry.kind == "ZenName":
            raw = data[entry.offset:entry.offset + entry.meta["bytes"]]
            return raw.decode("ascii" if entry.meta["encoding"] == "ascii"
                              else "utf-16-le")
        assert entry.kind == "FString"
        (ln,) = struct.unpack_from("<i", data, entry.offset)
        assert ln < 0
        n = -ln
        units = struct.unpack_from(f"<{n}H", data, entry.offset + 4)
        assert units[-1] == 0
        return "".join(map(chr, units[:-1]))

    for src, scan in ((st_src, st_scan), (ef_src, ef_scan)):
        entries = scan["entries"]
        sample = entries[::max(1, len(entries) // 20)][:20]
        assert len(sample) == 20
        for e in sample:
            assert reread(src, e) == e.source


def test_zero_abort_and_warning_classes(st_scan, ef_scan):
    """Full sweeps complete with zero errors; warning classes recorded in
    stats. Phase 7 collect-all-visible: the retired exclusions
    (ftext-ascii-source / bcstring-nonjp) are EMITTED rows now —
    FText-Ascii + all BcStrings — and the only remaining warning class
    is the shape-W site counter."""
    for scan in (st_scan, ef_scan):
        assert scan["stats"]["errors"] == []
        assert scan["stats"]["text_entries"] == len(scan["entries"])
        assert sum(scan["stats"]["by_kind"].values()) == len(scan["entries"])
        w = scan["stats"]["warnings"]
        assert w.get("shape-W-sites", 0) > 0     # framed sites, counted
        assert set(w) == {"shape-W-sites"}
    assert st_scan["stats"]["warnings"]["shape-W-sites"] == 9
    assert ef_scan["stats"]["warnings"]["shape-W-sites"] == 592
    # kinds are exactly the pinned set (Phase 7 adds FText-Ascii and the
    # non-JP BcString glyphs; ZenName joins for zen games only)
    for scan in (st_scan, ef_scan):
        kinds = set(e.kind for e in scan["entries"])
        assert kinds <= {"FText-Base", "FText-Ascii", "FString", "BcString",
                         "ZenName"}
    assert st_scan["stats"]["by_kind"]["BcString"] == 9
    assert ef_scan["stats"]["by_kind"]["BcString"] == 592
    assert st_scan["stats"]["by_kind"]["FText-Ascii"] == 91
    assert ef_scan["stats"]["by_kind"]["FText-Ascii"] == 2796
    # every FText-Ascii source is pure ASCII and non-empty (emission rule)
    for scan in (st_scan, ef_scan):
        for e in scan["entries"]:
            if e.kind == "FText-Ascii":
                assert e.source and all(ord(c) < 0x80 for c in e.source)
    # ZenName: ST name batches are collected JP-gated and risk-tagged;
    # EF (legacy, no name batch) collects none
    assert not [e for e in ef_scan["entries"] if e.kind == "ZenName"]
    st_zn = [e for e in st_scan["entries"] if e.kind == "ZenName"]
    assert st_zn and all(e.meta["risk"] in ("yellow", "red") for e in st_zn)
    opt = [e for e in st_zn if e.asset.endswith("DT_オプション.uasset")]
    assert {e.source for e in opt if e.meta["risk"] == "yellow"} >= {
        "ゲームクオリティ-高", "ゲームクオリティ-中", "ゲームクオリティ-低",
        "解像度-1920×1080", "フルスクリーン", "ボーダレスウィンドウ"}
    assert {e.source for e in opt if e.meta["risk"] == "red"} == {
        "DT_オプション", "/Game/common/データ/DT_オプション"}
    # user-review round 2: mesh sub-object identifiers are RED —
    # numbered variant tails (bones/material slots/morphs) and names
    # shared across many packages' batches (sockets, morph/anim switch
    # names); the displayed option keys stay yellow
    st_risk = {e.source: e.meta["risk"] for e in st_zn}
    for structural in ("マテリアル_001", "スカート_001_L", "スカート_001_R",
                       "スカート前_001_L", "スカート後ろ_001"):
        assert st_risk.get(structural) == "red", structural
    for displayed in ("ゲームクオリティ-高", "解像度-1920×1080",
                      "フルスクリーン", "ウィンドウ", "ボーダレスウィンドウ"):
        assert st_risk.get(displayed) == "yellow", displayed
    for e in opt:
        assert e.meta["encoding"] == "wide"
        assert e.property_path[0] == "<name>"
    # the menu strings the user asked for are collected (EF WG_Title)
    title = [e for e in ef_scan["entries"]
             if e.asset.endswith("WG_Title.uexp") and e.kind == "FText-Ascii"]
    assert {e.source for e in title} >= {"New Game", "Option", "Credit",
                                         "Game End"}


# --------------------------------------------------------------------------
# synthetic, game-independent pins

def test_property_tag_walk_pins_named_path():
    """When export data IS tag-serialized (neither game cooks it this way,
    but the walk must try first), a TextProperty's frame gets the walked
    property name instead of the fallback path."""
    names = ["Text", "TextProperty", "None"]
    frame = _frame_bytes("A" * 32, "テスト本文")
    # FPropertyTag: FName(0) name | FName(1) type | i64 size | i32 array
    # | u8 has_guid | value ...
    tag = struct.pack("<ii", 0, 0) + struct.pack("<ii", 1, 0) \
        + struct.pack("<q", len(frame)) + struct.pack("<i", 0) + b"\x00"
    # terminator: FName "None" (index 2) — walk stops there
    term = struct.pack("<ii", 2, 0)
    pkg = _legacy_pkg(tag + frame + term, names)
    entries, warnings = textscan.scan_text(pkg)
    assert len(entries) == 1
    e = entries[0]
    assert e.kind == "FText-Base"
    assert e.property_path == ["Text"]
    assert e.source == "テスト本文"
    assert e.key == "A" * 32
    assert e.namespace == ""
    assert e.offset == len(tag)                # serial_offset - ths == 0
    assert e.class_name == "DataTable"
    assert warnings == []


def test_fallback_path_when_tags_absent():
    """Cooked data (both real games): tagless serialization — the walk
    resolves nothing and frames carry the honest ["<frame>", N] path with
    the export-data-relative offset."""
    preamble = b"\x00\x03\x63\xfe\xff\xff\x00\x00\x00\x00" \
               + b"\x00" * 18 + b"\x80\x13\xc0\x01\x00\x00\x00\x00\x00"
    frame = _frame_bytes("B" * 32, "明日香")
    pkg = _legacy_pkg(preamble + frame, ["Some", "Names"])
    entries, _ = textscan.scan_text(pkg)
    assert len(entries) == 1
    e = entries[0]
    assert e.property_path == ["<frame>", str(len(preamble))]
    assert e.offset == len(preamble)           # serial_offset - ths == 0


def test_chained_fstring_emission():
    """DataTable cell streams: a JP plain FString chained behind another
    valid FString (the observed [table][key][value] layout) is emitted as
    kind=FString; a lone (unchained) JP FString is NOT (context unpinned)."""
    chained = _fstring_bytes("EF_Table") + _fstring_bytes("おにぎり_Name") \
        + _fstring_bytes("おにぎり")
    # separator: 6 zero bytes are no FString prefix/terminator chain — the
    # lone JP string that follows has no valid FString ending at its prefix
    lone = _fstring_bytes("どこかのぶんしょう")          # no predecessor
    pkg = _legacy_pkg(chained + b"\x00" * 6 + lone, ["N"])
    entries, warnings = textscan.scan_text(pkg)
    assert [e.kind for e in entries] == ["FString", "FString"]
    assert [e.source for e in entries] == ["おにぎり_Name", "おにぎり"]
    assert all(e.key == "" and e.namespace == "" for e in entries)
    assert entries[0].offset < entries[1].offset
    assert warnings == []


def test_shape_w_excluded_with_warning():
    """Widget postfix strings ([text][00 00][1f][32-hex][00]) are detected
    but NEVER emitted — no length field exists and the surrounding bytes
    are structurally unexplained (pinning impossible from bytes); the
    package reports the shape-W-sites warning class instead."""
    text = "いらっしゃ～い".encode("utf-16-le")
    site = text + b"\x00\x00\x1f" + b"C" * 32 + b"\x00"
    pkg = _legacy_pkg(site, ["N"])
    entries, warnings = textscan.scan_text(pkg)
    assert entries == []
    assert warnings == ["shape-W-sites: 1"]


def test_ascii_source_frame_emitted_as_ftext_ascii():
    """Grammar-shape FText frame with a POSITIVE-length ANSI source
    ("Start"): collected as an FText-Ascii row since Phase 7 (the
    main menus are exactly this shape)."""
    site = (b"\x01\x00\x00\x00\x00" + struct.pack("<i", 33)
            + b"D" * 32 + b"\x00" + struct.pack("<i", 6) + b"Start\x00")
    pkg = _legacy_pkg(site, ["N"])
    entries, warnings = textscan.scan_text(pkg)
    assert len(entries) == 1
    e = entries[0]
    assert e.kind == "FText-Ascii" and e.source == "Start"
    assert e.key == "D" * 32
    assert warnings == []


def test_junk_data_never_aborts():
    """Arbitrary junk export data: no raise, no entries (or only fully
    validated ones), warnings may explain — the zero-abort contract."""
    junk = bytes(range(256)) * 4 + b"\x01\x00\x00\x00\x00\x21\x00\x00\x00" \
        + b"0" * 32 + b"\x00\x7f\xff\xff\xff" + b"\xff" * 640
    pkg = _legacy_pkg(junk, ["N"])
    entries, warnings = textscan.scan_text(pkg)      # must not raise
    assert entries == []
    assert isinstance(warnings, list)
