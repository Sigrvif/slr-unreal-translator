import json
import os
import pytest

from backend.ut import payload, textscan

# Local test games (skip-gated): point these at your own copies via env.
GAMES = {
    "sample_a": os.environ.get("SLRUNREAL_GAME_A", ""),
    "sample_b": os.environ.get("SLRUNREAL_GAME_B", ""),
}


@pytest.fixture(scope="session")
def game_paths():
    missing = [k for k, v in GAMES.items() if not os.path.isdir(v)]
    if missing:
        pytest.skip(f"test games not present: {missing}")
    return dict(GAMES)


# Shared game-scan fixtures (Task 6: moved here from test_textscan so
# every module reuses ONE scan per game per run — pytest gives each
# module its own FixtureDef for same-named fixtures, so cross-module
# imports or per-module copies would re-scan EF's 7,656 pairs).
def _ef_key() -> bytes:
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    if key is None:
        pytest.skip("sample_b AES key not set (SLRUNREAL_SAMPLE_B_AES_KEY or "
                    "fixtures/local/sample_b.key)")
    return key


@pytest.fixture(scope="session")
def st_src(game_paths):
    return payload.find_sources(game_paths["sample_a"])


@pytest.fixture(scope="session")
def ef_src(game_paths):
    return payload.find_sources(game_paths["sample_b"], aes_key=_ef_key())


@pytest.fixture(scope="session")
def st_scan(st_src):
    return textscan.scan_game(st_src)


@pytest.fixture(scope="session")
def ef_scan(ef_src):
    return textscan.scan_game(ef_src)
