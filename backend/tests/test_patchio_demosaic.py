# -*- coding: utf-8 -*-
"""Phase 8b/8c tests: the IoStore patch container, the ST portable
export, and the standalone demosaic patch. Real-game pins follow the
fixture-key pattern (skipped when the game tree is absent).
"""
import json
import os

import pytest

from backend.ut import demosaicpatch, iostore, patchio, stage as stage_mod
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games


ST_ROOT = os.path.join(_GAMES_ROOT, "SampleGameA")
_HERE = os.path.dirname(os.path.abspath(__file__))
_KEY_FIXTURE = os.path.join(_HERE, "fixtures",
                            "sample_b_index_facts.json")
EF_ROOT = os.path.join(_GAMES_ROOT, "SampleGameB")


def test_patch_container_names():
    u, c = patchio.patch_container_names(r"X:\G\Content\Paks\Game-Windows.utoc")
    assert (os.path.basename(u), os.path.basename(c)) == \
        ("Game-Windows_SLR_P.utoc", "Game-Windows_SLR_P.ucas")


def _st_available():
    return os.path.isdir(ST_ROOT)


@pytest.mark.skipif(not _st_available(), reason="ST game tree not present")
def test_st_portable_export_roundtrip(tmp_path):
    """capture → export_patch (zen route) → verify + structural re-scan
    through the patch pair — the same bytes boot test 13 mounts."""
    from backend.ut import exporttext, payload, textscan, zen
    stage_dir = str(tmp_path / "stage")
    stage_mod.capture_stage(ST_ROOT, stage_dir=stage_dir,
                            extract_out=str(tmp_path / "extract.json"))
    assert stage_mod.load_model(stage_dir)["zen_mode"] is True

    extract = json.load(open(str(tmp_path / "extract.json"), encoding="utf-8"))
    filled = 0
    first_asset = None
    for r in extract["text_table"]:
        # frame rows only: this pins the boot-13 frame patch path. (ZenName
        # rows match the hiragana filter now — and a JP name translated to
        # English legitimately VANISHES from a re-scan, JP-gated; the name
        # path is pinned by test_zenname.py.)
        if r["field"].startswith("<name>"):
            continue
        if any("\u3040" <= c <= "\u309f" for c in r["japanese"]) \
                and len(r["japanese"]) > 2:
            r["english"] = "Test line."
            first_asset = first_asset or r["file"]
            filled += 1
            if filled >= 3:
                break
    res = exporttext.export_patch(stage_dir, extract,
                                  out_dir=str(tmp_path / "out"))
    assert res["verify"] == "ok" and res["rows_patched"] == 3
    assert len(res["containers"]) == 3     # utoc + ucas + anchor pak
    utoc, ucas = res["containers"][:2]
    assert os.path.basename(utoc) == "sample_a-Windows_SLR_P.utoc"

    # the patch TOC carries a REAL directory index naming the patched
    # files (boot-14 lesson: an empty index mounts but never overrides)
    with open(utoc, "rb") as f:
        ptoc = iostore.parse_toc(f.read())
    assert ptoc.mount_point == "../../../"
    assert list(ptoc.file_map) == [
        "../../../sample_a/Content/common/EN_ステータス.uasset"]
    for raw, entry_index in ptoc.file_map.items():
        assert ptoc.entries[entry_index].chunk_id[11] & 0x3F == 1

    # the mount anchor: a .pak ships beside the pair (trio rule — UE
    # registers IoStore containers through the pak platform file; boot
    # 14/14b proved a bare pair is never consulted, and boot 14d proved
    # the game's own pak works as the anchor). The synthesized anchor
    # mirrors the GAME pak's footer — this build uses the patched-e1
    # magic and rejects the stock one — and carries one placeholder
    # entry so the index shape matches the proven EF patch pak.
    from backend.ut import pak
    anchor = os.path.splitext(utoc)[0] + ".pak"
    assert os.path.isfile(anchor)
    af = pak.read_footer(anchor)
    aidx = pak.read_index(anchor, af)
    assert af.magic == pak.PAK_MAGIC_PATCHED
    assert af.method_names.split(b"\x00")[0] == b"Oodle"
    assert aidx.mount_point == "../../../"
    assert len(aidx.entries) == 1
    assert aidx.entries[0].name == "SLRUnreal_mount_anchor.txt"
    assert pak.read_entry_data(anchor, aidx.entries[0]) == b""

    # structural re-scan: the patch TOC parses; the patched package
    # re-reads and re-scans with the translated row present
    with open(utoc, "rb") as f:
        toc = iostore.parse_toc(f.read())
    model = stage_mod.load_model(stage_dir)
    chunk = bytes.fromhex(next(
        t["entries"][first_asset]["chunk_id"]
        for t in model["io"]["tocs"].values()
        if first_asset in t["entries"]))
    idx = next(i for i, e in enumerate(toc.entries) if e.chunk_id == chunk)
    data = iostore.read_entry(toc, ucas, idx)
    so = stage_mod.sources(stage_dir)["_script_objects_pre"]
    pkg = zen.parse_zen(data, first_asset, script_objects=so)
    entries, _w = textscan.scan_text(pkg)
    assert any(e.source == "Test line." for e in entries)

    # container header chunk parses and registers the patched package
    chi = next(i for i, e in enumerate(toc.entries) if e.chunk_type == 6)
    ch = iostore.parse_container_header(iostore.read_entry(toc, ucas, chi))
    assert ch.container_id == toc.header.container_id
    assert len(ch.package_ids) >= 1


@pytest.mark.skipif(not _st_available(), reason="ST game tree not present")
def test_st_demosaic_patch_standalone(tmp_path):
    """The standalone demosaic on ST: 7 high-confidence ops, its OWN
    patch container name (_SLRDM_P), byte-verified read-back."""
    stage_dir = str(tmp_path / "stage")
    stage_mod.capture_stage(ST_ROOT, stage_dir=stage_dir)
    res = demosaicpatch.export_demosaic_patch(stage_dir,
                                              out_dir=str(tmp_path / "out"))
    assert res["verify"] == "ok" and res["ops_applied"] == 7
    assert res["ops_skipped"] == 0
    utoc, ucas = res["containers"][:2]
    assert "_SLRDM_P.utoc" in utoc and "_SLRDM_P.ucas" in ucas
    # re-apply on the SAME staging set: the stage tree is immutable (the
    # first run wrote a patch container, not back into the stage), so a
    # second run re-patches the pristine bytes — same report, same result
    res2 = demosaicpatch.export_demosaic_patch(
        stage_dir, out_dir=str(tmp_path / "out2"))
    assert res2["ops_applied"] == 7 and res2["ops_skipped"] == 0
    assert res2["containers"][0].endswith("_SLRDM_P.utoc")


def _ef_available():
    return os.path.isdir(EF_ROOT) and os.path.isfile(_KEY_FIXTURE)


@pytest.mark.skipif(not _ef_available(), reason="EF game + key fixture not present")
def test_ef_demosaic_patch_standalone(tmp_path):
    from backend.tests.keys import sample_b_key
    key = sample_b_key()
    stage_dir = str(tmp_path / "stage")
    stage_mod.capture_stage(EF_ROOT, aes_key=key, stage_dir=stage_dir)
    # default (high only): EF has no high candidates — empty result, no
    # container, honest report
    res = demosaicpatch.export_demosaic_patch(stage_dir,
                                              out_dir=str(tmp_path / "out"))
    assert res["ops_applied"] == 0 and res["containers"] == []
    # include_medium: the WG_H_Event02 texture-ref swap ships
    res2 = demosaicpatch.export_demosaic_patch(
        stage_dir, out_dir=str(tmp_path / "out2"), include_medium=True)
    assert res2["ops_applied"] == 1
    assert res2["containers"][0].endswith("_SLRDM_P.pak")
