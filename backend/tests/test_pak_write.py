import hashlib
import os
import struct
import tempfile

import pytest

from backend.ut import aes, pak


def _st_pak(game_paths):
    return os.path.join(game_paths["sample_a"], "sample_a", "Content", "Paks",
                        "sample_a-Windows.pak")


def _encoded_meta(path, reread, entry):
    """Decode the entry's record straight from the rebuilt file's encoded
    records blob — the view a v11 game decodes — independent of the inline
    record our reader prefers."""
    with open(path, "rb") as fh:
        fh.seek(reread.footer.index_offset)
        index = fh.read(reread.footer.index_size)
    pos = 0
    _, pos = pak._read_fstring(index, pos)
    pos += 4 + 8                                    # entry_count, seed
    (has_phi,) = struct.unpack_from("<I", index, pos)
    pos += 4
    if has_phi:
        pos += 8 + 8 + 20
    (has_fdi,) = struct.unpack_from("<I", index, pos)
    pos += 4
    if has_fdi:
        pos += 8 + 8 + 20
    (pes,) = struct.unpack_from("<i", index, pos)
    pos += 4
    blob = index[pos:pos + pes]
    meta, _ = pak._decode_encoded_entry(blob, entry.encoded_pos)
    return meta


def test_rebuild_unmodified_byte_identical_sample_a(game_paths):
    src = _st_pak(game_paths)
    idx = pak.read_index(src)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out)
        with open(src, "rb") as a, open(out, "rb") as b:
            assert os.path.getsize(out) == os.path.getsize(src)
            while True:
                ca, cb = a.read(1 << 20), b.read(1 << 20)
                assert ca == cb
                if not ca:
                    break


def test_rebuild_with_modified_entry(game_paths):
    src = _st_pak(game_paths)
    idx = pak.read_index(src)
    target = next(e for e in idx.entries if e.name.endswith("BaseEngine.ini"))
    original = pak.read_entry_data(src, target)
    modified = original + b"\r\n; slrunreal roundtrip probe\r\n"
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out, entries_data={target.name: modified})
        reread = pak.read_index(out)
        assert pak.read_entry_data(out, next(e for e in reread.entries
                                             if e.name == target.name)) == modified
        # every OTHER entry still byte-identical
        src_map = {e.name: e for e in idx.entries}
        diff_names = [e.name for e in reread.entries
                      if pak.read_entry_data(out, e) != pak.read_entry_data(src, src_map[e.name])]
        assert diff_names == [target.name]
        assert pak.read_footer(out).magic == pak.PAK_MAGIC_PATCHED


def test_rebuild_grown_small_compressed_entry_stays_consistent(game_paths):
    # Fix-round-1 finding 1: a small single-block compressed entry carries
    # block_size == uncompressed_size (the decoder substitution rule), which
    # is NOT a chunk size. Growing such an entry must chunk at 64 KiB and
    # keep the ENCODED record's decoded block_size equal to the inline
    # record's — a v11 game decodes the encoded record and computes
    # per-block uncompressed sizes from it.
    src = _st_pak(game_paths)
    idx = pak.read_index(src)
    target = next(e for e in idx.entries
                  if e.name.endswith("icudt64l/timezoneTypes.res"))
    assert target.block_count == 1
    assert target.block_size == target.uncompressed_size   # substitution case
    original = pak.read_entry_data(src, target)
    probes = {
        "grown-small": original + b"\r\n; slrunreal growth probe\r\n",  # 1 block
        "grown-multi": original * 4,                                     # > 64 KiB
    }
    for label, modified in probes.items():
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, f"grown-{label}.pak")
            pak.write_pak(src, idx, out, entries_data={target.name: modified})
            reread = pak.read_index(out)
            entry = next(e for e in reread.entries if e.name == target.name)
            assert pak.read_entry_data(out, entry) == modified
            meta = _encoded_meta(out, reread, entry)
            # encoded and inline records must agree on block_size
            assert meta["block_size"] == entry.block_size, label
            # per-block uncompressed sizes cover the content exactly, no
            # zero-size (dead) blocks
            bs, usize = meta["block_size"], len(modified)
            wants = [min(bs, usize - i * bs) for i in range(meta["block_count"])]
            assert sum(wants) == usize and all(w > 0 for w in wants), label


def test_write_pak_encrypted_index_sha1_covers_padding(game_paths):
    # Fix-round-1 finding 2a: the footer index hash must cover the PADDED
    # plaintext index (the reader's / Phase-1 oracle domain), not the bare
    # index bytes — else an encrypted writer->reader roundtrip fails
    # "sha1 mismatch" whenever padding > 0.
    # Task 5 (Task-4 review finding 2b): with aes_key the SECONDARY index
    # extents (path-hash + full-directory) must be encrypted too, hashed
    # over the padded plaintext — exactly the domains the reader verifies
    # (it decrypts both extents and checks them against the hashes embedded
    # in the primary index). The modification below grows one encoded
    # record so extent positions genuinely shift and must be re-spliced in
    # the PLAINTEXT domain before re-encryption.
    src = _st_pak(game_paths)
    idx = pak.read_index(src)
    key = bytes(range(32))
    target = next(e for e in idx.entries if e.name.endswith("BaseCompat.ini"))
    original = pak.read_entry_data(src, target)
    modified = original + b"\x00" * (65536 * 2)          # 1 block -> ~4
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "enc-index.pak")
        pak.write_pak(src, idx, out, entries_data={target.name: modified},
                      aes_key=key)
        f2 = pak.read_footer(out)
        assert f2.encrypted_index
        assert f2.index_size % 16 == 0
        assert f2.index_size > idx.footer.index_size        # padding present
        with open(out, "rb") as fh:
            fh.seek(f2.index_offset)
            blob = fh.read(f2.index_size)
        plain = aes.decrypt_ecb(key, blob)
        assert plain.startswith(idx.mount_raw)
        assert hashlib.sha1(plain).digest() == f2.index_sha1
        # reader roundtrip: decrypts + sha1-verifies BOTH secondary extents
        reread = pak.read_index(out, aes_key=key)
        entry = next(e for e in reread.entries if e.name == target.name)
        assert pak.read_entry_data(out, entry) == modified
        src_map = {e.name: e for e in idx.entries}
        diff = [e.name for e in reread.entries
                if pak.read_entry_data(out, e)
                != pak.read_entry_data(src, src_map[e.name])]
        assert diff == [target.name]
        # the encoded-record growth really shifted downstream positions
        assert any(e.encoded_pos != src_map[e.name].encoded_pos
                   for e in reread.entries if e.name != target.name)


@pytest.mark.slow
def test_rebuild_unmodified_byte_identical_sample_b(game_paths):
    import json as _json
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    if key is None:
        pytest.skip("sample_b AES key not set (tests/keys.py)")
    src = os.path.join(game_paths["sample_b"], "SampleGameB", "Content", "Paks",
                       "SampleGameB-Windows.pak")
    idx = pak.read_index(src, aes_key=key)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out, aes_key=key)
        a_size, b_size = os.path.getsize(src), os.path.getsize(out)
        assert b_size == a_size
        ha, hb = hashlib.sha256(), hashlib.sha256()
        with open(src, "rb") as a, open(out, "rb") as b:
            for chunk_a, chunk_b in zip(iter(lambda: a.read(1 << 22), b""),
                                        iter(lambda: b.read(1 << 22), b"")):
                ha.update(chunk_a); hb.update(chunk_b)
        assert ha.hexdigest() == hb.hexdigest()


@pytest.mark.slow
def test_rebuild_ef_modified_entry(game_paths):
    import json as _json
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    if key is None:
        pytest.skip("sample_b AES key not set (tests/keys.py)")
    src = os.path.join(game_paths["sample_b"], "SampleGameB", "Content", "Paks",
                       "SampleGameB-Windows.pak")
    idx = pak.read_index(src, aes_key=key)
    target = next(e for e in idx.entries if e.flags & 1 and e.method != 0)
    original = pak.read_entry_data(src, target, aes_key=key)
    modified = original + b"\x00slrunreal-probe"
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out, entries_data={target.name: modified}, aes_key=key)
        reread = pak.read_index(out, aes_key=key)
        got = pak.read_entry_data(out, next(e for e in reread.entries
                                            if e.name == target.name), aes_key=key)
        assert got == modified


@pytest.mark.slow
def test_rebuild_ef_stored_encrypted_entry(game_paths):
    # Phase 2 final-review pin: the STORED+encrypted replacement path
    # (method == 0, flags & 1 — payload re-encrypted with the exact-prefix
    # ciphertext sha1 rule, Task 1) had no direct probe on the real EF pak;
    # the modified-entry siblings above exercise only compressed entries.
    import json as _json
    import random
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    if key is None:
        pytest.skip("sample_b AES key not set (tests/keys.py)")
    src = os.path.join(game_paths["sample_b"], "SampleGameB", "Content", "Paks",
                       "SampleGameB-Windows.pak")
    idx = pak.read_index(src, aes_key=key)
    target = next(e for e in idx.entries
                  if e.method == 0 and e.flags & 1
                  and e.name.endswith("DefaultVolumeTexture2D.uasset"))
    assert target.block_count == 0
    original = pak.read_entry_data(src, target, aes_key=key)
    modified = original + b"\x00slrunreal-stored-probe"
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out, entries_data={target.name: modified},
                      aes_key=key)
        reread = pak.read_index(out, aes_key=key)
        got = pak.read_entry_data(out, next(e for e in reread.entries
                                            if e.name == target.name), aes_key=key)
        assert got == modified
        assert len(modified) == len(original) + len(b"\x00slrunreal-stored-probe")
        # ~10 random other entries (stored and compressed, all encrypted on
        # EF) still read byte-identical from the rebuild
        rng = random.Random(20261022)
        src_map = {e.name: e for e in idx.entries}
        for e in rng.sample([e for e in reread.entries if e.name != target.name],
                            10):
            assert pak.read_entry_data(out, e, aes_key=key) == \
                pak.read_entry_data(src, src_map[e.name], aes_key=key)
