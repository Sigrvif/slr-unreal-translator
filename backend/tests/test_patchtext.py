"""Task 1: length-aware text patching — patch_payload / rebase_entries.

Synthetic pins (game-independent, exact-byte): FText-Base grow and FString
shrink, multi-entry interleaved shifts (one untouched frame between two
patched ones), blank-translation skip, null-patch identity, non-JP source
+ astral (surrogate-pair) translation content, overlap hard error,
unpaired-surrogate rejection, unsupported-kind rejection.

Real-data pin (ST): DT_学校会話 — the 473-frame FText-Base asset patched
at 3 spread offsets (shrink + grow), re-scanned with textscan's frame
detector: exactly 3 changed sources, the other 470 frames byte-identical,
everything outside the 3 patchable ranges byte-identical, and
rebase_entries re-finds all 473 frames on the patched payload.
"""
import struct

import pytest

from backend.ut import patchtext, payload, textscan
from backend.ut.textscan import TextEntry

ST_GOLDEN = "sample_a/Content/common/データ/DT_学校会話.uasset"
ST_GOLDEN_FRAMES = 473             # Phase 3 census (test_textscan pin)


# --------------------------------------------------------------------------
# synthetic frame/entry builders (local mirrors of the scan grammar — no
# imports from other test modules)

def _ftext(key: str, text: str) -> bytes:
    """flags u32=1 | history u8=0 | FString key (32 hex) | FString source."""
    units = text.encode("utf-16-le") + b"\x00\x00"
    return (b"\x01\x00\x00\x00\x00"
            + struct.pack("<i", 33) + key.encode("ascii") + b"\x00"
            + struct.pack("<i", -(len(text) + 1)) + units)


def _fstr(text: str) -> bytes:
    units = text.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<i", -(len(text) + 1)) + units


def _entry(offset: int, kind: str, source: str, key: str = "") -> TextEntry:
    return TextEntry(asset="Synthetic/Payload.uexp", export_index=0,
                     class_name="DataTable", property_path=["<frame>", "0"],
                     kind=kind, namespace="", key=key, source=source,
                     offset=offset)


def _read_utf16(data: bytes, len_pos: int) -> str:
    """Re-read a negative-length UTF-16 FString at ``len_pos`` (the
    detector's unit-wise construction, WTF-16 for astral chars)."""
    (ln,) = struct.unpack_from("<i", data, len_pos)
    assert ln < 0
    units = struct.unpack_from(f"<{-ln}H", data, len_pos + 4)
    assert units[-1] == 0
    return "".join(map(chr, units[:-1]))


# --------------------------------------------------------------------------
# synthetic, game-independent pins

def test_null_patch_identity():
    """No translations: payload returned unchanged, empty report — the
    Task 2 null-patch golden depends on this being a pure no-op."""
    data = b"\x00" * 3 + _ftext("A" * 32, "こんにちは") + b"\xff" * 5
    entries = [_entry(3, "FText-Base", "こんにちは", "A" * 32)]
    out, report = patchtext.patch_payload(data, entries, {})
    assert out == data
    assert report == {"patched": [], "skipped": []}


def test_single_ftext_grow_exact_bytes():
    """One FText-Base frame, longer ASCII replacement: the negative length
    field and the UTF-16LE+NUL body are re-written at offset+42/+46 and
    NOTHING else changes (flags/key prefix and both surrounding zones
    byte-identical)."""
    key = "F" * 32
    old, new = "短い", "A MUCH LONGER REPLACEMENT!"
    pre, post = b"\x00" * 3, b"\xab\xcd"
    data = pre + _ftext(key, old) + post
    entries = [_entry(3, "FText-Base", old, key)]
    out, report = patchtext.patch_payload(data, entries, {0: new})
    assert out == pre + _ftext(key, new) + post      # exact full payload
    # prefix bytes (flags/history/key) untouched; exact new length + body
    assert out[:3 + 42] == data[:3 + 42]
    len_pos = 3 + 42
    assert struct.unpack_from("<i", out, len_pos)[0] == -(len(new) + 1)
    assert out[3 + 46:3 + 46 + 2 * (len(new) + 1)] \
        == new.encode("utf-16-le") + b"\x00\x00"
    assert out.endswith(post)
    assert report == {"patched": [{"entry_index": 0, "offset": 3,
                                   "old_chars": 2, "new_chars": len(new),
                                   "shift": 2 * (len(new) - 2),
                                   "new_source": new}],
                      "skipped": []}


def test_single_fstring_shrink_exact_bytes():
    """One FString frame, shorter replacement: payload shrinks by the
    exact byte delta; body terminator re-written; surroundings intact."""
    old, new = "こちらの文章は長めです", "X"
    pre, post = b"\x11\x22\x33", b"\x00" * 4
    data = pre + _fstr(old) + post
    entries = [_entry(3, "FString", old)]
    out, report = patchtext.patch_payload(data, entries, {0: new})
    assert out == pre + _fstr(new) + post
    assert len(out) == len(data) + 2 * (len(new) - len(old))   # -22
    assert struct.unpack_from("<i", out, 3)[0] == -2           # -(1+1)
    assert out[7:9] == b"X\x00"
    assert out.endswith(post)
    r = report["patched"]
    assert len(r) == 1 and r[0]["old_chars"] == len(old) \
        and r[0]["new_chars"] == 1 \
        and r[0]["shift"] == 2 * (len(new) - len(old))
    assert report["skipped"] == []


def test_multi_entry_interleaved_shifts():
    """Four interleaved frames (FText, FString, FText-UNTOUCHED, FString),
    three patched with independent grow/shrink shifts: exact payload
    reconstruction, descending-splice correctness pinned by the untouched
    middle frame, and rebase_entries relocates every frame — patched ones
    carry their new source, the untouched one keeps its original."""
    k1, k3 = "1" * 32, "3" * 32
    seg0 = b"\x00" * 3
    f1 = _ftext(k1, "一つ目")                       # FText, 3 chars
    seg1 = b"\xab" * 5
    f2 = _fstr("二つ目の文字列")                     # FString, 7 chars
    seg2 = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09"
    f3 = _ftext(k3, "三")                           # FText, 1 char, untouched
    seg3 = b"\xff" * 2
    f4 = _fstr("四つ目")                             # FString, 3 chars
    seg4 = b"\x77" * 4
    data = seg0 + f1 + seg1 + f2 + seg2 + f3 + seg3 + f4 + seg4
    o1 = len(seg0)
    o2 = o1 + len(f1) + len(seg1)
    o3 = o2 + len(f2) + len(seg2)
    o4 = o3 + len(f3) + len(seg3)
    entries = [_entry(o1, "FText-Base", "一つ目", k1),
               _entry(o2, "FString", "二つ目の文字列"),
               _entry(o3, "FText-Base", "三", k3),
               _entry(o4, "FString", "四つ目")]
    t0, t1, t3 = "FIRST ENTRY GROWN LONG", "2", "FOURTH ALSO GROWN"
    out, report = patchtext.patch_payload(data, entries, {0: t0, 1: t1, 3: t3})
    # exact reconstruction: only the three patchable ranges differ
    assert out == (seg0 + _ftext(k1, t0) + seg1 + _fstr(t1) + seg2
                   + f3 + seg3 + _fstr(t3) + seg4)
    assert [r["entry_index"] for r in report["patched"]] == [0, 1, 3]
    assert [r["shift"] for r in report["patched"]] \
        == [2 * (len(t0) - 3), 2 * (len(t1) - 7), 2 * (len(t3) - 3)]
    assert report["skipped"] == []
    # rebase: offsets shift by the sum of EARLIER patch shifts only
    rebased = patchtext.rebase_entries(entries, report)
    s0, s1 = 2 * (len(t0) - 3), 2 * (len(t1) - 7)
    assert [e.offset for e in rebased] == [o1, o2 + s0, o3 + s0 + s1,
                                           o4 + s0 + s1]
    assert _read_utf16(out, rebased[0].offset + 42) == t0
    assert _read_utf16(out, rebased[1].offset) == t1
    assert _read_utf16(out, rebased[2].offset + 42) == "三"   # untouched
    assert _read_utf16(out, rebased[3].offset) == t3
    # rebased entries reflect the patched payload: new sources on patched
    # rows, original on the untouched row; input list not mutated
    assert [e.source for e in rebased] == [t0, t1, "三", t3]
    assert entries[1].offset == o2 and entries[2].source == "三"


def test_skip_on_blank_translation():
    """None and "" translations are skipped (blank = not patched, the
    Phase 3 contract) with the reason recorded; other rows still patch."""
    k = "9" * 32
    f1, f2, f3 = _ftext(k, "一行目"), _fstr("二行目"), _ftext(k, "三行目")
    data = f1 + f2 + f3
    entries = [_entry(0, "FText-Base", "一行目", k),
               _entry(len(f1), "FString", "二行目"),
               _entry(len(f1) + len(f2), "FText-Base", "三行目", k)]
    out, report = patchtext.patch_payload(data, entries,
                                          {0: "", 1: None, 2: "KEPT"})
    assert report["skipped"] == [
        {"entry_index": 0, "reason": "blank translation"},
        {"entry_index": 1, "reason": "blank translation"}]
    assert [r["entry_index"] for r in report["patched"]] == [2]
    # frames 0 and 1 byte-identical, frame 2 rewritten
    assert out.startswith(f1 + f2)
    assert _read_utf16(out, len(f1) + len(f2) + 42) == "KEPT"


def test_non_jp_and_astral_content_patch():
    """The layer is content-agnostic: a non-JP source frame (ST's lone
    "→"-style frame) patches fine, and an astral translation char (a
    surrogate PAIR — valid UTF-16) is stored as 2 units: new_chars counts
    UTF-16 code units, not Python characters."""
    key = "0" * 32
    data = _ftext(key, "→")
    entries = [_entry(0, "FText-Base", "→", key)]
    new = "Мир 🌍!"                       # 6 chars, 7 UTF-16 units
    out, report = patchtext.patch_payload(data, entries, {0: new})
    assert struct.unpack_from("<i", out, 42)[0] == -(7 + 1)
    assert out[46:46 + 16] == new.encode("utf-16-le") + b"\x00\x00"
    assert out[:42] == data[:42]
    assert report["patched"][0]["old_chars"] == 1
    assert report["patched"][0]["new_chars"] == 7
    assert report["patched"][0]["shift"] == 12


def test_overlapping_ranges_rejected():
    """Two patches whose patchable ranges intersect = hard error (the
    payload is ONE shared buffer; double-writing a range is corruption)."""
    data = _fstr("重なり")
    entries = [_entry(0, "FString", "重なり"),
               _entry(0, "FString", "重なり")]        # duplicate site
    with pytest.raises(ValueError, match="overlap"):
        patchtext.patch_payload(data, entries, {0: "A", 1: "B"})


def test_unpaired_surrogates_rejected():
    """A translation with an unpaired surrogate cannot round-trip through
    the game's UTF-16 FStrings — rejected by name, payload never built."""
    data = _ftext("5" * 32, "本文")
    entries = [_entry(0, "FText-Base", "本文", "5" * 32)]
    for bad in ("a\ud800b", "\udc00x", "\ud800\udbff", "x\udc00", "\ud800"):
        with pytest.raises(ValueError, match="surrogate"):
            patchtext.patch_payload(data, entries, {0: bad})
    with pytest.raises(ValueError, match="NUL"):    # terminator is ours
        patchtext.patch_payload(data, entries, {0: "a\x00b"})


def test_unsupported_kind_rejected():
    """Kinds other than FText-Base/FString (e.g. the excluded shape-W)
    have no pinned patch geometry — ValueError naming the kind."""
    data = _ftext("6" * 32, "本文")
    entries = [_entry(0, "shape-W", "本文")]
    with pytest.raises(ValueError, match="kind.*shape-W"):
        patchtext.patch_payload(data, entries, {0: "TEXT"})


def test_mixed_assets_rejected():
    """patch_payload patches ONE asset's payload — entries from two
    different assets against one buffer is a caller bug, rejected."""
    e1 = _entry(0, "FString", "一方")
    e2 = _entry(0, "FString", "他方")
    e2.asset = "Synthetic/Other.uexp"
    with pytest.raises(ValueError, match="one asset"):
        patchtext.patch_payload(_fstr("一方"), [e1, e2], {0: "A", 1: "B"})


# --------------------------------------------------------------------------
# real-data pin (ST; session scan fixture — skips cleanly without the game)

def test_dt_school_dialog_patch_and_rescan(st_src, st_scan):
    """DT_学校会話: 473 scanned entries, 3 spread translations (two
    shrinks + one GROW), then the full verify chain:
      - exactly 3 changed sources on re-scan, keys all preserved;
      - the other 470 frames byte-identical (frame ranges compared);
      - everything outside the 3 patchable ranges byte-identical (manual
        splice reconstruction == patched payload);
      - rebase_entries re-finds all 473 frames at the shifted offsets.
    """
    data = payload.read_file(st_src, ST_GOLDEN)
    # the asset also carries 2 ZenName rows (its red self-names) — this
    # test pins the frame grammar, so only FText-Base entries ride
    entries = sorted((e for e in st_scan["entries"] if e.asset == ST_GOLDEN
                      and e.kind == "FText-Base"),
                     key=lambda e: e.offset)
    assert len(entries) == ST_GOLDEN_FRAMES
    assert all(e.kind == "FText-Base" for e in entries)
    orig = [f for f in textscan._detect_frames(data) if f[1] is not None]
    assert [f[0] for f in orig] == [e.offset for e in entries]
    assert len(orig) == ST_GOLDEN_FRAMES

    # idx 0 "明日香" (3 chars -> shrink), idx 40 "一同" (2 chars -> GROW),
    # idx 236 mid-sentence (25 chars -> shrink): three spread offsets
    translations = {0: "T1", 40: "A LONGER TEST STRING 333", 236: "TEST-22"}
    assert len(entries[40].source) == 2              # the grow site
    patched, report = patchtext.patch_payload(data, entries, translations)
    assert [r["entry_index"] for r in report["patched"]] == [0, 40, 236]
    assert any(r["shift"] > 0 for r in report["patched"])    # a real grow
    assert report["skipped"] == []

    # everything outside the 3 patchable ranges is byte-identical:
    # applying the same three splices by hand must reproduce the payload
    expected = data
    for r in sorted(report["patched"], key=lambda r: -r["offset"]):
        s = r["offset"] + 42
        e = s + 4 + 2 * (r["old_chars"] + 1)
        body = r["new_source"].encode("utf-16-le") + b"\x00\x00"
        expected = expected[:s] + struct.pack(
            "<i", -(r["new_chars"] + 1)) + body + expected[e:]
    assert patched == expected

    # re-scan: exactly 3 changed sources, 470 frames byte-identical
    new = [f for f in textscan._detect_frames(patched) if f[1] is not None]
    assert len(new) == ST_GOLDEN_FRAMES
    assert [f[2] for f in new] == [f[2] for f in orig]         # keys kept
    changed = [i for i, (a, b) in enumerate(zip(orig, new)) if a[3] != b[3]]
    assert changed == sorted(translations)
    for i in changed:
        assert new[i][3] == translations[i]
    same = [i for i in range(len(orig)) if i not in translations]
    for i in same:
        assert data[orig[i][0]:orig[i][1]] \
            == patched[new[i][0]:new[i][1]]                    # byte-equal

    # rebase: all 473 frames re-found at their shifted offsets
    rebased = patchtext.rebase_entries(entries, report)
    assert len(rebased) == ST_GOLDEN_FRAMES
    assert sorted(e.offset for e in rebased) == sorted(f[0] for f in new)
    for i, e in enumerate(rebased):
        assert e.key == new[i][2]
        want = translations[i] if i in translations else entries[i].source
        assert _read_utf16(patched, e.offset + 42) == want
        assert e.source == want
