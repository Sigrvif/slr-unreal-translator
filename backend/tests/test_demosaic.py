# -*- coding: utf-8 -*-
"""Phase 4a Task 5: demosaic patch implementation tests."""
import json
import os

import pytest

from backend.ut import demosaic, payload
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tests", "fixtures", "demosaic_discovery.json")
KEY_FIXTURE = os.path.join(os.path.dirname(FIXTURE), "sample_b_index_facts.json")
GAMES = {
    "sample_a": os.path.join(_GAMES_ROOT, "SampleGameA"),
    "sample_b": os.path.join(_GAMES_ROOT, "SampleGameB"),
}


def _key():
    with open(KEY_FIXTURE, encoding="utf-8") as f:
        return bytes.fromhex(json.load(f)["aes_key_hex"])


def test_plan_defaults_to_viable_candidates():
    plan = demosaic.plan_demosaic(GAMES["sample_a"])
    # 7 HIGH ST candidates; the LOW M_Mosaic material asset is excluded by default
    assert len(plan) == 7
    assert all(op.confidence == "high" for op in plan)
    assert all(op.old != op.new and len(op.old) == len(op.new) == 4 for op in plan)


def test_plan_sample_b_medium_excluded_by_default_but_selectable():
    assert demosaic.plan_demosaic(GAMES["sample_b"]) == []      # medium: opt-in
    plan = demosaic.plan_demosaic(GAMES["sample_b"], selection=None,
                                  include_medium=True)
    assert len(plan) == 1
    assert plan[0].asset.endswith("EV_Scene04.uexp")


def test_plan_selection_filters_by_asset():
    plan = demosaic.plan_demosaic(GAMES["sample_a"],
                                  selection=["sample_a/Content/Characters/Heroine.uasset"])
    assert len(plan) == 1
    assert plan[0].asset.endswith("Heroine.uasset")
    with pytest.raises(ValueError):
        demosaic.plan_demosaic(GAMES["sample_a"], selection=["nope.uasset"])


def test_plan_rejects_low_confidence():
    # the LOW unpinned EF candidate and the ST M_Mosaic material asset never plan
    for game in GAMES:
        for op in demosaic.plan_demosaic(GAMES[game], include_medium=True,
                                         include_low=True):
            assert op.confidence in ("high", "medium")


def test_apply_and_verify_sample_a(tmp_path):
    plan = demosaic.plan_demosaic(GAMES["sample_a"])
    result = demosaic.apply_demosaic(plan, GAMES["sample_a"], out_dir=str(tmp_path))
    assert result["verify"] == "ok"
    assert result["ops_applied"] == 7
    assert len(result["containers"]) == 2            # utoc + ucas
    # independent check: the patched zen chunk read back binds the clean material
    out_u, out_c = result["containers"]
    with open(out_u, "rb") as f:
        toc_staged = f.read()
    from backend.ut import iostore, oodle, zen
    toc = iostore.parse_toc(toc_staged)
    dll = oodle.get_dll()
    # find the heroine chunk via file_map on the OUTPUT toc and confirm no
    # M_Mosaic import remains in any patched package
    patched = {op.asset for op in plan}
    checked = 0
    for name, idx in toc.file_map.items():
        game_rel = name.split("../")[-1] if name.startswith("../") else name
        if game_rel in patched:
            op = next(o for o in plan if o.asset == game_rel)
            data = iostore.read_entry(toc, out_c, idx, dll=dll)
            pkg = zen.parse_zen(data, game_rel)
            # the new 4 bytes bind a CLEAN material import: -(idx+1) encoding
            new_idx = -int.from_bytes(op.new, "little", signed=True) - 1
            assert "M_Mosaic" not in str(pkg.imports[new_idx]), game_rel
            checked += 1
    assert checked == 7


def test_apply_and_verify_sample_b(tmp_path):
    plan = demosaic.plan_demosaic(GAMES["sample_b"], include_medium=True)
    assert len(plan) == 1
    result = demosaic.apply_demosaic(plan, GAMES["sample_b"], aes_key=_key(),
                                     out_dir=str(tmp_path))
    assert result["verify"] == "ok"
    assert result["ops_applied"] == 1
    # read the widget back out of the rebuilt (encrypted) pak: the patched
    # offset must now bind import[81] (clean) — and the mosaic import value
    # must be gone from that site
    from backend.ut import pak
    out_pak = result["containers"][0]
    pidx = pak.read_index(out_pak, aes_key=_key())
    entry = next(e for e in pidx.entries if e.name.endswith("EV_Scene04.uexp"))
    data = pak.read_entry_data(out_pak, entry, aes_key=_key())
    op = plan[0]
    assert data[op.offset:op.offset + 4] == op.new
