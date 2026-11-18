import os

from backend.ut.iostore import parse_toc, serialize_toc


def _toc_path(game_paths, name):
    return os.path.join(game_paths["sample_a"], "sample_a", "Content", "Paks", name)


def test_full_parse_and_roundtrip_summer(game_paths):
    with open(_toc_path(game_paths, "sample_a-Windows.utoc"), "rb") as f:
        data = f.read()
    toc = parse_toc(data)
    assert len(toc.entries) == 3247
    assert len(toc.blocks) == 19422
    assert toc.compression_methods == ["Oodle"]
    names = toc.file_names()
    # Ruling (Task 3): brief estimated 3000 < names; real directory index has
    # 1708 file entries (dc=218, fc=1708, sc=1847) — ~1.9 chunks per file.
    assert len(names) == 1708                 # files only (non-file chunks have no name)
    assert toc.mount_point.startswith("../../../")
    for name in names:
        assert name.startswith(toc.mount_point)
    for idx in toc.file_map.values():
        assert 0 <= idx < len(toc.entries)
    # every entry's block range stays inside the block table
    bs = toc.header.compression_block_size
    for e in toc.entries:
        if e.length == 0:
            continue
        first = e.offset // bs
        last = (e.offset + e.length - 1) // bs
        assert 0 <= first <= last < len(toc.blocks)
    assert serialize_toc(toc) == data         # golden: byte-identical round-trip
    # Ruling (Task 3): brief annotated "~128 observed", but the header counts
    # explain this file exactly (slack=0) — there are no trailing opaque bytes.
    assert len(toc.raw_tail) == 0


def test_full_parse_global(game_paths):
    with open(_toc_path(game_paths, "global.utoc"), "rb") as f:
        data = f.read()
    toc = parse_toc(data)
    assert len(toc.entries) == 1
    assert toc.file_names() == []             # no directory index in global.utoc
    assert serialize_toc(toc) == data


# ---------------------------------------------------------- dir index

def test_serialize_dir_index_roundtrip():
    """serialize_dir_index -> parse (parse_toc's exact grammar): the
    file_map must round-trip, including unicode segments and a mount
    prefix that is a strict prefix of the paths. Boot-14 lesson: a patch
    TOC with an EMPTY directory index mounted but never overrode — the
    patch writer now always writes the real index."""
    import io as _io
    import struct

    from backend.ut import iostore

    mount = "../../../sample_a/"
    names = {
        "../../../sample_a/Content/common/データ/DT_オプション.uasset": 2,
        "../../../sample_a/Content/common/GI_common.uasset": 1,
        "../../../sample_a/Content/common/BP_save.uasset": 0,
    }
    blob = iostore.serialize_dir_index(mount, names)

    buf = _io.BytesIO(blob)
    mp = iostore._read_fstring(buf)
    (dc,) = struct.unpack("<i", buf.read(4))
    dirs = [iostore._DirEntry(*struct.unpack("<4i", buf.read(16)))
            for _ in range(max(dc, 0))]
    (fc,) = struct.unpack("<i", buf.read(4))
    files = [iostore._FileEntry(*struct.unpack("<iiI", buf.read(12)))
             for _ in range(max(fc, 0))]
    (sc,) = struct.unpack("<i", buf.read(4))
    strings = [iostore._read_fstring(buf) for _ in range(max(sc, 0))]
    assert buf.read() == b""                       # blob consumed exactly

    got = {p: fe.user_data for p, fe in zip(
        iostore._collect_paths(mp, dirs, files, strings),
        iostore._iter_files(dirs, files))}
    assert got == names
