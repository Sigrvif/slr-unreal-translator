# -*- coding: utf-8 -*-
"""Phase 8a patch-pak tests: synthetic + real-EF round trips.

The synthetic tests build a stage model by hand (no game needed) and
pin the patch-pak contract: names resolve through the synthesized FDI,
byte-exact read-back, method/encryption shapes preserved, patch naming.
The real-game pin runs the full portable path on EF (capture → export_patch
→ verify → structural re-scan) — the same bytes boot test 11 mounts.
"""
import json
import os
import struct

import pytest

from backend.ut import pak, payload, patchpak, stage as stage_mod
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games


def _hand_model(entries_meta, mount_raw=b"", enc=False, magic=0x5A6F12E1):
    """A stage model fragment for one pak, as capture_stage would write it."""
    return {"version": 1, "game_root_name": "Game",
            "aes_key_hex": "11" * 32 if enc else None,
            "zen_mode": False, "script_objects": False,
            "containers": [],
            "pak": {"paks": {"X:/Game/Content/Paks/Game-Windows.pak": {
                "footer": {"version": 11, "magic": magic,
                           "encrypted_index": enc,
                           "encryption_guid": "00" * 16,
                           "method_names": "00" * 160},
                "mount_raw": mount_raw.hex(),
                "path_hash_seed": 0,
                "entries": entries_meta}}},
            "io": {}, "staged_files": [], "ufonts": [],
            "demosaic_assets": []}


def test_patch_pak_name():
    assert patchpak.patch_pak_name(
        r"X:\Game\Content\Paks\SampleGameB-Windows.pak") == \
        "SampleGameB-Windows_SLR_P.pak"
    assert patchpak.patch_pak_name("Game.pak") == "Game_SLR_P.pak"


def test_patch_pak_synthetic_roundtrip(tmp_path):
    """Unencrypted source pak shape: two entries, method 0, flags 0 —
    the patch pak re-reads byte-exact and resolves names through the
    synthesized FDI."""
    entries = {"Game/Content/A.uasset": b"HEADER-A",
               "Game/Content/A.uexp": b"EXP-A"}
    meta = {n: {"index_name": n, "method": 0, "flags": 0,
                "uncompressed_size": len(d), "order": i}
            for i, (n, d) in enumerate(sorted(entries.items()))}
    model = _hand_model(meta)
    stage_dir = str(tmp_path / "stage")
    os.makedirs(stage_dir)
    with open(os.path.join(stage_dir, "stage.json"), "w", encoding="utf-8") as f:
        json.dump(model, f)
    out = str(tmp_path / "Game-Windows_SLR_P.pak")
    patchpak.write_patch_pak(model, stage_dir, dict(entries), out)
    patchpak.verify_patch_pak(out, dict(entries), model)

    idx = pak.read_index(out)
    assert sorted(e.name for e in idx.entries) == sorted(entries)
    assert all(e.method == 0 and not e.encrypted for e in idx.entries)
    assert idx.footer.encrypted_index is False
    # one-pak payload handle reads the entries back
    src = {"paks": [out], "io_pairs": [], "aes_key": None, "_files": None}
    assert payload.read_file(src, "Game/Content/A.uexp") == b"EXP-A"


def test_patch_pak_missing_stage_entry_raises(tmp_path):
    meta = {"Game/Content/A.uexp": {"index_name": "Game/Content/A.uexp",
                                    "method": 0, "flags": 0,
                                    "uncompressed_size": 5, "order": 0}}
    model = _hand_model(meta)
    stage_dir = str(tmp_path / "stage")
    os.makedirs(stage_dir)
    with open(os.path.join(stage_dir, "stage.json"), "w", encoding="utf-8") as f:
        json.dump(model, f)
    with pytest.raises(KeyError, match="only carries files captured"):
        patchpak.write_patch_pak(model, stage_dir,
                                 {"Game/Content/B.uexp": b"x"},
                                 str(tmp_path / "out.pak"))


# --------------------------------------------------------------------------
# real-game pin: the full portable path on EF

_HERE = os.path.dirname(os.path.abspath(__file__))
_KEY_FIXTURE = os.path.join(_HERE, "fixtures",
                            "sample_b_index_facts.json")
EF_ROOT = os.path.join(_GAMES_ROOT, "SampleGameB")


def _ef_available():
    return os.path.isdir(EF_ROOT) and os.path.isfile(_KEY_FIXTURE)


@pytest.mark.skipif(not _ef_available(), reason="EF game + key fixture not present")
def test_ef_portable_export_roundtrip(tmp_path):
    """capture → export_patch → verify + structural re-scan, no game reads
    after the capture (the game tree is never touched in export_patch)."""
    from backend.ut import exporttext
    key = bytes.fromhex(json.load(open(_KEY_FIXTURE, encoding="utf-8"))["aes_key_hex"])
    stage_dir = str(tmp_path / "stage")
    stage_mod.capture_stage(EF_ROOT, aes_key=key, stage_dir=stage_dir,
                            extract_out=str(tmp_path / "extract.json"))

    extract = json.load(open(str(tmp_path / "extract.json"), encoding="utf-8"))
    WG = "SampleGameB/Content/SampleGameB/Widget/Title/WG_Title.uexp"
    for r in extract["text_table"]:
        if r["file"] == WG and r["japanese"] in ("Option", "Credit"):
            r["english"] = ("Options" if r["japanese"] == "Option"
                            else "Credits")
    res = exporttext.export_patch(stage_dir, extract, out_dir=str(tmp_path / "out"))
    assert res["verify"] == "ok"
    assert res["rows_patched"] == 2
    assert len(res["containers"]) == 1
    out = res["containers"][0]
    assert os.path.basename(out) == "SampleGameB-Windows_SLR_P.pak"
    assert os.path.getsize(out) < 100_000        # a patch, not a rebuild

    # structural re-scan through the patch pak
    from backend.ut import uasset, textscan
    vsrc = {"paks": [out], "io_pairs": [], "aes_key": key, "_files": None}
    ua = payload.read_file(vsrc, WG[:-5] + ".uasset")
    ux = payload.read_file(vsrc, WG)
    pkg = uasset.parse_legacy(ua, ux, WG)
    entries, _w = textscan.scan_text(pkg)
    sources = {e.source for e in entries}
    assert {"Options", "Credits"} <= sources
    assert "Option" not in sources and "Credit" not in sources

    # shape pin: encrypted entries + encrypted index like the source
    idx = pak.read_index(out, aes_key=key)
    assert idx.footer.encrypted_index is True
    assert all(e.encrypted for e in idx.entries)
    assert all(e.method == 1 for e in idx.entries)
