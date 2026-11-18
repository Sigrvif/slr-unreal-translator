"""Task 2 discovery fixture: schema gate for the text-layout ground truth.

The fixture ``fixtures/text_discovery.json`` is the hand-off artifact from
the Task 2 discovery spike to the parser tasks (T3-T5): it pins WHERE
Japanese text lives (which assets, which half of uasset/uexp pairs) and in
WHICH serialization shapes. This test pins the SCHEMA only — values are
filled by the spike and are expected to be nonzero for real games.
"""
import json
import os

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "text_discovery.json")

GAMES = ("sample_a", "sample_b")
SHAPE_KEYS = ("shape", "count", "example_asset", "notes")
SAMPLE_KEYS = ("asset", "offset", "text")
NOTABLE_KEYS = ("name", "class_hint", "japanese_runs")


def _load():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def test_fixture_exists_with_both_games():
    data = _load()
    assert set(data) == set(GAMES)


def test_schema_sample_a():
    st = _load()["sample_a"]
    assert isinstance(st["packages_scanned"], int) and st["packages_scanned"] > 0
    runs = st["utf16_japanese_runs"]
    assert isinstance(runs["total"], int) and runs["total"] >= 0
    assert isinstance(runs["samples"], list) and len(runs["samples"]) <= 50
    for s in runs["samples"]:
        assert set(s) == set(SAMPLE_KEYS)
        assert isinstance(s["asset"], str) and s["asset"]
        assert isinstance(s["offset"], int) and s["offset"] >= 0
        assert isinstance(s["text"], str) and len(s["text"]) >= 2
    # samples are the FIRST min(total, 50) runs of the sweep
    assert len(runs["samples"]) == min(runs["total"], 50)
    for shape in st["text_shapes"]:
        assert set(shape) == set(SHAPE_KEYS)
        assert shape["count"] > 0 and shape["example_asset"]
    for a in st["notable_assets"]:
        assert set(a) == set(NOTABLE_KEYS)
        assert a["name"] and a["class_hint"] and a["japanese_runs"] > 0
    enc = st["other_encodings"]
    assert isinstance(enc["utf8_runs"], int) and enc["utf8_runs"] >= 0
    assert isinstance(enc["shiftjis_runs"], int) and enc["shiftjis_runs"] >= 0


def test_schema_sample_b():
    ef = _load()["sample_b"]
    assert isinstance(ef["packages_scanned"], int) and ef["packages_scanned"] > 0
    # EF pairing rule: every package ships its export stream as a separate
    # .uexp pak entry — the fixture pins how many such pairs were scanned.
    assert ef["uasset_uexp_pairs"] == ef["packages_scanned"]
    runs = ef["utf16_japanese_runs"]
    assert isinstance(runs["total"], int) and runs["total"] >= 0
    assert isinstance(runs["samples"], list) and len(runs["samples"]) <= 50
    for s in runs["samples"]:
        assert set(s) == set(SAMPLE_KEYS)
    assert len(runs["samples"]) == min(runs["total"], 50)
    for shape in ef["text_shapes"]:
        assert set(shape) == set(SHAPE_KEYS)
        assert shape["count"] > 0 and shape["example_asset"]
    for a in ef["notable_assets"]:
        assert set(a) == set(NOTABLE_KEYS)
    enc = ef["other_encodings"]
    assert isinstance(enc["utf8_runs"], int) and enc["utf8_runs"] >= 0
    assert isinstance(enc["shiftjis_runs"], int) and enc["shiftjis_runs"] >= 0


def test_at_least_one_game_has_japanese_text():
    """The spike exists because the games contain Japanese; if BOTH totals
    were 0 the parser tasks would have no subject (hunting side channels
    would be the follow-up, and this assertion would flip)."""
    data = _load()
    assert (data["sample_a"]["utf16_japanese_runs"]["total"] > 0
            or data["sample_b"]["utf16_japanese_runs"]["total"] > 0)
