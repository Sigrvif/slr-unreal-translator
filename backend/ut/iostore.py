"""IoStore (.utoc/.ucas) reader/writer. Layouts verified against retoc and the
real SampleGameA files; see docs/plans/2026-10-22-phase1-repair-read.md."""
from __future__ import annotations

import os
import struct
import tempfile
from dataclasses import dataclass

TOC_MAGIC = b"-==--==--==--==-"  # stock Epic magic constant, not damage
TOC_HEADER_SIZE = 0x90

_HEADER = struct.Struct("<16s10IQ16sB3xIQII40x")

FLAG_COMPRESSED = 0b0001
FLAG_ENCRYPTED = 0b0010
FLAG_SIGNED = 0b0100
FLAG_INDEXED = 0b1000


@dataclass
class TocHeader:
    version: int
    header_size: int
    entry_count: int
    compressed_block_entry_count: int
    compressed_block_entry_size: int
    compression_method_name_count: int
    compression_method_name_length: int
    compression_block_size: int
    directory_index_size: int
    partition_count: int
    container_id: int
    encryption_key_guid: bytes
    container_flags: int
    perfect_hash_seeds_count: int
    partition_size: int
    chunks_without_perfect_hash_count: int

    @property
    def encrypted(self) -> bool:
        return bool(self.container_flags & FLAG_ENCRYPTED)

    @property
    def signed(self) -> bool:
        return bool(self.container_flags & FLAG_SIGNED)


def parse_toc_header(data: bytes, offset: int = 0) -> TocHeader:
    if len(data) - offset < TOC_HEADER_SIZE:
        raise ValueError("utoc too small for header")
    (magic, version, header_size, entry_count, block_count, block_entry_size,
     method_count, method_name_len, block_size, dir_index_size, partition_count,
     container_id, key_guid, flags, seeds_count, partition_size, without_ph,
     _reserved7) = _HEADER.unpack_from(data, offset)
    if magic != TOC_MAGIC:
        raise ValueError(f"bad utoc magic: {magic!r}")
    if header_size != TOC_HEADER_SIZE:
        raise ValueError(f"unexpected utoc header size {header_size:#x}")
    return TocHeader(version, header_size, entry_count, block_count, block_entry_size,
                     method_count, method_name_len, block_size, dir_index_size,
                     partition_count, container_id, key_guid, flags, seeds_count,
                     partition_size, without_ph)


def serialize_toc_header(h: TocHeader) -> bytes:
    return _HEADER.pack(
        TOC_MAGIC, h.version, h.header_size, h.entry_count,
        h.compressed_block_entry_count, h.compressed_block_entry_size,
        h.compression_method_name_count, h.compression_method_name_length,
        h.compression_block_size, h.directory_index_size, h.partition_count,
        h.container_id, h.encryption_key_guid, h.container_flags,
        h.perfect_hash_seeds_count, h.partition_size,
        h.chunks_without_perfect_hash_count, 0)


import io

_OFFSET_LEN = struct.Struct(">5s5s")            # big-endian 40-bit offset + length
_BLOCK = struct.Struct("<5s3s3sB")              # LE: offset 40, csize 24, usize 24, method
_META_V8 = struct.Struct("<20sB3x")
_META_OLD = struct.Struct("<20sB")

CHUNKTYPE_CONTAINER_HEADER = 6

# --- FIoContainerHeader ("IoCn" chunk type 6) — the only store-entry
# authority for package resolution. ST ships version 5
# (EIoContainerHeaderVersion::SoftPackageReferencesOffset); layout
# verified byte-exactly against the real 62,712-byte chunk (retoc
# container_header.rs parity):
#
#   u32 magic 0x496F436E | u32 version | u64 container_id
#   u32 package_count | package_count x u64 package ids (BTreeMap keys,
#       ascending)
#   u32 entry_buffer_len | buffer:
#       package_count x 16-byte FFilePackageStoreEntry — for version 5
#       exactly TWO TArrayView headers each: imported packages
#       (u32 array_num, u32 offset_to_data_from_this) then shader map
#       hashes (u32, u32) — with the arrays appended contiguously after
#       the table (u64 per imported package, 20 bytes per shader hash).
#       VERSION 5 STORE ENTRIES CARRY NO export_bundles_size: 1,359 x 16
#       + 30,032 array bytes consume the 51,776-byte buffer EXACTLY and
#       the whole chunk EXACTLY; export-bundle sizes are the TOC chunk
#       entry lengths in this version.
#   tail: optional segment package ids (u32 + u64s) + optional segment
#       store entries (u32 + bytes) + redirect name batch + localized
#       packages (u32 + 20B each) + package redirects (u32 + 24B each)
#       + soft package references serial info (i64 offset, i64 size;
#       when size > 0 a has-soft-refs bool follows, then the payload).
#       The tail is grammar-validated on parse and re-emitted verbatim
#       (all sections empty in ST; the serial info carries real values).
CONTAINER_HEADER_MAGIC = 0x496F436E
CHV_SOFT_PACKAGE_REFERENCES_OFFSET = 5
_STORE_ENTRY_SIZE = 16
_NAME_HASH_ALGORITHM_ID = 0xC1640000


class ContainerHeaderError(ValueError):
    """Container-header parse/serialize failures (named, never guessed)."""


@dataclass
class ContainerStoreEntry:
    imported_packages: list           # u64 FPackageIds
    shader_map_hashes: list           # 20-byte FSHAHash values


@dataclass
class ContainerHeader:
    version: int
    container_id: int
    package_ids: list                 # ascending (BTreeMap key order)
    store_entries: "list[ContainerStoreEntry]"
    tail: bytes = b""                 # validated on parse, re-emitted verbatim


def _walk_name_batch(data: bytes, pos: int) -> int:
    """Validate a UE name batch at ``pos``; return the end position."""
    if pos + 4 > len(data):
        raise ContainerHeaderError("name batch header out of bounds")
    (num,) = struct.unpack_from("<I", data, pos)
    pos += 4
    if num == 0:
        return pos
    if pos + 12 > len(data):
        raise ContainerHeaderError("name batch header out of bounds")
    pos += 4                                   # num_string_bytes
    (hash_version,) = struct.unpack_from("<Q", data, pos)
    if hash_version != _NAME_HASH_ALGORITHM_ID:
        raise ContainerHeaderError(
            f"name batch hash version {hash_version:#x} unexpected")
    pos += 8 + num * 8
    if pos + num * 2 > len(data):
        raise ContainerHeaderError("name batch length headers out of bounds")
    lengths = struct.unpack_from(f">{num}h", data, pos)
    pos += num * 2
    for h in lengths:
        if h >= 0:
            pos += h                           # ascii byte count
        else:
            pos += 2 * (32768 + h)             # wide: chars = 32768 + header
        if pos > len(data):
            raise ContainerHeaderError("name batch strings out of bounds")
    return pos


def parse_container_header(data: bytes) -> ContainerHeader:
    """Parse a version-5 FIoContainerHeader chunk; byte-exact (see the
    layout note above). Any other version or a structural violation
    raises ContainerHeaderError."""
    if len(data) < 20:
        raise ContainerHeaderError(f"container header too small ({len(data)})")
    (magic, version) = struct.unpack_from("<II", data, 0)
    if magic != CONTAINER_HEADER_MAGIC:
        raise ContainerHeaderError(f"bad container header magic {magic:#x}")
    if version != CHV_SOFT_PACKAGE_REFERENCES_OFFSET:
        raise ContainerHeaderError(
            f"container header version {version} unsupported (only "
            f"{CHV_SOFT_PACKAGE_REFERENCES_OFFSET} is implemented)")
    (container_id, count) = struct.unpack_from("<QI", data, 8)
    pos = 20
    if pos + 8 * count > len(data):
        raise ContainerHeaderError("package id table out of bounds")
    pids = list(struct.unpack_from(f"<{count}Q", data, pos)) if count else []
    pos += 8 * count
    if any(a >= b for a, b in zip(pids, pids[1:])):
        raise ContainerHeaderError("package ids not strictly ascending")
    if pos + 4 > len(data):
        raise ContainerHeaderError("entry buffer length out of bounds")
    (blen,) = struct.unpack_from("<I", data, pos)
    pos += 4
    if blen < count * _STORE_ENTRY_SIZE or pos + blen > len(data):
        raise ContainerHeaderError(f"entry buffer size {blen} invalid")
    buf = data[pos:pos + blen]
    pos += blen

    entries: "list[ContainerStoreEntry]" = []
    array_at = count * _STORE_ENTRY_SIZE      # canonical retoc writer layout
    for i in range(count):
        imp_n, imp_off, sh_n, sh_off = struct.unpack_from(
            "<IIII", buf, i * _STORE_ENTRY_SIZE)
        if imp_n:
            if imp_off != array_at - i * _STORE_ENTRY_SIZE:
                raise ContainerHeaderError(
                    f"store entry {i}: non-canonical imported-packages "
                    f"offset {imp_off}")
            imported = list(struct.unpack_from(
                f"<{imp_n}Q", buf, array_at))
            array_at += 8 * imp_n
        else:
            imported = []
        if sh_n:
            if sh_off != array_at - i * _STORE_ENTRY_SIZE - 8:
                raise ContainerHeaderError(
                    f"store entry {i}: non-canonical shader-map offset "
                    f"{sh_off}")
            hashes = [bytes(buf[array_at + 20 * k:array_at + 20 * k + 20])
                      for k in range(sh_n)]
            array_at += 20 * sh_n
        else:
            hashes = []
        entries.append(ContainerStoreEntry(imported, hashes))
    if array_at != blen:
        raise ContainerHeaderError(
            f"entry buffer consumes {array_at} of {blen} bytes — layout "
            "drift, refusing")

    tail_start = pos
    # ---- tail grammar validation (re-emitted verbatim, never rebuilt)
    (n,) = struct.unpack_from("<I", data, pos); pos += 4 + 8 * n     # opt seg
    (n,) = struct.unpack_from("<I", data, pos); pos += 4 + n         # opt seg
    pos = _walk_name_batch(data, pos)                                # names
    (n,) = struct.unpack_from("<I", data, pos); pos += 4 + 20 * n    # localized
    (n,) = struct.unpack_from("<I", data, pos); pos += 4 + 24 * n    # redirects
    if pos + 16 > len(data):
        raise ContainerHeaderError("soft package references info truncated")
    _off, soft_size = struct.unpack_from("<qq", data, pos)
    pos += 16
    if soft_size > 0:
        (has_soft,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if has_soft:
            (n,) = struct.unpack_from("<I", data, pos); pos += 4 + 8 * n
            (n,) = struct.unpack_from("<I", data, pos); pos += 4 + n
    if pos != len(data):
        raise ContainerHeaderError(
            f"container header consumes {pos} of {len(data)} bytes — "
            "layout drift, refusing")
    return ContainerHeader(version, container_id, pids, entries,
                           data[tail_start:])


def serialize_container_header(ch: ContainerHeader) -> bytes:
    """Re-serialize byte-identically to :func:`parse_container_header`
    input (store-entry buffer rebuilt canonically; tail verbatim)."""
    if ch.version != CHV_SOFT_PACKAGE_REFERENCES_OFFSET:
        raise ContainerHeaderError(
            f"container header version {ch.version} unsupported")
    out = bytearray(struct.pack("<IIQI", CONTAINER_HEADER_MAGIC, ch.version,
                                ch.container_id, len(ch.package_ids)))
    for pid in ch.package_ids:
        out += struct.pack("<Q", pid)
    buf = bytearray(len(ch.package_ids) * _STORE_ENTRY_SIZE)
    array_at = len(buf)
    for i, entry in enumerate(ch.store_entries):
        base = i * _STORE_ENTRY_SIZE
        if entry.imported_packages:
            struct.pack_into("<II", buf, base,
                             len(entry.imported_packages),
                             array_at - base)
            for pid in entry.imported_packages:
                buf += struct.pack("<Q", pid)
            array_at = len(buf)
        if entry.shader_map_hashes:
            struct.pack_into("<II", buf, base + 8,
                             len(entry.shader_map_hashes),
                             array_at - base - 8)
            for h in entry.shader_map_hashes:
                if len(h) != 20:
                    raise ContainerHeaderError("shader hash not 20 bytes")
                buf += h
            array_at = len(buf)
    out += struct.pack("<I", len(buf))
    out += buf
    out += ch.tail
    return bytes(out)


def _u40(be5: bytes) -> int:
    return int.from_bytes(be5, "big")


def _pack40(v: int) -> bytes:
    return v.to_bytes(5, "big")


def _read_fstring(buf: io.BytesIO) -> str:
    (n,) = struct.unpack("<i", buf.read(4))
    if n == 0:
        return ""
    if n < 0:  # UE FString: negative length = UTF-16LE TCHAR units incl. null
        raw = buf.read(-2 * n)
        return raw[:-2].decode("utf-16-le") if raw.endswith(b"\x00\x00") else raw.decode("utf-16-le")
    raw = buf.read(n)
    return raw[:-1].decode("utf-8") if raw.endswith(b"\x00") else raw.decode("utf-8")


def _write_fstring(out: io.BytesIO, s: str) -> None:
    if not s:
        out.write(struct.pack("<i", 0))
    else:
        raw = s.encode("utf-8") + b"\x00"
        out.write(struct.pack("<i", len(raw)))
        out.write(raw)


def serialize_dir_index(mount_point: str, file_map: "dict[str, int]") -> bytes:
    """FIoDirectoryIndexResource for one container's file list.

    ``file_map`` maps FULL paths (mount_point + relative, exactly the keys
    :meth:`parse_toc` produces) to TOC entry indexes — the writer side of
    the directory index. The patch containers need a REAL index: retoc
    always writes one and its containers load; our empty-index patch pair
    mounted but never overrode anything (boot 14). Grammar mirrors
    :func:`parse_toc` (which consumed 83,911/83,911 real bytes):
    mount FString | i32 dir_count, dirs × (name, first_child,
    next_sibling, first_file) | i32 file_count, files × (name,
    next_file, user_data=entry index) | i32 string_count, strings ×
    FString. Root (index 0) is nameless.
    """
    root: dict = {}
    for full, entry_index in file_map.items():
        rel = full[len(mount_point):] if mount_point and \
            full.startswith(mount_point) else full
        parts = rel.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(("$dirs", part), {})
        node.setdefault("$files", []).append((parts[-1], entry_index))

    strings: list[str] = []
    string_index: "dict[str, int]" = {}

    def _s(s: str) -> int:
        if s not in string_index:
            string_index[s] = len(strings)
            strings.append(s)
        return string_index[s]

    dirs: "list[tuple[int, int, int, int]]" = []
    files: "list[tuple[int, int, int]]" = []

    def walk(node: dict, dir_index: int) -> None:
        def place_children() -> None:
            children = [k for k in node if isinstance(k, tuple)]
            first_child = -1
            prev = -1
            for key in children:
                _tag, part = key
                dirs.append((_s(part), -1, -1, -1))   # placeholder chain
                child_index = len(dirs) - 1
                if first_child == -1:
                    first_child = child_index
                else:
                    dirs[prev] = dirs[prev][:2] + (child_index, dirs[prev][3])
                prev = child_index
                walk(node[key], child_index)
            if first_child != -1:
                dirs[dir_index] = (dirs[dir_index][0], first_child,
                                   dirs[dir_index][2], dirs[dir_index][3])

        def place_files() -> None:
            entries_list = node.get("$files", [])
            first_file = -1
            prev = -1
            for leaf, entry_index in entries_list:
                files.append((_s(leaf), -1, entry_index))
                file_index = len(files) - 1
                if first_file == -1:
                    first_file = file_index
                else:
                    pf, pu = files[prev][0], files[prev][2]
                    files[prev] = (pf, file_index, pu)
                prev = file_index
            if first_file != -1:
                dirs[dir_index] = dirs[dir_index][:3] + (first_file,)

        place_children()
        place_files()

    dirs.append((-1, -1, -1, -1))          # root
    walk(root, 0)

    out = io.BytesIO()
    _write_fstring(out, mount_point)
    out.write(struct.pack("<i", len(dirs)))
    for d in dirs:
        out.write(struct.pack("<4i", *d))
    out.write(struct.pack("<i", len(files)))
    for f in files:
        out.write(struct.pack("<iiI", *f))
    out.write(struct.pack("<i", len(strings)))
    for s in strings:
        _write_fstring(out, s)
    return out.getvalue()


def _read_i32_array(buf: io.BytesIO, count: int) -> list[int]:
    return list(struct.unpack(f"<{count}i", buf.read(4 * count))) if count else []


@dataclass
class ChunkEntry:
    chunk_id: bytes
    offset: int
    length: int
    meta_flags: int
    io_hash: bytes

    @property
    def chunk_type(self) -> int:
        return self.chunk_id[11] & 0x3F


@dataclass
class _DirEntry:
    name: int      # index into string table, -1 = none
    first_child: int
    next_sibling: int
    first_file: int


@dataclass
class _FileEntry:
    name: int
    next_file: int
    user_data: int


@dataclass
class Toc:
    header: TocHeader
    entries: list[ChunkEntry]
    perfect_hash_seeds: list[int]
    chunks_without_perfect_hash: list[int]
    blocks: list[tuple[int, int, int, int]]
    compression_methods: list[str]
    mount_point: str
    file_map: dict[str, int]
    raw_tail: bytes
    _dir_entries: list[_DirEntry]
    _file_entries: list[_FileEntry]
    _string_table: list[str]
    _directory_blob: bytes

    def file_names(self) -> list[str]:
        names = []

        def walk(dir_idx: int, prefix: str) -> None:
            d = self._dir_entries[dir_idx]
            if d.name >= 0:
                prefix = prefix + self._string_table[d.name] + "/"
            f = d.first_file
            while f != -1:
                fe = self._file_entries[f]
                names.append(self.mount_point + prefix + self._string_table[fe.name])
                f = fe.next_file
            c = d.first_child
            while c != -1:
                walk(c, prefix)
                c = self._dir_entries[c].next_sibling

        if self._dir_entries:
            walk(0, "")
        return names


def parse_toc(data: bytes) -> Toc:
    h = parse_toc_header(data)
    pos = TOC_HEADER_SIZE
    need = (h.entry_count * 12 + h.entry_count * 10
            + h.perfect_hash_seeds_count * 4 + h.chunks_without_perfect_hash_count * 4
            + h.compressed_block_entry_count * 12
            + h.compression_method_name_count * h.compression_method_name_length
            + h.directory_index_size
            + h.entry_count * (24 if h.version >= 8 else 21))
    if len(data) - pos < need:
        raise ValueError(f"utoc truncated: need {need} bytes after header, have {len(data) - pos}")

    entries: list[ChunkEntry] = []
    raw_ids = data[pos:pos + h.entry_count * 12]
    pos += h.entry_count * 12
    raw_ol = data[pos:pos + h.entry_count * 10]
    pos += h.entry_count * 10
    seeds = _read_i32_array(io.BytesIO(data[pos:pos + h.perfect_hash_seeds_count * 4]),
                            h.perfect_hash_seeds_count)
    pos += h.perfect_hash_seeds_count * 4
    without_ph = _read_i32_array(
        io.BytesIO(data[pos:pos + h.chunks_without_perfect_hash_count * 4]),
        h.chunks_without_perfect_hash_count)
    pos += h.chunks_without_perfect_hash_count * 4

    blocks = []
    for i in range(h.compressed_block_entry_count):
        off, cs, us, mi = _BLOCK.unpack_from(data, pos + i * 12)
        blocks.append((int.from_bytes(off, "little"),
                       int.from_bytes(cs, "little"),
                       int.from_bytes(us, "little"), mi))
    pos += h.compressed_block_entry_count * 12

    methods = []
    for i in range(h.compression_method_name_count):
        name = data[pos + i * h.compression_method_name_length:
                    pos + (i + 1) * h.compression_method_name_length]
        methods.append(name.split(b"\x00")[0].decode("ascii"))
    pos += h.compression_method_name_count * h.compression_method_name_length

    if h.signed:
        raise ValueError("signed containers not supported yet (no test game uses them)")
    directory_blob = data[pos:pos + h.directory_index_size]
    pos += h.directory_index_size
    if h.encrypted:
        raise ValueError("encrypted TOC not supported (no test game uses it)")

    meta_struct = _META_V8 if h.version >= 8 else _META_OLD
    for i in range(h.entry_count):
        cid = raw_ids[i * 12:(i + 1) * 12]
        o, l = _OFFSET_LEN.unpack_from(raw_ol, i * 10)
        hsh, mflags = meta_struct.unpack_from(data, pos + i * meta_struct.size)
        entries.append(ChunkEntry(cid, _u40(o), _u40(l), mflags, hsh))
    pos += h.entry_count * meta_struct.size

    mount_point = ""
    dir_entries: list[_DirEntry] = []
    file_entries: list[_FileEntry] = []
    string_table: list[str] = []
    if directory_blob:
        buf = io.BytesIO(directory_blob)
        mount_point = _read_fstring(buf)
        (dc,) = struct.unpack("<i", buf.read(4))
        dir_entries = [_DirEntry(*struct.unpack("<4i", buf.read(16))) for _ in range(max(dc, 0))]
        (fc,) = struct.unpack("<i", buf.read(4))
        # Ruling (Task 3): brief read 16 B ("<3iI") into this 3-field dataclass;
        # real layout is 12 B (UE FIoFileIndexEntry) — blob consumed exactly 83911/83911.
        file_entries = [_FileEntry(*struct.unpack("<iiI", buf.read(12))) for _ in range(max(fc, 0))]
        (sc,) = struct.unpack("<i", buf.read(4))
        string_table = [_read_fstring(buf) for _ in range(max(sc, 0))]

    file_map = {name: fe.user_data
                for name, fe in zip(_collect_paths(mount_point, dir_entries, file_entries, string_table),
                                    _iter_files(dir_entries, file_entries))}
    return Toc(h, entries, seeds, without_ph, blocks, methods, mount_point, file_map,
               data[pos:], dir_entries, file_entries, string_table, directory_blob)


def _iter_files(dir_entries, file_entries):
    def walk(i):
        d = dir_entries[i]
        f = d.first_file
        while f != -1:
            yield file_entries[f]
            f = file_entries[f].next_file
        c = d.first_child
        while c != -1:
            yield from walk(c)
            c = dir_entries[c].next_sibling
    if dir_entries:
        yield from walk(0)


def _collect_paths(mount_point, dir_entries, file_entries, string_table):
    result = []
    def walk(i, prefix):
        d = dir_entries[i]
        p = prefix + (string_table[d.name] + "/" if d.name >= 0 else "")
        f = d.first_file
        while f != -1:
            result.append(mount_point + p + string_table[file_entries[f].name])
            f = file_entries[f].next_file
        c = d.first_child
        while c != -1:
            walk(c, p)
            c = dir_entries[c].next_sibling
    if dir_entries:
        walk(0, "")
    return result


def serialize_toc(toc: Toc) -> bytes:
    h = toc.header
    out = bytearray()
    out += serialize_toc_header(h)
    for e in toc.entries:
        out += e.chunk_id
    for e in toc.entries:
        out += _pack40(e.offset) + _pack40(e.length)
    out += struct.pack(f"<{len(toc.perfect_hash_seeds)}i", *toc.perfect_hash_seeds)
    out += struct.pack(f"<{len(toc.chunks_without_perfect_hash)}i", *toc.chunks_without_perfect_hash)
    for off, cs, us, mi in toc.blocks:
        out += _BLOCK.pack(off.to_bytes(5, "little"), cs.to_bytes(3, "little"),
                           us.to_bytes(3, "little"), mi)
    for m in toc.compression_methods:
        raw = m.encode("ascii")[:31]
        out += raw + b"\x00" * (h.compression_method_name_length - len(raw))
    out += toc._directory_blob
    meta_struct = _META_V8 if h.version >= 8 else _META_OLD
    for e in toc.entries:
        out += meta_struct.pack(e.io_hash, e.meta_flags)
    out += toc.raw_tail
    return bytes(out)


def _align_up(v: int, a: int) -> int:
    return (v + a - 1) & ~(a - 1)


def read_entry(toc: Toc, ucas_path: str, entry_index: int, dll=None) -> bytes:
    e = toc.entries[entry_index]
    bs = toc.header.compression_block_size
    if e.offset % bs != 0:
        raise ValueError(f"entry {entry_index} offset {e.offset:#x} not block-aligned")
    first = e.offset // bs
    last = (_align_up(e.offset + e.length, bs) - 1) // bs
    out = bytearray()
    with open(ucas_path, "rb") as f:
        for bi in range(first, last + 1):
            off, csize, usize, mi = toc.blocks[bi]
            f.seek(off)
            blob = f.read(csize)
            if mi == 0:
                if len(blob) != usize:
                    raise ValueError(f"block {bi}: stored size mismatch")
                out += blob
            else:
                from . import oodle
                method = toc.compression_methods[mi - 1]
                if method != "Oodle":
                    raise ValueError(f"unsupported compression method {method!r}")
                out += oodle.decompress(dll or oodle.get_dll(), blob, usize)
    if len(out) != e.length:
        raise ValueError(f"entry {entry_index}: read {len(out)} bytes, expected {e.length}")
    return bytes(out)


def write_iostore(utoc_path: str, ucas_path: str, out_utoc: str, out_ucas: str,
                  chunk_patches: "dict[int, bytes] | None" = None,
                  container_header_patches: "dict[int, int] | None" = None
                  ) -> None:
    """Rebuild an IoStore container pair.

    Unmodified rebuild: the ucas is copied VERBATIM (streamed, payloads never
    decompressed) and the utoc re-serialized from the parsed model —
    byte-identical by the Phase 1 golden round-trip.

    ``chunk_patches`` (TOC entry index -> new chunk data) appends each patched
    chunk's blocks at the END of the ucas: the file is zero-padded to a
    compression-block boundary first, so every new chunk offset stays
    block-aligned (entry offsets are logical ``block_index * block_size``).
    New blocks are 16-byte aligned, matching every original block in these
    containers. A patched chunk keeps its original block SHAPE: chunks whose
    original blocks are all stored (method 0) are re-stored; otherwise each
    block-sized piece is Oodle-compressed (Kraken/Normal) with a fallback to
    stored when compression does not shrink it. The entry's offset/length are
    repointed at the appended blocks, ``io_hash`` recomputed via
    blake3.hash20, and the block table EXTENDED — the chunk's stale original
    blocks (and every other entry's blocks) stay in the table and file
    untouched. Both outputs are written to temp files in their target
    directories and only then replaced (see ``_atomic_writes``).

    ``container_header_patches`` (package id -> new export_bundles_size) is
    the ONLY sanctioned way to touch the container-header chunk (type 6):
    blind ``chunk_patches`` on it are rejected below. Structure-aware
    behavior: the header chunk is parsed and its canonical re-serialization
    must round-trip byte-identically; an empty dict performs exactly that
    verification and modifies nothing. ST's header is version 5
    (SoftPackageReferencesOffset), whose store entries carry NO
    export_bundles_size (see :func:`parse_container_header`) — export-bundle
    sizes are the TOC chunk entry lengths, which ``chunk_patches`` already
    updates — so any size-update request raises a named ValueError instead
    of guessing.
    """
    with open(utoc_path, "rb") as f:
        toc = parse_toc(f.read())
    h = toc.header
    if h.partition_count != 1:
        raise NotImplementedError("multi-partition containers unsupported")
    chunk_patches = dict(chunk_patches or {})
    for idx in chunk_patches:
        if not 0 <= idx < len(toc.entries):
            raise KeyError(f"chunk_patches index {idx} not in [0, {len(toc.entries)})")
        if toc.entries[idx].chunk_type == CHUNKTYPE_CONTAINER_HEADER:
            # Discovery (Task 2): there is NO physical container header at
            # ucas offset 0 in ST — the header is a normal block-system
            # chunk. Blind byte replacement stays rejected: container-
            # header edits must go through the structure-aware
            # container_header_patches path, never raw chunk bytes.
            raise ValueError(
                f"chunk_patches index {idx} is the container-header chunk "
                "(chunk_type 6); blind byte replacement is rejected — use "
                "the structure-aware container_header_patches path instead")
    if container_header_patches is not None:
        ch_idx = next((i for i, e in enumerate(toc.entries)
                       if e.chunk_type == CHUNKTYPE_CONTAINER_HEADER), None)
        if ch_idx is None:
            raise ValueError(
                "container_header_patches given but this container has no "
                "container-header chunk")
        from . import oodle
        old = read_entry(toc, ucas_path, ch_idx, dll=oodle.get_dll())
        ch = parse_container_header(old)
        if container_header_patches:
            raise ValueError(
                "container_header_patches refused: this container header "
                f"is version {ch.version} (SoftPackageReferencesOffset), "
                "whose store entries carry NO export_bundles_size — each "
                "entry is exactly the imported-packages + shader-map-hashes "
                "array views and the chunk consumes byte-exactly with none "
                "(parse_container_header note). Export-bundle sizes are the "
                "TOC chunk entry lengths in this version, which "
                "chunk_patches already updates; refusing to guess a size "
                "update")
        if serialize_container_header(ch) != old:
            raise ValueError(
                "container header failed the structure-aware round-trip "
                "(parse + re-serialize differs from the stored chunk)")
    bs = h.compression_block_size

    append_base = None
    payloads: list[tuple[int, bytes]] = []
    if chunk_patches:
        from . import blake3, oodle

        append_base = _align_up(os.path.getsize(ucas_path), bs)
        dll = oodle.get_dll()
        new_tuples: list[tuple[int, int, int, int]] = []
        cur = append_base
        for idx in sorted(chunk_patches):
            e = toc.entries[idx]
            data = bytes(chunk_patches[idx])
            first = e.offset // bs
            last = (_align_up(e.offset + e.length, bs) - 1) // bs
            if last >= len(toc.blocks):
                raise ValueError(f"entry {idx}: block range past end of table")
            all_stored = all(mi == 0 for _, _, _, mi in toc.blocks[first:last + 1])
            e.offset = (len(toc.blocks) + len(new_tuples)) * bs
            e.length = len(data)
            e.io_hash = blake3.hash20(data)
            pieces = [data[i:i + bs] for i in range(0, len(data), bs)] or [b""]
            for piece in pieces:
                if all_stored:
                    payload, method = piece, 0
                else:
                    comp = oodle.compress(dll, piece)
                    if len(comp) < len(piece):
                        payload, method = comp, 1
                    else:
                        payload, method = piece, 0
                cur = _align_up(cur, 16)
                payloads.append((cur, payload))
                new_tuples.append((cur, len(payload), len(piece), method))
                cur += len(payload)
        toc.blocks.extend(new_tuples)
        h.compressed_block_entry_count = len(toc.blocks)

    utoc_bytes = serialize_toc(toc)

    def _write_ucas(dst) -> None:
        copied = 0
        with open(ucas_path, "rb") as src:
            while True:
                buf = src.read(1 << 20)
                if not buf:
                    break
                dst.write(buf)
                copied += len(buf)
        pos = copied
        if append_base is not None:
            if pos > append_base:
                raise ValueError("ucas grew during write")
            dst.write(b"\x00" * (append_base - pos))
            pos = append_base
            for off, payload in payloads:
                dst.write(b"\x00" * (off - pos))
                dst.write(payload)
                pos = off + len(payload)

    # Pair protocol (Task-6 review hardening): both temps are written and
    # fsynced BEFORE either output is replaced; the ucas is replaced first
    # and the utoc LAST. A crash between the two replaces then leaves a
    # stale utoc beside a new ucas — appended blocks the old utoc never
    # references, the benign pre-existing failure — never a new utoc
    # pointing at a not-yet-replaced ucas. Residual crash window: the pair
    # is still not atomic as a unit (full pair-atomicity needs a
    # directory-level transaction — out of scope).
    _atomic_writes([(out_ucas, _write_ucas),
                    (out_utoc, lambda dst: dst.write(utoc_bytes))])


def _atomic_writes(jobs: "list[tuple[str, object]]") -> None:
    """Phase 1 incident rule (tempfile + os.replace, never partial) applied
    to a set of outputs: EVERY temp is written and fsynced before ANY output
    is replaced, then outputs are replaced in list order. On any failure the
    temps are removed (outputs already replaced stay — the caller orders the
    list so that outcome is the benign one)."""
    temps: list[str] = []
    try:
        for path, writer in jobs:
            directory = os.path.dirname(os.path.abspath(path))
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".",
                                       suffix=".tmp")
            temps.append(tmp)
            with os.fdopen(fd, "wb") as f:
                writer(f)
                f.flush()
                os.fsync(f.fileno())
        for (path, _writer), tmp in zip(jobs, temps):
            os.replace(tmp, path)
    except BaseException:
        for tmp in temps:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
