"""Text extraction: TextEntry production over both games' parsed packages
(Phase 3, Task 5). Scan-only — games stay read-only.

``scan_text(pkg)`` accepts any parsed package exposing ``name``, ``names``
and ``exports[i].data`` / ``.class_name`` / ``.serial_offset`` (zen
``ZenPackage`` and legacy ``LegacyPackage`` both qualify; offsets are
payload-absolute: zen ``header_size + serial_offset``, legacy
``serial_offset - total_header_size`` — i.e. the .uexp half for EF pairs,
so pass the .uexp payload name, as :func:`scan_game` does).

Text shapes (pinned by the Task 2 census; ``text_discovery.json`` is
ground truth):

- ``FText-Base`` — THE dialogue shape, both games:
  ``flags u32=1 | history u8=0 | FString key (32 UPPERCASE hex) | FString
  source (negative length, UTF-16LE, NUL-terminated)``. Exact census
  totals: ST 992, EF 6,359 (6,191 JP). No namespace FString exists in any
  verified frame, so ``namespace`` is always "".
- ``FString`` — plain negative-length UTF-16 FStrings carrying Japanese in
  DataTable exports, emitted ONLY when CHAINED: a valid FString (wide or
  ANSI, NUL-terminated) must end exactly at the candidate's length prefix.
  That is the observed DataTable cell layout (e.g. EF_Item04_Food:
  ``[table][row_Name][text][row_Text][text]...``). Scoped to DataTable
  exports — the only context the census pinned — and the key-vs-value
  roles inside a pair are deliberately NOT interpreted: both chained JP
  strings are emitted (Phase 4 rewrites by offset; the editor decides
  what to translate).

Excluded classes (warnings, never aborts; counted per package, aggregated
by :func:`scan_game`):

- ``shape-W`` strings — DECISION REVERSED (Phase 6, see
  docs/plans/2026-10-25-phase6-bcstrings.md). Phase 3 excluded the
  ``[UTF-16 text][00 00][1f][32-hex key][00]`` shape as unpinned; the
  grammar is now identified as Kismet bytecode string constants in
  ``Function`` exports — ``34`` (EX_UnicodeStringConst, NUL-terminated
  UTF-16) followed by ``1f`` (EX_StringConst, NUL-terminated ANSI)
  carrying a 32-hex key — and they are emitted as ``BcString`` entries
  (same-size in-place patching needs no length field). Sites whose text
  carries no Japanese (symbol glyphs: ▶, ☆ ratings) are still not
  translation targets and are counted under ``bcstring-nonjp``.
- ``ftext-ascii-source`` — RETIRED (Phase 7, collect-all-visible):
  ASCII-source FText frames are emitted as ``FText-Ascii`` rows (the
  main menus — "New Game", "Option" — are exactly this shape). Only
  sources with bytes > 0x7F (codepage-encoded text we cannot round-trip)
  are still counted, as ``ftext-ansi-highbyte``.
- ``bcstring-nonjp`` — RETIRED (Phase 7): all grammar-valid BcString
  rows are emitted regardless of language (gray-tagged client-side when
  Japanese source is on). DataTable chained FStrings REMAIN JP-gated —
  cell streams are full of ASCII keys/ids/binary runs and the JP gate is
  the noise filter there.
- ``in-header-export-data`` — legacy export data sliced from the .uasset
  half (never observed in EF; would make .uexp-relative offsets wrong).

property_path: a UE property-tag walk (FName name, FName type, i64 size,
i32 array_index, per-type tag data, HasGuid; unknown types skipped by
size) is attempted first on every export with data. BOTH games cook their
packages TAGLESS (unversioned/raw struct serialization — observed: no
name/type/size tags anywhere around the frames), so on real data the walk
resolves nothing and frames carry the honest fallback path
``["<frame>", "<offset in export data>"]``. A synthetic pin in
test_textscan.py exercises the walk for tag-serialized data.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from . import iostore, payload, uasset, zen

__all__ = ["TextEntry", "scan_text", "scan_game"]

# Japanese codepoint set — the discovery scanner's (core kana/CJK/
# fullwidth + extended CJK punctuation and halfwidth kana).
_JP_RANGES = ((0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xFF01, 0xFF60),
              (0x3000, 0x303F), (0xFF61, 0xFF9F))

# flags u32=1 | history u8=0 | FString len 33 | 32 UPPER hex | NUL | i32 len
_FRAME = re.compile(
    rb"\x01\x00\x00\x00\x00\x21\x00\x00\x00([0-9A-F]{32})\x00(....)", re.S)

# shape-W widget postfix: [UTF-16 text + NUL] 00 00 1f [32 hex] 00.
# UTF-16 text cannot itself produce contiguous ASCII hex after 1f, so the
# only real collision source is binary payload (probability ~2^-264 per
# site — negligible; counts are recorded, not asserted as exact frame data).
_SHAPE_W = re.compile(rb"\x00\x00\x1f[0-9A-F]{32}\x00")

_FSTRING_MAX = 4096                        # sane length cap for chains

_PROPERTY_TYPES = frozenset((
    "BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
    "UInt64Property", "FloatProperty", "DoubleProperty", "NameProperty",
    "StrProperty", "TextProperty", "ObjectProperty", "WeakObjectProperty",
    "LazyObjectProperty", "AssetObjectProperty", "SoftObjectProperty",
    "ClassProperty", "AssetClassProperty", "SoftClassProperty",
    "InterfaceProperty", "DelegateProperty", "MulticastDelegateProperty",
    "MulticastInlineDelegateProperty", "MulticastSparseDelegateProperty",
    "EnumProperty", "ByteProperty", "ArrayProperty", "MapProperty",
    "SetProperty", "StructProperty",
))

# extra tag-data bytes after (i64 size, i32 array_index), before HasGuid
_TAG_EXTRA = {"ByteProperty": 8, "EnumProperty": 8, "StructProperty": 8,
              "ArrayProperty": 8, "SetProperty": 8, "MapProperty": 16}


def _has_jp(s: str) -> bool:
    return any(lo <= ord(c) <= hi for lo, hi in _JP_RANGES for c in s)


@dataclass
class TextEntry:
    """One extractable source string in one game payload.

    ``offset`` is the byte offset in the asset's payload (the zen chunk
    for ST, the .uexp bytes for EF) where the frame starts — the Phase 4
    patch address. ``property_path`` names the containing property when
    the property-tag walk identified one, else the honest
    ``["<frame>", "<export-data offset>"]`` fallback (both games cook
    tagless).
    """
    asset: str                       # payload-layer file name
    export_index: int                # index into pkg.exports
    class_name: "str | None"
    property_path: list
    kind: str                        # "FText-Base" | "FString"
    namespace: str                   # "" — no namespace in any census frame
    key: str                         # 32-hex FText key ("" for FString)
    source: str
    offset: int
    # Kind-specific patch geometry not derivable from the frame alone
    # (ZenName: name-batch storage charset / byte length / batch index —
    # the length header and CityHash live in side tables, see ut.zen).
    meta: "dict | None" = None


# ZenName risk tagging (SLR tag scheme): name-batch strings are mostly
# internal identifiers, but the user's standing rule is collect-first,
# tag-for-risk — a skipped row is invisible, a tagged row is the user's
# choice. Red = structural identifier patterns (prefix/suffix/GUID), a
# payload path (package identity — also hard-refused at patch time), or
# a name that IS a game asset (matches a payload file stem — e.g. ST's
# BGM cue '1_短くドビュ_Cue': renaming it would break every sound
# reference to that asset); yellow = everything else Japanese (display
# labels like DT row keys land here; the user confirms before
# translating).
_ZENNAME_RED_PREFIXES = (
    "ExecuteUbergraph_", "K2Node_", "BndEvt__", "CallFunc_",
    "A_", "ABP_", "AM_", "BP_", "WBP_", "DT_", "K_", "DA_", "T_",
    "M_", "MI_", "SM_", "SK_", "NS_", "GC_", "GE_", "PT_", "WS_",
)
_ZENNAME_RED_SUFFIXES = (
    "_GEN_VARIABLE", "_C", "_Skeleton", "_PhysicsAsset", "_Default",
    "_Montage", "_Anim", "_Section", "_Mat", "_Material", "_Cue",
)
_ZENNAME_GUID = re.compile(r"_[0-9A-F]{32}$")
# numbered variant tails: スカート_001_L / マテリアル_001 — mesh bones,
# material slots, morph targets. Display labels never number like this.
_ZENNAME_NUM_TAIL = re.compile(r"_[0-9]+(_[LR])?$")
# a JP name-batch entry present in >= this many packages' batches is a
# shared structural identifier (sockets, morphs, anim/voice switch
# names replicated into every referencing package) — display row keys
# stay in their owning DataTable + few consumers (ST options keys: 3-4)
_ZENNAME_SHARED_PKG_RED = 10


def _zenname_risk(name: str, asset_stems: "set[str] | None" = None) -> str:
    if "/" in name or name.startswith(_ZENNAME_RED_PREFIXES) \
            or name.endswith(_ZENNAME_RED_SUFFIXES) \
            or _ZENNAME_GUID.search(name) or _ZENNAME_NUM_TAIL.search(name):
        return "red"
    if asset_stems is not None and name in asset_stems:
        return "red"                 # the name IS an asset file (exact)
    return "yellow"


def upgrade_zenname_risk(entries: list, freq: dict) -> None:
    """Post-scan pass: promote yellow ZenName rows whose source string
    appears in many packages' name batches to red (a shared structural
    identifier — the frequency signal only exists after the whole game
    is scanned, so :func:`_zenname_risk` cannot see it)."""
    for e in entries:
        if e.kind == "ZenName" and e.meta and e.meta.get("risk") == "yellow" \
                and freq.get(e.source, 0) >= _ZENNAME_SHARED_PKG_RED:
            e.meta["risk"] = "red"


def asset_stems(src) -> "set[str]":
    """Stems of every package payload name in the game (the asset-file
    identity set for :func:`_zenname_risk`)."""
    return {n.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            for n in payload.list_files(src)
            if n.lower().endswith((".uasset", ".umap"))}


# --------------------------------------------------------------------------
# frame / FString primitives

def _detect_frames(data: bytes):
    """(start, end, key, source) per census-grammar FText frame in
    ``data``; ASCII-source grammar sites are returned as
    (start, None, None, None) for the warning counter."""
    out = []
    for m in _FRAME.finditer(data):
        (ln,) = struct.unpack_from("<i", m.group(2), 0)
        if ln >= 0:                          # census: negative UTF-16 only
            out.append((m.start(), None, None, None))
            continue
        n = -ln                              # UTF-16 units incl NUL
        end = m.end() + 2 * n
        if end > len(data):
            continue
        units = struct.unpack_from(f"<{n}H", data, m.end())
        if units[-1] != 0:                   # terminator required
            continue
        out.append((m.start(), end, m.group(1).decode("ascii"),
                    "".join(map(chr, units[:-1]))))
    return out


def _fstring_at(data: bytes, pos: int):
    """(end, text, is_wide) if a sane FString starts at ``pos``, else None."""
    if pos + 4 > len(data):
        return None
    (ln,) = struct.unpack_from("<i", data, pos)
    if ln < 0:
        n = -ln
        if not 1 <= n <= _FSTRING_MAX:
            return None
        end = pos + 4 + 2 * n
        if end > len(data):
            return None
        if struct.unpack_from("<H", data, end - 2)[0] != 0:
            return None
        units = struct.unpack_from(f"<{n}H", data, pos + 4)
        return end, "".join(map(chr, units[:-1])), True
    if not 1 <= ln <= _FSTRING_MAX:
        return None
    end = pos + 4 + ln
    if end > len(data) or data[end - 1] != 0:
        return None
    return end, data[pos + 4:end - 1].decode("latin-1"), False


def _chained_fstrings(data: bytes, frame_ranges):
    """(pos, text) per JP wide FString whose length prefix is immediately
    preceded by the end of another valid FString (the observed DataTable
    cell-stream layout), excluding FText frame interiors.

    Candidate starts are scanned at EVERY byte offset: the observed cell
    streams mix ANSI and wide FStrings, and an ANSI cell shifts the
    stream's parity (e.g. EF_Item04_Food's row keys start at odd offsets
    behind the even-aligned table-name FString)."""
    fstrings = []
    pos = 0
    while pos + 4 <= len(data):
        f = _fstring_at(data, pos)
        if f is None:
            pos += 1
            continue
        fstrings.append((pos, f[0], f[1], f[2]))
        pos += 1
    ends = {f[1] for f in fstrings}
    out = []
    for start, _end, text, wide in fstrings:
        if not wide or len(text) < 2 or not _has_jp(text):
            continue
        if start not in ends:
            continue
        # display text never embeds C0 controls; a "candidate" that does is
        # a binary coincidence that chained by luck (observed: a 64-unit
        # junk string in EF_Item04_Food's row-1 binary zone)
        if any(ord(c) < 0x20 and c not in "\t\n\r" for c in text):
            continue
        if any(a <= start < b for a, b in frame_ranges):
            continue
        out.append((start, text))
    return out


# --------------------------------------------------------------------------
# Kismet bytecode string constants (Phase 6: the former shape-W sites)

_BCHEX = frozenset(b"0123456789ABCDEF")
_BC_MAX_CHARS = 1024                     # sane cap; real max seen 156


def _scan_bc_strings(data: bytes):
    """``(op_pos, text, key, units)`` per keyed Kismet string constant:

    ``[29 01][34][UTF-16LE text][00 00][1f][32-hex UPPERCASE][00]``

    ``0x34`` = EX_UnicodeStringConst, NUL-terminated (no length field);
    the ``0x1f`` EX_StringConst carries the FText-style key. The ``29 01``
    prefix precedes the opcode at every real site (591/592 census — the
    one exception was a false positive anchored at a ``4`` byte INSIDE a
    key, whose mojibake "text" slipped the JP gate) and is REQUIRED:
    hex-key bytes can never supply it, so key-internal anchors are
    structurally impossible. ``op_pos`` is the BYTECODE string constant's
    patch address (the opcode byte — an ANSI-mode patch rewrites it)."""
    out = []
    pos = 2
    n = len(data)
    while True:
        op = data.find(b"\x34", pos)
        if op < 0:
            break
        pos = op + 1
        if data[op - 2:op] != b"\x29\x01":
            continue
        t = op + 1
        chars = []
        ok = False
        while t + 2 <= n and len(chars) <= _BC_MAX_CHARS:
            unit = data[t] | (data[t + 1] << 8)
            if unit == 0:
                ok = t + 3 <= n and data[t + 2] == 0x1F
                break
            chars.append(unit)
            t += 2
        if not ok or not chars:
            continue
        k = t + 2
        if k + 34 > n or data[k] != 0x1F or data[k + 33] != 0:
            continue
        key = data[k + 1:k + 33]
        if not all(c in _BCHEX for c in key):
            continue
        out.append((op, "".join(map(chr, chars)), key.decode("ascii"),
                    len(chars)))
        pos = k + 34
    return out


# --------------------------------------------------------------------------
# property-tag walk (attempted first; both games cook tagless)

def _fname_at(buf: bytes, pos: int, names: list, mapped: bool):
    """(name, next_pos) if an FName at ``pos`` resolves, else None.
    Legacy: i32 index + i32 number. Zen: FMappedName u32 (local block
    only — imported/global names are not in the package name batch) +
    i32 number."""
    if mapped:
        if pos + 8 > len(buf):
            return None
        iat, number = struct.unpack_from("<Ii", buf, pos)
        if iat >> 30:                        # imported/global block
            return None
        index = iat & 0x3FFFFFFF
    else:
        if pos + 8 > len(buf):
            return None
        index, number = struct.unpack_from("<ii", buf, pos)
    if not 0 <= index < len(names):
        return None
    base = names[index]
    return (base if number == 0 else f"{base}_{number - 1}"), pos + 8


def _walk_properties(data: bytes, names: list, mapped: bool):
    """[(path, value_start, value_end)] when the whole of ``data`` parses
    as a tagged-property list ending on the "None" terminator, else None
    (any violation -> the walk identified nothing; honest fallback)."""
    ranges = []

    def walk(buf, base, prefix, depth) -> bool:
        pos = 0
        while True:
            nm = _fname_at(buf, pos, names, mapped)
            if nm is None:
                return False
            name, pos = nm
            if name == "None":               # end-of-list terminator
                return True
            tp = _fname_at(buf, pos, names, mapped)
            if tp is None:
                return False
            ptype, pos = tp
            if ptype not in _PROPERTY_TYPES:
                return False
            if pos + 12 > len(buf):
                return False
            (size,) = struct.unpack_from("<q", buf, pos)
            (array_index,) = struct.unpack_from("<i", buf, pos + 8)
            pos += 12
            if size < 0:
                return False
            if ptype == "BoolProperty":      # native bool rides the tag
                if pos + 1 > len(buf):
                    return False
                pos += 1
            else:
                extra = _TAG_EXTRA.get(ptype, 0)
                if pos + extra > len(buf):
                    return False
                pos += extra
                if ptype == "StructProperty":
                    if pos + 1 > len(buf):
                        return False
                    if buf[pos]:
                        pos += 16
                    pos += 1
            if pos + 1 > len(buf):
                return False
            has_guid = buf[pos]
            pos += 1
            if has_guid:
                pos += 16
            if pos + size > len(buf):
                return False
            vstart, vend = pos, pos + size
            path = prefix + [name]
            if array_index:
                path = path + [str(array_index)]
            ranges.append((path, base + vstart, base + vend))
            if ptype == "StructProperty" and depth < 4:
                # nested tagged list inside the struct value; failure is
                # fine (raw struct payload) — the outer range still holds
                walk(buf[vstart:vend], base + vstart, path, depth + 1)
            pos = vend

    if not walk(data, 0, [], 0):
        return None
    return ranges


def _path_for(ranges, start, end):
    """Narrowest walked value range wholly containing [start, end)."""
    best = None
    for path, s, e in ranges or []:
        if s <= start and end <= e and (best is None or e - s < best[2] - best[1]):
            best = (path, s, e)
    return best[0] if best else None


# --------------------------------------------------------------------------
# per-package scan

def _data_base(pkg, ex, warnings) -> "int | None":
    """Payload offset of this export's data slice (None + warning when a
    legacy export's data sits inside the .uasset header — never observed
    in EF, would make .uexp-relative patch offsets wrong)."""
    if hasattr(pkg, "header_size"):          # zen
        return pkg.header_size + ex.serial_offset
    if ex.serial_offset < pkg.total_header_size:
        warnings.append("in-header-export-data: 1")
        return None
    return ex.serial_offset - pkg.total_header_size


def scan_text(pkg, asset_stems: "set[str] | None" = None):
    """Extract TextEntries from one parsed package.

    Returns ``(entries, warnings)``. Never aborts: unknown shapes are
    skipped with a warning (see module docstring for the classes); a
    walk-resolved property path is used when the property-tag walk
    identifies one, else the honest ``["<frame>", N]`` fallback.
    """
    entries: list = []
    warnings: list = []
    names = list(getattr(pkg, "names", None) or [])
    mapped = hasattr(pkg, "header_size")     # zen vs legacy FName encoding
    ansi_highbyte = 0
    shape_w = 0
    for index, ex in enumerate(pkg.exports):
        data = getattr(ex, "data", b"") or b""
        if not data:
            continue
        base = _data_base(pkg, ex, warnings)
        if base is None:
            continue
        shape_w += len(_SHAPE_W.findall(data))
        walked = _walk_properties(data, names, mapped) if names else None
        export_entries = []
        frame_ranges = []
        for start, end, key, source in _detect_frames(data):
            if end is None:
                # ASCII-source FText frame (Phase 7: collected — the main
                # menus are these). Validated: body must NUL-terminate and
                # be pure ASCII (higher bytes = codepage-encoded text we
                # cannot round-trip — counted, not emitted); empty
                # sources are not visible text.
                (ln,) = struct.unpack_from("<i", data[start + 42:start + 46], 0)
                body = data[start + 46:start + 46 + ln]
                if len(body) < ln or body[-1:] != b"\x00":
                    continue
                txt = body[:-1]
                if not txt:
                    continue
                if any(b > 0x7F for b in txt):
                    ansi_highbyte += 1
                    continue
                # frame layout: flags(4) history(1) keylen(4) key(32) NUL
                key = data[start + 9:start + 41].decode("ascii")
                path = ["<frame>", str(start)]
                export_entries.append(TextEntry(
                    asset=pkg.name, export_index=index,
                    class_name=ex.class_name, property_path=path,
                    kind="FText-Ascii", namespace="", key=key,
                    source=txt.decode("ascii"), offset=base + start))
                frame_ranges.append((start, start + 46 + ln))
                continue
            frame_ranges.append((start, end))
            path = _path_for(walked, start, end)
            if path is None:
                path = ["<frame>", str(start)]
            export_entries.append(TextEntry(
                asset=pkg.name, export_index=index,
                class_name=ex.class_name, property_path=path,
                kind="FText-Base", namespace="", key=key, source=source,
                offset=base + start))
        if ex.class_name == "DataTable":     # census-pinned FString context
            for pos, text in _chained_fstrings(data, frame_ranges):
                export_entries.append(TextEntry(
                    asset=pkg.name, export_index=index,
                    class_name=ex.class_name,
                    property_path=["<fstring>", str(pos)],
                    kind="FString", namespace="", key="", source=text,
                    offset=base + pos))
        if ex.class_name == "Function":      # Kismet bytecode string consts
            for op_pos, text, key, _units in _scan_bc_strings(data):
                # Phase 7: no JP gate — every user-visible literal is a
                # row (the gray tag sorts them when Japanese source is on)
                export_entries.append(TextEntry(
                    asset=pkg.name, export_index=index,
                    class_name=ex.class_name,
                    property_path=["<bcstring>", str(op_pos)],
                    kind="BcString", namespace="", key=key, source=text,
                    offset=base + op_pos))
        export_entries.sort(key=lambda e: e.offset)
        entries.extend(export_entries)
    if ansi_highbyte:
        warnings.append(f"ftext-ansi-highbyte: {ansi_highbyte}")
    if shape_w:
        # all framed sites (extracted BcStrings + non-JP glyphs); the
        # Phase-3 name "shape-W-unpinned" died with the Phase-6 pin
        warnings.append(f"shape-W-sites: {shape_w}")
    # Zen names (zen only): the package name batch carries FName strings,
    # and some games (ST's options menu) DISPLAY them — DataTable row
    # keys are the visible labels. Collect Japanese names risk-tagged
    # (yellow/red, skipped by default client-side) rather than guess
    # which are internal; the user decides. Main batch only: imported
    # package names and the script-object store are identity.
    for ne in getattr(pkg, "name_entries", None) or ():
        if not _has_jp(ne.text):
            continue
        entries.append(TextEntry(
            asset=pkg.name, export_index=-1, class_name=None,
            property_path=["<name>", str(ne.index)],
            kind="ZenName", namespace="", key="", source=ne.text,
            offset=ne.str_offset,
            meta={"encoding": ne.encoding, "bytes": ne.str_bytes,
                  "name_index": ne.index,
                      "risk": _zenname_risk(ne.text, asset_stems)}))
    return entries, warnings


# --------------------------------------------------------------------------
# per-game scan

def _script_objects(src):
    """The game's ScriptObjects chunk (zen only; type 5) or None. Class
    names stay None without it — nothing is guessed, nothing aborts."""
    try:
        for utoc_path, ucas_path in src.get("io_pairs") or ():
            with open(utoc_path, "rb") as f:
                toc = iostore.parse_toc(f.read())
            for i, e in enumerate(toc.entries):
                if e.chunk_type == 5:
                    return zen.parse_script_objects(
                        iostore.read_entry(toc, ucas_path, i))
    except Exception:
        return None
    return None


def scan_game(src: payload.PayloadSources, progress=None) -> dict:
    """Scan every package payload of one game. Returns
    ``{"entries": [TextEntry...], "stats": {assets_scanned, text_entries,
    by_kind, by_kind_jp, warnings, errors}}`` — warnings aggregate the
    per-package classes ("class: N" lines) into class -> total count;
    errors list per-asset parse failures (empty on both real games).

    Mode: zen (ST, single-file IoStore packages) when the sources carry
    IoStore pairs, else legacy (EF .uasset + exact-stem .uexp pairs; the
    package is named after the .uexp so entry offsets + asset together
    address the patchable payload). Each payload is read exactly once per
    run (parsed packages are not retained — entries carry extracted
    strings only).
    """
    files = payload.list_files(src)
    names = [n for n in files if n.lower().endswith((".uasset", ".umap"))]
    zen_mode = bool(src.get("io_pairs"))
    script_objects = _script_objects(src) if zen_mode else None
    stems = asset_stems(src) if zen_mode else None
    name_freq: dict = {}             # JP batch text -> #packages (zen)
    entries: list = []
    errors: list = []
    assets_scanned = 0
    by_kind: dict = {}
    by_kind_jp: dict = {}
    warnings: dict = {}
    total = len(names)
    for done, n in enumerate(names):
        if progress:
            progress(done, total, n)
        try:
            if zen_mode:
                pkg = zen.parse_zen(payload.read_file(src, n), n,
                                    script_objects=script_objects)
            else:
                stem = n[:-len(".uasset")] if n.endswith(".uasset") \
                    else n[:-len(".umap")]
                uexp_name = stem + ".uexp"
                pkg = uasset.parse_legacy(payload.read_file(src, n),
                                          payload.read_file(src, uexp_name),
                                          uexp_name)
            pkg_entries, pkg_warnings = scan_text(pkg, stems)
        except Exception as exc:             # noqa: BLE001 - recorded below
            errors.append(f"{n}: {exc!r}")
            continue
        if zen_mode:
            # one count per package (set semantics) — the domain of the
            # shared-identifier rule in upgrade_zenname_risk
            for t in {ne.text for ne in pkg.name_entries if _has_jp(ne.text)}:
                name_freq[t] = name_freq.get(t, 0) + 1
        assets_scanned += 1
        for e in pkg_entries:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
            if _has_jp(e.source):
                by_kind_jp[e.kind] = by_kind_jp.get(e.kind, 0) + 1
        entries.extend(pkg_entries)
        for w in pkg_warnings:
            cls, _, cnt = w.partition(":")
            warnings[cls.strip()] = warnings.get(cls.strip(), 0) + int(cnt)
    if zen_mode:
        upgrade_zenname_risk(entries, name_freq)
    return {
        "entries": entries,
        "stats": {
            "assets_scanned": assets_scanned,
            "text_entries": len(entries),
            "by_kind": by_kind,
            "by_kind_jp": by_kind_jp,
            "warnings": warnings,
            "errors": errors,
        },
    }
