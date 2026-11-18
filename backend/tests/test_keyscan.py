# -*- coding: utf-8 -*-
"""Phase 4b Task 2: offline AES key discovery tests."""
import json
import os

import pytest

from backend.ut import keyscan
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games

EF_GAME = os.path.join(_GAMES_ROOT, "SampleGameB")
KEY_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "sample_b_index_facts.json")


def _real_key() -> bytes:
    with open(KEY_FIXTURE, encoding="utf-8") as f:
        return bytes.fromhex(json.load(f)["aes_key_hex"])


def test_validator_accepts_real_key_rejects_wrong():
    from backend.ut import pak
    p = os.path.join(EF_GAME, "SampleGameB", "Content", "Paks",
                     "SampleGameB-Windows.pak")
    if not os.path.isfile(p):
        pytest.skip("game absent")
    footer = pak.read_footer(p)
    with open(p, "rb") as f:
        f.seek(footer.index_offset)
        blob = f.read(16 * 4)                      # first blocks only
    assert keyscan._plausible_index_block(_real_key(), blob)
    wrong = bytes(b ^ 0xFF for b in _real_key())
    assert not keyscan._plausible_index_block(wrong, blob)


def test_keys_json_cache_roundtrip(tmp_path):
    store = tmp_path / "keys.json"
    keyscan.save_key(store, "sample_b", "AB" * 32, source="test")
    loaded = keyscan.load_keys(store)
    assert loaded["sample_b"]["key_hex"] == "ab" * 32  # stored lowercase
    # tolerant of garbage
    store.write_text("{not json", encoding="utf-8")
    assert keyscan.load_keys(store) == {}


def test_discover_finds_real_sample_b_key(game_paths):
    if "sample_b" not in game_paths:
        pytest.skip("game absent")
    result = keyscan.discover_key(game_paths["sample_b"],
                                  max_candidates=4_000_000)
    assert result["key_hex"] == _real_key().hex()
    assert result["source"] == "exe-scan"
    # offset: byte position for contiguous hits, -1 for compiler-chain hits
    assert isinstance(result["offset"], int)


def test_discover_no_encrypted_pak_raises(game_paths):
    if "sample_a" not in game_paths:
        pytest.skip("game absent")
    with pytest.raises(keyscan.KeyNotFoundError):
        keyscan.discover_key(game_paths["sample_a"])
