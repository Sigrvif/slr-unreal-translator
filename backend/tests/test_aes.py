import json
import os

import pytest

from backend.ut import aes
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games

DEV = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEY_FIXTURE = os.path.join(DEV, "backend", "tests", "fixtures", "sample_b_index_facts.json")
ORACLE = os.path.join(DEV, ".sdd", "2026-10-22-phase1-repair-read", "sdd",
                      "task8_index_decrypted.bin")
EF_PAK = os.path.join(_GAMES_ROOT, "SampleGameB", "SampleGameB", "Content", "Paks", "SampleGameB-Windows.pak")


def _key() -> bytes:
    with open(KEY_FIXTURE, encoding="utf-8") as f:
        return bytes.fromhex(json.load(f)["aes_key_hex"])


def test_roundtrip_random_blocks():
    key = bytes(range(32))
    data = bytes((i * 7 + 3) & 0xFF for i in range(16 * 100))
    assert aes.encrypt_ecb(key, data) != data
    assert aes.decrypt_ecb(key, aes.encrypt_ecb(key, data)) == data


def test_pure_backend_matches_bcrypt_on_samples():
    if "bcrypt" not in aes.available_backends():
        pytest.skip("bcrypt unavailable")
    if not os.path.isfile(ORACLE):
        pytest.skip("Phase 1 oracle artifact absent")
    key = _key()
    with open(ORACLE, "rb") as f:
        data = f.read(16 * 64)
    enc = aes.encrypt_ecb(key, data)
    pure = aes._pure_ecb
    assert pure.decrypt_ecb(key, enc) == data
    assert pure.encrypt_ecb(key, data) == enc


def test_decrypt_ef_index_oracle():
    if not (os.path.isfile(ORACLE) and os.path.isfile(EF_PAK)):
        pytest.skip("oracle artifact or game absent")
    key = _key()
    from backend.ut import pak
    footer = pak.read_footer(EF_PAK)
    with open(EF_PAK, "rb") as f:
        f.seek(footer.index_offset)
        blob = f.read(footer.index_size)
    plain = aes.decrypt_ecb(key, blob)
    with open(ORACLE, "rb") as f:
        assert plain == f.read()
