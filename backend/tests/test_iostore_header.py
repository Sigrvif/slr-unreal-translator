import os

import pytest

from backend.ut.iostore import TOC_HEADER_SIZE, parse_toc_header, serialize_toc_header

SUMMER = os.path.join("sample_a", "Content", "Paks")
GLOBAL = os.path.join("sample_a", "Content", "Paks")


def _read(game_paths, rel):
    with open(os.path.join(game_paths["sample_a"], rel), "rb") as f:
        return f.read()


def test_header_fields_and_roundtrip_summer(game_paths):
    data = _read(game_paths, os.path.join(SUMMER, "sample_a-Windows.utoc"))
    h = parse_toc_header(data)
    assert h.version == 8
    assert h.header_size == TOC_HEADER_SIZE
    assert h.entry_count == 3247
    assert h.compressed_block_entry_count == 19422
    assert h.compressed_block_entry_size == 12
    assert h.compression_method_name_count == 1
    assert h.compression_method_name_length == 32
    assert h.compression_block_size == 0x10000
    assert h.directory_index_size == 83911
    assert h.partition_count == 1
    assert h.perfect_hash_seeds_count == 1624
    assert not h.encrypted
    assert not h.signed
    assert serialize_toc_header(h) == data[:TOC_HEADER_SIZE]


def test_header_global(game_paths):
    data = _read(game_paths, os.path.join(GLOBAL, "global.utoc"))
    h = parse_toc_header(data)
    assert h.version == 8
    assert h.entry_count == 1
    assert h.compressed_block_entry_count == 41
    assert h.directory_index_size == 0
    assert not h.encrypted


def test_bad_magic_raises():
    with pytest.raises(ValueError):
        parse_toc_header(b"\x00" * 256)
