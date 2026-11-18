"""Task 7: slr_cli manifest/result contract v2 (subprocess, both games).

The CLI mirrors SLRUnity's backend/slr_cli.py envelope (read from the
installed source at E:\\Dev\\SLR Unity Translator\\backend\\slr_cli.py):

  - argv: ``--manifest M --result R`` — all paths travel inside the
    manifest file (non-ASCII-safe), never on the command line
  - manifest: ``{"version": 2, "command": "scan"|"extract",
    "game_dir": ...}`` (+ ``"out"`` for extract, ``"aes_key_fixture"``
    for EF's dev-side AES key)
  - success envelope: ``{"ok": true, "version": 2, **payload}``
  - hard-error envelope: ``{"ok": false, "version": 2, "error":
    "TypeName: msg", "traceback": <format_exc>}`` — the result file is
    ALWAYS written and the process still exits 0 (the caller only ever
    parses the result file)
  - contract gates mirror SLRUnity's exact error strings: ``unsupported
    contract version ...`` and ``unknown command ...``

Entry counts cross-checked against Task 5 (scan census) / Task 6
(scene graph): ST 5,157 / EF 10,645 (Phase 7 collect-all +
the ZenName name-batch collector: 992 FText-Base + 91 FText-Ascii +
9 BcString + 4,065 ZenName on ST), 617/466 assets with text, golden
DT_学校会話 475 (473 frames + 2 red self-names) / DT_EroEventTalkTable
1,143. The CLI subprocess re-scans (no cross-process cache) — accepted
cost, ~2.5 min for the four game runs.
"""
import json
import re
import os
import subprocess
import sys

DEV = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(DEV, "backend", "slr_cli.py")
EF_KEY_FIXTURE = os.path.join(DEV, "backend", "tests", "fixtures",
                              "sample_b_index_facts.json")

# scenegraph.TABLE_FIELDS mirror (SLRUnity strings.csv columns + the
# SLRUnreal entry_index/tag back-refs) — pinned by test_scenegraph.py
TABLE_FIELDS = ["japanese", "english", "context", "field", "file",
                "path_id", "hits", "entry_index", "tag"]

# Census-exact expectations (Tasks 1/5/6 + Phases 6/7 + ZenName)
ST = {
    "text_entries": 992 + 91 + 9 + 4065,
    "by_kind": {"FText-Base": 992, "FText-Ascii": 91, "BcString": 9,
                "ZenName": 4065},
    "assets_scanned": 1359,             # 1355 .uasset + 4 .umap
    "files_scanned": 3377,              # payload-layer name count
    "warnings": {"shape-W-sites": 9},
    "assets_with_text": 617,            # Phase 7 + ZenName name batches
    "golden": "sample_a/Content/common/データ/DT_学校会話.uasset",
    "golden_count": 475,                # 473 frames + 2 red self-names
}
EF = {
    "text_entries": 6359 + 898 + 583 + 9 + 2796,   # + BcString glyphs + Ascii
    "by_kind": {"FText-Base": 6359, "FText-Ascii": 2796, "FString": 898,
                "BcString": 592},
    "assets_scanned": 7656,             # .uasset + .umap package names
    "files_scanned": 17829,
    "warnings": {"shape-W-sites": 592},
    "assets_with_text": 541,           # Phase 7 collect-all-visible
    "golden": "SampleGameB/Content/SampleGameB/DataTable/EroEvent/"
              "DT_EroEventTalkTable.uexp",
    "golden_count": 2853,              # 1,143 FText-Base + 1,710 Ascii
}


def run_cli(manifest, tmp_path, name):
    """Write the manifest, spawn the CLI the way the SLRUnity addon does
    (python slr_cli.py --manifest M --result R), return the parsed result
    envelope. The result file must exist in every outcome (envelope
    contract) and the exit code must stay 0."""
    m = os.path.join(tmp_path, f"{name}-manifest.json")
    r = os.path.join(tmp_path, f"{name}-result.json")
    with open(m, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    p = subprocess.run([sys.executable, CLI, "--manifest", m, "--result", r],
                       cwd=DEV, capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr[-2000:]
    with open(r, encoding="utf-8") as f:
        return json.load(f)


def game_manifest(game, game_dir, command, out=None):
    m = {"version": 2, "command": command, "game_dir": game_dir}
    if game == "sample_b":         # dev-side AES key (Phase 4 seam)
        m["aes_key_fixture"] = EF_KEY_FIXTURE
    if out is not None:
        m["out"] = out
    return m


# --------------------------------------------------------------------------
# 1. scan on both real games

def test_scan_both_games(game_paths, tmp_path):
    for game, exp in (("sample_a", ST), ("sample_b", EF)):
        res = run_cli(game_manifest(game, game_paths[game], "scan"),
                      tmp_path, f"scan-{game}")
        assert res["ok"] is True
        assert res["version"] == 2
        stats = res["stats"]
        assert stats["files_scanned"] == exp["files_scanned"]
        assert stats["assets_scanned"] == exp["assets_scanned"]
        assert stats["text_entries"] == exp["text_entries"]
        assert stats["by_kind"] == exp["by_kind"]
        assert stats["warnings"] == exp["warnings"]
        assert stats["errors"] == []    # per-asset soft errors: none on real games
        fp = res["containers"]          # protection.fingerprint passthrough
        assert fp["engine"] == "ue5"
        assert fp["iostore"] == (game == "sample_a")
        assert fp["containers"]
        preview = res["text_preview"]   # first 50 entries, source[:40]
        assert len(preview) == 50
        for row in preview:
            assert set(row) == {"asset", "kind", "key", "source"}
            assert row["kind"] in exp["by_kind"]
            assert len(row["source"]) <= 40


# --------------------------------------------------------------------------
# 2. extract on both real games: output parses back with graph+table intact

def test_extract_round_trip_both_games(game_paths, tmp_path):
    for game, exp in (("sample_a", ST), ("sample_b", EF)):
        out = os.path.join(tmp_path, f"extract-{game}.json")
        res = run_cli(game_manifest(game, game_paths[game], "extract", out),
                      tmp_path, f"extract-{game}")
        assert res["ok"] is True
        assert res["version"] == 2
        assert res["out"] == out
        stats = res["stats"]
        assert stats["text_entries"] == exp["text_entries"]
        assert stats["table_rows"] == exp["text_entries"]
        assert stats["assets_with_text"] == exp["assets_with_text"]
        assert stats["errors"] == []
        with open(out, encoding="utf-8") as f:
            data = json.load(f)         # round-trips as utf-8 JSON
        graph, table = data["graph"], data["text_table"]
        assert graph["level"] == "game"
        assert len(graph["assets"]) == exp["assets_with_text"]
        assert len(table) == exp["text_entries"]
        # rows are one-per-entry in entry order (Phase 3 rows == entries)
        assert [row["entry_index"] for row in table] == list(range(len(table)))
        for row in table[:200]:
            assert list(row) == TABLE_FIELDS
            assert row["japanese"]
            assert row["english"] == ""
            assert row["hits"] == 1
        golden = graph["assets"][exp["golden"]]
        assert golden["class_name"] == "DataTable"
        assert golden["entry_count"] == exp["golden_count"]
        # atomic write leaves no temp files behind
        assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


# --------------------------------------------------------------------------
# 3. hard-error contract: nonexistent game_dir

def test_hard_error_nonexistent_game_dir(tmp_path):
    res = run_cli({"version": 2, "command": "scan",
                   "game_dir": str(tmp_path / "no-such-game")},
                  tmp_path, "hard-error")
    assert res["ok"] is False
    assert res["version"] == 2
    assert "no-such-game" in res["error"]
    assert res["traceback"].lstrip().startswith("Traceback")
    assert "FileNotFoundError" in res["traceback"]


# --------------------------------------------------------------------------
# 4. contract gates (mirror SLRUnity's exact error strings)

def test_contract_gates(tmp_path):
    res = run_cli({"version": 1, "command": "scan", "game_dir": "."},
                  tmp_path, "bad-version")
    assert res["ok"] is False
    assert "unsupported contract version" in res["error"]

    res = run_cli({"version": 2, "command": "explode", "game_dir": "."},
                  tmp_path, "bad-command")
    assert res["ok"] is False
    assert "unknown command" in res["error"]


def test_scan_without_key_autodiscovers_sample_b(game_paths):
    """Product path: encrypted pak + no key in the manifest -> offline exe
    discovery (sub-second on EF) -> cached to backend/keys.json -> scan
    proceeds; the envelope carries the full text census."""
    if "sample_b" not in game_paths:
        import pytest
        pytest.skip("game absent")
    keys_path = os.path.join(DEV, "backend", "keys.json")
    if os.path.isfile(keys_path):
        os.remove(keys_path)                      # force the discovery path
    manifest = os.path.join(DEV, "backend", "tests", "fixtures",
                            "_slr_discover_manifest.json")
    result = os.path.join(DEV, "backend", "tests", "fixtures",
                          "_slr_discover_result.json")
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump({"version": 2, "command": "scan",
                   "game_dir": game_paths["sample_b"]}, f)
    if os.path.isfile(result):
        os.remove(result)
    r = subprocess.run(
        [sys.executable, "slr_cli.py", "--manifest", manifest, "--result", result],
        cwd=os.path.join(DEV, "backend"), capture_output=True, text=True,
        timeout=600, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0, r.stderr[-2000:]
    with open(result, encoding="utf-8") as f:
        envelope = json.load(f)
    assert envelope["ok"] is True, envelope.get("error")
    assert envelope["stats"]["text_entries"] == 10645
    with open(keys_path, encoding="utf-8") as f:
        cached = json.load(f)
    assert any(re.fullmatch(r"[0-9A-F]{64}", v["key_hex"].upper())
               for v in cached.values())
    assert '"type": "progress"' in r.stdout or '"type":"progress"' in r.stdout
