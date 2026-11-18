import os
import struct

import pytest

from backend.ut import iostore, oodle

PAKS = os.path.join("sample_a", "Content", "Paks")


def _dll():
    return oodle.get_dll()


def test_decompress_dll_loads():
    dll = _dll()
    assert dll is not None


def test_extract_container_header_chunk(game_paths):
    # Ruling (Task 4): the ContainerHeader chunk lives in the game container's
    # TOC (sample_a-Windows.utoc), not global.utoc; the magic u32
    # 0x496F436E is LE-serialized (bytes b"nCoI").
    with open(os.path.join(game_paths["sample_a"], PAKS,
                           "sample_a-Windows.utoc"), "rb") as f:
        toc = iostore.parse_toc(f.read())
    ucas = os.path.join(game_paths["sample_a"], PAKS,
                        "sample_a-Windows.ucas")
    idx = next(i for i, e in enumerate(toc.entries)
               if e.chunk_type == iostore.CHUNKTYPE_CONTAINER_HEADER)
    data = iostore.read_entry(toc, ucas, idx, dll=_dll())
    assert struct.unpack("<I", data[:4])[0] == 0x496F436E


def test_extract_sample_lengths(game_paths):
    # Ruling (Task 4): io_hash is BLAKE3 truncated to 20 bytes (proven 100/100
    # entries with a dev-only blake3 install). hashlib has no blake3 and no
    # third-party deps are allowed, so the suite gates integrity on
    # read_entry's internal length check + the LE magic above, and walks both
    # stored and Oodle-compressed entries to exercise both block paths.
    with open(os.path.join(game_paths["sample_a"], PAKS,
                           "sample_a-Windows.utoc"), "rb") as f:
        toc = iostore.parse_toc(f.read())
    ucas = os.path.join(game_paths["sample_a"], PAKS,
                        "sample_a-Windows.ucas")
    dll = _dll()
    bs = toc.header.compression_block_size
    checked = compressed = 0
    for i, e in enumerate(toc.entries):
        if e.length == 0:
            continue
        if toc.blocks[e.offset // bs][3] != 0:
            compressed += 1
        iostore.read_entry(toc, ucas, i, dll=dll)  # raises on length mismatch
        checked += 1
        if checked >= 100:
            break
    assert checked >= 25
    assert compressed >= 5
