"""Legacy package parser for SampleGameB — UE5-era legacy ``.uasset`` +
``.uexp`` packages out of a Phase 2 pak. Original code; the STRUCTURE is
ported from retoc (read-only reference: ``retoc/src/legacy_asset.rs``,
``version_heuristics.rs``), validated byte-for-byte against the real game
(see the Task 4 report).

The legacy package is a VIRTUAL STREAM: ``uasset_bytes + uexp_bytes`` are
two halves of one serial stream (probe: ``bulk_data_start_offset`` ==
len(uasset) + len(uexp) - 4). The head carries the package tag
0x9E2A83C1 FIRST (opposite of zen's trailing placement); the tail of the
``.uexp`` carries it again after the last export. ``total_header_size``
== the whole ``.uasset`` file for split packages, and every export's
``serial_offset`` is absolute in the virtual stream — so the export data
slice is ``uexp[serial_offset - total_header_size : + serial_size]``
(in-header offsets would slice the ``.uasset``; never observed in EF).

Layout of the head (all EF packages: unversioned, legacy_file_version
-8, presumed engine gate EUnrealEngineObjectUE5Version::DataResources —
UE 5.2/5.3; see ``_presume_version``)::

    u32 tag 0x9E2A83C1
    FLegacyPackageVersioningInfo (see LegacyVersionInfo):
        i32 legacy_file_version (-8 UE5, -7 UE4, <= -9 UE5.6+)
        i32 legacy_ue3_version (864 or 0)
        i32 file_version_ue4 [, i32 file_version_ue5 if lfv <= -8]
        i32 licensee_version
        [20-byte saved hash + i32 total_header_size]      lfv <= -9 only
        u32 custom-version count, 20 bytes each (FGuid + i32)
        i32 total_header_size                              lfv > -9 only
    FString package_name ("/Game/...")
    u32 package_flags
    [summary — field order per FLegacyPackageFileSummary, gated by the
     presumed/declared UE5 version; bools are 4 bytes (retoc reads
     ``read_u32`` for bool; confirmed by the exact 32-byte import stride
     and 96-byte export stride on the real game). ONE DEVIATION from
     stock UE 5.2/5.3 and retoc: an 8-byte zone where stock writes only
     PackageSource, after NumCompressedChunks (engine-build quirk,
     observed game-wide; see _read_summary)]
    name map at summary.names.offset: FString + u16 + u16 hashes per
    entry (FNameEntrySerialized; wide entries = negative-length UTF-16LE
    FStrings — 4,606 Japanese object names game-wide, e.g.
    ``03 薬バフ付与ステート``; every non-ASCII EF name is wide)
    import map: 4 x FMinimalName (i32 index, i32 number) + optional
     package name (not FilterEditorOnly) + is_optional u32 (>= 1003)
    export map: see LegacyExport (serial_size/serial_offset are i64)
    ... depends map (u32 count 0 per export), asset registry data,
     preload dependencies (i32 per entry) — skipped, offsets validated

Class names: an export's ``class_index`` is an FPackageIndex import
reference (negative); it resolves to that import's ``object_name`` via
the name map (e.g. DataTable). Anything else stays ``None`` — never
guessed.

Shape B: EF ``.uasset`` halves also serialize DataTable text as
``u32 | i32 -N | UTF-16LE`` cells (1,896 runs game-wide). This parser
treats halves as METADATA ONLY — the u32's provenance is an open risk
(discovery pins it read-only) and nothing here interprets it.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

PACKAGE_FILE_TAG = 0x9E2A83C1              # head (and .uexp tail) marker

# --- EUnrealEngineObjectUE5Version gates (values per UE/retoc zen.rs) ---
UE5_NAMES_REFERENCED_FROM_EXPORT_DATA = 1001
UE5_PAYLOAD_TOC = 1002
UE5_OPTIONAL_RESOURCES = 1003
UE5_LARGE_WORLD_COORDINATES = 1004
UE5_REMOVE_OBJECT_EXPORT_PACKAGE_GUID = 1005
UE5_TRACK_OBJECT_EXPORT_IS_INHERITED = 1006
UE5_ADD_SOFT_OBJECT_PATH_LIST = 1008
UE5_DATA_RESOURCES = 1009
UE5_SCRIPT_SERIALIZATION_OFFSET = 1010
UE5_METADATA_SERIALIZATION_OFFSET = 1014
UE5_VERSE_CELLS = 1015
UE5_PACKAGE_SAVED_HASH = 1016
UE5_OS_SUB_OBJECT_SHADOW_SERIALIZATION = 1017
UE5_IMPORT_TYPE_HIERARCHIES = 1018

UE4_VER_NON_OUTER_PACKAGE_IMPORT = 520     # minimum supported (retoc)
UE4_VER_CORRECT_LICENSEE_FLAG = 522        # 4.26/4.27 latest
VER_UE3_LATEST = 864

LEGACY_FILE_VERSION_UE4 = -7
LEGACY_FILE_VERSION_UE5 = -8
LEGACY_FILE_VERSION_UE5_6 = -9             # UE 5.6+ (and 5.7)

# --- package flags ---
PF_COOKED = 0x00000200
PF_FILTER_EDITOR_ONLY = 0x80000000
PF_UNVERSIONED_PROPERTIES = 0x00002000

# Unversioned-package candidate UE5 gates, tried newest-first (retoc
# heuristic_package_version_from_legacy_package; 5.2 and 5.3 share the
# DataResources gate and cannot be told apart from the summary alone,
# 5.4/5.5 cannot be told apart either — the first exact fit wins).
_CANDIDATES_UE5_6 = [UE5_OS_SUB_OBJECT_SHADOW_SERIALIZATION]        # 5.6/5.7
_CANDIDATES_UE5 = [1013, 1012, UE5_DATA_RESOURCES,                 # 5.5/5.4/5.3+5.2
                   UE5_ADD_SOFT_OBJECT_PATH_LIST,                  # 5.1
                   UE5_LARGE_WORLD_COORDINATES]                    # 5.0


class LegacyError(ValueError):
    """Base for legacy-package parse failures (named, never guessed past)."""


class LegacyUnsupportedError(LegacyError):
    """The payload uses a version/layout this parser does not support."""


class LegacyFormatError(LegacyError):
    """The payload violates the expected legacy layout (corrupt/new shape)."""


# --------------------------------------------------------------------------
# data model

@dataclass
class LegacyVersionInfo:
    """FLegacyPackageVersioningInfo (the summary's version block).

    ``presumed_ue5`` is the heuristic engine gate actually used to parse
    an unversioned package (0 = UE4 layout, None = versioned package,
    which declares its versions). Custom versions are (16-byte FGuid,
    i32 version) tuples.
    """
    legacy_file_version: int
    legacy_ue3_version: int
    file_version_ue4: int
    file_version_ue5: int
    licensee_version: int
    saved_hash: "bytes | None"
    custom_versions: "list[tuple[bytes, int]]"
    total_header_size: int
    is_unversioned: bool
    presumed_ue5: "int | None" = None


@dataclass
class LegacyImport:
    """FObjectImport with names resolved through the package name map."""
    class_package: str
    class_name: str
    outer_index: int                  # raw FPackageIndex (import/export)
    object_name: str
    is_optional: bool


@dataclass
class LegacyExport:
    """FObjectExport; ``data`` is the export's serial slice from the
    paired ``.uexp`` (empty when no .uexp was supplied)."""
    object_name: str
    class_index: int                  # raw FPackageIndex of the class
    class_name: "str | None"          # resolved import name; None = unknown
    super_index: int
    template_index: int
    outer_index: int
    object_flags: int
    serial_size: int
    serial_offset: int                # absolute in the uasset+uexp stream
    is_not_for_client: bool
    is_not_for_server: bool
    is_asset: bool
    data: bytes


@dataclass
class LegacyPackage:
    name: str                         # payload-layer file name passed in
    package_name: str                 # summary name ("/Game/...")
    package_flags: int
    total_header_size: int
    names: list[str]
    imports: list[LegacyImport]
    exports: list[LegacyExport]
    version_info: LegacyVersionInfo
    # Task 2 propagation metadata: the summary's bulk data start offset
    # (value + byte position in the .uasset) and per-export
    # (SerialSize pos, SerialOffset pos) pairs into the export map.
    # bulk_data_start_offset is None only for layouts lacking the field.
    bulk_data_start_offset: "int | None" = None
    _export_field_positions: "list[tuple[int, int]] | None" = None
    _bulk_data_start_offset_pos: int = -1

    def export_ranges(self) -> "list[tuple[int, int]]":
        """(start, end) byte range of every export's data, .uexp-relative
        (serial_offset - total_header_size); the Task 5 bridge for asking
        "is this byte inside an export?" without holding slices."""
        base = self.total_header_size
        return [(ex.serial_offset - base,
                 ex.serial_offset - base + ex.serial_size)
                for ex in self.exports]


# --------------------------------------------------------------------------
# low-level cursor

class _R:
    """Bounds-checked little-endian cursor over a bytes payload."""
    __slots__ = ("b", "p")

    def __init__(self, b: bytes, p: int = 0):
        self.b = b
        self.p = p

    def _need(self, n: int, what: str) -> None:
        if self.p < 0 or self.p + n > len(self.b):
            raise LegacyFormatError(
                f"{what} out of bounds at {self.p} (+{n} > {len(self.b)})")

    def i32(self) -> int:
        self._need(4, "i32")
        (v,) = struct.unpack_from("<i", self.b, self.p)
        self.p += 4
        return v

    def u32(self) -> int:
        self._need(4, "u32")
        (v,) = struct.unpack_from("<I", self.b, self.p)
        self.p += 4
        return v

    def i64(self) -> int:
        self._need(8, "i64")
        (v,) = struct.unpack_from("<q", self.b, self.p)
        self.p += 8
        return v

    def raw(self, n: int, what: str) -> bytes:
        self._need(n, what)
        out = self.b[self.p:self.p + n]
        self.p += n
        return out

    def fstring(self) -> str:
        """UE FString: i32 length; negative = UTF-16LE char count (NUL
        included), positive = byte count (NUL included)."""
        ln = self.i32()
        if ln < 0:
            n = -ln
            self._need(2 * n, "wide FString")
            units = struct.unpack_from(f"<{n}H", self.b, self.p)
            self.p += 2 * n
            s = "".join(map(chr, units))
            return s[:s.index("\x00")] if "\x00" in s else s
        self._need(ln, "ascii FString")
        raw = self.b[self.p:self.p + ln]
        self.p += ln
        raw = raw.split(b"\x00", 1)[0]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")


def _fname(names: list[str], r: _R) -> str:
    """FMinimalName: i32 name index + i32 number (number 0 = bare name,
    else ``name_{number-1}`` — the UE FName instance-number convention)."""
    index, number = r.i32(), r.i32()
    if not 0 <= index < len(names):
        raise LegacyFormatError(
            f"FName index {index} outside name map of {len(names)}")
    base = names[index]
    return base if number == 0 else f"{base}_{number - 1}"


# --------------------------------------------------------------------------
# versioning info + summary

def _read_versioning(r: _R) -> LegacyVersionInfo:
    lfv = r.i32()
    if lfv > LEGACY_FILE_VERSION_UE4:
        raise LegacyUnsupportedError(
            f"legacy file version {lfv} too old (supported: -7 UE4, "
            f"-8 UE5, <= -9 UE5.6+)")
    ue3 = r.i32()
    if ue3 not in (VER_UE3_LATEST, 0):
        raise LegacyFormatError(
            f"legacy UE3 version {ue3} (want {VER_UE3_LATEST} or 0)")
    ue4 = r.i32()
    ue5 = r.i32() if lfv <= LEGACY_FILE_VERSION_UE5 else 0
    licensee = r.i32()
    saved_hash = None
    total_header_size = None
    if lfv <= LEGACY_FILE_VERSION_UE5_6:      # with package saved hash
        saved_hash = r.raw(20, "saved hash")
        total_header_size = r.i32()
    customs: "list[tuple[bytes, int]]" = []
    for _ in range(r.u32()):                  # custom versions
        guid = r.raw(16, "custom version guid")
        customs.append((guid, r.i32()))
    if total_header_size is None:             # pre-saved-hash placement
        total_header_size = r.i32()
    unversioned = (ue3 == 0 and ue4 == 0 and ue5 == 0 and licensee == 0
                   and not customs)
    return LegacyVersionInfo(
        legacy_file_version=lfv, legacy_ue3_version=ue3,
        file_version_ue4=ue4, file_version_ue5=ue5,
        licensee_version=licensee, saved_hash=saved_hash,
        custom_versions=customs, total_header_size=total_header_size,
        is_unversioned=unversioned)


class _Summary:
    """The summary fields this parser consumes (everything else is read
    past with version-gated strides and validated where cheap)."""
    __slots__ = ("names", "imports", "exports", "depends_offset", "bulk_pos")

    def __init__(self, names: tuple, imports: tuple, exports: tuple,
                 depends_offset: int, bulk_pos: int = -1):
        self.names = names
        self.imports = imports
        self.exports = exports
        self.depends_offset = depends_offset
        self.bulk_pos = bulk_pos


def _pair(r: _R) -> "tuple[int, int]":
    return (r.i32(), r.i32())


def _read_summary(r: _R, ue5: int, filter_editor_only: bool) -> _Summary:
    names = _pair(r)
    if ue5 >= UE5_ADD_SOFT_OBJECT_PATH_LIST:
        _pair(r)                                # soft object paths
    if not filter_editor_only:
        r.fstring()                             # localization id
    _pair(r)                                    # gatherable text data
    exports = _pair(r)
    imports = _pair(r)
    if ue5 >= UE5_VERSE_CELLS:
        _pair(r)                                # cell exports
        _pair(r)                                # cell imports
    if ue5 >= UE5_METADATA_SERIALIZATION_OFFSET:
        r.i32()                                 # metadata offset
    depends_offset = r.i32()
    _pair(r)                                    # soft package references
    r.i32()                                     # searchable names offset
    r.i32()                                     # thumbnail table offset
    if ue5 >= UE5_IMPORT_TYPE_HIERARCHIES:
        _pair(r)                                # import type hierarchies
    if ue5 < UE5_PACKAGE_SAVED_HASH:
        r.raw(16, "package guid")
    if not filter_editor_only:
        r.raw(16, "persistent package guid")
    (ngen,) = struct.unpack("<I", r.raw(4, "generation count"))
    r.raw(ngen * 8, "generations")
    for _ in range(2):                          # engine versions (unused)
        r.raw(8, "engine version")
        r.fstring()                             # branch
    compression = r.u32()
    nchunks = r.i32()
    if compression != 0 or nchunks != 0:
        raise LegacyFormatError(
            f"legacy compression present (flags {compression:#x}, "
            f"chunks {nchunks}) — unsupported")
    # ENGINE-BUILD DEVIATION from stock UE 5.2/5.3 (and retoc): an 8-byte
    # zone where stock serializes only PackageSource (4). It holds
    # PackageSource plus one unidentified i32 — observed as (CRC, 0) on
    # some packages and (0, CRC) on others, size always 8 (see the
    # report). Read past raw; the fit check validates it game-wide.
    r.raw(8, "package source zone")
    for _ in range(r.u32()):                    # additional packages to cook
        r.fstring()
    r.i32()                                     # asset registry data offset
    bulk_pos = r.p                              # (Task 2 propagation)
    r.i64()                                     # bulk data start offset
    r.i32()                                     # world tile info offset
    (nch,) = struct.unpack("<I", r.raw(4, "chunk id count"))
    r.raw(nch * 4, "chunk ids")
    _pair(r)                                    # preload dependencies
    if ue5 >= UE5_NAMES_REFERENCED_FROM_EXPORT_DATA:
        r.i32()                                 # names referenced from export
    if ue5 >= UE5_PAYLOAD_TOC:
        r.i64()                                 # payload TOC offset
    if ue5 >= UE5_DATA_RESOURCES:
        r.i32()                                 # data resource offset
    return _Summary(names, imports, exports, depends_offset, bulk_pos)


def _read_name_map(r: _R, count: int, offset: int) -> "list[str]":
    """FNameEntrySerialized table: FString + 2 x u16 hashes per entry.
    Wide (negative-length UTF-16) entries decode like narrow ones."""
    if offset < 0 or offset > len(r.b):
        raise LegacyFormatError(f"name map offset {offset} invalid")
    r.p = offset
    names: "list[str]" = []
    for _ in range(count):
        name = r.fstring()
        r.raw(4, "name entry hashes")           # hashes verified unused
        names.append(name)
    return names


def _read_imports(r: _R, count: int, offset: int, ue5: int,
                  filter_editor_only: bool, names: list[str]
                  ) -> "list[LegacyImport]":
    if count == 0:
        return []
    if offset < 0 or offset > len(r.b):
        raise LegacyFormatError(f"import map offset {offset} invalid")
    r.p = offset
    out: "list[LegacyImport]" = []
    for _ in range(count):
        class_package = _fname(names, r)
        class_name = _fname(names, r)
        outer_index = r.i32()
        object_name = _fname(names, r)
        if not filter_editor_only:
            _fname(names, r)                    # One-File-Per-Actor name
        is_optional = False
        if ue5 >= UE5_OPTIONAL_RESOURCES:
            is_optional = bool(r.u32())         # bools are 4 bytes (retoc)
        out.append(LegacyImport(
            class_package=class_package, class_name=class_name,
            outer_index=outer_index, object_name=object_name,
            is_optional=is_optional))
    return out


def _read_exports(r: _R, count: int, offset: int, ue5: int,
                  unversioned_props: bool) -> "tuple[list, list]":
    """(raw export tuples, per-export serial field positions). Class/name
    resolution happens once the name map is known; the caller positions
    are what the version fit checks use, and the (SerialSize, SerialOffset)
    byte positions feed the Task 2 propagation."""
    if count == 0:
        return [], []
    if offset < 0 or offset > len(r.b):
        raise LegacyFormatError(f"export map offset {offset} invalid")
    r.p = offset
    out: "list[tuple]" = []
    positions: "list[tuple[int, int]]" = []     # (serial_size pos, offset pos)
    for _ in range(count):
        class_index = r.i32()
        super_index = r.i32()
        template_index = r.i32()
        outer_index = r.i32()
        oname_index, oname_number = r.i32(), r.i32()
        object_flags = r.u32()
        size_pos = r.p
        serial_size = r.i64()
        offset_pos = r.p
        serial_offset = r.i64()
        r.u32()                                 # is_forced_export
        is_not_for_client = bool(r.u32())
        is_not_for_server = bool(r.u32())
        if ue5 < UE5_REMOVE_OBJECT_EXPORT_PACKAGE_GUID:
            r.raw(16, "export package guid")
        if ue5 >= UE5_TRACK_OBJECT_EXPORT_IS_INHERITED:
            r.u32()                             # is_inherited_instance
        r.u32()                                 # export package flags
        r.u32()                                 # is_not_always_loaded_for_editor_game
        is_asset = bool(r.u32())
        if ue5 >= UE5_OPTIONAL_RESOURCES:
            r.u32()                             # generate_public_hash
        for _ in range(5):                      # first dep + 4 sort ints
            r.i32()
        if not unversioned_props \
                and ue5 >= UE5_SCRIPT_SERIALIZATION_OFFSET:
            r.i64()                             # script serialization start
            r.i64()                             # script serialization end
        out.append((oname_index, oname_number, class_index, super_index,
                    template_index, outer_index, object_flags, serial_size,
                    serial_offset, is_not_for_client, is_not_for_server,
                    is_asset))
        positions.append((size_pos, offset_pos))
    return out, positions


# --------------------------------------------------------------------------
# unversioned-package engine-version heuristic (retoc
# heuristic_package_version_from_legacy_package; the fit check here is
# end-to-end — full name/import/export tables must land exactly on their
# section boundaries — where retoc checks the first entry's stride)

def _fit(b: bytes, summary_end: int, ue5: int, flags: int, s: _Summary,
         names: list[str]) -> bool:
    nc, no = s.names
    ic, io = s.imports
    ec, eo = s.exports
    dep = s.depends_offset
    if min(no, io, eo, dep) < 0:
        return False
    if no != summary_end:                       # names follow the summary
        return False
    if nc > 0 and not no < io:                   # standard order n < i <= e < d
        return False
    if ic > 0 and io >= eo:
        return False
    if ec > 0 and eo >= dep:
        return False
    if io < no or eo < io or dep < eo:           # empty sections coincide
        return False
    r = _R(b, summary_end)
    _read_name_map(r, nc, no)                   # r.p -> name map end
    if r.p != io:
        return False
    _read_imports(r, ic, io, ue5, bool(flags & PF_FILTER_EDITOR_ONLY), names)
    if ic > 0 and r.p != eo:
        return False
    _read_exports(r, ec, eo, ue5, bool(flags & PF_UNVERSIONED_PROPERTIES))
    if ec > 0 and r.p != dep:
        return False
    return True


def _presume_version(b: bytes, p: int, lfv: int, flags: int) -> int:
    """Retoc's unversioned fallback: try the engine gates newest-first and
    keep the first whose layout fits the head exactly. Returns the UE5
    gate (0 for the UE4 layout). Requires Cooked | FilterEditorOnly
    (engine intrinsics; retoc bails otherwise)."""
    if flags & PF_COOKED != PF_COOKED \
            or flags & PF_FILTER_EDITOR_ONLY != PF_FILTER_EDITOR_ONLY:
        raise LegacyUnsupportedError(
            "cannot presume a version for an unversioned package that is "
            "not cooked with editor data filtered out "
            f"(flags {flags:#010x})")
    if lfv <= LEGACY_FILE_VERSION_UE5_6:
        candidates = _CANDIDATES_UE5_6
    elif lfv <= LEGACY_FILE_VERSION_UE5:
        candidates = _CANDIDATES_UE5
    else:
        candidates = [0]                        # UE4 4.26/4.27 layout
    for cand in candidates:
        ue5 = cand if lfv <= LEGACY_FILE_VERSION_UE5 else 0
        try:
            probe = _R(b, p)
            s = _read_summary(probe, ue5,
                              bool(flags & PF_FILTER_EDITOR_ONLY))
            names = _read_name_map(_R(b, 0), s.names[0], s.names[1])
            if not _fit(b, probe.p, ue5, flags, s, names):
                continue
        except LegacyError:
            continue
        return cand
    raise LegacyFormatError(
        "no candidate engine version fits this unversioned package head")


# --------------------------------------------------------------------------
# main entry

def parse_legacy(uasset: bytes, uexp: "bytes | None", name: str = ""
                 ) -> LegacyPackage:
    """Parse one legacy .uasset head (+ its paired .uexp, when available).

    ``uexp`` supplies export data slices; without it every export's
    ``data`` is empty (metadata-only parse — used for EF .uasset halves,
    which carry shape-B text this parser deliberately does not touch).
    Unknown or non-conforming structure raises LegacyError
    (LegacyUnsupportedError for old versions, LegacyFormatError for
    structural violations); nothing is guessed past the failure.
    """
    r = _R(uasset)
    (tag,) = struct.unpack("<I", r.raw(4, "package tag"))
    if tag != PACKAGE_FILE_TAG:
        raise LegacyFormatError(
            f"bad package tag {tag:#010x} (want {PACKAGE_FILE_TAG:#010x})")
    vi = _read_versioning(r)
    package_name = r.fstring()
    (flags,) = struct.unpack("<I", r.raw(4, "package flags"))
    filter_editor_only = bool(flags & PF_FILTER_EDITOR_ONLY)
    unversioned_props = bool(flags & PF_UNVERSIONED_PROPERTIES)

    presumed = None
    if vi.is_unversioned:
        presumed = _presume_version(r.b, r.p, vi.legacy_file_version, flags)
        ue5_gate = presumed
    else:
        if vi.file_version_ue4 < UE4_VER_NON_OUTER_PACKAGE_IMPORT:
            raise LegacyUnsupportedError(
                f"UE4 package file version {vi.file_version_ue4} below "
                f"minimum supported {UE4_VER_NON_OUTER_PACKAGE_IMPORT}")
        ue5_gate = vi.file_version_ue5

    summary = _read_summary(r, ue5_gate, filter_editor_only)
    names = _read_name_map(r, summary.names[0], summary.names[1])
    imports = _read_imports(r, summary.imports[0], summary.imports[1],
                            ue5_gate, filter_editor_only, names)
    raw_exports, export_positions = _read_exports(
        r, summary.exports[0], summary.exports[1], ue5_gate,
        unversioned_props)

    exports: "list[LegacyExport]" = []
    ths = vi.total_header_size
    if ths < 0:
        raise LegacyFormatError(f"negative total header size {ths}")
    for (oname_index, oname_number, class_index, super_index, template_index,
         outer_index, object_flags, serial_size, serial_offset,
         is_not_for_client, is_not_for_server, is_asset) in raw_exports:
        if not 0 <= oname_index < len(names):
            raise LegacyFormatError(
                f"export name index {oname_index} outside name map")
        oname = names[oname_index]
        if oname_number:
            oname = f"{oname}_{oname_number - 1}"
        class_name: "str | None" = None
        if class_index < 0:                     # import reference
            imp_i = -class_index - 1
            if imp_i < len(imports):
                class_name = imports[imp_i].object_name
        data = b""
        if uexp is not None and serial_size > 0:
            if serial_offset >= ths:            # split-stream half (.uexp)
                start = serial_offset - ths
                if start + serial_size > len(uexp):
                    raise LegacyFormatError(
                        f"export {oname!r} slice [{serial_offset}, "
                        f"{serial_offset + serial_size}) outside the "
                        f".uexp stream ({len(uexp)} bytes)")
                data = uexp[start:start + serial_size]
            elif 0 <= serial_offset \
                    and serial_offset + serial_size <= len(uasset):
                data = uasset[serial_offset:serial_offset + serial_size]
            else:
                raise LegacyFormatError(
                    f"export {oname!r} slice [{serial_offset}, "
                    f"{serial_offset + serial_size}) outside the package "
                    "stream")
        exports.append(LegacyExport(
            object_name=oname, class_index=class_index, class_name=class_name,
            super_index=super_index, template_index=template_index,
            outer_index=outer_index, object_flags=object_flags,
            serial_size=serial_size, serial_offset=serial_offset,
            is_not_for_client=is_not_for_client,
            is_not_for_server=is_not_for_server, is_asset=is_asset,
            data=data))

    if uexp is not None and len(uexp) >= 4:
        (tail,) = struct.unpack_from("<I", uexp, len(uexp) - 4)
        if tail != PACKAGE_FILE_TAG:            # drift catch (zen parity)
            raise LegacyFormatError(
                f"bad .uexp trailing tag {tail:#010x} "
                f"(want {PACKAGE_FILE_TAG:#010x})")

    vi.presumed_ue5 = presumed
    bulk_value = None
    if summary.bulk_pos >= 0:
        (bulk_value,) = struct.unpack_from("<q", uasset, summary.bulk_pos)
    return LegacyPackage(
        name=name, package_name=package_name, package_flags=flags,
        total_header_size=ths, names=names, imports=imports, exports=exports,
        version_info=vi, bulk_data_start_offset=bulk_value,
        _export_field_positions=export_positions,
        _bulk_data_start_offset_pos=summary.bulk_pos)


# --------------------------------------------------------------------------
# package-size propagation (Phase 4a, Task 2)
#
# Discovery (probed read-only on all 7,656 EF pairs): the split package
# is one virtual stream uasset+uexp whose exports sit CONTIGUOUSLY in
# the .uexp over [total_header_size, ths + len(uexp) - 4) — zero gaps,
# zero alignment padding, first export at ths, last ending on the .uexp
# tail tag; ths == len(uasset) for every pair; and the summary's
# bulk_data_start_offset == len(uasset) + len(uexp) - 4 for every pair
# (the value is present in 7,656/7,656 .uasset halves). A text patch in
# the .uexp therefore needs exactly three things after Task 1's splice:
# each patched export's SerialSize (fixed i64 inside its export-map
# entry), each later export's SerialOffset (i64, absolute in the
# virtual stream), and bulk_data_start_offset (i64). The .uasset never
# changes SIZE, so this is a fixed-width in-place field rewrite — no
# re-serializer is needed and untouched bytes stay byte-identical by
# construction (null patch == identity). The engine-build "package
# source zone" CRC (unidentified i32; Phase 2 survey) is left untouched
# — its provenance is unknown and rewriting it would be a guess.

def patch_legacy_pair(uasset_bytes: bytes, uexp_bytes: bytes, entries: list,
                      translations: "dict[int, str]"
                      ) -> "tuple[bytes, bytes]":
    """Patch one legacy .uasset/.uexp pair.

    ``entries`` are the asset's TextEntry rows (.uexp-relative offsets,
    the scan contract) and ``translations`` maps entry index ->
    replacement text (Task 1 contract; frames are re-verified against
    the .uexp before any splice). Returns ``(uasset, uexp)``: the .uexp
    splice + per-export SerialSize/SerialOffset rewrites + the summary
    bulk_data_start_offset update; the .uexp tail tag is preserved.
    Null patch (``{}``) returns the pair byte-identically. Raises
    LegacyError (named) when a patchable range straddles an export
    boundary, when the pair breaks the game-wide layout pins (ths ==
    len(uasset), bulk_data_start_offset == ths + len(uexp) - 4), or
    when the result would not re-parse; rejections leave the caller's
    bytes untouched.
    """
    from .patchtext import patch_payload, patch_span

    new_uexp, report = patch_payload(uexp_bytes, entries, translations)
    pkg = parse_legacy(uasset_bytes, uexp_bytes,
                       entries[0].asset if entries else "")

    patches = []                              # (start, end, shift)
    for p in report.get("patched", ()):
        patches.append(patch_span(entries[p["entry_index"]], p))
    if not patches:
        return uasset_bytes, uexp_bytes        # null patch: identity

    ths = pkg.total_header_size
    if ths != len(uasset_bytes):
        raise LegacyUnsupportedError(
            f"total header size {ths} != len(uasset) {len(uasset_bytes)} — "
            "in-header trailing data layout unknown, refusing to patch")

    def delta(pos: int) -> int:
        """How far content at ORIGINAL .uexp position ``pos`` moves."""
        return sum(sh for _lp, oe, sh in patches if oe <= pos)

    out = bytearray(new_uexp)
    new_ua = bytearray(uasset_bytes)

    for ex, (size_pos, offset_pos) in zip(pkg.exports,
                                          pkg._export_field_positions):
        if ex.serial_offset < ths:
            continue                           # in-header export: no text
        start = ex.serial_offset - ths         # .uexp-relative
        end = start + ex.serial_size
        for lp, oe, _sh in patches:
            if lp < start < oe or lp < end < oe:
                raise LegacyFormatError(
                    f"patchable range [{lp:#x}, {oe:#x}) straddles an "
                    f"export boundary at {start:#x} ({ex.object_name!r}) — "
                    "refusing to splice")
        grown = sum(sh for lp, oe, sh in patches
                    if start <= lp and oe <= end)
        struct.pack_into("<q", new_ua, offset_pos,
                         ex.serial_offset + delta(start))
        struct.pack_into("<q", new_ua, size_pos, ex.serial_size + grown)

    if pkg.bulk_data_start_offset is None:
        raise LegacyUnsupportedError(
            "package summary carries no bulk_data_start_offset — "
            "unknown layout, refusing to patch")
    if pkg.bulk_data_start_offset != ths + len(uexp_bytes) - 4:
        raise LegacyFormatError(
            f"bulk_data_start_offset {pkg.bulk_data_start_offset} breaks "
            f"the game-wide pin (== len(uasset) + len(uexp) - 4 = "
            f"{ths + len(uexp_bytes) - 4}) — refusing to guess the update")
    net = sum(sh for _lp, _oe, sh in patches)
    struct.pack_into("<q", new_ua, pkg._bulk_data_start_offset_pos,
                     pkg.bulk_data_start_offset + net)

    result_ua, result_ux = bytes(new_ua), bytes(out)
    parse_legacy(result_ua, result_ux, pkg.name)   # full self-check
    return result_ua, result_ux
