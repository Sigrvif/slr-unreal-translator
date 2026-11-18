import os

import pytest

from backend.ut import blake3, iostore, oodle

PAKS = os.path.join("sample_a", "Content", "Paks")


def test_known_small_vectors():
    # self-consistency + documented properties; the real gate is the game oracle below
    assert blake3.hash32(b"").hex().startswith("af1349b9f5f9a1a6a0404dea36dcc949")
    assert len(blake3.hash20(b"x")) == 20


def test_matches_real_io_hashes(game_paths):
    with open(os.path.join(game_paths["sample_a"], PAKS,
                           "sample_a-Windows.utoc"), "rb") as f:
        toc = iostore.parse_toc(f.read())
    ucas = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.ucas")
    dll = oodle.get_dll()
    checked = 0
    for i, e in enumerate(toc.entries):
        if e.length == 0 or e.io_hash == b"\x00" * 20:
            continue
        data = iostore.read_entry(toc, ucas, i, dll=dll)
        assert blake3.hash20(data) == e.io_hash, f"entry {i}"
        checked += 1
        if checked >= 20:
            break
    assert checked >= 10
