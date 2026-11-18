"""Pak v11 footer + index reader.

Both test games' paks use the *patched* pak magic 0x5A6F12E1 (stock UE is
0x5A6F12F1) and otherwise-standard v11 footers: guid(16) + encrypted(1) +
magic(4)@+17 + version(4) + index_offset(8) + index_size(8) + index_sha1(20)
+ compression-method names (5 x 32B) = 221 bytes. SampleGameA's index is
plaintext (v11-encoded family: encoded records blob + full directory index);
SampleGameB's index (and payloads) are AES-encrypted (Task 8 hunts the key).

Layout knowledge ported in structure from the local reference tools
(rust-u4pak src/index.rs + record.rs; u4pak.py) — reference reading only.
Verified empirically on SampleGameA:
  - the entry sha1 covers the ON-DISK payload bytes (compressed bytes for
    compressed entries), not the uncompressed content — 60/60 sample;
  - inline records (48B head + [4 + 16*n blocks] + flags(1) + block_size(4))
    carry Offset=0 and block pairs RELATIVE to the record position;
  - multi-block Oodle: per-block uncompressed size = min(block_size,
    uncompressed_size - produced_so_far).

``scan_pak`` below is the pre-revision inline walker, kept ONLY as a
diagnostic: both games' data regions break it after a few records (entries
are 0x800-aligned and their inline Offset fields are 0), so it must never be
treated as a source of truth.
"""
from __future__ import annotations

import hashlib
import json
import contextlib
import os
import struct
import tempfile
from dataclasses import dataclass

from . import aes, oodle

_S64 = struct.Struct("<q")
_S32 = struct.Struct("<i")
_U32 = struct.Struct("<I")
_U8 = struct.Struct("<B")

PAK_MAGIC_STOCK = 0x5A6F12F1
PAK_MAGIC_PATCHED = 0x5A6F12E1
FOOTER_SIZE_V11 = 221
TAIL_LIMIT = 64 << 20          # diagnostic walker only: largest accepted tail

_V3_HEADER_SIZE = 53           # 48 head + flags(1) + block_size(4)


@dataclass
class Footer:
    encryption_guid: bytes
    encrypted_index: bool
    magic: int
    version: int
    index_offset: int
    index_size: int
    index_sha1: bytes
    method_names: bytes = b""      # 5 x 32B compression-method names (raw)

    @property
    def magic_variant(self) -> str:
        if self.magic == PAK_MAGIC_STOCK:
            return "stock"
        if self.magic == PAK_MAGIC_PATCHED:
            return "patched-e1"
        return "other"


def read_footer(path: str) -> "Footer":
    """Parse the trailing 221-byte v11 footer; validate the magic pair."""
    file_size = os.path.getsize(path)
    if file_size < FOOTER_SIZE_V11:
        raise ValueError(f"{path}: too small for a pak footer")
    with open(path, "rb") as f:
        f.seek(-FOOTER_SIZE_V11, os.SEEK_END)
        footer = f.read(FOOTER_SIZE_V11)
    (magic,) = _U32.unpack_from(footer, 17)          # guid(16) + encrypted(1)
    if magic not in (PAK_MAGIC_STOCK, PAK_MAGIC_PATCHED):
        raise ValueError(f"{path}: bad pak magic {magic:#x}")
    (version,) = _U32.unpack_from(footer, 21)
    (index_offset,) = _S64.unpack_from(footer, 25)
    (index_size,) = _S64.unpack_from(footer, 33)
    if index_size < 0 or not (0 <= index_offset <= file_size
                              and index_offset + index_size <= file_size):
        raise ValueError(f"{path}: index range [{index_offset:#x},"
                         f"{index_offset + index_size:#x}) outside file")
    return Footer(footer[:16], footer[16] != 0, magic, version,
                  index_offset, index_size, footer[41:61], footer[61:221])


@dataclass
class PakEntry:
    name: str                    # index name, mount-relative
    offset: int                  # inline record start (index metadata)
    size: int                    # payload bytes on disk
    uncompressed_size: int
    method: int                  # 0 = stored, 1 = first footer method ("Oodle")
    sha1: bytes                  # sha1 of the on-disk payload bytes
    block_count: int
    blocks: list                 # (start, end) absolute payload spans
    flags: int                   # bit0 encrypted payload
    block_size: int
    data_offset: int             # absolute payload start
    encoded_pos: int = -1        # offset of this entry's record in the
                                 # v11 encoded-records blob (writer ordering)

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & 1)


@dataclass
class PakIndex:
    mount_point: str
    entries: list
    encrypted: bool
    footer: Footer
    # v11 primary-index fields the writer must round-trip (read paths are
    # unchanged; defaults keep older constructions valid):
    path_hash_seed: int = 0
    file_count: int = 0          # legacy (pre-encoded) record count, v11 = 0
    mount_raw: bytes = b""       # exact original FString bytes of mount point
    phi_offset: int = -1         # path-hash-index extent (-1 = has_phi == 0)
    phi_size: int = 0
    fdi_offset: int = -1         # full-directory-index extent (-1 = has_fdi == 0)
    fdi_size: int = 0


def _read_fstring(buf: bytes, pos: int) -> tuple[str, int]:
    """UE FString: i32 length; negative = UTF-16LE units, positive = UTF-8."""
    (n,) = _S32.unpack_from(buf, pos)
    pos += 4
    if n < 0:
        raw = buf[pos:pos + (-n) * 2]
        pos += (-n) * 2
        return raw.decode("utf-16-le").rstrip("\x00"), pos
    raw = buf[pos:pos + n]
    pos += n
    return raw.decode("utf-8").rstrip("\x00"), pos


def _decode_encoded_entry(blob: bytes, pos: int) -> tuple[dict, int]:
    """Decode one bitfield-packed record from the encoded records blob."""
    (bitfield,) = _U32.unpack_from(blob, pos)
    pos += 4
    method = (bitfield >> 23) & 0x3F
    encrypted = bool(bitfield & (1 << 22))
    block_count = (bitfield >> 6) & 0xFFFF
    bs_field = bitfield & 0x3F
    block_size = bs_field << 11
    if bitfield & (1 << 31):
        (offset,) = _U32.unpack_from(blob, pos)
        pos += 4
    else:
        (offset,) = _S64.unpack_from(blob, pos)
        pos += 8
    if bitfield & (1 << 30):
        (usize,) = _U32.unpack_from(blob, pos)
        pos += 4
    else:
        (usize,) = _S64.unpack_from(blob, pos)
        pos += 8
    if method != 0:
        if bitfield & (1 << 29):
            (size,) = _U32.unpack_from(blob, pos)
            pos += 4
        else:
            (size,) = _S64.unpack_from(blob, pos)
            pos += 8
    else:
        size = usize
    if block_count > 0 and (usize <= 0xFFFF or bs_field == 0):
        # Substitution: a zero bs field means "block size == uncompressed
        # size". The original encoder writes 0 for EVERY single-block
        # compressed record — small ones (usize < 64 KiB, where the 6-bit
        # field cannot express the exact value) AND the EF class with
        # usize == 65536 == block_size exactly (32 would decode to the same
        # value; the original still writes 0). Pinned across both games:
        # field 0 <=> single block (13,284 compressed records checked).
        block_size = usize
    rec_size = _V3_HEADER_SIZE + (4 + 16 * block_count if method != 0 else 0)
    blocks: list[tuple[int, int]] = []
    if block_count == 1 and not encrypted:
        blocks = [(rec_size, rec_size + size)]       # synthetic, relative
    elif block_count > 0:
        # Reference (rust-u4pak record.rs ~261-272): every block_count > 0
        # EXCEPT the synthetic unencrypted single-block case consumes one u32
        # block size per block — including encrypted block_count == 1.
        start = rec_size
        alignment = 16 if encrypted else 1
        for _ in range(block_count):
            (bsz,) = _U32.unpack_from(blob, pos)
            pos += 4
            blocks.append((start, start + bsz))
            start += (bsz + alignment - 1) & ~(alignment - 1)
    return dict(offset=offset, size=size, uncompressed_size=usize, method=method,
                encrypted=encrypted, block_count=block_count,
                block_size=block_size, blocks=blocks), pos


def _read_legacy_record(buf: bytes, pos: int, name: str) -> tuple[PakEntry, int]:
    """Legacy (v<10-family) index record: full metadata inline, WITH sha1."""
    offset, size, usize = struct.unpack_from("<qqq", buf, pos)
    pos += 24
    (method,) = _U32.unpack_from(buf, pos)
    pos += 4
    sha1 = buf[pos:pos + 20]
    pos += 20
    blocks: list[tuple[int, int]] = []
    if method != 0:
        (count,) = _U32.unpack_from(buf, pos)
        pos += 4
        for _ in range(count):
            s, e = struct.unpack_from("<qq", buf, pos)
            pos += 16
            blocks.append((s, e))
    (flags,) = _U8.unpack_from(buf, pos)
    pos += 1
    (bsize,) = _U32.unpack_from(buf, pos)
    pos += 4
    data_offset = offset + _V3_HEADER_SIZE + (4 + 16 * len(blocks) if method else 0)
    return (PakEntry(name, offset, size, usize, method, sha1, len(blocks),
                     [(offset + s, offset + e) for s, e in blocks], flags, bsize,
                     data_offset), pos)


def _parse_inline_record(f, pos: int) -> dict:
    """Parse the inline record at ``pos``; block pairs are relative to it."""
    f.seek(pos)
    head = f.read(48)
    if len(head) < 48:
        raise ValueError(f"inline record at {pos:#x}: truncated header")
    offset_field, size, usize = struct.unpack("<qqq", head[:24])
    (method,) = _U32.unpack(head[24:28])
    sha1 = head[28:48]
    consumed = 48
    blocks: list[tuple[int, int]] = []
    if method != 0:
        (count,) = _S32.unpack(f.read(4))
        consumed += 4
        if not (0 < count <= 1 << 20):
            raise ValueError(f"inline record at {pos:#x}: bad block count {count}")
        for _ in range(count):
            s, e = struct.unpack("<qq", f.read(16))
            consumed += 16
            blocks.append((s, e))
    (flags,) = _U8.unpack(f.read(1))
    consumed += 1
    (bsize,) = _U32.unpack(f.read(4))
    consumed += 4
    return dict(offset_field=offset_field, size=size, uncompressed_size=usize,
                method=method, sha1=sha1, flags=flags, block_size=bsize,
                blocks=blocks, data_offset=pos + consumed)


def _validate_entries(entries: list[PakEntry], index_offset: int, file_size: int) -> None:
    for e in entries:
        if not e.name or e.name.startswith("/") or "\\" in e.name:
            raise ValueError(f"implausible index name: {e.name!r}")
        if not (0 <= e.offset < index_offset):
            raise ValueError(f"{e.name}: offset {e.offset:#x} not below index")
        if e.data_offset + e.size > file_size:
            raise ValueError(f"{e.name}: payload runs past EOF")
    if not entries:
        return                      # anchor paks (mount-only, zero entries)
    ordered = sorted(entries, key=lambda e: e.offset)
    if ordered[0].offset != 0:
        raise ValueError(f"data chain does not start at 0 "
                         f"(first entry at {ordered[0].offset:#x})")
    prev = None
    for e in ordered:
        if prev is not None and e.offset < prev.data_offset + prev.size:
            raise ValueError(f"{e.name} at {e.offset:#x} overlaps "
                             f"{prev.name} ending {prev.data_offset + prev.size:#x}")
        prev = e
    if ordered[-1].data_offset + ordered[-1].size > index_offset:
        raise ValueError("data chain ends beyond the index base")


def read_index(path: str, footer: "Footer | None" = None,
               aes_key: "bytes | None" = None) -> "PakIndex":
    """Read the pak index.

    Encrypted indexes WITHOUT a key keep the Phase 1 encrypted-report
    behavior (empty entries + encrypted=True); WITH ``aes_key`` the index
    blob is read, decrypted (AES-256-ECB; the plaintext is padded to the
    AES block size by the encoder's prefix-repeat rule — FAES cyclic
    extension, see ``_pad16_prefix``), SHA-1 verified against the footer
    hash, and parsed with exactly the plaintext machinery (decrypt →
    identical parse). The v11 full-directory index is a separately stored
    SECONDARY index that is also decrypted and verified against the sha1
    embedded in the primary index.
    """
    footer = footer or read_footer(path)
    if footer.encrypted_index and aes_key is None:
        return PakIndex("", [], True, footer)
    file_size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(footer.index_offset)
        index = f.read(footer.index_size)
    if len(index) != footer.index_size:
        raise ValueError(f"{path}: short index read")
    if footer.encrypted_index:
        index = aes.decrypt_ecb(aes_key, index)
    if hashlib.sha1(index).digest() != footer.index_sha1:
        raise ValueError(f"{path}: index sha1 mismatch against footer")
    return _parse_index(index, path, footer, file_size, aes_key)


def _parse_index(index: bytes, path: str, footer: "Footer",
                 file_size: int, aes_key: "bytes | None" = None) -> "PakIndex":
    """Parse a plaintext (or already-decrypted) index buffer."""
    pos = 0
    mount_point, pos = _read_fstring(index, pos)
    mount_raw = index[:pos]                 # exact bytes, writer round-trips
    (entry_count,) = _S32.unpack_from(index, pos)
    pos += 4
    (path_hash_seed,) = _S64.unpack_from(index, pos)
    pos += 8
    (has_phi,) = _U32.unpack_from(index, pos)
    pos += 4
    phi_offset = phi_size = -1
    if has_phi:
        (phi_offset,) = _S64.unpack_from(index, pos)
        pos += 8
        (phi_size,) = _S64.unpack_from(index, pos)
        pos += 8
        pos += 20                           # phi hash (recomputed on write)
    (has_fdi,) = _U32.unpack_from(index, pos)
    pos += 4
    fdi_offset = fdi_size = -1
    fdi_hash = b""
    if has_fdi:
        (fdi_offset,) = _S64.unpack_from(index, pos)
        pos += 8
        (fdi_size,) = _S64.unpack_from(index, pos)
        pos += 8
        fdi_hash = index[pos:pos + 20]        # sha1 of the (decrypted) FDI
        pos += 20
    (pak_entries_size,) = _S32.unpack_from(index, pos)
    pos += 4
    encoded = index[pos:pos + pak_entries_size]
    pos += pak_entries_size
    (file_count,) = _U32.unpack_from(index, pos)
    pos += 4

    entries: list[PakEntry] = []
    for _ in range(file_count):               # legacy records (family fallback)
        name, pos = _read_fstring(index, pos)
        entry, pos = _read_legacy_record(index, pos, name)
        entries.append(entry)

    if has_fdi and fdi_size > 0:              # v11-encoded family (our games)
        with open(path, "rb") as f:
            f.seek(fdi_offset)
            fdi = f.read(fdi_size)
        if len(fdi) != fdi_size:
            raise ValueError(f"{path}: short full-directory-index read")
        if footer.encrypted_index:
            # Empirical (EF): the full directory index is a separately stored,
            # separately AES-encrypted secondary index (fdi at 0xBDBBA2CE,
            # 622,464 B, 16-aligned); the stored hash covers the DECRYPTED
            # blob (verified on EF and SampleGameA both).
            if aes_key is None:
                raise ValueError(f"{path}: encrypted index requires aes_key")
            fdi = aes.decrypt_ecb(aes_key, fdi)
        if hashlib.sha1(fdi).digest() != fdi_hash:
            raise ValueError(f"{path}: full-directory-index sha1 mismatch")
        dpos = 0
        (dir_count,) = _U32.unpack_from(fdi, dpos)
        dpos += 4
        for _ in range(dir_count):
            dir_path, dpos = _read_fstring(fdi, dpos)
            (nfiles,) = _U32.unpack_from(fdi, dpos)
            dpos += 4
            for _ in range(nfiles):
                file_name, dpos = _read_fstring(fdi, dpos)
                (encoded_pos,) = _U32.unpack_from(fdi, dpos)
                dpos += 4
                meta, _ = _decode_encoded_entry(encoded, encoded_pos)
                entries.append(PakEntry(
                    (dir_path + file_name) if dir_path != "/" else file_name,
                    meta["offset"], meta["size"], meta["uncompressed_size"],
                    meta["method"], b"", meta["block_count"],
                    [(meta["offset"] + s, meta["offset"] + e)
                     for s, e in meta["blocks"]],
                    1 if meta["encrypted"] else 0, meta["block_size"],
                    meta["offset"] + _V3_HEADER_SIZE
                    + (4 + 16 * meta["block_count"] if meta["method"] else 0),
                    encoded_pos))

    if len(entries) != entry_count:
        raise ValueError(f"index claims {entry_count} entries, parsed {len(entries)}")

    # Fill sha1/flags/block_size/data_offset from the inline records and
    # cross-check them against the index metadata.
    with open(path, "rb") as f:
        for e in entries:
            if e.sha1:                        # legacy record already complete
                continue
            rec = _parse_inline_record(f, e.offset)
            if (rec["size"], rec["uncompressed_size"], rec["method"]) != \
                    (e.size, e.uncompressed_size, e.method):
                raise ValueError(f"{e.name}: inline record disagrees with index")
            rec_blocks = [(e.offset + s, e.offset + s2) for s, s2 in rec["blocks"]]
            if rec["method"] != 0 and rec_blocks != e.blocks:
                raise ValueError(f"{e.name}: inline blocks disagree with index")
            e.sha1 = rec["sha1"]
            e.flags = rec["flags"]
            e.block_size = rec["block_size"]
            e.data_offset = rec["data_offset"]

    _validate_entries(entries, footer.index_offset, file_size)
    return PakIndex(mount_point, entries, False, footer, path_hash_seed,
                    file_count, mount_raw, phi_offset,
                    phi_size if phi_offset >= 0 else 0,
                    fdi_offset, fdi_size if fdi_offset >= 0 else 0)


def read_entry_data(path: str, entry: PakEntry, dll=None, aes_key=None) -> bytes:
    """Payload of one entry: inline-record parse, cross-check, decrypt, decompress.

    The entry sha1 covers the bytes AS STORED ON DISK, checked before any
    decryption/decompression — compressed bytes for unencrypted entries
    (verified on SampleGameA) and CIPHERTEXT for encrypted ones (verified
    on SampleGameB entry0). Encrypted payloads are AES-256-ECB over
    16-byte-aligned on-disk spans (zero padding beyond each block's
    compressed size): every block is decrypted over its aligned span and
    trimmed back to the block's compressed size before the stored/Oodle
    dispatch.
    """
    with open(path, "rb") as f:
        rec = _parse_inline_record(f, entry.offset)
        if (rec["size"], rec["uncompressed_size"], rec["method"]) != \
                (entry.size, entry.uncompressed_size, entry.method):
            raise ValueError(f"entry at {entry.offset:#x}: inline record "
                             f"disagrees with index metadata")
        if entry.sha1 and rec["sha1"] != entry.sha1:
            raise ValueError(f"entry at {entry.offset:#x}: inline sha1 "
                             f"disagrees with index metadata")
        encrypted = bool(rec["flags"] & 1)
        if encrypted and aes_key is None:
            raise ValueError("payload encrypted; AES key required (aes_key=)")
        if rec["method"] != 0:
            # block pairs are relative to the inline record position
            spans = list(rec["blocks"])
        else:
            # stored entries have no block table; one implicit payload block
            base_rel = rec["data_offset"] - entry.offset
            spans = [(base_rel, base_rel + rec["size"])]
        on_disk: list[bytes] = []
        for s, e in spans:
            csize = e - s
            span = (csize + 15) & ~15 if encrypted else csize
            f.seek(entry.offset + s)
            raw = f.read(span)
            if len(raw) != span:
                raise ValueError(f"entry at {entry.offset:#x}: "
                                 f"short payload read")
            on_disk.append(raw)
    if rec["method"] == 0:
        # stored entries hash the exact stored-size ciphertext prefix
        # (align padding excluded — empirical on EF stored+encrypted)
        stored = on_disk[0][:rec["size"]]
    else:
        # compressed entries hash the full aligned spans, pads included
        stored = b"".join(on_disk)
    if hashlib.sha1(stored).digest() != rec["sha1"]:
        raise ValueError(f"entry at {entry.offset:#x}: payload sha1 mismatch")
    if rec["method"] == 0:
        if encrypted:
            return aes.decrypt_ecb(aes_key, on_disk[0])[:rec["size"]]
        return on_disk[0]
    if encrypted:
        blobs = [aes.decrypt_ecb(aes_key, raw)[: e - s]
                 for raw, (s, e) in zip(on_disk, spans)]
    else:
        blobs = on_disk
    block_size = rec["block_size"] or rec["uncompressed_size"]
    dll = dll or oodle.get_dll()
    out = bytearray()
    produced = 0
    for blob in blobs:
        want = min(block_size, rec["uncompressed_size"] - produced)
        out += oodle.decompress(dll, blob, want)
        produced += want
    if len(out) != rec["uncompressed_size"]:
        raise ValueError(f"entry at {entry.offset:#x}: decompressed "
                         f"{len(out)} bytes, expected {rec['uncompressed_size']}")
    return bytes(out)


# ---------------------------------------------------------------------------
# WRITER — v11 encoded-index family (both test games). Unmodified payloads
# are copied VERBATIM by slicing the source data region; only the index is
# re-encoded (mirroring _decode_encoded_entry bit-for-bit) and the footer
# rebuilt. With aes_key the primary index AND the secondary extents are
# encrypted (prefix-repeat padding, plaintext-domain hashes) and modified
# payloads of originally-encrypted entries are re-encrypted per the Task-1
# ciphertext-sha1 rules; SampleGameA exercises the plaintext path.
# ---------------------------------------------------------------------------

def _encode_record(rec: dict) -> bytes:
    """Bitfield-pack one encoded-records-blob entry, mirroring
    ``_decode_encoded_entry``: method<<23 | encrypted<<22 | block_count<<6 |
    block_size>>11, zeroed when the decode rule substitutes usize; 32-bit
    union arms (bits 31/30/29) whenever the value fits a u32 — the packing
    observed on every record of the game indexes. Stored entries carry no
    size field (size == uncompressed_size)."""
    method = rec["method"]
    enc = bool(rec["encrypted"])
    bc = rec["block_count"]
    usize = rec["uncompressed_size"]
    if not 0 <= method <= 0x3F or not 0 <= bc <= 0xFFFF:
        raise ValueError(f"method/block_count out of range: {method}/{bc}")
    if bc > 0 and rec["block_size"] == usize:
        # Single-block convention: the decoder substitutes block_size =
        # usize for a zero field, and the original encoder writes 0 for
        # EVERY single-block compressed record — including usize == 65536
        # == block_size exactly (field 32 decodes identically, but the
        # original bytes carry 0; pinned on both games, EF 5 such records).
        bs_field = 0
    else:
        bs_field = rec["block_size"] >> 11
        if not 0 <= bs_field <= 0x3F:
            raise ValueError(f"block_size {rec['block_size']} not encodable "
                             f"in 6 bits")
    bf = (method << 23) | ((1 if enc else 0) << 22) | (bc << 6) | bs_field
    out = bytearray()
    off, size = rec["offset"], rec["size"]
    if 0 <= off <= 0xFFFFFFFF:
        bf |= 1 << 31
        out += _U32.pack(off)
    else:
        out += _S64.pack(off)
    if 0 <= usize <= 0xFFFFFFFF:
        bf |= 1 << 30
        out += _U32.pack(usize)
    else:
        out += _S64.pack(usize)
    # bit29 mirrors "size fits u32" even for stored records — the original
    # encoder sets it from the value though no size field follows (decoder
    # ignores it there; pinned by the first byte-identity bisect round)
    if 0 <= size <= 0xFFFFFFFF:
        bf |= 1 << 29
    if method != 0:
        if 0 <= size <= 0xFFFFFFFF:
            out += _U32.pack(size)
        else:
            out += _S64.pack(size)
        # block table: every block EXCEPT the synthetic unencrypted
        # single-block case consumes one u32 (the block's logical size)
        if bc > 0 and not (bc == 1 and not enc):
            if len(rec["block_sizes"]) < bc:
                raise ValueError("block size table shorter than block_count")
            for bsz in rec["block_sizes"][:bc]:
                out += _U32.pack(bsz)
    elif size != usize:
        raise ValueError("stored entry requires size == uncompressed_size")
    return _U32.pack(bf) + bytes(out)


def _record_from_entry(e: PakEntry) -> dict:
    """Encoded-record fields for an UNMODIFIED entry (from the parsed model:
    e.blocks spans are logical, so their lengths are the u32 block sizes)."""
    return dict(offset=e.offset, size=e.size,
                uncompressed_size=e.uncompressed_size, method=e.method,
                encrypted=e.encrypted, block_count=e.block_count,
                block_size=e.block_size,
                block_sizes=[end - start for start, end in e.blocks])


def _build_replacement(e: PakEntry, data: bytes,
                       aes_key: "bytes | None") -> dict:
    """New stored form for a modified entry, keeping its method shape
    (stored stays stored; compressed re-Oodles Kraken/Normal at the entry's
    original block size). Produces the new inline record (Offset field zero,
    block pairs relative to the record start), the payload bytes and the
    encoded-record fields. sha1 rules follow the parser's on-disk-bytes
    convention (ciphertext for encrypted entries, Task 1 facts)."""
    encrypted = e.encrypted
    if encrypted and aes_key is None:
        raise ValueError(f"{e.name}: payload encrypted; aes_key required")
    if e.method == 0:
        usize = size = len(data)
        block_count, block_size, block_sizes = 0, 0, []
        if encrypted:
            payload = aes.encrypt_ecb(
                aes_key, data + b"\x00" * ((-len(data)) % 16))
            sha = hashlib.sha1(payload[:size]).digest()   # exact stored prefix
        else:
            payload = data
            sha = hashlib.sha1(data).digest()
    else:
        usize = len(data)
        # Chunk at the pak-standard 64 KiB block. A small single-block
        # original carries block_size == uncompressed_size (the decoder
        # substitution rule) — NOT a chunk size: reusing it for a grown
        # replacement desynchronizes the inline and encoded block_size and
        # a v11 game (which decodes the encoded record) computes wrong
        # per-block sizes. Single-block replacements keep the bs == usize
        # convention the substitution reproduces; 65536 round-trips the
        # 6-bit field exactly (32).
        bs = 65536
        dll = oodle.get_dll()
        chunks = [oodle.compress(dll, data[i:i + bs])
                  for i in range(0, usize, bs)] or [b""]
        block_count = len(chunks)
        block_size = bs if block_count > 1 else usize  # single-block: bs == usize
        block_sizes = [len(c) for c in chunks]
        if encrypted:
            payload = b"".join(
                aes.encrypt_ecb(aes_key, c + b"\x00" * ((-len(c)) % 16))
                for c in chunks)
        else:
            payload = b"".join(chunks)
        size = len(payload)                      # aligned spans for encrypted
        sha = hashlib.sha1(payload).digest()
    # write-time invariant: the encoded record must decode to the same
    # scalars the inline record carries (substitution rule included) —
    # catches block-size drift before it can reach a game
    check = _encode_record(dict(offset=0, size=size, uncompressed_size=usize,
                                method=e.method, encrypted=encrypted,
                                block_count=block_count, block_size=block_size,
                                block_sizes=block_sizes))
    meta, _ = _decode_encoded_entry(check, 0)
    if (meta["block_size"], meta["block_count"], meta["size"],
            meta["uncompressed_size"]) != (block_size, block_count, size,
                                           usize):
        raise ValueError(f"{e.name}: replacement records inconsistent")
    inline = bytearray()
    inline += struct.pack("<qqq", 0, size, usize)
    inline += _U32.pack(e.method)
    inline += sha
    if e.method != 0:
        inline += _S32.pack(block_count)
        start = 48 + 4 + 16 * block_count + 1 + 4      # record size, method != 0
        for bsz in block_sizes:
            inline += struct.pack("<qq", start, start + bsz)
            start += (bsz + 15) & ~15 if encrypted else bsz
    inline += _U8.pack(1 if encrypted else 0)
    inline += _U32.pack(block_size)
    return dict(inline=bytes(inline), payload=payload, offset=0, size=size,
                uncompressed_size=usize, method=e.method, encrypted=encrypted,
                block_count=block_count, block_size=block_size,
                block_sizes=block_sizes)


def _walk_fdi(fdi: bytes):
    """Yield (entry name, byte offset of the encoded-position u32) for every
    file in a full directory index, using the reader's name construction."""
    dpos = 0
    (dir_count,) = _U32.unpack_from(fdi, dpos)
    dpos += 4
    for _ in range(dir_count):
        dir_path, dpos = _read_fstring(fdi, dpos)
        (nfiles,) = _U32.unpack_from(fdi, dpos)
        dpos += 4
        for _ in range(nfiles):
            file_name, dpos = _read_fstring(fdi, dpos)
            yield ((dir_path + file_name) if dir_path != "/" else file_name), dpos
            dpos += 4


def _splice_positions(buf: bytes, slots: list, new_pos: dict) -> bytes:
    """Rewrite only the encoded-position u32s; names stay verbatim."""
    out = bytearray(buf)
    for name, field_off in slots:
        if name in new_pos:
            _U32.pack_into(out, field_off, new_pos[name])
    return bytes(out)


def _splice_phi(phi: bytes, order: list, new_pos: dict) -> bytes:
    """Re-splice the path-hash index: u32 count + (u64 path-hash, u32 pos)
    pairs in BLOB ORDER, then a partial directory listing (name → pos).
    Only position u32s are rewritten; hashes and strings stay verbatim."""
    out = bytearray(phi)
    (count,) = _U32.unpack_from(phi, 0)
    if count != len(order):
        raise ValueError(f"path hash index lists {count} entries, "
                         f"index has {len(order)}")
    p = 4
    for e in order:
        _U32.pack_into(out, p + 8, new_pos[e.name])
        p += 12
    (dir_count,) = _U32.unpack_from(phi, p)
    p += 4
    for _ in range(dir_count):
        dir_path, p = _read_fstring(phi, p)
        (nfiles,) = _U32.unpack_from(phi, p)
        p += 4
        for _ in range(nfiles):
            file_name, p = _read_fstring(phi, p)
            name = (dir_path + file_name) if dir_path != "/" else file_name
            if name in new_pos:
                _U32.pack_into(out, p, new_pos[name])
            p += 4
    if p != len(phi):
        tail = len(phi) - p
        # an ENCRYPTED source pads the extent to the AES block size
        # (_pad16_prefix); the parse must consume the logical content and
        # leave at most that pad, which by the pinned rule repeats the
        # blob's own first bytes (EF phi: 2 pad bytes == phi[:2])
        if not (0 < tail < 16 and phi[p:] == phi[:tail]):
            raise ValueError("path hash index layout not understood")
    return bytes(out)


def _pad16_prefix(buf: bytes) -> bytes:
    """Pad to the AES block size with the blob's OWN leading bytes.

    Empirical (EF — all three encrypted extents: primary index, path-hash
    index, full-directory index): the original encoder's encryption
    padding is a cyclic repeat of the plaintext's first bytes, NOT zeros
    (index pad 6 B == its FString mount header prefix; phi/fdi pads 2 B ==
    their leading u32 counts' low bytes). Pinned 3/3 byte-exact on EF;
    reproducing it makes an unmodified rebuild's ciphertext identical."""
    pad = (-len(buf)) % 16
    return (buf + buf[:pad]) if pad else buf


def _encrypt_index(index_plain: bytes, aes_key: bytes) -> "tuple[bytes, bytes]":
    """Pad the plaintext index to 16 and AES-encrypt it.

    The footer hash covers the PADDED plaintext — the same domain the reader
    verifies (decrypt of the on-disk index_size bytes, Phase-1 oracle: footer
    IndexHash == SHA-1 of the full decrypted buffer, padding included).
    Padding bytes follow the original encoder's prefix-repeat rule
    (_pad16_prefix). Returns (on-disk bytes, sha1).
    """
    padded = _pad16_prefix(index_plain)
    return aes.encrypt_ecb(aes_key, padded), hashlib.sha1(padded).digest()


def _encrypt_extent(plain: bytes, aes_key: bytes) -> "tuple[bytes, bytes]":
    """Output form of a secondary index extent (path-hash / full-directory)
    for an encrypted write: pad (prefix rule), hash the PADDED plaintext,
    encrypt. Mirrors the reader exactly: it reads extent_size ciphertext
    bytes, decrypts, and SHA-1-verifies against the hash embedded in the
    primary index (Task 1 pinned that domain on EF: the stored hashes cover
    the DECRYPTED blobs, pads included)."""
    padded = _pad16_prefix(plain)
    return aes.encrypt_ecb(aes_key, padded), hashlib.sha1(padded).digest()


def _atomic_write(path: str, writer) -> None:
    """Phase 1 incident rule applied to the pak writer's single output:
    write to a tempfile in the target directory, fsync, then os.replace
    into place — a failed or interrupted rebuild never leaves a partial
    pak at ``path`` (and the temp is removed on any BaseException). Same
    contract as iostore's ``_atomic_writes``; kept local because that one
    also orders multi-file replaces the pak writer does not need."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _empty_source():
    """A context manager yielding a zero-length read-only 'file' — lets
    write_pak run source-less (patch paks: every entry in entries_data,
    a synthesized full-directory index supplies the entry names)."""
    import contextlib
    import io
    return contextlib.nullcontext(io.BytesIO(b""))


def _synth_fdi(order: list, new_pos: dict) -> bytes:
    """A minimal full-directory index for a source-less pak, using the
    name grammar of the real one (EF: dir 'Engine/Content/' + file
    'Sphere.ubulk'; the root dir serializes as '/'): entries grouped by
    directory, in blob order, each carrying its encoded-position u32.
    The names must MATCH the base pak's index names exactly — the engine
    shadows patch entries by that path."""
    dirs: dict = {}
    for e in order:
        d, _, f = e.name.rpartition("/")
        dirs.setdefault(d or "/", []).append((f, e.name))
    out = bytearray()
    out += _U32.pack(len(dirs))
    for d in sorted(dirs):
        dstr = d if d == "/" else d + "/"    # real FDI dirs carry the tail /
        raw = dstr.encode("utf-8") + b"\x00"
        out += _S32.pack(len(raw)) + raw
        out += _U32.pack(len(dirs[d]))
        for fname, name in dirs[d]:
            fraw = fname.encode("utf-8") + b"\x00"
            out += _S32.pack(len(fraw)) + fraw
            out += _U32.pack(new_pos[name])
    return bytes(out)


def write_pak(src_path: "str | None", idx: PakIndex, out_path: str,
              entries_data: "dict[str, bytes] | None" = None,
              aes_key: "bytes | None" = None,
              magic: "int | None" = None) -> None:
    """Rebuild a pak container from a parsed index (v11 encoded family).

    Unmodified entries are copied VERBATIM — the source data region
    [0, footer.index_offset) is sliced through unchanged, payloads never
    decompressed/recompressed. Entries in ``entries_data`` (name → new
    content) are re-stored in their original method shape and appended,
    record+payload, after the original data region (their stale original
    bytes stay in place as unreferenced padding). The encoded records blob
    is re-encoded exactly as parsed; the path-hash and full-directory
    position tables re-spliced; the footer written with ``magic`` (or the
    source magic — patched-e1 for these games), recomputed
    index_offset/index_size, the SHA-1 of the plaintext index, and the
    source guid + method-name bytes. ``aes_key`` pads (prefix-repeat rule,
    ``_pad16_prefix``) and encrypts the primary index AND both secondary
    extents — the path-hash and full-directory indexes are decrypted from
    an encrypted source, re-spliced in the plaintext domain, re-hashed
    over the padded plaintext and re-encrypted — and re-encrypts modified
    payloads of originally-encrypted entries. The output is written
    atomically (tempfile in ``out_path``'s directory + fsync + os.replace
    — ``_atomic_write``), so a failed rebuild never leaves a partial pak.
    """
    entries_data = dict(entries_data or {})
    footer = idx.footer
    if footer.encrypted_index and aes_key is None:
        raise ValueError("source index is encrypted; aes_key required to write")
    if idx.file_count:
        raise NotImplementedError("legacy index records unsupported by the writer")
    for e in idx.entries:
        if e.encoded_pos < 0:
            raise NotImplementedError(
                f"{e.name}: writer supports the v11 encoded index family only")
    known = {e.name for e in idx.entries}
    if len(known) != len(idx.entries):
        raise ValueError("duplicate entry names in the index")
    unknown = set(entries_data) - known
    if unknown:
        raise KeyError(f"entries_data names not in the index: {sorted(unknown)}")

    order = sorted(idx.entries, key=lambda e: e.encoded_pos)   # blob order

    repl: dict[str, dict] = {}
    for e in order:
        if e.name in entries_data:
            repl[e.name] = _build_replacement(e, entries_data[e.name], aes_key)

    blob = bytearray()
    new_pos: dict[str, int] = {}
    appended = 0
    for e in order:
        new_pos[e.name] = len(blob)
        if e.name in repl:
            r = repl[e.name]
            r["offset"] = footer.index_offset + appended
            appended += len(r["inline"]) + len(r["payload"])
            blob += _encode_record(r)
        else:
            blob += _encode_record(_record_from_entry(e))

    with open(src_path, "rb") if src_path else _empty_source() as src:
        def _load_extent(off: int, size: int, what: str) -> bytes:
            if off < 0 or size <= 0:
                return b""
            src.seek(off)
            raw = src.read(size)
            if len(raw) != size:
                raise ValueError(f"{src_path}: short {what} read")
            if footer.encrypted_index:
                # an encrypted source stores the extent AES-encrypted too
                # (Task 1: EF's phi/fdi both are; primary-index hashes cover
                # the DECRYPTED blobs) — splice positions in the PLAINTEXT
                # domain, never in ciphertext
                raw = aes.decrypt_ecb(aes_key, raw)
            return raw

        if src_path is None:
            # patch mode: no source extents to splice — a synthesized
            # full-directory index supplies the entry names (a v11 pak's
            # only name source: the engine resolves entries through the
            # fdi; phi stays empty/absent)
            phi_plain = b""
            fdi_plain = _synth_fdi(order, new_pos)
        else:
            phi_plain = _load_extent(idx.phi_offset, idx.phi_size,
                                     "path-hash-index")
            if phi_plain:
                phi_plain = _splice_phi(phi_plain, order, new_pos)
            fdi_plain = _load_extent(idx.fdi_offset, idx.fdi_size,
                                     "full-directory-index")
            if fdi_plain:
                slots = list(_walk_fdi(fdi_plain))
                if len(slots) != len(order):
                    raise ValueError("full directory index does not cover every entry")
                fdi_plain = _splice_positions(fdi_plain, slots, new_pos)

        # output extents are encrypted (and hashed over the padded
        # plaintext) exactly when the output index is — mirror of the
        # reader's domains, so an encrypted rebuild re-reads clean
        if aes_key is not None:
            phi_bytes, phi_sha = _encrypt_extent(phi_plain, aes_key)
            fdi_bytes, fdi_sha = _encrypt_extent(fdi_plain, aes_key)
        else:
            phi_bytes, phi_sha = phi_plain, hashlib.sha1(phi_plain).digest()
            fdi_bytes, fdi_sha = fdi_plain, hashlib.sha1(fdi_plain).digest()

        # primary index: fixed-width header, so extent offsets are known
        # before the fields that carry them are packed
        has_phi = idx.phi_offset >= 0
        has_fdi = idx.fdi_offset >= 0 or bool(fdi_plain)  # patch: synthesized
        data_end = footer.index_offset + appended
        header_len = (len(idx.mount_raw) + 4 + 8 + 4
                      + (36 if has_phi else 0) + 4 + (36 if has_fdi else 0)
                      + 4 + len(blob) + 4)
        # the on-disk index is the plaintext PADDED to the AES block size
        # when encrypted (EF: 0xBDA5C59E + 1,190,592 == the phi offset), so
        # the extent base sits past the padding, not at the logical end
        index_pad = (-header_len) % 16 if aes_key is not None else 0
        phi_off = data_end + header_len + index_pad
        primary = bytearray(idx.mount_raw)
        primary += _S32.pack(len(idx.entries))
        primary += _S64.pack(idx.path_hash_seed)
        primary += _U32.pack(1 if has_phi else 0)
        if has_phi:
            primary += _S64.pack(phi_off) + _S64.pack(len(phi_bytes))
            primary += phi_sha
        primary += _U32.pack(1 if has_fdi else 0)
        if has_fdi:
            primary += _S64.pack(phi_off + len(phi_bytes)) + _S64.pack(len(fdi_bytes))
            primary += fdi_sha
        primary += _S32.pack(len(blob))
        primary += blob
        primary += _U32.pack(idx.file_count)

        index_plain = bytes(primary)
        enc_byte = 0
        if aes_key is not None:
            index_bytes, index_sha = _encrypt_index(index_plain, aes_key)
            enc_byte = 1
        else:
            index_bytes = index_plain
            index_sha = hashlib.sha1(index_plain).digest()

        def _write_out(out) -> None:
            if src_path is not None:
                src.seek(0)
            left = footer.index_offset
            while left:
                chunk = src.read(min(1 << 20, left))
                if not chunk:
                    raise ValueError(f"{src_path}: short data-region read")
                out.write(chunk)
                left -= len(chunk)
            for e in order:
                if e.name in repl:
                    out.write(repl[e.name]["inline"])
                    out.write(repl[e.name]["payload"])
            out.write(index_bytes)
            out.write(phi_bytes)
            out.write(fdi_bytes)
            out.write(footer.encryption_guid
                      + bytes([enc_byte])
                      + _U32.pack(footer.magic if magic is None else magic)
                      + _U32.pack(footer.version)
                      + _S64.pack(data_end)
                      + _S64.pack(len(index_bytes))
                      + index_sha
                      + (footer.method_names or b"\x00" * 160))

        _atomic_write(out_path, _write_out)


# ---------------------------------------------------------------------------
# DIAGNOSTIC ONLY: pre-revision inline walker. Both test games break this
# after a few records (entries are 0x800-aligned; inline Offset fields are 0),
# so it is kept purely to characterize damaged layouts — never a source of
# truth. See the module docstring and the task-5 report.
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    entries: list
    chain_end: int             # offset where entry walking stopped
    tail_bytes: int            # file_size - chain_end
    complete: bool             # True = walked to EOF exactly (or hit max_entries)


def _diag_read_entry_at(f, pos: int, file_size: int) -> "tuple[PakEntry, int] | None":
    f.seek(pos)
    head = f.read(8 * 3 + 4 + 20)
    if len(head) < 8 * 3 + 4 + 20:
        return None
    offset, size, usize = struct.unpack("<qqq", head[:24])
    (method,) = _U32.unpack(head[24:28])
    sha1 = head[28:48]
    if offset != pos:
        raise ValueError(f"entry at {pos:#x}: Offset field {offset:#x} != record position")
    if not (0 <= size <= file_size and 0 <= usize <= 1 << 40):
        raise ValueError(f"entry at {pos:#x}: implausible sizes size={size} usize={usize}")
    cur = pos + 48
    blocks: list[tuple[int, int]] = []
    if method != 0:
        (count,) = _S32.unpack(f.read(4))
        cur += 4
        if not (0 < count <= 1 << 20):
            raise ValueError(f"entry at {pos:#x}: implausible block count {count}")
        for _ in range(count):
            s, e = struct.unpack("<qq", f.read(16))
            blocks.append((s, e))
            cur += 16
    (flags,) = _U8.unpack(f.read(1))
    cur += 1
    (bsize,) = _U32.unpack(f.read(4))
    cur += 4
    if flags & 1:
        raise ValueError(f"entry at {pos:#x}: encrypted payload (no key support yet)")
    data_offset = cur
    if data_offset + size > file_size:
        raise ValueError(f"entry at {pos:#x}: data runs past EOF")
    return (PakEntry("", pos, size, usize, method, sha1, len(blocks), blocks,
                     flags, bsize, data_offset), data_offset + size)


def scan_pak(path: str, max_entries: "int | None" = None,
             cache: "str | None" = None) -> "ScanResult":
    if cache and os.path.isfile(cache):
        with open(cache, encoding="utf-8") as f:
            row = json.load(f)
        return ScanResult(
            [PakEntry(r["name"], r["offset"], r["size"], r["uncompressed_size"],
                      r["method"], bytes.fromhex(r["sha1"]), r["block_count"],
                      [tuple(b) for b in r["blocks"]], r["flags"], r["block_size"],
                      r["data_offset"]) for r in row["entries"]],
            row["chain_end"], row["tail_bytes"], row["complete"])
    file_size = os.path.getsize(path)
    entries: list[PakEntry] = []
    pos = 0
    complete = False
    with open(path, "rb") as f:
        while pos < file_size:
            if max_entries is not None and len(entries) >= max_entries:
                complete = True
                break
            try:
                got = _diag_read_entry_at(f, pos, file_size)
            except ValueError:
                remaining = file_size - pos
                if entries and remaining <= TAIL_LIMIT:
                    break                      # index/footer remnant after last entry
                raise
            if got is None:
                break
            entry, next_pos = got
            entries.append(entry)
            pos = next_pos
            pos += (-(pos % 2)) % 2            # paks align records to 2 bytes
            f.seek(pos)
        else:
            complete = True
    tail_bytes = file_size - pos
    if cache:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"chain_end": pos, "tail_bytes": tail_bytes, "complete": complete,
                       "entries": [{"name": e.name, "offset": e.offset, "size": e.size,
                                    "uncompressed_size": e.uncompressed_size,
                                    "method": e.method, "sha1": e.sha1.hex(),
                                    "block_count": e.block_count,
                                    "blocks": [list(b) for b in e.blocks],
                                    "flags": e.flags, "block_size": e.block_size,
                                    "data_offset": e.data_offset}
                                   for e in entries]}, f, indent=1)
    return ScanResult(entries, pos, tail_bytes, complete)
