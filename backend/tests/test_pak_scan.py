import hashlib
import os
import struct

import pytest

from backend.ut import pak


def _pak(game_paths, game, rel):
    return os.path.join(game_paths[game], *rel)


def test_footer_and_index_sample_a(game_paths):
    p = _pak(game_paths, "sample_a", ("sample_a", "Content", "Paks",
                                          "sample_a-Windows.pak"))
    f = pak.read_footer(p)
    assert f.magic == pak.PAK_MAGIC_PATCHED       # the E1 patch IS the damage
    assert f.version == 11
    assert not f.encrypted_index
    idx = pak.read_index(p, f)
    assert not idx.encrypted
    assert idx.mount_point.startswith("../../../")
    assert len(idx.entries) == 1669
    for e in idx.entries:
        assert e.name and not e.name.startswith("/") and "\\" not in e.name
        assert e.offset < f.index_offset
    e0 = min(idx.entries, key=lambda e: e.offset)
    assert e0.offset == 0 and e0.uncompressed_size == 0x4D   # the BOM INI at offset 0
    assert e0.method == 0


def test_read_entry_data_sample_a_first_entry(game_paths):
    p = _pak(game_paths, "sample_a", ("sample_a", "Content", "Paks",
                                          "sample_a-Windows.pak"))
    idx = pak.read_index(p)
    e0 = min(idx.entries, key=lambda e: e.offset)
    data = pak.read_entry_data(p, e0)
    assert data.startswith(b"\xef\xbb\xbf")
    assert hashlib.sha1(data).digest() == e0.sha1


def test_footer_encrypted_index_sample_b(game_paths):
    p = _pak(game_paths, "sample_b", ("SampleGameB", "Content", "Paks",
                                          "SampleGameB-Windows.pak"))
    f = pak.read_footer(p)
    assert f.magic == pak.PAK_MAGIC_PATCHED
    assert f.version == 11
    assert f.encrypted_index                     # index+payload AES — Task 8 hunts the key
    idx = pak.read_index(p, f)
    assert idx.encrypted and idx.entries == []


def _encoded_blob(method, encrypted, block_count, bsize_field, offset, usize,
                  size, block_sizes=()):
    """Hand-craft one encoded-records-blob entry: bitfield + 64-bit fields."""
    bitfield = (method << 23) | ((1 << 22) if encrypted else 0) \
        | (block_count << 6) | bsize_field
    parts = [struct.pack("<I", bitfield), struct.pack("<q", offset),
             struct.pack("<q", usize)]
    if method != 0:
        parts.append(struct.pack("<q", size))
    parts.extend(struct.pack("<I", b) for b in block_sizes)
    return b"".join(parts)


def test_decode_encoded_entry_block_size_consumption():
    """Pins the reference (rust-u4pak record.rs ~261-272) u32-consumption rule:
    every block_count > 0 reads one u32 per block EXCEPT the synthetic
    unencrypted single-block case. The pre-fix code skipped the u32 for
    encrypted single-block records, which Phase 2's SampleGameB index
    decrypt would hit on nearly every record."""
    # (a) encrypted + block_count == 1: the u32 block size MUST be consumed.
    blob = _encoded_blob(1, True, 1, 0x20, 0x1000, 200000, 123456, [1111])
    meta, pos = pak._decode_encoded_entry(blob, 0)
    rec_size = 53 + 4 + 16                        # header + count + one 16B pair
    assert pos == 4 + 8 + 8 + 8 + 4 == len(blob)  # bitfield+off+usize+size+u32
    assert (meta["encrypted"], meta["block_count"], meta["method"]) == (True, 1, 1)
    assert meta["block_size"] == 0x20 << 11       # usize > 0xFFFF: no override
    assert meta["blocks"] == [(rec_size, rec_size + 1111)]
    # (b) not encrypted + block_count == 1: synthetic block, NO u32 consumed.
    blob = _encoded_blob(1, False, 1, 0x20, 0x2000, 300000, 2222)
    meta, pos = pak._decode_encoded_entry(blob, 0)
    assert pos == 4 + 8 + 8 + 8 == len(blob)
    assert not meta["encrypted"]
    assert meta["blocks"] == [(rec_size, rec_size + 2222)]
    # (c) multi-block, not encrypted: one u32 per block, alignment 1.
    blob = _encoded_blob(1, False, 3, 0x20, 0x3000, 400000, 600, [100, 200, 300])
    meta, pos = pak._decode_encoded_entry(blob, 0)
    assert pos == 4 + 8 + 8 + 8 + 12 == len(blob)
    start = 53 + 4 + 16 * 3
    assert meta["blocks"] == [(start, start + 100), (start + 100, start + 300),
                              (start + 300, start + 600)]
    # (d) multi-block, encrypted: u32s consumed, offsets advance by align-16.
    blob = _encoded_blob(1, True, 2, 0x20, 0x4000, 500000, 400, [100, 200])
    meta, pos = pak._decode_encoded_entry(blob, 0)
    assert pos == 4 + 8 + 8 + 8 + 8 == len(blob)
    start = 53 + 4 + 16 * 2                       # 89; 100 aligns up to 112
    assert meta["blocks"] == [(start, start + 100),
                              (start + 112, start + 112 + 200)]


def test_read_entry_data_compressed_multiblock_sample_a(game_paths):
    """Regression: multi-block Oodle read + on-disk-payload sha1 semantics
    (Task 5 ruling: entry.sha1 covers the compressed bytes on disk)."""
    p = _pak(game_paths, "sample_a", ("sample_a", "Content", "Paks",
                                          "sample_a-Windows.pak"))
    idx = pak.read_index(p)
    cands = sorted((e for e in idx.entries if e.method != 0 and e.block_count >= 2
                    and e.name.lower().endswith(".ini")), key=lambda e: e.name)
    assert cands                                   # the 4-block BaseEngine.ini
    entry = cands[0]
    data = pak.read_entry_data(p, entry)
    assert len(data) == entry.uncompressed_size
    with open(p, "rb") as f:                       # raw on-disk payload slice
        f.seek(entry.data_offset)
        payload = f.read(entry.size)
    assert len(payload) == entry.size
    assert hashlib.sha1(payload).digest() == entry.sha1
    text = data.decode("utf-8-sig")                # plausible .ini content
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    assert printable / len(text) > 0.95 and "[" in text and "=" in text


def test_read_index_encrypted_with_key_sample_b(game_paths):
    import json as _json
    fx = os.path.join(os.path.dirname(__file__), "fixtures", "sample_b_index_facts.json")
    with open(fx, encoding="utf-8") as f:
        key = bytes.fromhex(_json.load(f)["aes_key_hex"])
    p = _pak(game_paths, "sample_b", ("SampleGameB", "Content", "Paks",
                                          "SampleGameB-Windows.pak"))
    idx = pak.read_index(p, aes_key=key)
    assert not idx.encrypted
    assert len(idx.entries) == 17829
    assert idx.mount_point == "../../../"
    # every entry is encrypted payload (Phase 1 sampled flags=1); reading one without key raises
    e0 = min(idx.entries, key=lambda e: e.offset)
    with pytest.raises(ValueError):
        pak.read_entry_data(p, e0)
    data = pak.read_entry_data(p, e0, aes_key=key)
    assert len(data) == e0.uncompressed_size
