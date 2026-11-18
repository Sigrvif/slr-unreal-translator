import os

import pytest

from backend.ut import rewrite

ST_PAKS = os.path.join("sample_a", "Content", "Paks")


def test_rewrite_pak_and_iostore_end_to_end(game_paths, tmp_path):
    import json as _json
    from backend.ut import pak, rewrite
    st = game_paths["sample_a"]
    src = os.path.join(st, "sample_a", "Content", "Paks", "sample_a-Windows.pak")
    idx = pak.read_index(src)
    name = next(e.name for e in idx.entries if e.name.endswith("BaseEngine.ini"))
    plan = rewrite.plan_rewrite(st, src, {name: b"; probe\r\n"})
    assert plan["kind"] == "pak" and len(plan["affected"]) == 1
    written = rewrite.execute_rewrite(plan, str(tmp_path))
    assert len(written) == 1 and written[0].endswith(".pak")
    out = plan["outputs"][0]
    reread = pak.read_index(out)
    assert pak.read_entry_data(out, next(e for e in reread.entries
                                         if e.name == name)) == b"; probe\r\n"


@pytest.mark.slow
def test_rewrite_iostore_end_to_end(game_paths, tmp_path):
    # the brief's test name promises the iostore arm too: plan over the
    # utoc (names resolved via the container file map), execute writes the
    # PAIR mirrored game-relative, chunk patched + neighbors untouched.
    from backend.ut import iostore, oodle, rewrite
    st = game_paths["sample_a"]
    up = os.path.join(st, ST_PAKS, "sample_a-Windows.utoc")
    cp = os.path.join(st, ST_PAKS, "sample_a-Windows.ucas")
    with open(up, "rb") as f:
        toc = iostore.parse_toc(f.read())
    dll = oodle.get_dll()
    name, idx = next((n, ud) for n, ud in toc.file_map.items()
                     if 0 < toc.entries[ud].length < 4096
                     and toc.entries[ud].chunk_type == 1)
    patched = iostore.read_entry(toc, cp, idx, dll=dll) + b"slrunreal-probe"
    plan = rewrite.plan_rewrite(st, up, {name: patched})
    assert plan["kind"] == "iostore" and plan["affected"] == [name]
    written = rewrite.execute_rewrite(plan, str(tmp_path))
    assert written == [
        "sample_a/Content/Paks/sample_a-Windows.utoc",
        "sample_a/Content/Paks/sample_a-Windows.ucas",
    ]
    with open(plan["outputs"][0], "rb") as f:
        toc2 = iostore.parse_toc(f.read())
    assert iostore.read_entry(toc2, plan["outputs"][1], toc2.file_map[name],
                              dll=dll) == patched
    other = next(n for n, ud in toc.file_map.items()
                 if ud != idx and toc.entries[ud].length)
    assert iostore.read_entry(toc2, plan["outputs"][1], toc2.file_map[other],
                              dll=dll) == iostore.read_entry(
                                  toc, cp, toc.file_map[other], dll=dll)


def test_rewrite_plan_validation(game_paths):
    st = game_paths["sample_a"]
    src = os.path.join(st, ST_PAKS, "sample_a-Windows.pak")
    up = os.path.join(st, ST_PAKS, "sample_a-Windows.utoc")
    # replacements must all resolve
    with pytest.raises(KeyError, match="no-such.ini"):
        rewrite.plan_rewrite(st, src, {"no-such.ini": b"x"})
    with pytest.raises(KeyError, match="no-such.uasset"):
        rewrite.plan_rewrite(st, up, {"no-such.uasset": b"x"})
    # kind is chosen by extension; anything else is rejected
    with pytest.raises(ValueError, match="unsupported container"):
        rewrite.plan_rewrite(st, os.path.join(st, "sample_a", "Engine.ini"), {})
    # encrypted index without a key -> informative error, not an empty plan
    ef = os.path.join(game_paths["sample_b"], "SampleGameB", "Content",
                      "Paks", "SampleGameB-Windows.pak")
    with pytest.raises(ValueError, match="aes_key"):
        rewrite.plan_rewrite(game_paths["sample_b"], ef, {})
