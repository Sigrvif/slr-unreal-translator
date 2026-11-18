import json
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tests", "fixtures", "sample_b_index_facts.json")


def test_sample_b_index_facts_schema():
    if not os.path.isfile(FIXTURES):
        pytest.fail("fixtures/sample_b_index_facts.json missing (Task 8)")
    with open(FIXTURES, encoding="utf-8") as f:
        facts = json.load(f)
    for key in ("index_location", "foreign_pak_checks", "evidence", "confidence"):
        assert key in facts, key
    assert facts["index_location"] in ("footer", "hardcoded-offset", "unknown")
    assert facts["foreign_pak_checks"] in ("none-found", "signature", "hash", "unknown")
    assert facts["confidence"] in ("low", "medium", "high")
    assert len(facts["evidence"]) >= 2
    for ev in facts["evidence"]:
        assert {"claim", "function", "file"} <= set(ev)


DEMOSAIC = os.path.join(os.path.dirname(FIXTURES), "sample_a_demosaic_leads.json")


def test_sample_a_demosaic_leads_schema():
    if not os.path.isfile(DEMOSAIC):
        pytest.fail("fixtures/sample_a_demosaic_leads.json missing (Task 9)")
    with open(DEMOSAIC, encoding="utf-8") as f:
        leads = json.load(f)
    assert isinstance(leads.get("pak_mounted"), bool)
    assert "pak_mount_evidence" in leads
    assert isinstance(leads.get("mosaic_strings"), list)
