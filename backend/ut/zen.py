"""Zen (IoStore) package parser for SampleGameA — UE 5.6.1 single-file
packages. Original code; the STRUCTURE is ported from retoc (read-only
reference: ``retoc/src/zen.rs``, ``name_map.rs``, ``script_objects.rs``),
validated byte-for-byte against the real game (see the Task 3 report).

What a "package payload" is here: the ExportBundleData chunk behind each
``sample_a/Content/....uasset`` / ``.umap`` name in the payload layer
(``payload.read_file``). Layout, for the one container-header version ST
ships (``SoftPackageReferencesOffset`` = 5, UE 5.6.1 — probed: all 1,359
packages match it)::

    FZenPackageSummary (see _read_summary)
    [FZenPackageVersioningInfo]        only when summary.has_versioning_info
                                       (ST packages are unversioned)
    cell_import_map_offset, cell_export_map_offset (2 x i32, UE 5.6+)
    FPackageNameBatch  (inline: num, num_string_bytes, hash version u64,
                        num x u64 hashes, num x BE i16 lengths, strings)
    [u64 pad + padding bytes]          UE 5.4+ alignment before the map
    i64 bulk_data_map_size + entries   FBulkDataMapEntry = 32 bytes each
    imported_public_export_hashes      u64 x (import_map_offset - ipeh)/8
    import map                         FPackageObjectIndex x 8 bytes
    export map                         FExportMapEntry x 72 bytes
    cell import map / cell export map  8 / 40 bytes per entry (empty in ST)
    export bundle entries              2 per export (observed: tier-grouped
                                       interleaving — a Create run then the
                                       matching Serialize run per dependency
                                       tier, e.g. WBP_痴漢システム: C×30 S×29
                                       C×8 S×8 … C×137 S×113; each run in
                                       dependency order)
    dependency bundle headers/entries  20 / 4 bytes per entry
    imported package names             name batch + i32 numbers

Export data lives AFTER the header: export slice = payload[
header_size + cooked_serial_offset : + cooked_serial_size] (retoc
asset_conversion.rs: "Zen's serial offset is relative to the start of
export data, which is at HeaderSize"). The chunk ends with the package
tag 0x9E2A83C1 after the last export (not part of any slice; validated
by parse_zen as the final 4 bytes).

Script imports (FPackageObjectIndex kind 1) carry a CityHash of the
object path — they resolve through the game's global ScriptObjects chunk
(type 5, in ST's global.utoc/global.ucas, all-zero chunk id):
``parse_script_objects`` builds the raw-index -> "/Script/..." table;
``parse_zen(..., script_objects=...)`` uses it to fill export class
names. Without the store those names stay ``None`` (never guessed).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --- container-header version gating (only ST's shipped version parses) ---
CHV_SOFT_PACKAGE_REFERENCES_OFFSET = 5     # UE 5.6+; the only supported one

# FPackageObjectIndex: 2 kind bits on top, 62-bit value below.
POI_KIND_EXPORT, POI_KIND_SCRIPT, POI_KIND_PACKAGE, POI_KIND_NULL = 0, 1, 2, 3
POI_INDEX_MASK = (1 << 62) - 1

_I16_MIN = -32768
_NAME_HASH_ALGORITHM_ID = 0xC1640000

PACKAGE_FILE_TAG = 0x9E2A83C1              # trailing u32 of every zen chunk
EXPORT_ENTRY_SIZE = 72                     # FExportMapEntry (repr(C))
BULK_ENTRY_SIZE = 32                       # FBulkDataMapEntry
CELL_IMPORT_ENTRY_SIZE = 8                 # FPackageObjectIndex
CELL_EXPORT_ENTRY_SIZE = 40                # FCellExportMapEntry
BUNDLE_ENTRY_SIZE = 8                      # FExportBundleEntry
DEP_HEADER_SIZE = 20                       # FDependencyBundleHeader
DEP_ENTRY_SIZE = 4                         # FDependencyBundleEntry


class ZenError(ValueError):
    """Base for zen-package parse failures (named, never guessed past)."""


class ZenUnsupportedError(ZenError):
    """The payload uses a layout this parser does not support."""


class ZenFormatError(ZenError):
    """The payload violates the expected zen layout (corrupt or new shape)."""


# --------------------------------------------------------------------------
# script objects (global store; needed to resolve script-import class names)

@dataclass
class ScriptObjects:
    """Global name batch + script object entries, keyed by the raw
    FPackageObjectIndex value (the same u64 a package's import map holds)."""
    names: list[str]
    _by_index: dict[int, tuple[str, int, int]] = field(repr=False, default_factory=dict)

    def resolve(self, raw: int) -> "str | None":
        """Full object path ("/Script/Engine.DataTable") for a script-import
        raw index, or None when unknown."""
        chain = []
        cur = raw
        for _ in range(16):                       # bounded outer walk
            entry = self._by_index.get(cur)
            if entry is None:
                return None
            name, outer, _cdo = entry
            chain.append(name)
            if outer >> 62 == POI_KIND_NULL or outer == 0xFFFFFFFFFFFFFFFF:
                break
            cur = outer
        return ".".join(reversed(chain)) if chain else None


def parse_script_objects(payload: bytes) -> ScriptObjects:
    """Parse a ScriptObjects chunk: global name batch + u32 count +
    FScriptObjectEntry x 32 bytes (FMappedName + 3 x FPackageObjectIndex)."""
    names, pos, _geo = _read_name_batch(payload, 0)
    if pos + 4 > len(payload):
        raise ZenFormatError("script objects chunk truncated before entry count")
    (count,) = struct.unpack_from("<I", payload, pos)
    pos += 4
    if count * 32 != len(payload) - pos:
        raise ZenFormatError(
            f"script objects entry count {count} leaves "
            f"{len(payload) - pos} bytes (want {count * 32})")
    store: dict[int, tuple[str, int, int]] = {}
    for i in range(count):
        base = pos + i * 32
        iat, number, global_index, outer_index, cdo = struct.unpack_from(
            "<IIQQQ", payload, base)
        store[global_index] = (_mapped_name(names, iat, number), outer_index, cdo)
    return ScriptObjects(names=names, _by_index=store)


# --------------------------------------------------------------------------
# package model

@dataclass
class ZenVersionInfo:
    """FZenPackageVersioningInfo (present only for versioned packages)."""
    zen_version: int
    file_version_ue4: int
    file_version_ue5: int
    licensee_version: int
    custom_versions: "list[tuple[bytes, int]]"


@dataclass
class BulkDataMapEntry:
    serial_offset: int
    duplicate_serial_offset: int
    serial_size: int
    flags: int
    cooked_index: int


@dataclass
class ZenImport:
    """One import-map FPackageObjectIndex, resolved where cheap.

    kind: "export" (this package) / "script" / "package" / "null".
    ``name`` is the full script path (script), the imported package name
    (package), or the local export name (export); None when unresolvable
    without reading other packages (the public-export hash of a package
    import names an object in ANOTHER package — not cheap, so
    ``export_hash`` carries it unresolved)."""
    kind: str
    raw: int
    name: "str | None" = None
    package_name: "str | None" = None
    export_hash: "int | None" = None


@dataclass
class ZenExport:
    object_name: str
    class_index: int                  # raw FPackageObjectIndex of the class
    class_name: "str | None"          # leaf class name; None = unresolved
    class_path: "str | None"          # full "/Script/..." path when known
    outer_index: int
    super_index: int
    template_index: int
    serial_offset: int                # cooked, relative to header_size
    serial_size: int
    object_flags: int
    filter_flags: int
    public_export_hash: int
    data: bytes                       # payload[header_size+off : +size]


@dataclass
class ZenPackage:
    name: str                         # payload-layer file name passed in
    package_name: str                 # mapped summary name ("/Game/...")
    header_size: int
    cooked_header_size: int
    package_flags: int
    is_unversioned: bool
    version_info: "ZenVersionInfo | None"
    names: list[str]
    imports: list[ZenImport]
    exports: list[ZenExport]
    bulk_map: list[BulkDataMapEntry]
    imported_public_export_hashes: list[int]
    imported_package_names: "list[str | None]"
    export_bundle_entries: "list[tuple[int, int]]"
    cell_import_count: int
    cell_export_count: int
    # Payload positions of the fixed-stride tables (Task 2 propagation):
    # export i's SerialOffset i64 at export_map_offset + 72*i, its
    # SerialSize i64 at +8; bulk entry j's SerialOffset at
    # _bulk_map_offset + 32*j (-1 when the package has no bulk map).
    export_map_offset: int = -1
    _bulk_map_offset: int = -1
    # Name-batch patch geometry (zen only): per-entry byte positions plus
    # the payload offset of the batch's u32 num_string_bytes field (-1
    # when the batch is empty). The imported-package-names batch is NOT
    # recorded — package paths are identity, never patchable.
    name_entries: "list[NameBatchEntry]" = field(default_factory=list)
    _name_num_string_bytes_offset: int = -1


# --------------------------------------------------------------------------
# low-level readers

@dataclass
class NameBatchEntry:
    """One name-batch entry's patch geometry (payload-absolute).

    ``encoding`` is the storage charset ("ascii" 1 B/char, "wide"
    UTF-16LE 2 B/char); ``header_offset`` is the entry's BE i16 length
    header (positive = ascii byte count, ``i16::MIN - chars`` = wide);
    ``hash_offset`` the entry's u64 CityHash64 of the LOWERCASED string
    in its storage charset (verified against 23,712 real ST hashes;
    see :mod:`ut.cityhash`)."""
    index: int
    text: str
    encoding: str                     # "ascii" | "wide"
    str_offset: int
    str_bytes: int
    header_offset: int
    hash_offset: int


def _read_name_batch(buf: bytes, pos: int) -> "tuple[list[str], int, list[NameBatchEntry]]":
    """UE name batch: u32 num [u32 string-bytes, u64 hash version,
    num x u64 hashes, num x BE i16 length headers, strings]. Wide strings
    encode length as i16::MIN - char_count (chars = 32768 + header).
    Returns ``(names, end_pos, per-name geometry)``."""
    if pos + 4 > len(buf):
        raise ZenFormatError(f"name batch header out of bounds at {pos}")
    batch_start = pos
    (num,) = struct.unpack_from("<I", buf, pos)
    pos += 4
    if num == 0:
        return [], pos, []
    if pos + 12 + num * 8 > len(buf):
        raise ZenFormatError("name batch hashes out of bounds")
    pos += 4                                   # num_string_bytes (unused)
    (hash_version,) = struct.unpack_from("<Q", buf, pos)
    if hash_version != _NAME_HASH_ALGORITHM_ID:
        raise ZenFormatError(f"unexpected name-batch hash version {hash_version:#x}")
    pos += 8
    hash_base = pos
    pos += num * 8                             # hashes
    if pos + num * 2 > len(buf):
        raise ZenFormatError("name batch length headers out of bounds")
    hdr_base = pos
    lengths = struct.unpack_from(f">{num}h", buf, pos)
    pos += num * 2
    names: list[str] = []
    entries: list[NameBatchEntry] = []
    for i, h in enumerate(lengths):
        if h >= 0:
            if pos + h > len(buf):
                raise ZenFormatError("name batch ascii string out of bounds")
            names.append(buf[pos:pos + h].decode("ascii"))
            entries.append(NameBatchEntry(
                i, names[-1], "ascii", pos, h, hdr_base + i * 2,
                hash_base + i * 8))
            pos += h
        else:
            wide = _I16_MIN - h                # char count, positive
            n = -wide                          # read_string_data(-n)
            if pos + 2 * n > len(buf):
                raise ZenFormatError("name batch wide string out of bounds")
            names.append("".join(map(chr, struct.unpack_from(f"<{n}H", buf, pos))))
            entries.append(NameBatchEntry(
                i, names[-1], "wide", pos, 2 * n, hdr_base + i * 2,
                hash_base + i * 8))
            pos += 2 * n
    return names, pos, entries


def name_batch_meta(batch_start: int) -> int:
    """Payload offset of the batch's ``u32 num_string_bytes`` field."""
    return batch_start + 4


def _mapped_name(names: list[str], index_and_type: int, number: int) -> str:
    index = index_and_type & 0x3FFFFFFF
    if index >= len(names):
        raise ZenFormatError(f"mapped name index {index} outside batch of {len(names)}")
    base = names[index]
    return f"{base}_{number - 1}" if number else base


def _read_versioning_info(buf: bytes, pos: int) -> "tuple[ZenVersionInfo, int]":
    if pos + 20 > len(buf):
        raise ZenFormatError("versioning info truncated")
    zen_version, ue4, ue5, licensee = struct.unpack_from("<Iiii", buf, pos)
    pos += 16
    (count,) = struct.unpack_from("<I", buf, pos)
    pos += 4
    if zen_version > 3:
        raise ZenFormatError(f"unknown EZenPackageVersion {zen_version}")
    if pos + count * 20 > len(buf):
        raise ZenFormatError("custom versions truncated")
    customs = []
    for i in range(count):
        base = pos + i * 20
        guid = buf[base:base + 16]
        (ver,) = struct.unpack_from("<i", buf, base + 16)
        customs.append((guid, ver))
    pos += count * 20
    return ZenVersionInfo(zen_version, ue4, ue5, licensee, customs), pos


# --------------------------------------------------------------------------
# summary

@dataclass
class _Summary:
    has_versioning_info: int
    header_size: int
    name: "tuple[int, int]"           # (index_and_type, number)
    package_flags: int
    cooked_header_size: int
    imported_public_export_hashes_offset: int
    import_map_offset: int
    export_map_offset: int
    export_bundle_entries_offset: int
    dependency_bundle_headers_offset: int
    dependency_bundle_entries_offset: int
    imported_package_names_offset: int


def _read_summary(buf: bytes) -> _Summary:
    # container_header_version == SoftPackageReferencesOffset layout only.
    if len(buf) < 64:
        raise ZenFormatError(f"payload too small for zen summary ({len(buf)} bytes)")
    (has_vi, header_size, name_iat, name_num, package_flags, cooked_header_size,
     ipeh, import_map, export_map, bundle_entries,
     dep_headers, dep_entries, imported_names) = struct.unpack_from(
        "<IIIIIIiiiiiii", buf, 0)
    s = _Summary(has_vi, header_size, (name_iat, name_num), package_flags,
                 cooked_header_size, ipeh, import_map, export_map,
                 bundle_entries, dep_headers, dep_entries, imported_names)
    offsets = [ipeh, import_map, export_map, bundle_entries, dep_headers,
               dep_entries, imported_names]
    if any(v < 0 for v in offsets):
        raise ZenFormatError(f"negative summary offset in {offsets}")
    if any(a > b for a, b in zip(offsets, offsets[1:])):
        raise ZenFormatError(f"summary offsets not ordered: {offsets}")
    if not 0 < header_size <= len(buf):
        raise ZenFormatError(f"implausible header size {header_size}")
    return s


# --------------------------------------------------------------------------
# main entry

def parse_zen(payload: bytes, name: str = "",
              script_objects: "ScriptObjects | None" = None,
              container_header_version: int = CHV_SOFT_PACKAGE_REFERENCES_OFFSET
              ) -> ZenPackage:
    """Parse one zen package payload (ExportBundleData chunk bytes).

    ``script_objects`` (from :func:`parse_script_objects`) enables
    script-import resolution (export class names, import names); without
    it those stay ``None`` — nothing is guessed. Only the UE 5.6.1
    container-header layout is supported; anything else raises
    ZenUnsupportedError, and any structural violation raises
    ZenFormatError (both ZenError).
    """
    if container_header_version != CHV_SOFT_PACKAGE_REFERENCES_OFFSET:
        raise ZenUnsupportedError(
            f"container header version {container_header_version} unsupported "
            f"(only {CHV_SOFT_PACKAGE_REFERENCES_OFFSET}, UE 5.6.1, is implemented)")
    s = _read_summary(payload)

    # Trailing package tag: the chunk ends with 0x9E2A83C1 after the last
    # export (observed on all 1,359 ST packages). _read_summary guarantees
    # >= 64 bytes, so len-4 is in bounds. A free drift catch: anything that
    # shifts the tail (stride/slice math regressions) fails here.
    (tag,) = struct.unpack_from("<I", payload, len(payload) - 4)
    if tag != PACKAGE_FILE_TAG:
        raise ZenFormatError(
            f"bad trailing package tag {tag:#010x} (want {PACKAGE_FILE_TAG:#010x})")

    pos = 13 * 4                              # end of the summary fields
    version_info = None
    if s.has_versioning_info:
        version_info, pos = _read_versioning_info(payload, pos)
    # UE 5.6+: cell import/export map offsets follow (before the name batch)
    if pos + 8 > len(payload):
        raise ZenFormatError("cell map offsets out of bounds")
    cell_import_off, cell_export_off = struct.unpack_from("<ii", payload, pos)
    pos += 8
    if cell_import_off < 0 or cell_export_off < cell_import_off \
            or s.export_bundle_entries_offset < cell_export_off:
        raise ZenFormatError(
            f"cell map offsets inconsistent: {cell_import_off}, {cell_export_off}")

    batch_start = pos
    names, pos, name_entries = _read_name_batch(payload, pos)

    # Bulk data map (UE 5.4+ alignment pad, then size + entries). It sits
    # between the name batch and the imported public export hashes; retoc's
    # heuristic: present whenever the hashes offset lies beyond us.
    bulk_map: list[BulkDataMapEntry] = []
    bulk_map_offset = -1
    if s.imported_public_export_hashes_offset > pos:
        (pad,) = struct.unpack_from("<Q", payload, pos)
        pos += 8 + pad
        (map_size,) = struct.unpack_from("<q", payload, pos)
        pos += 8
        bulk_map_offset = pos                    # entries start (Task 2)
        if map_size < 0 or map_size % BULK_ENTRY_SIZE:
            raise ZenFormatError(f"bulk data map size {map_size} invalid")
        if pos + map_size > len(payload):
            raise ZenFormatError("bulk data map out of bounds")
        for i in range(map_size // BULK_ENTRY_SIZE):
            so, dso, ss, flags, cooked, _pad = struct.unpack_from(
                "<qqqIB3s", payload, pos + i * BULK_ENTRY_SIZE)
            bulk_map.append(BulkDataMapEntry(so, dso, ss, flags, cooked))
        pos += map_size
    if pos != s.imported_public_export_hashes_offset:
        raise ZenFormatError(
            f"post-bulk position {pos} != imported_public_export_hashes_offset "
            f"{s.imported_public_export_hashes_offset}")

    n_hashes = (s.import_map_offset - s.imported_public_export_hashes_offset) // 8
    hashes = list(struct.unpack_from(f"<{n_hashes}Q", payload,
                                     s.imported_public_export_hashes_offset))
    n_imports = (s.export_map_offset - s.import_map_offset) // 8
    if (s.export_map_offset - s.import_map_offset) % 8:
        raise ZenFormatError("import map size not a multiple of 8")
    import_raws = list(struct.unpack_from(f"<{n_imports}Q", payload,
                                          s.import_map_offset))

    export_bytes = cell_import_off - s.export_map_offset
    if export_bytes % EXPORT_ENTRY_SIZE:
        raise ZenFormatError(
            f"export map size {export_bytes} not a multiple of "
            f"{EXPORT_ENTRY_SIZE}")
    n_exports = export_bytes // EXPORT_ENTRY_SIZE
    cell_import_count = (cell_export_off - cell_import_off) // CELL_IMPORT_ENTRY_SIZE
    cell_export_count = ((s.export_bundle_entries_offset - cell_export_off)
                         // CELL_EXPORT_ENTRY_SIZE)

    # imported package names (name batch + i32 numbers)
    imported_package_names: "list[str | None]" = []
    if s.imported_package_names_offset > 0:
        ipn, ipn_pos, _ipn_geo = _read_name_batch(
            payload, s.imported_package_names_offset)
        if ipn_pos + 4 * len(ipn) > len(payload):
            raise ZenFormatError("imported package name numbers truncated")
        nums = struct.unpack_from(f"<{len(ipn)}i", payload, ipn_pos)
        imported_package_names = [
            n if num == 0 else f"{n}_{num - 1}" for n, num in zip(ipn, nums)]

    # export bundle entries (retoc invariant: exactly Create+Serialize per
    # export) — reads from export_bundle_entries_offset to dependency headers
    n_bundle = (s.dependency_bundle_headers_offset
                - s.export_bundle_entries_offset) // BUNDLE_ENTRY_SIZE
    if n_bundle != 2 * n_exports:
        raise ZenFormatError(
            f"export bundle entries {n_bundle} != 2 x exports {n_exports}")
    bundle_entries = []
    for i in range(n_bundle):
        local_index, command = struct.unpack_from(
            "<II", payload, s.export_bundle_entries_offset + i * BUNDLE_ENTRY_SIZE)
        if command > 1:                # Create=0, Serialize=1 only in bundles
            raise ZenFormatError(f"unexpected bundle command type {command}")
        if local_index >= n_exports:
            raise ZenFormatError(f"bundle local export index {local_index} "
                                 f">= export count {n_exports}")
        bundle_entries.append((local_index, command))
    n_dep_headers = (s.dependency_bundle_entries_offset
                     - s.dependency_bundle_headers_offset) // DEP_HEADER_SIZE
    if n_dep_headers != n_exports:
        raise ZenFormatError(
            f"dependency bundle headers {n_dep_headers} != exports {n_exports}")

    # ---- exports (raw parse first; class refs resolve after both maps)
    raw_exports: list[tuple] = []
    for i in range(n_exports):
        base = s.export_map_offset + i * EXPORT_ENTRY_SIZE
        cso, css, on_iat, on_num, outer, cls, sup, tmpl, peh, oflags = \
            struct.unpack_from("<QQIIQQQQQI", payload, base)
        (fflags,) = struct.unpack_from("<B", payload, base + 68)
        if fflags > 2:
            raise ZenFormatError(f"invalid export filter flags {fflags}")
        raw_exports.append((cso, css, on_iat, on_num, outer, cls, sup, tmpl,
                            peh, oflags, fflags))
    export_names = [_mapped_name(names, r[2], r[3]) for r in raw_exports]

    def _resolve_class(raw: int) -> "tuple[str | None, str | None]":
        kind = raw >> 62
        value = raw & POI_INDEX_MASK
        if kind == POI_KIND_EXPORT:
            if value < len(export_names):
                return export_names[value], None
            return None, None
        if kind == POI_KIND_SCRIPT:
            if script_objects is not None:
                path = script_objects.resolve(raw)
                if path is not None:
                    return (path.rsplit(".", 1)[-1], path) if "." in path \
                        else (path, path)
            return None, None
        return None, None                      # package import / null: not cheap

    exports: list[ZenExport] = []
    for r, oname in zip(raw_exports, export_names):
        cso, css, _iat, _num, outer, cls, sup, tmpl, peh, oflags, fflags = r
        cname, cpath = _resolve_class(cls)
        start = s.header_size + cso
        if css < 0 or start < s.header_size or start + css > len(payload):
            raise ZenFormatError(
                f"export {oname!r} slice [{start}, {start + css}) outside "
                f"payload of {len(payload)}")
        exports.append(ZenExport(
            object_name=oname, class_index=cls, class_name=cname,
            class_path=cpath, outer_index=outer, super_index=sup,
            template_index=tmpl, serial_offset=cso, serial_size=css,
            object_flags=oflags, filter_flags=fflags, public_export_hash=peh,
            data=payload[start:start + css]))

    imports: list[ZenImport] = []
    for raw in import_raws:
        kind = raw >> 62
        value = raw & POI_INDEX_MASK
        if kind == POI_KIND_EXPORT:
            nm = export_names[value] if value < len(export_names) else None
            imports.append(ZenImport("export", raw, name=nm))
        elif kind == POI_KIND_SCRIPT:
            nm = script_objects.resolve(raw) if script_objects else None
            imports.append(ZenImport("script", raw, name=nm))
        elif kind == POI_KIND_PACKAGE:
            pkg_i, hash_i = value >> 32, value & 0xFFFFFFFF
            pkg_name = (imported_package_names[pkg_i]
                        if pkg_i < len(imported_package_names) else None)
            ehash = hashes[hash_i] if hash_i < len(hashes) else None
            imports.append(ZenImport("package", raw, name=pkg_name,
                                     package_name=pkg_name, export_hash=ehash))
        else:
            imports.append(ZenImport("null", raw))

    return ZenPackage(
        name=name,
        package_name=_mapped_name(names, s.name[0], s.name[1]),
        header_size=s.header_size,
        cooked_header_size=s.cooked_header_size,
        package_flags=s.package_flags,
        is_unversioned=not s.has_versioning_info,
        version_info=version_info,
        names=names,
        imports=imports,
        exports=exports,
        bulk_map=bulk_map,
        imported_public_export_hashes=hashes,
        imported_package_names=imported_package_names,
        export_bundle_entries=bundle_entries,
        cell_import_count=cell_import_count,
        cell_export_count=cell_export_count,
        export_map_offset=s.export_map_offset,
        _bulk_map_offset=bulk_map_offset,
        name_entries=name_entries,
        _name_num_string_bytes_offset=(
            name_batch_meta(batch_start) if name_entries else -1),
    )


# --------------------------------------------------------------------------
# package-size propagation (Phase 4a, Task 2)
#
# Discovery (probed read-only on all 1,359 ST packages): export data
# slices are PERFECTLY contiguous from header_size to len(payload)-4 —
# zero gaps, zero alignment padding (4,124 serial offsets are not even
# 4-aligned), the first export starts at header_size, the last ends at
# the trailing package tag, and every package's cell import/export maps
# are empty. No summary field encodes the payload length. A text patch
# inside one export therefore needs exactly three things after Task 1's
# splice: each patched export's SerialSize (fixed i64 at +8 of its
# 72-byte FExportMapEntry), each later export's SerialOffset (i64 at
# +0), and — when a bulk-data-map entry's region is INLINE in the
# package payload — that entry's SerialOffset. Bulk entries pointing
# outside the payload reference separate external bulk chunks and are
# untouched. A full re-serializer is neither needed nor wanted: every
# change is a fixed-width in-place field rewrite, so the untouched
# bytes stay byte-identical by construction (null patch == identity).

def patch_zen_package(pkg_payload: bytes, entries: list,
                      translations: "dict[int, str]") -> bytes:
    """Patch one zen package payload (ExportBundleData chunk bytes).

    ``entries`` are that package's TextEntry rows and ``translations``
    maps entry index -> replacement text (Task 1 contract; the frame
    grammar is re-verified against the payload before any splice).
    Returns the patched payload: Task 1's splice + updated export
    SerialSize/SerialOffset fields for resized/shifted exports +
    inline bulk-map offset shifts. ZenName patches (name-batch strings)
    additionally rewrite each name's BE i16 length header and u64
    CityHash64, the batch's string-bytes counter, and every summary /
    cell-map offset that points past the batch (header-relative export
    and bulk SerialOffsets cancel the header growth). The trailing
    package tag is preserved at the new end. Null patch (``{}``)
    returns the payload byte-identically. Raises ZenError (named) when a
    patchable range straddles an export boundary or a bulk region, when
    cell maps are non-empty (layout unknown — never guessed), when a
    path-separated name (package identity) is patched, or when the
    result would not re-parse; rejections leave the caller's bytes
    untouched.
    """
    from .patchtext import patch_payload, patch_span

    new_payload, report = patch_payload(pkg_payload, entries, translations)
    pkg = parse_zen(pkg_payload, entries[0].asset if entries else "")

    patches = []                              # (start, end, shift)
    for p in report.get("patched", ()):
        patches.append(patch_span(entries[p["entry_index"]], p))
    if not patches:
        return pkg_payload                    # null patch: identity

    if pkg.cell_import_count or pkg.cell_export_count:
        # Never observed in ST (all 1,359 packages: empty); the cell map
        # payload layout is unknown, so a package using it is refused.
        raise ZenUnsupportedError(
            "package has non-empty cell import/export maps — their payload "
            "layout is unknown, refusing to propagate size changes")

    def delta(pos: int) -> int:
        """How far content at ORIGINAL position ``pos`` moves."""
        return sum(sh for _lp, oe, sh in patches if oe <= pos)

    name_patches = [(lp, oe, sh) for lp, oe, sh in patches
                    if getattr(pkg, "_name_num_string_bytes_offset", -1) >= 0
                    and oe <= (pkg.header_size or 0)]
    out = bytearray(new_payload)

    if name_patches:
        # Name-batch rewrites: the strings grew/shrunk, so the fixed
        # metadata that DESCRIBES them (hashes, length headers, the
        # batch's total string-bytes) and every summary offset pointing
        # past the batch must follow. Hashes/headers sit BEFORE the
        # string region, so their POSITIONS never move — only values.
        from .cityhash import cityhash64
        by_index = {}
        for p in report.get("patched", ()):
            e = entries[p["entry_index"]]
            if e.kind == "ZenName":
                by_index[e.meta["name_index"]] = (e, p["new_source"])
        total_shift = sum(sh for _lp, _oe, sh in name_patches)
        if pkg._name_num_string_bytes_offset >= 0:
            (nsb,) = struct.unpack_from("<I", out,
                                        pkg._name_num_string_bytes_offset)
            struct.pack_into("<I", out, pkg._name_num_string_bytes_offset,
                             nsb + total_shift)
        for idx, (e, new_text) in by_index.items():
            ne = pkg.name_entries[idx]
            if "/" in ne.text:
                raise ZenFormatError(
                    f"name {ne.text!r} contains a path separator — package "
                    "identity, never patchable")
            # re-verify the stored geometry before writing
            raw = pkg_payload[ne.str_offset:ne.str_offset + ne.str_bytes]
            cur = raw.decode("ascii" if ne.encoding == "ascii"
                             else "utf-16-le")
            if cur != e.source:
                raise ZenFormatError(
                    f"name-batch entry {idx} content changed under us")
            if all(ord(c) < 128 for c in new_text):
                header = len(new_text.encode("ascii"))
                hash_input = new_text.lower().encode("ascii")
            else:
                wide = new_text.encode("utf-16-le")
                header = _I16_MIN + len(wide) // 2    # reader: n = MIN - h
                hash_input = new_text.lower().encode("utf-16-le")
            struct.pack_into(">h", out, ne.header_offset, header)
            struct.pack_into("<Q", out, ne.hash_offset,
                             cityhash64(hash_input))
        # summary i32 offsets + the UE5.6 cell-map offsets all point past
        # the batch: shift by the movement of their pointee.
        for field_off in (4, 20, 24, 28, 32, 36, 40, 44, 48):
            (val,) = struct.unpack_from("<i", out, field_off)
            d = delta(val)
            if d:
                struct.pack_into("<i", out, field_off, val + d)
        for field_off in (52, 56):
            (val,) = struct.unpack_from("<i", out, field_off)
            if val > 0:
                d = delta(val)
                if d:
                    struct.pack_into("<i", out, field_off, val + d)

    def _check_boundaries(start: int, end: int, what: str) -> None:
        for lp, oe, _sh in patches:
            if lp < start < oe or lp < end < oe:
                raise ZenFormatError(
                    f"patchable range [{lp:#x}, {oe:#x}) straddles an "
                    f"export boundary at {start:#x} ({what}) — refusing "
                    "to splice")

    # The export map and bulk map are themselves payload regions AFTER the
    # name batch — when names moved, the maps moved too; write entries at
    # their NEW positions.
    export_map_pos = pkg.export_map_offset + delta(pkg.export_map_offset)
    bulk_map_pos = (pkg._bulk_map_offset
                    + (delta(pkg._bulk_map_offset)
                       if pkg._bulk_map_offset >= 0 else 0))

    for i, ex in enumerate(pkg.exports):
        start = pkg.header_size + ex.serial_offset
        end = start + ex.serial_size
        _check_boundaries(start, end, f"export {ex.object_name!r}")
        grown = sum(sh for lp, oe, sh in patches
                    if start <= lp and oe <= end)
        base = export_map_pos + i * EXPORT_ENTRY_SIZE
        # SerialOffset is header-relative: when the header itself grew
        # (name patches), that growth cancels out — only patch movement
        # INSIDE the export-data region may move a serial offset.
        head_d = delta(pkg.header_size)
        struct.pack_into("<q", out, base,
                         ex.serial_offset + delta(start) - head_d)
        struct.pack_into("<q", out, base + 8, ex.serial_size + grown)

    head_d = delta(pkg.header_size)
    for j, b in enumerate(pkg.bulk_map):
        bstart = pkg.header_size + b.serial_offset
        bend = bstart + b.serial_size
        if b.serial_size == 0:
            continue
        if b.flags & 0x10000:
            # BULK_DataPayloadInSeparatePacket: the offset addresses the
            # package's separate bulk chunk(s), not this payload — fixed,
            # nothing to shift. (Geometry alone misclassifies entries
            # whose chunk offset happens to be small; ST: 90 external
            # entries, every one flagged 0x10501.)
            continue
        if pkg.header_size <= bstart and bend <= len(pkg_payload) - 4:
            # INLINE bulk data (inside the package payload): moves with
            # the content; its size cannot change (no text patches live
            # inside bulk regions — a boundary inside a patch is refused
            # below via the same straddle rule).
            _check_boundaries(bstart, bend, "inline bulk-data region")
            if delta(bstart) - head_d:
                struct.pack_into("<q", out,
                                 bulk_map_pos + j * BULK_ENTRY_SIZE,
                                 b.serial_offset + delta(bstart) - head_d)
            dstart = None
            if b.duplicate_serial_offset > 0:
                d = pkg.header_size + b.duplicate_serial_offset
                if pkg.header_size <= d <= len(pkg_payload) - 4:
                    dstart = d                   # duplicate is inline too
            if dstart is not None and delta(dstart) - head_d:
                struct.pack_into(
                    "<q", out,
                    bulk_map_pos + j * BULK_ENTRY_SIZE + 8,
                    b.duplicate_serial_offset + delta(dstart) - head_d)
        elif bstart >= len(pkg_payload):
            pass                               # external bulk chunk: fixed
        else:
            raise ZenFormatError(
                f"bulk-data region [{bstart:#x}, {bend:#x}) is neither "
                f"wholly inside the {len(pkg_payload)}-byte payload nor "
                f"wholly outside it — unknown layout, refusing")

    result = bytes(out)
    check_pkg = parse_zen(result, pkg.name)    # full self-check of the result
    if name_patches:
        # the re-parse must see EXACTLY the intended names
        by_index2 = {e.meta["name_index"]: p["new_source"]
                     for p in report.get("patched", ())
                     if (e := entries[p["entry_index"]]).kind == "ZenName"}
        for idx, text in by_index2.items():
            if check_pkg.names[idx] != text:
                raise ZenFormatError(
                    f"name-batch self-check: entry {idx} is "
                    f"{check_pkg.names[idx]!r}, want {text!r}")
    return result
