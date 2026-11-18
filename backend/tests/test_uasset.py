"""Task 4: legacy package parser (SampleGameB, UE5-era legacy .uasset/.uexp).

Invariants pinned against the real game and the Task 2 discovery fixture
(``text_discovery.json`` is ground truth):
  - ALL 7,656 EF pairs (7,555 .uasset + 101 .umap, each with its exact-stem
    .uexp sibling) parse without exceptions; malformed count reported is 0.
  - Golden ``DT_EroEventTalkTable``: exports carry resolved class names
    (DataTable) and its export data slices cover >= 1,060 of the asset's
    1,065 FText frames (the Task 5 bridge).
  - EF-wide: FText frames found INSIDE export slices — 6,359 total, of
    which 6,191 JP in 458 .uexp files (461 .uexp files carry a frame at
    all; the fixture's frame census counts JP-bearing files), and ZERO
    FText frames exist in any .uasset half (shape-B halves are metadata
    only — the parser never slices exports out of a .uasset).
  - Shape B (.uasset-half ``u32 | i32 -N | UTF-16LE`` cells, 1,896 runs
    game-wide): recorded, NOT interpreted — no speculative u32 decoding.

The FText frame detector below is TEST-ONLY (the discovery grammar:
flags u32=1 | history u8=0 | 32-UPPERCASE-hex key | negative-length
UTF-16LE source); production detection belongs to Task 5.
"""
import json
import os
import re
import struct

import pytest

from backend.ut import payload, uasset

EF_TOTAL_PAIRS = 7656              # fixture: uasset_uexp_pairs
EF_TOTAL_FRAMES = 6359             # fixture: ftext_frames_total (incl non-JP)
EF_JP_FRAMES = 6191                # fixture: ftext_frames_jp
EF_FRAME_FILES = 458               # fixture: ftext_frame_files
GOLDEN_STEM = ("SampleGameB/Content/SampleGameB/DataTable/EroEvent/"
               "DT_EroEventTalkTable")
GOLDEN_FRAMES = 1065               # discovery: 637 runs = 1,065 JP frames
SHAPEB_STEM = ("SampleGameB/Content/SampleGameB/DataTable/Item/"
               "EF_DT_BookText")

# flags u32=1 | history u8=0 | FString len 33 | 32 UPPER hex | NUL | i32 len
_FRAME = re.compile(
    rb"\x01\x00\x00\x00\x00\x21\x00\x00\x00([0-9A-F]{32})\x00(....)", re.S)

# discovery scanner's Japanese codepoint set (core kana/CJK/fullwidth +
# extended CJK punctuation and halfwidth kana)
def _has_jp(units) -> bool:
    return any(0x3040 <= c <= 0x30FF or 0x4E00 <= c <= 0x9FFF
               or 0xFF01 <= c <= 0xFF60 or 0x3000 <= c <= 0x303F
               or 0xFF61 <= c <= 0xFF9F for c in units)


def _ftext_frames(data: bytes) -> "list[tuple[int, int, bool]]":
    """(start, end, is_jp) per FText-Base frame in a raw payload: grammar
    match with negative-length UTF-16LE source, NUL-terminated and inside
    the payload. is_jp uses the discovery JP codepoint set — the golden's
    78 non-JP frames are censorship bars (U+25A0 runs), counted by the
    census total but not the JP count."""
    out = []
    for m in _FRAME.finditer(data):
        (ln,) = struct.unpack_from("<i", m.group(2), 0)
        if ln >= 0:                       # source must be negative-length
            continue
        n = -ln                           # UTF-16 units incl NUL
        end = m.end() + 2 * n
        if end > len(data):
            continue
        units = struct.unpack_from(f"<{n}H", data, m.end())
        if units[-1] != 0:                # terminator required
            continue
        out.append((m.start(), end, _has_jp(units[:-1])))
    return out


def _shapeb_cells(data: bytes) -> int:
    """TEST-ONLY recording count of shape-B cells in a raw .uasset half:
    ``i32 -(n+1)`` prefix, UTF-16LE payload with a JP codepoint, NUL
    terminator. The preceding u32 is deliberately NOT validated or
    interpreted (open provenance — discovery pins it read-only). Loose
    by design: a binary coincidence inside the run region can add an
    extra overlapping match, so exact counts are not asserted."""
    out = 0
    for i in range(0, max(len(data) - 10, 0), 2):
        (ln,) = struct.unpack_from("<i", data, i)
        if -600 < ln < -1 and i + 4 + 2 * (-ln) <= len(data):
            chars = struct.unpack_from(f"<{-ln}H", data, i + 4)
            if chars[-1] == 0 and any(
                    0x3040 <= c <= 0x30FF or 0x4E00 <= c <= 0x9FFF
                    or 0xFF01 <= c <= 0xFF60 for c in chars[:-1]):
                out += 1
    return out


def _ef_key() -> bytes:
    fx = os.path.join(os.path.dirname(__file__), "fixtures",
                      "sample_b_index_facts.json")
    with open(fx, encoding="utf-8") as f:
        return bytes.fromhex(json.load(f)["aes_key_hex"])


def _uexp_of(name: str) -> str:
    stem = name[:-len(".uasset")] if name.endswith(".uasset") \
        else name[:-len(".umap")]
    return stem + ".uexp"


@pytest.fixture(scope="session")
def ef_src(game_paths):
    return payload.find_sources(game_paths["sample_b"], aes_key=_ef_key())


@pytest.fixture(scope="session")
def sweep(ef_src):
    """Parse every EF package pair once (uasset + paired uexp); return
    per-sweep stats plus small payload caches for the per-asset asserts."""
    names = [n for n in payload.list_files(ef_src)
             if n.lower().endswith((".uasset", ".umap"))]
    stats = {
        "pairs": 0,
        "errors": [],                     # (name, repr) of named failures
        "presumed_versions": {},          # presumed ue5 version -> count
        "versioned": 0,
        "total_exports": 0,
        "names_decoded": 0,
        "wide_names": 0,
        "frames_total": 0,                # frames detected in .uexp halves
        "frames_in": 0,                   # ... inside an export slice
        "frames_jp": 0,                   # ... whose source holds JP text
        "uasset_frames": 0,               # frames in .uasset halves (want 0)
        "bad_slices": [],
        "uexp_tag_bad": [],
        "frame_files": 0,                 # .uexp halves with any frame
        "frame_files_jp": 0,              # ... with at least one JP frame
        "cache": {},                      # stem -> (uasset, uexp, package)
    }
    wanted = {GOLDEN_STEM, SHAPEB_STEM}
    for n in names:
        stem = n[:-len(".uasset")] if n.endswith(".uasset") \
            else n[:-len(".umap")]
        try:
            ua = payload.read_file(ef_src, n)
            ux = payload.read_file(ef_src, _uexp_of(n))
            pkg = uasset.parse_legacy(ua, ux, n)
            stats["pairs"] += 1
            v = pkg.version_info
            key = v.presumed_ue5 if v.is_unversioned else v.file_version_ue5
            stats["presumed_versions"][key] = \
                stats["presumed_versions"].get(key, 0) + 1
            if not v.is_unversioned:
                stats["versioned"] += 1
            stats["names_decoded"] += len(pkg.names)
            stats["wide_names"] += sum(
                1 for s in pkg.names if any(ord(c) > 127 for c in s))
            stats["total_exports"] += len(pkg.exports)
            for ex in pkg.exports:
                if ex.serial_size > 0 and len(ex.data) != ex.serial_size:
                    stats["bad_slices"].append((n, ex.object_name))
            (tag,) = struct.unpack_from("<I", ux, len(ux) - 4)
            if tag != uasset.PACKAGE_FILE_TAG:
                stats["uexp_tag_bad"].append(n)
            slices = pkg.export_ranges()
            frames = _ftext_frames(ux)
            if frames:
                stats["frame_files"] += 1
                if any(f[2] for f in frames):
                    stats["frame_files_jp"] += 1
            stats["frames_total"] += len(frames)
            stats["frames_jp"] += sum(1 for f in frames if f[2])
            stats["frames_in"] += sum(
                1 for s, e, _jp in frames
                if any(a <= s and e <= b for a, b in slices))
            stats["uasset_frames"] += len(_ftext_frames(ua))
            if stem in wanted:
                stats["cache"][stem] = (ua, ux, pkg)
        except Exception as exc:          # noqa: BLE001 - recorded, asserted
            stats["errors"].append((n, repr(exc)))
    return stats


def test_all_pairs_parse(sweep):
    assert sweep["pairs"] + len(sweep["errors"]) == EF_TOTAL_PAIRS
    assert sweep["errors"] == []          # malformed count reported: none
    assert sweep["names_decoded"] > 0
    assert sweep["bad_slices"] == []
    assert sweep["uexp_tag_bad"] == []
    # every EF package is unversioned (observed); the presumed version is
    # the newest layout that fits: gates 1009/1012/1013 (UE 5.2-5.5) are
    # byte-identical for these packages (all discriminating summary gates
    # sit at <= 1008 or >= 1014), so the heuristic reports 1013 — the
    # same newest-first convention retoc uses for its 5.5-vs-5.4 tie
    assert sweep["versioned"] == 0
    assert set(sweep["presumed_versions"]) == {1013}
    # wide (negative-length UTF-16) name entries are REAL in EF: 4,606
    # Japanese object names game-wide (e.g. "03 薬バフ付与ステート");
    # every non-ASCII EF name arrives via a wide entry (none via the
    # latin-1 fallback — probed in the Task 4 report)
    assert sweep["wide_names"] == 4606


def test_golden_dt_export_classes(sweep):
    _, _, pkg = sweep["cache"][GOLDEN_STEM]
    assert pkg.package_name == \
        "/Game/SampleGameB/DataTable/EroEvent/DT_EroEventTalkTable"
    assert "NewRow" in pkg.names
    assert pkg.exports
    assert any(ex.class_name == "DataTable" for ex in pkg.exports)
    assert pkg.version_info.is_unversioned
    assert pkg.version_info.custom_versions == []
    # split-stream math: the .uasset file IS the package header
    assert pkg.total_header_size == len(sweep["cache"][GOLDEN_STEM][0])
    ex = pkg.exports[0]
    assert ex.serial_offset == pkg.total_header_size   # first export at 0
    assert len(ex.data) == ex.serial_size


def test_golden_frame_coverage(sweep):
    """The golden's export data slices must cover its FText frames (Task 5
    bridge): every JP frame lands wholly inside one export slice."""
    _, ux, pkg = sweep["cache"][GOLDEN_STEM]
    frames = [f for f in _ftext_frames(ux) if f[2]]
    assert len(frames) == GOLDEN_FRAMES   # discovery: 1,065 JP frames
    slices = pkg.export_ranges()
    covered = [f for f in frames if any(a <= f[0] and f[1] <= b
                                        for a, b in slices)]
    assert len(covered) >= GOLDEN_FRAMES - 5      # task floor: >= 1,060


def test_efwide_frames_inside_slices(sweep):
    """Census ground truth: every FText frame in every EF .uexp lands
    inside an export data slice — 6,359 total (incl non-JP sources, e.g.
    the golden's 78 censorship-bar frames) of which 6,191 carry Japanese;
    .uasset halves carry none (shape-B halves are metadata only).

    File counts: the fixture's ``ftext_frame_files`` 458 counts .uexp
    halves with JP frames; 461 halves carry a frame at all — the extra 3
    are widget .uexp halves whose single frame each is non-JP
    (WG_WInMessage, WG_DebugEnemyHunt, WG_DebugMoveLevel), and their
    frames are part of the 6,359 total (6,359 - 6,191 = 168 non-JP)."""
    assert sweep["frames_total"] == EF_TOTAL_FRAMES
    assert sweep["frames_in"] == EF_TOTAL_FRAMES
    assert sweep["frames_jp"] == EF_JP_FRAMES
    assert sweep["uasset_frames"] == 0
    assert sweep["frame_files_jp"] == EF_FRAME_FILES
    assert sweep["frame_files"] == EF_FRAME_FILES + 3


def test_shapeb_uasset_metadata_only(sweep):
    """EF_DT_BookText: the .uasset half parses WITHOUT its .uexp (the
    parser treats .uasset halves as metadata), names decode, exports carry
    resolved classes, and shape-B cells are recorded — the u32 is
    preserved raw, never interpreted."""
    ua, _, pkg = sweep["cache"][SHAPEB_STEM]
    alone = uasset.parse_legacy(ua, None, SHAPEB_STEM + ".uasset")
    assert alone.names                     # name map decodes on the half
    assert alone.exports
    assert all(ex.data == b"" for ex in alone.exports)   # nothing sliced
    assert alone.exports[0].class_name == "DataTable"
    # shape B observed in the wild on this half: the fixture's Shape-B
    # example asset (956-byte half) carries 4 real cells — the book-
    # category titles アイテムの知識 / クエストと依頼 / ゲームの基本 /
    # メニューと操作 (the 1,896 shape-B count is game-wide, not this file)
    assert _shapeb_cells(ua) >= 4          # recorded observation, not a guess
    # and the paired parse slices real data out of the .uexp half
    assert any(len(ex.data) > 0 for ex in pkg.exports)


def test_bad_payload_raises_named_error():
    for junk in (b"", b"\x00" * 64, b"\xc1\x83\x2a\x9e" + b"\x00" * 96,
                 b"\x00" * 34194):
        with pytest.raises(uasset.LegacyError):
            uasset.parse_legacy(junk, b"\xc1\x83\x2a\x9e", "garbage.uasset")


def _synthetic_summary(flags, customs, names_count, names_off, exports,
                       imports, depends_off, guid):
    """UE 5.5-layout summary body (after tag+versioning+pkg name+flags)."""
    body = struct.pack("<ii", names_count, names_off)   # names pair
    body += struct.pack("<ii", 0, 0)                    # soft object paths
    body += struct.pack("<ii", 0, 0)                    # gatherable
    body += struct.pack("<ii", exports[0], exports[1]) if exports else \
        struct.pack("<ii", 0, 0)                        # exports pair
    body += struct.pack("<ii", *imports)                # imports pair
    body += struct.pack("<i", depends_off)              # depends offset
    body += struct.pack("<ii", 0, 0)                    # soft package refs
    body += struct.pack("<i", 0) + struct.pack("<i", 0)  # searchable, thumb
    body += guid                                        # package guid
    body += struct.pack("<I", 1) + struct.pack("<ii", 0, 1)   # generations
    for _ in range(2):                                  # engine versions
        body += struct.pack("<HHHI", 0, 0, 0, 0) + struct.pack("<i", 0)
    body += struct.pack("<I", 0) + struct.pack("<i", 0)  # comp + chunks
    body += struct.pack("<II", 7, 0)      # package source zone (8 bytes)
    body += struct.pack("<I", 0)                        # additional packages
    body += struct.pack("<i", 0)                        # asset registry
    body += struct.pack("<q", 0)                        # bulk data start
    body += struct.pack("<i", 0)                        # world tile
    body += struct.pack("<I", 0)                        # chunk ids
    body += struct.pack("<ii", 0, 0)                    # preload deps
    body += struct.pack("<i", 1)                        # names referenced
    body += struct.pack("<q", -1)                       # payload toc
    body += struct.pack("<i", -1)                       # data resource off
    return body


def test_synthetic_versioned_package():
    """No REAL EF package is versioned (all 7,656 parse unversioned —
    observed), so the versioned branch is pinned with a synthetic payload
    in the exact UE 5.5 summary layout: one custom version, one name, one
    import, no exports, export stream split into a synthetic .uexp."""
    def fstr(s):
        b = s.encode("ascii")
        return struct.pack("<i", len(b) + 1) + b + b"\x00"

    guid = bytes(range(16))
    vi = struct.pack("<iii", -8, 864, 522) + struct.pack("<i", 1013) \
        + struct.pack("<i", 0)                      # + licensee 0
    customs = struct.pack("<I", 1) + guid + struct.pack("<i", 42)
    name_entry = fstr("DummyName") + struct.pack("<HH", 1, 2)
    import_entry = struct.pack("<iiiiiii", 0, 0, 0, 0, 0, 0, 0) \
        + struct.pack("<I", 1)                      # is_optional (u32 bool)
    pkg_name = fstr("/Game/Dummy")
    head_len = 4 + len(vi) + len(customs) + 4 + len(pkg_name) + 4 \
        + len(_synthetic_summary(0, 0, 1, 0, None, (0, 0), 0, guid))
    names_off = head_len
    imports_off = names_off + len(name_entry)
    depends_off = imports_off + len(import_entry)
    body = _synthetic_summary(0, 0, 1, names_off, None, (1, imports_off),
                              depends_off, guid)
    ua = (struct.pack("<I", 0x9E2A83C1) + vi + customs
          + struct.pack("<i", depends_off)          # total header size
          + pkg_name + struct.pack("<I", 0x80042200)
          + body + name_entry + import_entry)
    ux = b"\x00" * 8 + struct.pack("<I", 0x9E2A83C1)
    p = uasset.parse_legacy(ua, ux, "Dummy.uasset")
    assert not p.version_info.is_unversioned
    assert p.version_info.file_version_ue5 == 1013
    assert p.version_info.legacy_ue3_version == 864
    assert p.version_info.custom_versions == [(guid, 42)]
    assert p.names == ["DummyName"]
    assert p.exports == []
    assert len(p.imports) == 1
    assert p.imports[0].is_optional
    assert p.imports[0].class_package == "DummyName"
    assert p.total_header_size == depends_off


def test_synthetic_wide_name():
    """FNameEntrySerialized wide (UTF-16) entries decode. Real EF name
    tables DO carry them — 4,606 Japanese object names game-wide (see
    test_all_pairs_parse) — but this synthetic pin keeps the branch
    deterministic on a skip-clean machine with no game data: a name
    table whose single entry is a negative-length UTF-16LE FString."""
    text = "アイテム".encode("utf-16-le") + b"\x00\x00"
    name_entry = struct.pack("<i", -(len(text) // 2)) + text \
        + struct.pack("<HH", 0, 0)
    vi = struct.pack("<iii", -8, 864, 522) + struct.pack("<i", 1013) \
        + struct.pack("<i", 0)
    customs = struct.pack("<I", 0)
    pkg_name = struct.pack("<i", 8) + b"/Game/W\x00"
    guid = bytes(range(16))
    body0 = _synthetic_summary(0, 0, 1, 0, None, (0, 0), 0, guid)
    head_len = 4 + len(vi) + len(customs) + 4 + len(pkg_name) + 4 + len(body0)
    body = _synthetic_summary(0, 0, 1, head_len, None, (0, head_len),
                              head_len + len(name_entry), guid)
    ua = (struct.pack("<I", 0x9E2A83C1) + vi + customs
          + struct.pack("<i", head_len + len(name_entry))
          + pkg_name + struct.pack("<I", 0x80042200) + body + name_entry)
    p = uasset.parse_legacy(ua, None, "W.uasset")
    assert p.names == ["アイテム"]
