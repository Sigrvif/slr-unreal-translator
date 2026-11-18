"""Task 2 (Phase 4a): package-size propagation — patched packages whose
lengths changed must load correctly, on both formats.

Discovery driving this module (probed game-wide, read-only):

- ZEN (ST, all 1,359 packages): export data slices are PERFECTLY
  contiguous from header_size to len(payload)-4 (0 gaps, no alignment
  padding — 4,124 serial offsets are not even 4-aligned), cell maps are
  empty everywhere, and no text-bearing package has bulk-data-map
  entries. A patch inside export k therefore shifts every LATER export
  by the accumulated byte delta: the fix is Task 1's splice + in-place
  i64 rewrites of each FExportMapEntry's SerialOffset/SerialSize (the
  72-byte table is fixed-stride) + inline bulk-map offset shifts when
  present. No summary field encodes the payload length.
- LEGACY (EF, all 7,656 pairs): exports are contiguous over
  [ths, ths + len(uexp) - 4) with 0 gaps, ths == len(uasset) for every
  pair, and the summary's bulk_data_start_offset == len(uasset) +
  len(uexp) - 4 (value-present in 7,656/7,656 .uasset halves). The fix
  is the .uexp splice + in-place i64 rewrites of each FObjectExport's
  SerialSize/SerialOffset (92-byte stride at the EF gate) + the
  bulk_data_start_offset update.
- CONTAINER HEADER (ST chunk type 6, "IoCn" version 5 =
  SoftPackageReferencesOffset): parses and re-serializes byte-exactly,
  but version-5 store entries carry NO export_bundles_size — each entry
  is exactly two TArrayView headers (imported packages + shader map
  hashes; 1,359 x 16 B + 30,032 array bytes = the 51,776-byte entry
  buffer, consumed EXACTLY; whole 62,712-byte chunk consumed exactly).
  Export-bundle sizes are the TOC chunk lengths in this version —
  asking for a size update is a named refusal, not a guess.

Golden coverage choice (measured, documented in the Task 2 report): FULL
sweeps on both games — ST all 1,359 packages (~2 s), EF all 7,656 pairs
(~1 min, slow-marked).
"""
import os
import re
import struct

import pytest

from backend.ut import blake3, iostore, oodle, patchtext, payload, uasset, zen
from backend.ut.textscan import TextEntry

# --------------------------------------------------------------------------
# shared helpers (local mirrors — no imports from other test modules)

ST_GOLDEN = "sample_a/Content/common/データ/DT_学校会話.uasset"
EF_GOLDEN_STEM = ("SampleGameB/Content/SampleGameB/DataTable/EroEvent/"
                  "DT_EroEventTalkTable")
PAKS = os.path.join("sample_a", "Content", "Paks")

# flags u32=1 | history u8=0 | FString len 33 | 32 UPPER hex | NUL | i32 len
_FRAME = re.compile(
    rb"\x01\x00\x00\x00\x00\x21\x00\x00\x00([0-9A-F]{32})\x00(....)", re.S)


def _ftext(key: str, text: str) -> bytes:
    units = text.encode("utf-16-le") + b"\x00\x00"
    return (b"\x01\x00\x00\x00\x00"
            + struct.pack("<i", 33) + key.encode("ascii") + b"\x00"
            + struct.pack("<i", -(len(text) + 1)) + units)


def _fstr(text: str) -> bytes:
    units = text.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<i", -(len(text) + 1)) + units


def _entry(asset: str, offset: int, kind: str, source: str,
           key: str = "") -> TextEntry:
    return TextEntry(asset=asset, export_index=0, class_name="DataTable",
                     property_path=["<frame>", "0"], kind=kind,
                     namespace="", key=key, source=source, offset=offset)


def _read_utf16(data: bytes, len_pos: int) -> str:
    (ln,) = struct.unpack_from("<i", data, len_pos)
    assert ln < 0
    units = struct.unpack_from(f"<{-ln}H", data, len_pos + 4)
    assert units[-1] == 0
    return "".join(map(chr, units[:-1]))


def _frames(data: bytes) -> "list[tuple[int, int, str]]":
    """(start, end, key) of FText-Base frames in a raw payload."""
    out = []
    for m in _FRAME.finditer(data):
        (ln,) = struct.unpack_from("<i", m.group(2), 0)
        if ln >= 0:
            continue
        end = m.end() + 2 * (-ln)
        if end > len(data):
            continue
        out.append((m.start(), end, m.group(1).decode()))
    return out


# --------------------------------------------------------------------------
# synthetic zen package builder (multi-export layout for the pins below)

_NULL_POI = 0xC000000000000000            # FPackageObjectIndex null kind


def _zen_name_batch(names: "list[str]") -> bytes:
    hashes = b"\x00" * 8 * len(names)
    lens = b"".join(struct.pack(">h", len(n.encode("ascii"))) for n in names)
    body = b"".join(n.encode("ascii") for n in names)
    return struct.pack("<IIQ", len(names), 0, 0xC1640000) + hashes + lens + body


def _zen_export_entry(serial_offset: int, serial_size: int,
                      name_index: int) -> bytes:
    return struct.pack("<QQIIQQQQQI", serial_offset, serial_size,
                       name_index, 0, _NULL_POI, _NULL_POI, _NULL_POI,
                       _NULL_POI, 0, 0) + b"\x00\x00\x00\x00"


def _zen_bulk_entry(so: int, ss: int) -> bytes:
    return struct.pack("<qqqIB3s", so, 0, ss, 0, 0, b"\x00\x00\x00")


def _build_zen(export_datas: "list[bytes]",
               bulk: "list[tuple[int, int]]" = ()) -> bytes:
    """Minimal but layout-exact zen package: N named exports at contiguous
    serial offsets, empty import map/cell maps, canonical bundle entries
    (Create+Serialize per export) and dependency headers."""
    n = len(export_datas)
    names = ["Pkg"] + [f"E{i}" for i in range(n)]
    nb = _zen_name_batch(names)
    pos = 52 + 8 + len(nb)                       # summary + cell offs + names
    bulk_blob = b""
    if bulk:
        bulk_blob = struct.pack("<Qq", 0, 32 * len(bulk)) \
            + b"".join(_zen_bulk_entry(so, ss) for so, ss in bulk)
        pos += len(bulk_blob)
    ipeh = import_off = pos                      # 0 hashes, 0 imports
    export_off = import_off
    cell_import_off = cell_export_off = export_off + 72 * n
    bundle_off = cell_export_off
    dep_h_off = bundle_off + 8 * 2 * n
    dep_e_off = dep_h_off + 20 * n
    ipn_off = dep_e_off
    header_size = ipn_off + 4
    summary = struct.pack(
        "<IIIIIIiiiiiii",
        0, header_size, 0, 0, 0, header_size,
        ipeh, import_off, export_off, bundle_off, dep_h_off, dep_e_off, ipn_off)
    bundles = b"".join(struct.pack("<II", i, cmd)
                       for i in range(n) for cmd in (0, 1))
    offsets = [sum(len(d) for d in export_datas[:i]) for i in range(n)]
    data = (summary + struct.pack("<ii", cell_import_off, cell_export_off)
            + nb + bulk_blob
            + b"".join(_zen_export_entry(offsets[i], len(d), 1 + i)
                       for i, d in enumerate(export_datas))
            + bundles
            + b"\x00" * (20 * n)
            + struct.pack("<I", 0)                        # empty ipn batch
            + b"".join(export_datas)
            + struct.pack("<I", 0x9E2A83C1))
    assert len(data) == header_size + sum(len(d) for d in export_datas) + 4
    return data


ZASSET = "Synthetic/Zen.uasset"


def _zen_three_export_package():
    """3 exports; export0 holds a patchable FText frame, export1 a FString,
    export2 a second FText frame (must survive shifting byte-identically)."""
    k1, k2 = "A" * 32, "B" * 32
    f0 = b"HEAD0\x00" + _ftext(k1, "短い")
    f1 = b"MID1" + _fstr("真ん中") + b"TAIL1"
    f2 = b"\x11\x22" + _ftext(k2, "最後") + b"\x33"
    data = _build_zen([f0, f1, f2])
    pkg = zen.parse_zen(data, ZASSET)
    assert [ex.serial_offset for ex in pkg.exports] == [0, len(f0),
                                                        len(f0) + len(f1)]
    entries = [_entry(ZASSET, data.find(_ftext(k1, "短い")), "FText-Base",
                      "短い", k1),
               _entry(ZASSET, data.find(_fstr("真ん中")), "FString", "真ん中"),
               _entry(ZASSET, data.find(_ftext(k2, "最後")), "FText-Base",
                      "最後", k2)]
    return data, entries, pkg


# --------------------------------------------------------------------------
# synthetic, game-independent pins — zen

def test_zen_null_patch_identity():
    """Null patch round-trips the synthetic package byte-identically —
    both with no entries and with entries-but-no-translations."""
    data, entries, _ = _zen_three_export_package()
    assert zen.patch_zen_package(data, [], {}) == data
    assert zen.patch_zen_package(data, entries, {}) == data


def test_zen_grow_shifts_later_exports():
    """A GROWING patch in export 0: export 0's serial_size grows by the
    shift, exports 1 and 2 keep their sizes but their serial_offsets move
    by the same shift; the header bytes are untouched; later exports'
    data is byte-identical; the trailing tag survives."""
    data, entries, pkg = _zen_three_export_package()
    old = [(ex.serial_offset, ex.serial_size) for ex in pkg.exports]
    new_text = "A MUCH LONGER REPLACEMENT STRING"
    assert len(new_text) > len("短い")
    out = zen.patch_zen_package(data, entries, {0: new_text})
    shift = 2 * (len(new_text) - 2)
    assert len(out) == len(data) + shift
    # trailing package tag preserved at the new end
    assert struct.unpack_from("<I", out, len(out) - 4)[0] == 0x9E2A83C1
    p2 = zen.parse_zen(out, ZASSET)              # re-parse: fully consistent
    assert [(ex.serial_offset, ex.serial_size) for ex in p2.exports] == [
        (0, old[0][1] + shift),
        (old[1][0] + shift, old[1][1]),
        (old[2][0] + shift, old[2][1])]
    # later exports' DATA byte-identical (moved, not modified)
    for i in (1, 2):
        assert p2.exports[i].data == pkg.exports[i].data
    # export 0's frame re-reads the new text
    assert _read_utf16(p2.exports[0].data, 6 + 42) == new_text
    # exact bytes: the header equals the ORIGINAL header with only the
    # four serial i64s rewritten (export 0: size; exports 1/2: offsets)
    emap = pkg.export_map_offset
    expected = bytearray(data[:pkg.header_size])
    struct.pack_into("<q", expected, emap + 8, old[0][1] + shift)
    struct.pack_into("<q", expected, emap + 72, old[1][0] + shift)
    struct.pack_into("<q", expected, emap + 144, old[2][0] + shift)
    assert out[:pkg.header_size] == bytes(expected)


def test_zen_patches_in_two_exports_net_shift():
    """Grow in export 0 AND grow in export 1: export 2's offset moves by
    the SUM; export 1's size takes only its own shift; the untouched
    frame in export 2 is still readable at its shifted offset."""
    data, entries, pkg = _zen_three_export_package()
    t0, t1 = "FIRST GROWN LONG STRING", "SECOND ALSO GROWN"
    out = zen.patch_zen_package(data, entries, {0: t0, 1: t1})
    p2 = zen.parse_zen(out, ZASSET)
    s0 = 2 * (len(t0) - 2)
    s1 = 2 * (len(t1) - 3)
    assert p2.exports[0].serial_size == pkg.exports[0].serial_size + s0
    assert p2.exports[1].serial_size == pkg.exports[1].serial_size + s1
    assert p2.exports[2].serial_offset == pkg.exports[2].serial_offset + s0 + s1
    assert p2.exports[2].serial_size == pkg.exports[2].serial_size
    # untouched frame in export 2: same key + source, shifted position
    k2_frames = [f for f in _frames(out) if f[2] == "B" * 32]
    assert len(k2_frames) == 1
    shifted = [e for e in patchtext.rebase_entries(entries, {
        "patched": [
            {"entry_index": 0, "offset": entries[0].offset, "shift": s0},
            {"entry_index": 1, "offset": entries[1].offset, "shift": s1}]})
        if e.kind == "FText-Base" and e.key == "B" * 32]
    assert shifted[0].offset == k2_frames[0][0]
    assert _read_utf16(out, shifted[0].offset + 42) == "最後"


def test_zen_boundary_inside_patch_rejected():
    """A patchable range that straddles an export boundary is a named
    error (a boundary cannot sit inside a spliced range); the caller's
    payload is untouched."""
    k = "C" * 32
    frame = _ftext(k, "跨ぎ")                     # 3-char frame
    # export 0 = first part of the frame, export 1 = the rest: the frame
    # grammar is contiguous in the payload so patch_payload accepts it,
    # but the export boundary falls inside the patchable range.
    half = 48                                   # inside the patchable range [42, 54)
    datas = [frame[:half], frame[half:] + b"TAIL"]
    data = _build_zen(datas)
    pkg = zen.parse_zen(data, ZASSET)
    assert pkg.exports[0].serial_size == half
    entries = [_entry(ZASSET, data.find(frame), "FText-Base", "跨ぎ", k)]
    with pytest.raises(zen.ZenError, match="export boundary"):
        zen.patch_zen_package(data, entries, {0: "REPLACEMENT"})
    assert data == _build_zen(datas)             # caller bytes untouched


def test_zen_inline_bulk_entry_shifts():
    """A bulk-data-map entry whose region is INSIDE the package payload
    (inline bulk data) shifts with the content; one pointing outside the
    payload (an external bulk chunk) is untouched."""
    k = "D" * 32
    frame = _ftext(k, "本文")
    payload_region = frame + b"\x7f" * 16        # inline bulk bytes at +len
    datas = [payload_region, b"SECOND"]
    data = _build_zen(datas, bulk=[(len(frame), 16),          # inline
                                   (10 ** 9, 4096)])          # external
    pkg = zen.parse_zen(data, ZASSET)
    assert len(pkg.bulk_map) == 2
    entries = [_entry(ZASSET, data.find(frame), "FText-Base", "本文", k)]
    new_text = "GROWN REPLACEMENT TEXT"
    out = zen.patch_zen_package(data, entries, {0: new_text})
    p2 = zen.parse_zen(out, ZASSET)
    shift = 2 * (len(new_text) - 2)
    # inline entry moved by the shift, size kept; external untouched
    assert p2.bulk_map[0].serial_offset == pkg.bulk_map[0].serial_offset + shift
    assert p2.bulk_map[0].serial_size == 16
    assert p2.bulk_map[1].serial_offset == pkg.bulk_map[1].serial_offset
    assert p2.bulk_map[1].serial_size == 4096
    # the inline bulk bytes themselves moved byte-identically
    old_blob = pkg.exports[0].data[len(frame):len(frame) + 16]
    at = len(frame) + shift
    assert p2.exports[0].data[at:at + 16] == old_blob
    assert p2.exports[1].data == pkg.exports[1].data


# --------------------------------------------------------------------------
# synthetic EF legacy pair builder

def _legacy_fstring(s: str) -> bytes:
    if not s:
        return struct.pack("<i", 0)
    b = s.encode("ascii")
    return struct.pack("<i", len(b) + 1) + b + b"\x00"


def _legacy_export_entry(size: int, offset: int, name_index: int) -> bytes:
    # exact _read_exports field order at ue5=1013 + unversioned props:
    # 96-byte stride (incl. the is_inherited u32 of UE5 >= 1006)
    return struct.pack("<iiiiii", 0, 0, 0, 0, name_index, 0) \
        + struct.pack("<I", 0) + struct.pack("<qq", size, offset) \
        + struct.pack("<III", 0, 0, 0) + struct.pack("<I", 0) \
        + struct.pack("<IIII", 0, 0, 0, 0) \
        + struct.pack("<5i", 0, 0, 0, 0, 0)


def _legacy_summary(n, names_off, exports_off, depends_off, bulk_start) -> bytes:
    guid = bytes(range(16))
    return (struct.pack("<ii", n + 1, names_off)
            + struct.pack("<ii", 0, 0)                    # soft object paths
            + struct.pack("<ii", 0, 0)                    # gatherable
            + struct.pack("<ii", n, exports_off)          # exports
            + struct.pack("<ii", 0, exports_off)          # imports
            + struct.pack("<i", depends_off)
            + struct.pack("<ii", 0, 0)                    # soft pkg refs
            + struct.pack("<i", 0) + struct.pack("<i", 0)  # searchable, thumb
            + guid
            + struct.pack("<I", 1) + struct.pack("<ii", 0, 1)  # generations
            + struct.pack("<II", 0, 0) + struct.pack("<i", 0)   # engine ver
            + struct.pack("<II", 0, 0) + struct.pack("<i", 0)
            + struct.pack("<I", 0) + struct.pack("<i", 0)  # comp + chunks
            + struct.pack("<II", 7, 0)                    # package source
            + struct.pack("<I", 0)                        # additional pkgs
            + struct.pack("<i", 0)                        # asset registry
            + struct.pack("<q", bulk_start)
            + struct.pack("<i", 0) + struct.pack("<I", 0)  # tile, chunk ids
            + struct.pack("<ii", 0, 0)                    # preload deps
            + struct.pack("<i", 1)                        # names referenced
            + struct.pack("<q", -1) + struct.pack("<i", -1))


def _build_legacy(export_datas: "list[bytes]") -> "tuple[bytes, bytes]":
    """Versioned legacy pair at the UE-5.5 gate (ue5=1013, flags
    FilterEditorOnly|Cooked|UnversionedProperties): name map with N+1
    names, no imports, N exports at the exact 92-byte stride, export
    serial offsets absolute in the uasset+uexp virtual stream."""
    n = len(export_datas)
    names = ["/Game/Synth"] + [f"Exp{i}" for i in range(n)]
    name_entries = b"".join(_legacy_fstring(nm) + struct.pack("<HH", 0, 0)
                            for nm in names)
    pkg_name = _legacy_fstring("/Game/Synth")
    vi = struct.pack("<iii", -8, 864, 522) + struct.pack("<i", 1013) \
        + struct.pack("<i", 0)                    # ue5, licensee
    customs = struct.pack("<I", 0)
    ux_body = b"".join(export_datas)
    head_fixed = (4 + len(vi) + len(customs) + 4 + len(pkg_name) + 4)
    names_off = head_fixed + len(_legacy_summary(0, 0, 0, 0, 0))
    exports_off = names_off + len(name_entries)
    ths = depends_off = exports_off + n * 96
    ux = ux_body + struct.pack("<I", 0x9E2A83C1)
    bulk_start = ths + len(ux) - 4
    entries = b""
    at = ths
    for i, d in enumerate(export_datas):
        entries += _legacy_export_entry(len(d), at, 1 + i)
        at += len(d)
    ua = (struct.pack("<I", 0x9E2A83C1) + vi + customs
          + struct.pack("<i", ths) + pkg_name + struct.pack("<I", 0x80042200)
          + _legacy_summary(n, names_off, exports_off, depends_off, bulk_start)
          + name_entries + entries)
    assert len(ua) == ths
    return ua, ux


LUASSET, LUEXP = "Synthetic/L.uasset", "Synthetic/L.uexp"


def _legacy_two_export_pair():
    """2 exports; export0 holds a patchable FText frame, export1 a second
    frame (must survive shifting byte-identically)."""
    k1, k2 = "E" * 32, "F" * 32
    e0 = b"HEAD\x00" + _ftext(k1, "短い")
    e1 = b"\xaa\xbb" + _ftext(k2, "後続") + b"\xcc"
    ua, ux = _build_legacy([e0, e1])
    pkg = uasset.parse_legacy(ua, ux, LUASSET)
    assert [ex.serial_offset for ex in pkg.exports] == [
        pkg.total_header_size, pkg.total_header_size + len(e0)]
    entries = [_entry(LUEXP, ux.find(_ftext(k1, "短い")), "FText-Base",
                      "短い", k1),
               _entry(LUEXP, ux.find(_ftext(k2, "後続")), "FText-Base",
                      "後続", k2)]
    return ua, ux, entries, pkg


# --------------------------------------------------------------------------
# synthetic, game-independent pins — legacy

def test_legacy_null_patch_identity():
    """Null patch round-trips the synthetic pair byte-identically (both
    files) — with entries and without."""
    ua, ux, entries, _ = _legacy_two_export_pair()
    assert uasset.patch_legacy_pair(ua, ux, [], {}) == (ua, ux)
    assert uasset.patch_legacy_pair(ua, ux, entries, {}) == (ua, ux)


def test_legacy_grow_shifts_later_exports():
    """A GROWING patch in export 0: export 0's SerialSize grows, export
    1's SerialOffset moves by the shift, the summary's
    bulk_data_start_offset moves by the net shift, the .uexp tail tag
    survives, and the .uasset differs ONLY at those three i64 fields."""
    ua, ux, entries, pkg = _legacy_two_export_pair()
    new_text = "A MUCH LONGER LEGACY REPLACEMENT"
    nua, nux = uasset.patch_legacy_pair(ua, ux, entries, {0: new_text})
    shift = 2 * (len(new_text) - 2)
    assert len(nux) == len(ux) + shift
    assert struct.unpack_from("<I", nux, len(nux) - 4)[0] == 0x9E2A83C1
    p2 = uasset.parse_legacy(nua, nux, LUASSET)  # re-parse: consistent
    assert p2.exports[0].serial_size == pkg.exports[0].serial_size + shift
    assert p2.exports[0].serial_offset == pkg.exports[0].serial_offset
    assert p2.exports[1].serial_offset == pkg.exports[1].serial_offset + shift
    assert p2.exports[1].serial_size == pkg.exports[1].serial_size
    assert p2.exports[1].data == pkg.exports[1].data
    assert p2.bulk_data_start_offset == pkg.bulk_data_start_offset + shift
    # exact-bytes: the .uasset equals the original with ONLY the three
    # i64 fields rewritten (positions captured by the parser)
    expected = bytearray(ua)
    (size0_pos, off0_pos) = pkg._export_field_positions[0]
    (size1_pos, off1_pos) = pkg._export_field_positions[1]
    struct.pack_into("<q", expected, size0_pos, p2.exports[0].serial_size)
    struct.pack_into("<q", expected, off1_pos, p2.exports[1].serial_offset)
    struct.pack_into("<q", expected, pkg._bulk_data_start_offset_pos,
                     p2.bulk_data_start_offset)
    assert nua == bytes(expected)


def test_legacy_bulk_pin_violation_rejected():
    """If a pair's bulk_data_start_offset does not follow the game-wide
    pin (== len(uasset)+len(uexp)-4), patching refuses with a named
    error rather than guessing what to write there."""
    ua, ux, entries, pkg = _legacy_two_export_pair()
    bad = bytearray(ua)
    struct.pack_into("<q", bad, pkg._bulk_data_start_offset_pos, 0)
    with pytest.raises(uasset.LegacyError, match="bulk_data_start_offset"):
        uasset.patch_legacy_pair(bytes(bad), ux, entries, {0: "NEW TEXT"})
    # the pristine pair still patches fine (rejection left it untouched)
    nua, nux = uasset.patch_legacy_pair(ua, ux, entries, {0: "NEW TEXT"})
    assert len(nux) > len(ux)


def test_legacy_boundary_inside_patch_rejected():
    """A patchable range straddling an export boundary = named error."""
    k = "G" * 32
    frame = _ftext(k, "跨ぎ")
    half = 48                                   # inside the patchable range
    datas = [frame[:half], frame[half:] + b"TAIL"]
    ua, ux = _build_legacy(datas)
    entries = [_entry(LUEXP, ux.find(frame), "FText-Base", "跨ぎ", k)]
    with pytest.raises(uasset.LegacyError, match="export boundary"):
        uasset.patch_legacy_pair(ua, ux, entries, {0: "REPLACEMENT"})


# --------------------------------------------------------------------------
# real data — ST zen goldens + propagation

def test_st_zen_null_golden_all_packages(st_src, st_scan):
    """ALL 1,359 ST packages survive a null-patch round-trip through
    patch_zen_package byte-identically (measured: full sweep ~2 s — no
    sampling needed)."""
    by_asset = {}
    for e in st_scan["entries"]:
        by_asset.setdefault(e.asset, []).append(e)
    names = [n for n in payload.list_files(st_src)
             if n.lower().endswith((".uasset", ".umap"))]
    assert len(names) == 1359
    for n in names:
        data = payload.read_file(st_src, n)
        entries = sorted(by_asset.get(n, []), key=lambda e: e.offset)
        assert zen.patch_zen_package(data, entries, {}) == data


def test_st_dt_school_grow_propagation(st_src, st_scan):
    """The real golden: 3 GROWING translations into DT_学校会話 (one
    export covering all 473 frames) — parse_zen re-reads the patched
    package with the export's serial_size grown by exactly the net
    shift, header byte-identical, all frames re-found (3 changed
    sources), tag intact."""
    data = payload.read_file(st_src, ST_GOLDEN)
    # the asset also carries 2 ZenName rows (its red self-names) — this
    # test pins export-frame propagation, so only FText-Base rides
    entries = sorted((e for e in st_scan["entries"] if e.asset == ST_GOLDEN
                      and e.kind == "FText-Base"),
                     key=lambda e: e.offset)
    assert len(entries) == 473
    translations = {0: "ZERO GROWN LONG", 40: "FORTY GROWN MUCH LONGER",
                    236: "TWO THIRTY SIX GROWN MUCH LONGER"}
    assert all(len(entries[i].source) < len(translations[i])
               for i in translations)
    out = zen.patch_zen_package(data, entries, translations)
    pkg_old = zen.parse_zen(data, ST_GOLDEN)
    pkg_new = zen.parse_zen(out, ST_GOLDEN)
    net = sum(2 * (len(t) - len(entries[i].source))
              for i, t in translations.items())
    assert len(out) == len(data) + net
    assert len(pkg_old.exports) == 1
    assert pkg_new.exports[0].serial_size \
        == pkg_old.exports[0].serial_size + net
    assert pkg_new.exports[0].serial_offset == 0
    # header identical OUTSIDE the export map (whose SerialSize i64 moves)
    emap = pkg_old.export_map_offset
    emap_end = emap + 72 * len(pkg_old.exports)
    assert out[:emap] == data[:emap]
    assert out[emap_end:pkg_old.header_size] == data[emap_end:pkg_old.header_size]
    assert struct.unpack_from("<I", out, len(out) - 4)[0] == 0x9E2A83C1
    # all 473 frames re-found at rebased offsets; exactly 3 changed sources
    new_frames = _frames(out)
    rebased = patchtext.rebase_entries(entries, {
        "patched": [{"entry_index": i, "offset": entries[i].offset,
                     "shift": 2 * (len(t) - len(entries[i].source))}
                    for i, t in translations.items()]})
    assert len(new_frames) == 473
    changed = 0
    for i, (f, e) in enumerate(zip(new_frames, rebased)):
        assert f[0] == e.offset
        text = _read_utf16(out, f[0] + 42)
        assert text == translations.get(i, entries[i].source)
        changed += text != entries[i].source
    assert changed == 3


def _st_container_paths(game_paths):
    return (os.path.join(game_paths["sample_a"], PAKS,
                         "sample_a-Windows.utoc"),
            os.path.join(game_paths["sample_a"], PAKS,
                         "sample_a-Windows.ucas"))


def _st_container_header(utoc_path, ucas_path):
    with open(utoc_path, "rb") as f:
        toc = iostore.parse_toc(f.read())
    idx = next(i for i, e in enumerate(toc.entries)
               if e.chunk_type == iostore.CHUNKTYPE_CONTAINER_HEADER)
    data = iostore.read_entry(toc, ucas_path, idx, dll=oodle.get_dll())
    return toc, idx, data


def test_st_container_header_roundtrip_golden(game_paths):
    """The container header chunk parses structurally and re-serializes
    BYTE-IDENTICALLY (the null-patch golden for the structure-aware
    chunk-6 path); version 5, 1,359 store entries, no size fields."""
    utoc_path, ucas_path = _st_container_paths(game_paths)
    toc, idx, data = _st_container_header(utoc_path, ucas_path)
    ch = iostore.parse_container_header(data)
    assert ch.version == 5
    assert ch.container_id == toc.header.container_id
    assert len(ch.package_ids) == 1359
    assert ch.package_ids == sorted(ch.package_ids)
    assert len(ch.store_entries) == 1359
    assert iostore.serialize_container_header(ch) == data


def test_st_container_header_size_update_refused(game_paths, tmp_path):
    """Version-5 store entries carry NO export_bundles_size (byte-exact
    consumption proves it) — a size-update request is a NAMED refusal,
    raised before anything is written."""
    utoc_path, ucas_path = _st_container_paths(game_paths)
    toc, idx, data = _st_container_header(utoc_path, ucas_path)
    ch = iostore.parse_container_header(data)
    with pytest.raises(ValueError, match="export_bundles_size"):
        iostore.write_iostore(utoc_path, ucas_path,
                              os.path.join(tmp_path, "o.utoc"),
                              os.path.join(tmp_path, "o.ucas"),
                              container_header_patches={
                                  ch.package_ids[0]: 12345})
    assert os.listdir(str(tmp_path)) == []      # nothing written


@pytest.mark.slow
def test_st_write_propagation_with_container_header_verify(game_paths,
                                                            tmp_path,
                                                            st_src, st_scan):
    """End-to-end ST propagation: the grown DT_学校会話 payload is written
    through write_iostore (chunk_patches) WITH the structure-aware
    container-header verification (container_header_patches={}); the
    rebuilt container re-parses: the package entry's length/hash/data
    match the patched payload, the container-header entry is unchanged,
    and its chunk still round-trips byte-identically."""
    utoc_path, ucas_path = _st_container_paths(game_paths)
    toc, ch_idx, ch_data = _st_container_header(utoc_path, ucas_path)
    assert toc.entries[ch_idx].io_hash == blake3.hash20(ch_data)

    data = payload.read_file(st_src, ST_GOLDEN)
    entries = sorted((e for e in st_scan["entries"] if e.asset == ST_GOLDEN),
                     key=lambda e: e.offset)
    translations = {0: "ZERO GROWN LONG", 40: "FORTY GROWN MUCH LONGER"}
    patched = zen.patch_zen_package(data, entries, translations)
    assert len(patched) > len(data)

    pkg_idx = next(i for name, i in toc.file_map.items()
                   if name.endswith(ST_GOLDEN.split("sample_a/")[-1]))
    ou, oc = os.path.join(tmp_path, "o.utoc"), os.path.join(tmp_path, "o.ucas")
    iostore.write_iostore(utoc_path, ucas_path, ou, oc,
                          chunk_patches={pkg_idx: patched},
                          container_header_patches={})
    with open(ou, "rb") as f:
        toc3 = iostore.parse_toc(f.read())
    dll = oodle.get_dll()
    assert toc3.entries[pkg_idx].length == len(patched)
    assert toc3.entries[pkg_idx].io_hash == blake3.hash20(patched)
    assert iostore.read_entry(toc3, oc, pkg_idx, dll=dll) == patched
    # container-header entry: offset/length/hash unchanged, chunk intact
    assert toc3.entries[ch_idx].offset == toc.entries[ch_idx].offset
    assert toc3.entries[ch_idx].length == toc.entries[ch_idx].length
    assert toc3.entries[ch_idx].io_hash == toc.entries[ch_idx].io_hash
    ch2 = iostore.parse_container_header(
        iostore.read_entry(toc3, oc, ch_idx, dll=dll))
    assert iostore.serialize_container_header(ch2) == ch_data
    # an untouched entry still reads identical through the rebuilt pair
    j = next(j for j in range(len(toc.entries))
             if j not in (pkg_idx, ch_idx) and toc.entries[j].length)
    assert iostore.read_entry(toc3, oc, j, dll=dll) \
        == iostore.read_entry(toc, ucas_path, j, dll=dll)
    # the patched package re-parses as a consistent zen package
    rep = zen.parse_zen(patched, ST_GOLDEN)
    assert rep.exports[0].serial_size == len(patched) - rep.header_size - 4


# --------------------------------------------------------------------------
# real data — EF legacy goldens + propagation

@pytest.mark.slow
def test_ef_null_golden_all_pairs(ef_src):
    """ALL 7,656 EF pairs survive a null-patch round-trip through
    patch_legacy_pair byte-identically (both files), with the structural
    pins (ths == len(uasset); bulk_data_start_offset pin) holding for
    every pair. Measured: ~1 min full sweep — chosen over sampling."""
    files = set(payload.list_files(ef_src))
    heads = sorted(n for n in files if n.endswith((".uasset", ".umap")))
    pairs = 0
    for n in heads:
        stem = n[:-len(".uasset")] if n.endswith(".uasset") \
            else n[:-len(".umap")]
        if (stem + ".uexp") not in files:
            continue
        ua = payload.read_file(ef_src, n)
        ux = payload.read_file(ef_src, stem + ".uexp")
        pkg = uasset.parse_legacy(ua, ux)
        assert pkg.total_header_size == len(ua)
        assert pkg.bulk_data_start_offset == len(ua) + len(ux) - 4
        assert uasset.patch_legacy_pair(ua, ux, [], {}) == (ua, ux)
        pairs += 1
    assert pairs == 7656


def test_ef_dt_eroeventtalk_grow_propagation(ef_src, ef_scan):
    """Real EF propagation: 3 GROWING translations into
    DT_EroEventTalkTable's .uexp (one export covering the stream) — the
    re-parsed pair reads the updated SerialSize, bulk_data_start_offset
    moved by the net shift, the .uasset changed only at those fields,
    and every frame is re-found at its shifted offset."""
    ua = payload.read_file(ef_src, EF_GOLDEN_STEM + ".uasset")
    ux = payload.read_file(ef_src, EF_GOLDEN_STEM + ".uexp")
    pkg = uasset.parse_legacy(ua, ux)
    entries = sorted((e for e in ef_scan["entries"]
                      if e.asset == EF_GOLDEN_STEM + ".uexp"),
                     key=lambda e: e.offset)
    assert len(entries) == 2853             # scan: 1,143 FText-Base +
                                            # 1,710 FText-Ascii (Phase 7)
    # Phase 7: the stream also holds 1,710 FText-Ascii + 100 BcString
    # rows — translate three spread FTEXT-BASE rows so the propagation
    # math stays in the proven 2-byte domain
    ftext_idx = [i for i, e in enumerate(entries) if e.kind == "FText-Base"]
    assert len(ftext_idx) == 1143
    picks = (ftext_idx[0], ftext_idx[len(ftext_idx) // 2], ftext_idx[-1])
    def _grow(i):
        base = f"EF GROW {i} LONG REPLACEMENT"
        return base + "X" * (len(entries[i].source) - len(base) + 1)
    translations = {i: _grow(i) for i in picks}
    assert all(len(entries[i].source) < len(translations[i])
               for i in translations)
    nua, nux = uasset.patch_legacy_pair(ua, ux, entries, translations)
    net = sum(2 * (len(t) - len(entries[i].source))
              for i, t in translations.items())
    assert len(nux) == len(ux) + net
    p2 = uasset.parse_legacy(nua, nux)
    assert len(pkg.exports) == 1
    assert p2.exports[0].serial_size == pkg.exports[0].serial_size + net
    assert p2.bulk_data_start_offset == len(nua) + len(nux) - 4
    assert struct.unpack_from("<I", nux, len(nux) - 4)[0] == 0x9E2A83C1
    # exact bytes: .uasset == original with only the two i64 fields written
    expected = bytearray(ua)
    size0_pos, _ = pkg._export_field_positions[0]
    struct.pack_into("<q", expected, size0_pos, p2.exports[0].serial_size)
    struct.pack_into("<q", expected, pkg._bulk_data_start_offset_pos,
                     p2.bulk_data_start_offset)
    assert nua == bytes(expected)
    # every FText frame re-found at shifted offsets; exactly 3 changed
    # sources (the Phase 7 ascii/bc rows are untouched and interleave, so
    # the frame walk pairs against the FText-Base subset of the rebase)
    new_frames = _frames(nux)
    rebased = patchtext.rebase_entries(entries, {
        "patched": [{"entry_index": i, "offset": entries[i].offset,
                     "shift": 2 * (len(t) - len(entries[i].source))}
                    for i, t in translations.items()]})
    rebased_ftext = [e for e in rebased if e.kind == "FText-Base"]
    assert len(new_frames) == len(rebased_ftext) == 1143
    changed = 0
    # rebase preserves input order, so the FText-Base subset lines up
    # with ftext_idx positionally (TextEntry equality would match
    # duplicate-content rows — never index by value here)
    ftext_positions = [i for i, e in enumerate(entries)
                       if e.kind == "FText-Base"]
    for f, e, i in zip(new_frames, rebased_ftext, ftext_positions):
        assert f[0] == e.offset
        text = _read_utf16(nux, f[0] + 42)
        assert text == translations.get(i, entries[i].source)
        changed += text != entries[i].source
    assert changed == 3
