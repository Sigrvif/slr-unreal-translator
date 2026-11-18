"""Task 3: export orchestration end-to-end — exporttext.export_translations
(+ the slr_cli ``export`` command).

Pins per the brief:

- ST (zen): 12 varied-length translations across 3 assets (DT_学校会話 +
  a TextBlock widget asset + one more DataTable) → rebuilt utoc/ucas →
  full verify chain (export's own verify + an INDEPENDENT re-read of the
  golden through the rebuilt pair); the untouched sibling pak still
  reads fine.
- EF (legacy): 12 across 3 assets (DT_EroEventTalkTable + one
  FString-carrying asset + one more) → rebuilt pak via the AES path →
  full verify chain; 5 unmodified entries sampled byte-identical inside
  the rebuilt container.
- CLI: subprocess on both games with a table JSON in temp → ok:true,
  stats match, out files exist; blank-english rows skipped (counts).
- Edge: an asset with translations but zero valid entries → named error,
  nothing written.
"""
import json
import os
import subprocess
import sys

import pytest

from backend.ut import exporttext, iostore, pak, payload, textscan, uasset
from backend.ut.manifest import normalize_pak_name

DEV = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(DEV, "backend", "slr_cli.py")
EF_KEY_FIXTURE = os.path.join(DEV, "backend", "tests", "fixtures",
                              "sample_b_index_facts.json")

ST_PAKS = "sample_a/Content/Paks"        # game-relative (zip convention)
EF_PAKS = "SampleGameB/Content/Paks"

# golden + variety assets (census: see test_slr_cli / test_patchtext)
ST_ROOT_ASSET = "sample_a/Content/common/データ/DT_学校会話.uasset"       # 473
ST_TITLE_ASSET = "sample_a/Content/common/WBP_title.uasset"               # 6 TextBlock
ST_OP_ASSET = "sample_a/Content/common/データ/DT_オープニング.uasset"     # 48
EF_TALK_ASSET = ("SampleGameB/Content/SampleGameB/DataTable/EroEvent/"
                 "DT_EroEventTalkTable.uexp")                                  # 1143
EF_BOOK_ASSET = ("SampleGameB/Content/SampleGameB/DataTable/Item/"
                 "EF_Item02_Book.uexp")        # 23 FText-Base + 3 FString
EF_WALLET_ASSET = ("SampleGameB/Content/SampleGameB/DataTable/Item/"
                   "EF_Item10_Wallet.uexp")                                    # 3


def _ef_key() -> bytes:
    with open(EF_KEY_FIXTURE, encoding="utf-8") as f:
        return bytes.fromhex(json.load(f)["aes_key_hex"])


def _sibling(name: str) -> str:
    """The .uasset (or .umap) half of an EF .uexp asset name."""
    return name[:-len(".uexp")] + ".uasset"


def _rel_out(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


# --------------------------------------------------------------------------
# load_translations adapter (fast, synthetic)

def test_load_translations_adapter_shapes(tmp_path):
    """Rows list / extract-file dict / JSON path / ready asset dict all
    normalize to {asset: {per-asset entry index: english|None}}; blank
    english ("" / null / missing) becomes None = skipped."""
    rows = [
        {"japanese": "一", "english": "one", "context": "A/0/x", "field": "x",
         "file": "A.uexp", "path_id": 0, "hits": 1, "entry_index": 0},
        {"japanese": "二", "english": "", "context": "A/0/x", "field": "x",
         "file": "A.uexp", "path_id": 0, "hits": 1, "entry_index": 1},
        {"japanese": "三", "english": None, "context": "B/0/x", "field": "x",
         "file": "B.uexp", "path_id": 0, "hits": 1, "entry_index": 2},
        {"japanese": "四", "english": "four", "context": "B/0/x", "field": "x",
         "file": "B.uexp", "path_id": 0, "hits": 1, "entry_index": 3},
    ]
    want = {"A.uexp": {0: "one", 1: None}, "B.uexp": {0: None, 1: "four"}}
    assert exporttext.load_translations(rows) == want
    assert exporttext.load_translations({"text_table": rows}) == want
    p = tmp_path / "table.json"
    p.write_text(json.dumps({"text_table": rows}, ensure_ascii=False),
                 encoding="utf-8")
    assert exporttext.load_translations(str(p)) == want
    # ready asset map: JSON round-trip gives string keys + nulls — coerced
    assert exporttext.load_translations({"A.uexp": {"0": "one", "1": None}}) \
        == {"A.uexp": {0: "one", 1: None}}


# --------------------------------------------------------------------------
# 1. ST end-to-end (zen → iostore chunk patches)

def _st_translations():
    """12 non-blank rows across 3 assets + 1 blank; lengths from 1 char to
    60+ chars (shrink AND growth vs the JP originals), one astral char."""
    return {
        ST_ROOT_ASSET: {
            0: "TEST-000",
            40: "A MUCH LONGER TEST STRING FOR ENTRY FORTY (GROWTH)",
            100: None,                       # blank-English = skipped
            236: "MID ROW",
            300: "ENTRY THREE HUNDRED REPLACED WITH A LONG TEST STRING",
            472: "TAIL 🌍 ROW",
        },
        ST_TITLE_ASSET: {0: "TITLE", 1: "SUBTITLE TEST", 3: "ROW THREE GROWN", 5: "X"},
        ST_OP_ASSET: {1: "OP LINE ONE", 10: "OP LINE TEN GROWN CONSIDERABLY LONGER", 30: "OP-30"},
    }


@pytest.mark.slow
def test_st_export_three_assets_full_verify(game_paths, st_scan, tmp_path):
    tr = _st_translations()
    res = exporttext.export_translations(
        game_paths["sample_a"], tr, out_dir=str(tmp_path))
    assert res["verify"] == "ok"
    assert res["rows_patched"] == 12
    assert res["rows_skipped"] == 1
    assert res["assets"] == 3
    assert res["out_dir"] == str(tmp_path)
    # exactly the utoc/ucas pair, mirrored game-relative under out_dir
    assert sorted(_rel_out(p, str(tmp_path)) for p in res["containers"]) == [
        f"{ST_PAKS}/sample_a-Windows.ucas",
        f"{ST_PAKS}/sample_a-Windows.utoc"]
    for p in res["containers"]:
        assert os.path.isfile(p) and os.path.getsize(p) > 0

    # the untouched sibling pak (game tree read-only) still reads fine
    game_pak = os.path.join(game_paths["sample_a"], *ST_PAKS.split("/"),
                            "sample_a-Windows.pak")
    idx = pak.read_index(game_pak)
    assert idx.entries and not idx.encrypted

    # INDEPENDENT verify: re-read the golden through the rebuilt pair with
    # the raw frame detector — all 473 frames, row 0 translated, the blank
    # row 100 byte-identical to the scan's original source
    utoc_out = next(p for p in res["containers"] if p.endswith(".utoc"))
    ucas_out = next(p for p in res["containers"] if p.endswith(".ucas"))
    vsrc = {"paks": [], "io_pairs": [(utoc_out, ucas_out)],
            "aes_key": None, "_files": None}
    data = payload.read_file(vsrc, ST_ROOT_ASSET)
    frames = [f for f in textscan._detect_frames(data) if f[1] is not None]
    assert len(frames) == 473
    assert frames[0][3] == tr[ST_ROOT_ASSET][0]
    assert frames[40][3] == tr[ST_ROOT_ASSET][40]
    orig = [e for e in st_scan["entries"] if e.asset == ST_ROOT_ASSET]
    assert frames[100][3] == orig[100].source          # skipped row untouched
    assert frames[101][3] == orig[101].source
    with open(utoc_out, "rb") as f:                    # rebuilt toc parses
        assert iostore.parse_toc(f.read()).file_map


# --------------------------------------------------------------------------
# 2. EF end-to-end (legacy pair → pak entries, AES path)

def _ef_translations(ef_scan):
    book = [e for e in ef_scan["entries"] if e.asset == EF_BOOK_ASSET]
    ftext = [i for i, e in enumerate(book) if e.kind == "FText-Base"]
    fstr = [i for i, e in enumerate(book) if e.kind == "FString"]
    assert len(ftext) >= 2 and len(fstr) >= 2           # FString geometry arm
    return {
        EF_TALK_ASSET: {
            0: "TALK-0",
            100: "TALK ROW ONE HUNDRED — A CONSIDERABLY LONGER TEST LINE",
            600: None,                       # blank-English = skipped
            500: "TALK-500",
            1000: "TALK-1000",
            1142: "LAST ROW TEST",
        },
        EF_BOOK_ASSET: {
            ftext[0]: "BOOK FTEXT ONE (GROWN LONGER)",
            ftext[1]: "B1",
            fstr[0]: "BOOK ITEM NAME TEST",
            fstr[1]: "BOOK FSTRING ROW TWO — LONGER",
        },
        EF_WALLET_ASSET: {0: "W-0", 1: "WALLET ROW ONE", 2: "W-2"},
    }


@pytest.mark.slow
def test_ef_export_three_assets_full_verify(game_paths, ef_scan, tmp_path):
    key = _ef_key()
    tr = _ef_translations(ef_scan)
    res = exporttext.export_translations(
        game_paths["sample_b"], tr, aes_key=key, out_dir=str(tmp_path))
    assert res["verify"] == "ok"
    assert res["rows_patched"] == 12
    assert res["rows_skipped"] == 1
    assert res["assets"] == 3
    assert [_rel_out(p, str(tmp_path)) for p in res["containers"]] == [
        f"{EF_PAKS}/SampleGameB-Windows.pak"]
    out_pak = res["containers"][0]
    assert os.path.isfile(out_pak) and os.path.getsize(out_pak) > 0

    src_pak = os.path.join(game_paths["sample_b"], *EF_PAKS.split("/"),
                           "SampleGameB-Windows.pak")
    idx_src = pak.read_index(src_pak, aes_key=key)
    idx_out = pak.read_index(out_pak, aes_key=key)      # index decrypts + sha1
    out_map = {e.name: e for e in idx_out.entries}
    staged = {EF_TALK_ASSET, _sibling(EF_TALK_ASSET),
              EF_BOOK_ASSET, _sibling(EF_BOOK_ASSET),
              EF_WALLET_ASSET, _sibling(EF_WALLET_ASSET)}
    # sample 5 unmodified entries: byte-identical inside the rebuilt pak
    picked = 0
    for e in idx_src.entries:
        if normalize_pak_name(idx_src.mount_point, e.name) in staged:
            continue
        assert pak.read_entry_data(out_pak, out_map[e.name], aes_key=key) \
            == pak.read_entry_data(src_pak, e, aes_key=key)
        picked += 1
        if picked == 5:
            break
    assert picked == 5

    # INDEPENDENT verify: one patched pair re-read + re-scanned
    vsrc = {"paks": [out_pak], "io_pairs": [], "aes_key": key, "_files": None}
    ux = payload.read_file(vsrc, EF_WALLET_ASSET)
    ua = payload.read_file(vsrc, _sibling(EF_WALLET_ASSET))
    pkg = uasset.parse_legacy(ua, ux, EF_WALLET_ASSET)
    ventries, _ = textscan.scan_text(pkg)
    # the 3 patched FText-Base rows + Phase 7's collected FText-Ascii rows
    n_ascii = sum(1 for e in ef_scan["entries"]
                  if e.asset == EF_WALLET_ASSET and e.kind == "FText-Ascii")
    assert len(ventries) == 3 + n_ascii
    for i in range(3):
        assert ventries[i].source == tr[EF_WALLET_ASSET][i]


# --------------------------------------------------------------------------
# 3. CLI export (subprocess, both games, table JSON in temp)

def _run_cli(manifest, tmp_path, name):
    m = os.path.join(tmp_path, f"{name}-manifest.json")
    r = os.path.join(tmp_path, f"{name}-result.json")
    with open(m, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    p = subprocess.run([sys.executable, CLI, "--manifest", m, "--result", r],
                       cwd=DEV, capture_output=True, text=True, timeout=900)
    assert p.returncode == 0, p.stderr[-2000:]
    with open(r, encoding="utf-8") as f:
        return json.load(f)


def _prefix_rows(scan, asset, english):
    """Extract-table rows (TABLE_FIELDS mirror) for the asset's FIRST
    len(english) entries — a per-asset prefix in scan order, exactly what
    the order-based adapter consumes."""
    pos = {id(e): i for i, e in enumerate(scan["entries"])}
    entries = [e for e in scan["entries"] if e.asset == asset]
    assert len(entries) >= len(english)
    rows = []
    for e, eng in zip(entries, english):
        rows.append({"japanese": e.source, "english": eng,
                     "context": f"{e.asset}/{e.export_index}/x", "field": "x",
                     "file": e.asset, "path_id": e.export_index, "hits": 1,
                     "entry_index": pos[id(e)]})
    return rows


@pytest.mark.slow
def test_cli_export_both_games(game_paths, st_scan, ef_scan, tmp_path):
    specs = [
        ("st", "sample_a", game_paths["sample_a"], None, st_scan,
         [(ST_ROOT_ASSET, ["R0", "", "R2 GROWN LONGER THAN THE SOURCE", "R3", "R4"]),
          (ST_TITLE_ASSET, ["T0", "T1 LONGER", ""])]),
        ("ef", "sample_b", game_paths["sample_b"], EF_KEY_FIXTURE,
         ef_scan,
         [(EF_TALK_ASSET, ["E0", "E1 — MUCH LONGER TEST LINE FOR GROWTH", "", "E3", "E4"]),
          (EF_WALLET_ASSET, ["W0", "W1 LONGER", ""])]),
    ]
    for tag, _gname, game_dir, key_fx, scan, assets in specs:
        rows = []
        for asset, english in assets:
            rows.extend(_prefix_rows(scan, asset, english))
        table = os.path.join(tmp_path, f"table-{tag}.json")
        with open(table, "w", encoding="utf-8") as f:
            json.dump({"text_table": rows}, f, ensure_ascii=False)
        out = os.path.join(tmp_path, f"out-{tag}")
        manifest = {"version": 2, "command": "export", "game_dir": game_dir,
                    "translations": table, "out": out}
        if key_fx:
            manifest["aes_key_fixture"] = key_fx
        res = _run_cli(manifest, tmp_path, f"export-{tag}")
        assert res["ok"] is True, res.get("error")
        assert res["version"] == 2
        assert res["out"] == out
        stats = res["stats"]
        assert stats["rows_patched"] == 6          # 4 + 2 per game
        assert stats["rows_skipped"] == 2          # one blank per asset
        assert stats["assets"] == 2
        assert stats["verify"] == "ok"
        assert res["containers"] and all(os.path.isfile(p) for p in res["containers"])
        # no temp files leaked by the atomic writers
        assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


# --------------------------------------------------------------------------
# 3b. fix-round-1 IMPORTANT: .umap-hosted EF asset (staging regression)

EF_UMAP_ASSET = ("SampleGameB/Content/SampleGameB/Level/"
                 "Lv_Iromachi_Hotel.uexp")      # 4 FText-Base, .umap-hosted


@pytest.mark.slow
def test_ef_umap_hosted_asset_export(game_paths, ef_scan, tmp_path):
    """The patched header half must stage under the REAL sibling name
    (.umap for Level assets — no .uasset exists), not a synthesized
    .uasset name: pre-fix, scan+patch succeeded and staging then raised
    KeyError 'payload name not found: ...Lv_Iromachi_Hotel.uasset'."""
    key = _ef_key()
    orig = [e for e in ef_scan["entries"] if e.asset == EF_UMAP_ASSET]
    assert len(orig) == 8                     # 4 + 1 Ascii + 3 BcString
    ftext = [e for e in orig if e.kind != "BcString"]
    assert len(ftext) == 5
    tr = {EF_UMAP_ASSET: {0: "HOTEL ROW ZERO — A CONSIDERABLY LONGER LINE",
                          2: "H-2", 3: "HOTEL ROW THREE (GROWN)"}}
    res = exporttext.export_translations(
        game_paths["sample_b"], tr, aes_key=key, out_dir=str(tmp_path))
    assert res["verify"] == "ok"
    assert res["rows_patched"] == 3
    assert res["assets"] == 1
    # INDEPENDENT: BOTH halves read back from the rebuilt pak under their
    # real names; the .umap header half parses; the 3 patched FText rows
    # carry new sources, the untouched frame row + the 3 BcString rows
    # (JP-only emission) re-appear with their originals
    out_pak = res["containers"][0]
    vsrc = {"paks": [out_pak], "io_pairs": [], "aes_key": key, "_files": None}
    umap = EF_UMAP_ASSET[:-len(".uexp")] + ".umap"
    ux = payload.read_file(vsrc, EF_UMAP_ASSET)
    ua = payload.read_file(vsrc, umap)
    pkg = uasset.parse_legacy(ua, ux, EF_UMAP_ASSET)
    ventries, _ = textscan.scan_text(pkg)
    patched_map = tr[EF_UMAP_ASSET]
    # Phase 7: entry 0 is now the ASCII row ("EcstasyMemory"), so its
    # translation flips that frame to a UTF-16 source FString -> re-scans
    # as FText-Base. The patch RESIZES, so re-scan offsets shift —
    # compare (kind, source) multisets with the kind switch applied.
    idx_by_offset = {e.offset: i for i, e in enumerate(orig)}

    def expected_pair(e):
        i = idx_by_offset[e.offset]
        kind, src = e.kind, e.source
        if i in patched_map:
            src = patched_map[i]
            if e.kind == "FText-Ascii"                     and not all(ord(c) < 0x80 for c in src):
                kind = "FText-Base"
        return (kind, src)

    from collections import Counter
    want = Counter(expected_pair(e) for e in orig)
    got = Counter((v.kind, v.source) for v in ventries)
    assert got == want



# --------------------------------------------------------------------------
# 3c. fix-round-1 MINOR-1: pruned NON-prefix table targets the named rows

@pytest.mark.slow
def test_pruned_non_prefix_table_targets_named_rows(game_paths, st_scan,
                                                    tmp_path):
    """Extract-file shape (rows + graph) pruned to NON-prefix rows: the
    per-asset indexes come from entry_index - graph base, so rows #5/#40
    of DT_オープニング target entries 5/40 — order-based derivation would
    have landed them silently on entries 0/1 (and verify would have
    passed, checking only what was asked)."""
    from backend.ut import scenegraph
    scene = scenegraph.build_scenegraph(st_scan)     # the real producer
    rows = scene["text_table"]
    asset_rows = [r for r in rows if r["file"] == ST_OP_ASSET]
    assert len(asset_rows) == 50          # 48 frames + 2 red self-names
    picked = [dict(asset_rows[5], english="OP ROW FIVE (GROWN LONGER)"),
              dict(asset_rows[40], english="OP ROW FORTY")]
    table = {"text_table": picked, "graph": scene["graph"]}
    want = {ST_OP_ASSET: {5: "OP ROW FIVE (GROWN LONGER)", 40: "OP ROW FORTY"}}
    assert exporttext.load_translations(table) == want

    # end-to-end: the export patches exactly entries 5 and 40
    res = exporttext.export_translations(
        game_paths["sample_a"], table, out_dir=str(tmp_path))
    assert res["verify"] == "ok"
    assert res["rows_patched"] == 2
    assert res["assets"] == 1
    utoc_out = next(p for p in res["containers"] if p.endswith(".utoc"))
    ucas_out = next(p for p in res["containers"] if p.endswith(".ucas"))
    vsrc = {"paks": [], "io_pairs": [(utoc_out, ucas_out)],
            "aes_key": None, "_files": None}
    data = payload.read_file(vsrc, ST_OP_ASSET)
    frames = [f for f in textscan._detect_frames(data) if f[1] is not None]
    assert len(frames) == 48
    assert frames[5][3] == "OP ROW FIVE (GROWN LONGER)"
    assert frames[40][3] == "OP ROW FORTY"
    orig = asset_rows
    assert frames[0][3] == orig[0]["japanese"]       # untouched rows stay
    assert frames[6][3] == orig[6]["japanese"]


# --------------------------------------------------------------------------
# 4. edge: translations against a zero-entry asset

def test_export_zero_entries_named_error_nothing_written(
        game_paths, st_src, st_scan, tmp_path):
    scan_assets = {e.asset for e in st_scan["entries"]}
    quiet = next(n for n in payload.list_files(st_src)
                 if n.endswith(".uasset") and n not in scan_assets)
    with pytest.raises(ValueError, match="zero text entries") as exc:
        exporttext.export_translations(
            game_paths["sample_a"], {quiet: {0: "X"}}, out_dir=str(tmp_path))
    assert quiet in str(exc.value)
    assert os.listdir(tmp_path) == []              # nothing written
