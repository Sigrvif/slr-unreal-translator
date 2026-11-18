"""Task 4 discovery fixture: schema gate for the demosaic surface ground
truth.

The fixture ``fixtures/demosaic_discovery.json`` is the hand-off artifact
from the Task 4 demosaic discovery spike to the Task 5 patch
implementation: it records every evidence-cited candidate surface (WHERE
the mosaic is drawn, WHY we believe it, WHAT minimal edit flips it, and
with WHAT confidence). This test pins the SCHEMA only — candidate values
come from the sweep and must be evidence-cited (no speculation in the
fixture; the confidence ladder expresses residual doubt instead).

Confidence ladder (binding for Task 5):
- "high"   — byte-located edit (edit_hint names a hex offset) + usage
             evidence (the object is referenced by scene/widget assets);
- "medium" — byte-located edit or usage evidence, not both;
- "low"    — name/shape observation only; what's missing is in ``why``.
"""
import json
import os

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "demosaic_discovery.json")

GAMES = ("sample_a", "sample_b")
CANDIDATE_KEYS = ("asset", "object", "why", "edit_hint", "confidence")
CONFIDENCE = ("high", "medium", "low")
PROVENANCE_KEYS = ("files_scanned", "sweep_terms")


def _load():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def test_fixture_exists_with_both_games():
    data = _load()
    assert set(data) == set(GAMES)


def _check_game(game: dict) -> None:
    candidates = game["candidates"]
    assert isinstance(candidates, list)
    for c in candidates:
        assert set(c) == set(CANDIDATE_KEYS)
        assert isinstance(c["asset"], str) and c["asset"]
        assert isinstance(c["object"], str) and c["object"]
        assert isinstance(c["why"], str) and len(c["why"]) >= 20
        assert isinstance(c["edit_hint"], str) and c["edit_hint"]
        assert c["confidence"] in CONFIDENCE
        # The ladder is enforced structurally: only a byte-located edit
        # (a hex address in the hint) may carry "high".
        if c["confidence"] == "high":
            assert "0x" in c["edit_hint"], (
                "high confidence requires a byte-located edit")
    for k in PROVENANCE_KEYS:
        assert k in game
    assert isinstance(game["files_scanned"], int) and game["files_scanned"] > 0
    assert isinstance(game["sweep_terms"], list) and game["sweep_terms"]
    for t in game["sweep_terms"]:
        assert isinstance(t, str) and t


def test_schema_sample_a():
    _check_game(_load()["sample_a"])


def test_schema_sample_b():
    _check_game(_load()["sample_b"])


def test_at_least_one_game_has_a_candidate():
    """The spike exists because the games draw a mosaic somewhere; if BOTH
    candidate lists were empty Task 5 would have no subject (hunting via
    runtime inspection would be the follow-up, and this assertion would
    flip)."""
    data = _load()
    assert (len(data["sample_a"]["candidates"]) > 0
            or len(data["sample_b"]["candidates"]) > 0)
