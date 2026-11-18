import os

import pytest

from backend.ut import manifest


def _ufs(game_paths, game):
    root = game_paths[game]
    return os.path.join(root, "Manifest_UFSFiles_Win64.txt")


def test_load_ufs_summer(game_paths):
    ufs = manifest.load_ufs(_ufs(game_paths, "sample_a"))
    assert len(ufs) == 4737
    assert any(p.endswith(".ini") for p in ufs)
    assert any("Content/Paks" not in p for p in ufs)   # UFS = staged-for-containers list


def test_intersect_names_and_normalize():
    class E:
        def __init__(self, name):
            self.name = name
    ufs = ["Game/Content/A.ini", "Game/Content/B.uexp", "Engine/Content/C.uasset"]
    entries = [E("../../../Game/Content/A.ini"), E("../../../Game/Content/B.uexp")]
    assert manifest.normalize_pak_name("../../../Game/",
                                       "../../../Game/Content/A.ini") == "Game/Content/A.ini"
    assert manifest.intersect_names(entries, ufs) == 1.0
    assert manifest.intersect_names(entries + [E("../../../Game/Content/X.ini")],
                                    ufs) == pytest.approx(2 / 3)


def test_assign_by_order_exact_and_mismatch():
    entries = [type("E", (), {"__dict__": {}})() for _ in range(3)]
    mapping, conf = manifest.assign_by_order(entries, ["a", "b", "c"])
    assert mapping == {0: "a", 1: "b", 2: "c"} and conf == 1.0
    mapping, conf = manifest.assign_by_order(entries, ["a", "b"])
    assert mapping == {} and conf == 0.0


def test_ef_index_manifest_reconciliation(game_paths):
    """Closes the Phase 1 pinned question: 17,829 pak index entries vs 17,830
    manifest lines — name the difference.

    The single unmatched manifest line is the pak file ITSELF
    (SampleGameB/Content/Paks/SampleGameB-Windows.pak, manifest line 17,830
    with staging timestamp 2024-12-20T09:28:16.655Z): the UFS manifest lists
    every staged file in the install, including the container, while the pak
    index by construction cannot contain itself. Verified: that line resolves
    on disk to the very pak parsed here (same file, 3,183,813,419 B), the
    index holds ZERO container-extension entries (so .utoc/.ucas/.sig/.assign
    in the filter are moot for EF — only .pak is exercised), and the reverse
    direction is exact: all 17,829 normalized index names appear in the
    manifest. Coverage: 17,829/17,830 manifest lines are pak entries."""
    import json as _json
    from backend.ut import pak, manifest as M
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    if key is None:
        pytest.skip("sample_b AES key not set (tests/keys.py)")
    p = os.path.join(game_paths["sample_b"], "SampleGameB", "Content", "Paks",
                     "SampleGameB-Windows.pak")
    idx = pak.read_index(p, aes_key=key)
    ufs = M.load_ufs(os.path.join(game_paths["sample_b"], "Manifest_UFSFiles_Win64.txt"))
    names = {M.normalize_pak_name(idx.mount_point, e.name) for e in idx.entries}
    assert len(names) == 17829
    # reverse direction first: every index entry is a manifest line
    assert names <= set(ufs)
    # raw diff names the difference: exactly the pak itself, nothing else
    unmatched = {u for u in ufs if u not in names}
    assert unmatched == {"SampleGameB/Content/Paks/SampleGameB-Windows.pak"}
    # with the container self-reference filtered out, coverage is complete
    missing = [u for u in ufs if u not in names and not u.lower().endswith(
        (".pak", ".utoc", ".ucas", ".sig", ".assign"))]
    assert missing == []
