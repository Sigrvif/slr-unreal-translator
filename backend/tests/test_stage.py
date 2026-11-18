# -*- coding: utf-8 -*-
"""Phase 8a staging tests: capture → load → sources round trip, plus the
fingerprint check. Real-game pins reuse the existing fixture-key pattern
(test_ftext_ascii.py) — the small-game unit tests run against a
constructed legacy pak (test_pak_write.py's writer) so they need no
installed game.
"""
import json
import os
import struct

import pytest

from backend.ut import stage as stage_mod, pak, payload
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games


def _make_legacy_pak(tmp_path, entries):
    """A minimal unencrypted v11 pak with the given {name: bytes} entries,
    written through write_pak's source-less mode (boot-strapped with a
    synthesized fdi)."""
    # bootstrap: first write is source-less (synth fdi supplies names)
    entries_meta = {n: {"index_name": n, "method": 0, "flags": 0,
                        "uncompressed_size": len(d), "order": i}
                    for i, (n, d) in enumerate(sorted(entries.items()))}
    paks = {str(tmp_path / "Game-Windows.pak"): {
        "footer": {"version": 11, "magic": 0x5A6F12E1,
                   "encrypted_index": False,
                   "encryption_guid": "00" * 16, "method_names": "00" * 160},
        "mount_raw": (b"\x0e\x00\x00\x00../../../Game/\x00").hex()
        if False else b"",
        "path_hash_seed": 0, "entries": entries_meta}}
    model = {"version": 1, "game_root_name": "Game",
             "aes_key_hex": None, "zen_mode": False, "script_objects": False,
             "containers": [], "pak": {"paks": paks}, "io": {},
             "staged_files": sorted(entries), "ufonts": [],
             "demosaic_assets": []}
    from backend.ut import patchpak
    out = str(tmp_path / "Game-Windows.pak")
    patchpak.write_patch_pak(model, str(tmp_path), dict(entries), out)
    return out, model


def test_stage_sources_roundtrip(tmp_path):
    """capture over a real game is covered by the real-game pins below;
    here we pin the sources() contract over a hand-built stage dir."""
    files = {"Game/Content/A.uasset": b"HEADER",
             "Game/Content/A.uexp": b"EXPSTREAM"}
    d = tmp_path / "stage"
    (d / "files" / "Game" / "Content").mkdir(parents=True)
    for n, b in files.items():
        (d / "files" / n).write_bytes(b)
    (d / "stage.json").write_text(json.dumps({
        "version": 1, "game_root_name": "Game", "aes_key_hex": None,
        "zen_mode": False, "script_objects": False, "containers": [],
        "pak": {}, "io": {}, "staged_files": sorted(files),
        "ufonts": [], "demosaic_assets": []}), encoding="utf-8")
    src = stage_mod.sources(str(d))
    assert payload.list_files(src) == sorted(files)
    assert payload.read_file(src, "Game/Content/A.uexp") == b"EXPSTREAM"
    assert src.get("_zen_flag") is False


def test_stage_sources_zen_flag_and_script_objects(tmp_path):
    d = tmp_path / "stage"
    (d / "files").mkdir(parents=True)
    (d / "stage.json").write_text(json.dumps({
        "version": 1, "game_root_name": "ST", "aes_key_hex": None,
        "zen_mode": True, "script_objects": False, "containers": [],
        "pak": {}, "io": {}, "staged_files": [], "ufonts": [],
        "demosaic_assets": []}), encoding="utf-8")
    src = stage_mod.sources(str(d))
    assert src.get("_zen_flag") is True
    assert src.get("_script_objects_pre") is None


def test_check_fingerprint_unchanged_and_changed(tmp_path):
    container = tmp_path / "Game.pak"
    container.write_bytes(b"payload")
    model = {"containers": [
        {"path": str(container).replace(os.sep, "/"), "kind": "pak",
         "size": container.stat().st_size,
         "sha1": stage_mod._sha1_file(str(container))}]}
    # same file → unchanged
    assert stage_mod.check_fingerprint(str(tmp_path), model) == []
    # changed content (same size) → sha1 flag
    container.write_bytes(b"pyeload")
    changed = stage_mod.check_fingerprint(str(tmp_path), model)
    assert len(changed) == 1 and "(sha1)" in changed[0]
    # missing → listed
    os.remove(str(container))
    changed = stage_mod.check_fingerprint(str(tmp_path), model)
    assert len(changed) == 1 and "(missing)" in changed[0]


# --------------------------------------------------------------------------
# real-game pins (run only when the fixture key + game tree exist — the
# same pattern as test_ftext_ascii.py)

_HERE = os.path.dirname(os.path.abspath(__file__))
_KEY_FIXTURE = os.path.join(_HERE, "fixtures",
                            "sample_b_index_facts.json")
EF_ROOT = os.path.join(_GAMES_ROOT, "SampleGameB")


def _ef_available():
    return os.path.isdir(EF_ROOT) and os.path.isfile(_KEY_FIXTURE)


@pytest.mark.skipif(not _ef_available(), reason="EF game + key fixture not present")
def test_ef_capture_roundtrip(tmp_path):
    key = bytes.fromhex(json.load(open(_KEY_FIXTURE, encoding="utf-8"))["aes_key_hex"])
    d = str(tmp_path / "stage")
    res = stage_mod.capture_stage(EF_ROOT, aes_key=key, stage_dir=d)
    # census pin: Phase 7's numbers must survive the capture pass
    assert res["stats"]["text_entries"] == 10645
    assert res["stats"]["assets_scanned"] > 7000
    assert res["stats"]["errors"] == []
    model = stage_mod.load_model(d)
    assert model["zen_mode"] is False
    assert model["aes_key_hex"] == key.hex()
    assert len(model["staged_files"]) == 1091          # 541 assets x2 + ufonts/candidates
    assert len(model["ufonts"]) == 8
    assert len(model["demosaic_assets"]) == 2          # wall entry never resolves
    # fingerprint: unchanged game → []
    assert stage_mod.check_fingerprint(EF_ROOT, model) == []
    # sources: WG_Title reads back and re-scans to the known 7 rows
    from backend.ut import uasset, textscan
    src = stage_mod.sources(d)
    name = "SampleGameB/Content/SampleGameB/Widget/Title/WG_Title.uexp"
    ua = payload.read_file(src, name[:-5] + ".uasset")
    ux = payload.read_file(src, name)
    pkg = uasset.parse_legacy(ua, ux, name)
    entries, _w = textscan.scan_text(pkg)
    assert {e.source for e in entries} >= {"Option", "Credit", "Game End"}
