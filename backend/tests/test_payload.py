"""Task 1 payload layer: uniform (name -> decompressed bytes) over both games.

Pinned facts (2026-10-23 probe of the real games):
  ST (IoStore): sample_a-Windows.utoc file_map has 1708 entries
  (1355 .uasset, 345 .ubulk, 4 .umap, 4 .ushaderbytecode); global.utoc has
  no directory index (0 files); the co-installed pak (1669 entries) carries
  only non-IoStore files (ini/res/png/svg/...) with ZERO name overlap.
  IoStore .uasset payloads END with the package-summary tag 0x9E2A83C1
  (the brief's "plausible package data").
  EF (pak-only): 17,829 entries; every .uasset (7555) AND every .umap (101)
  has an exact-stem .uexp sibling stored as a SEPARATE pak entry
  (7555 + 101 == 7656 .uexp total). DefaultEngine.ini payload is 29,673
  bytes starting "[URL]\\r\\n".
"""
import json
import os

import pytest

from backend.ut import payload

PACKAGE_TAG = b"\xc1\x83*\x9e"          # 0x9E2A83C1 little-endian


def _ef_key() -> bytes:
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    if key is None:
        pytest.skip("sample_b AES key not set (tests/keys.py)")
    return key


@pytest.fixture(scope="session")
def st_src(game_paths):
    return payload.find_sources(game_paths["sample_a"])


@pytest.fixture(scope="session")
def ef_src(game_paths):
    return payload.find_sources(game_paths["sample_b"], aes_key=_ef_key())


def _assert_game_relative(names):
    for n in names:
        assert not n.startswith("/") and not n.startswith("../")
        assert "\\" not in n
        # game-relative, not container-relative (containers live under
        # <Project>/Content/Paks — no payload name may)
        assert "Content/Paks/" not in n


def test_st_list_files(st_src):
    names = payload.list_files(st_src)
    uasset = [n for n in names if n.lower().endswith(".uasset")]
    ubulk = [n for n in names if n.lower().endswith(".ubulk")]
    assert len(uasset) > 1300                  # pinned: 1355
    assert len(ubulk) > 0                      # pinned: 345
    _assert_game_relative(names)
    # game-root-relative: every top-level segment is a directory of the game
    # root itself (pinned: Engine/ 484 + sample_a/ 871 .uasset; plugin
    # content such as Engine/Plugins/.../Content/ included)
    for n in uasset:
        assert n.split("/")[0] in ("sample_a", "Engine")


def test_st_read_uasset_and_ubulk(st_src):
    names = payload.list_files(st_src)
    uasset = sorted(n for n in names if n.lower().endswith(".uasset"))[0]
    ubulk = sorted(n for n in names if n.lower().endswith(".ubulk"))[0]
    data = payload.read_file(st_src, uasset)
    assert data
    assert data.endswith(PACKAGE_TAG)          # plausible package data
    assert payload.read_file(st_src, uasset) == data   # stable re-read
    bulk = payload.read_file(st_src, ubulk)
    assert bulk
    assert payload.read_file(st_src, ubulk) == bulk


def test_ef_list_files(ef_src):
    names = payload.list_files(ef_src)
    assert len(names) > 17000                  # pinned: 17,829
    _assert_game_relative(names)
    assert "SampleGameB/Config/DefaultEngine.ini" in names


def test_ef_read_default_engine_ini(ef_src):
    name = "SampleGameB/Config/DefaultEngine.ini"
    data = payload.read_file(ef_src, name)
    assert data                                # pinned: 29,673 bytes
    assert data.startswith(b"[URL]\r\n")       # pinned head
    assert payload.read_file(ef_src, name) == data     # stable re-read


def test_ef_uasset_uexp_pairing(ef_src):
    """EF pairing rule (pinned): each package file (.uasset and .umap alike)
  is accompanied by its export stream as a SEPARATE pak entry whose name is
  the exact stem with the extension swapped to .uexp."""
    names = payload.list_files(ef_src)
    uasset = {n for n in names if n.lower().endswith(".uasset")}
    umap = {n for n in names if n.lower().endswith(".umap")}
    uexp = {n for n in names if n.lower().endswith(".uexp")}
    assert uasset and umap and uexp
    for n in uasset:
        assert n[:-len(".uasset")] + ".uexp" in uexp
    for n in umap:
        assert n[:-len(".umap")] + ".uexp" in uexp
    one = sorted(uasset)[0]
    assert payload.read_file(ef_src, one)              # both halves of one
    assert payload.read_file(ef_src, one[:-len(".uasset")] + ".uexp")  # pair read


def test_missing_name_raises_named_error(st_src, ef_src):
    for src in (st_src, ef_src):
        with pytest.raises((KeyError, ValueError)) as ei:
            payload.read_file(src, "NoSuch/Dir/Missing_File.uasset")
        assert "Missing_File.uasset" in str(ei.value)
