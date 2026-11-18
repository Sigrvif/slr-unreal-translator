"""Task 6: scene graph + flat text table (SLRUnity-mirrored rows).

Scene-graph model: game -> asset (class, kinds) -> export -> property
path, text entries attached as leaves; every entry reachable from
exactly one asset node (TextEntry.asset is the single grouping key).

text_table rows MIRROR SLRUnity's translator table — strings.csv as
written by SLRUnity's backend/ut/discovery.py::export_report, whose
columns are exactly:

    japanese, english, context, field, file, path_id, hits

- japanese — the source string (the column to translate; identical role)
- english — "" (blank for the translator; identical role)
- context — scene-graph node ref "<asset>/<export_index>/<field>"
  (SLRUnity: the owning GameObject hierarchy path, e.g. Canvas/Text)
- field — property path joined with "/" (SLRUnity: human-readable
  field path; both games cook tagless so real paths are the honest
  ["<frame>", N] fallback)
- file — payload file name (SLRUnity: game-relative asset file)
- path_id — the export index, the per-package object identifier
  (Unity path_id's direct analog; unique within the file)
- hits — always 1: rows are per-entry, not per-unique-string
  (SLRUnity dedups strings.csv to unique strings; Phase 3 pins
  rows == entries so every patchable offset keeps its own row)

plus ONE SLRUnreal extension: ``entry_index`` — the back-ref into
scan["entries"] (the entry carries offset, the Phase 4 patch address) —
and a second: ``tag``, the SLR risk tag of ZenName rows ("" elsewhere).

Real-game invariants (st_scan/ef_scan are session-scoped in conftest.py,
so each game is scanned exactly once per run and shared across modules):
  - ST: 617 assets with text (the ZenName collector adds every JP-named
    package's name-batch rows), 5,157 rows (992 FText-Base + 91
    FText-Ascii + 9 BcString + 4,065 ZenName = 1,098 yellow + 2,967
    red after the identifier rules (asset-file stems; numbered variant
    tails like スカート_001_L; names shared across 10+ packages' batches);
    DT_学校会話 node = DataTable, 475 leaves (473 frames + its two
    self-referential JP names, both red).
  - EF: 541 assets with text since Phase 7, 10,645 rows (6,359
    FText-Base + 2,796 FText-Ascii + 898 FString + 592 BcString);
    DT_EroEventTalkTable node = DataTable, 2,853 leaves (1,143
    FText-Base + 1,710 FText-Ascii — zero FString entries there).
  - Every entry index appears as exactly one graph leaf; table
    entry_index set == range(len(entries)).
  - Grouping: assets_by_class / entries_by_class contain DataTable and
    the UMG widget family — TextBlock (both games) and WG_* widget-BP
    generated classes (EF); no real class contains "widget".
  - EF class_name=None entries (2,826 — in-package class refs,
    unresolvable from imports) group under "<unknown>".
"""
import json

import pytest

from backend.ut import scenegraph, textscan

# SLRUnity strings.csv columns + the SLRUnreal back-refs (see docstring)
TABLE_FIELDS = ["japanese", "english", "context", "field", "file",
                "path_id", "hits", "entry_index", "tag"]

ST_TOTAL = 992 + 91 + 9 + 4065         # + Ascii/BcString + ZenName rows
ST_ASSETS = 617
ST_GOLDEN = "sample_a/Content/common/データ/DT_学校会話.uasset"
ST_GOLDEN_LEAVES = 475                 # 473 frames + 2 red self-names
EF_TOTAL = 6359 + 898 + 583 + 9 + 2796 # + BcString glyphs + Ascii (P6/P7)
EF_ASSETS = 541
EF_GOLDEN_EXP = ("SampleGameB/Content/SampleGameB/DataTable/EroEvent/"
                 "DT_EroEventTalkTable.uexp")
EF_GOLDEN_LEAVES = 2853                   # 1,143 FText-Base + 1,710 Ascii (P7)


@pytest.fixture(scope="session")
def st_scene(st_scan):
    return scenegraph.build_scenegraph(st_scan)


@pytest.fixture(scope="session")
def ef_scene(ef_scan):
    return scenegraph.build_scenegraph(ef_scan)


def _leaves(graph):
    """(asset_node, export_node, property_node, leaf) per graph leaf."""
    for asset in graph["assets"].values():
        for ex in asset["exports"].values():
            for prop in ex["properties"].values():
                for leaf in prop["leaves"]:
                    yield asset, ex, prop, leaf


# --------------------------------------------------------------------------
# 1. coverage + exactly-once reachability + row-count pins

def test_graph_covers_all_text_assets(st_scene, ef_scene):
    for scene, total, n_assets in ((st_scene, ST_TOTAL, ST_ASSETS),
                                   (ef_scene, EF_TOTAL, EF_ASSETS)):
        scan_entries = scene["stats"]["text_entries"]
        assert scan_entries == total
        assert scene["stats"]["table_rows"] == total
        assert scene["stats"]["assets_with_text"] == n_assets
        graph = scene["graph"]
        # every asset carrying an entry has exactly one node
        leaf_assets = {a["asset"] for a, *_ in _leaves(graph)}
        assert leaf_assets == set(graph["assets"])
        assert len(graph["assets"]) == n_assets
        # every entry reachable from exactly ONE asset node, once
        seen = [leaf["entry_index"] for *_rest, leaf in _leaves(graph)]
        assert len(seen) == total
        assert set(seen) == set(range(total))          # no orphans, no dupes
        assert len(scene["text_table"]) == total


# --------------------------------------------------------------------------
# 2. golden asset nodes

def test_golden_asset_nodes(st_scene, ef_scene):
    st_node = st_scene["graph"]["assets"][ST_GOLDEN]
    assert st_node["class_name"] == "DataTable"
    st_leaves = [leaf for a, ex, prop, leaf in _leaves(st_scene["graph"])
                 if a is st_node]
    assert len(st_leaves) == ST_GOLDEN_LEAVES

    ef_node = ef_scene["graph"]["assets"][EF_GOLDEN_EXP]
    assert ef_node["class_name"] == "DataTable"
    ef_leaves = [leaf for a, ex, prop, leaf in _leaves(ef_scene["graph"])
                 if a is ef_node]
    assert len(ef_leaves) == EF_GOLDEN_LEAVES


# --------------------------------------------------------------------------
# 3. table rows mirror SLRUnity's strings.csv fields

def test_table_rows_carry_slrunity_fields(st_scene, ef_scene, st_scan,
                                          ef_scan):
    for scene, scan in ((st_scene, st_scan), (ef_scene, ef_scan)):
        rows = scene["text_table"]
        entries = scan["entries"]
        assert len(rows) == len(entries)
        assert sorted(r["entry_index"] for r in rows) == list(range(len(rows)))
        for row in rows:
            assert list(row) == TABLE_FIELDS        # exact key set + order
            e = entries[row["entry_index"]]
            assert row["japanese"] == e.source
            assert row["english"] == ""
            assert row["file"] == e.asset
            assert row["path_id"] == e.export_index
            assert row["hits"] == 1
            assert row["field"] == "/".join(map(str, e.property_path))
            # context is the scene-graph node ref to this entry's leaf
            assert row["context"] == f"{e.asset}/{e.export_index}/{row['field']}"
            node = scene["graph"]["assets"][e.asset]["exports"][str(e.export_index)]
            assert node["properties"][row["field"]]["property_path"] == e.property_path
            assert row["entry_index"] in {l["entry_index"]
                                          for l in node["properties"][row["field"]]["leaves"]}


# --------------------------------------------------------------------------
# 4. grouping sanity

def test_grouped_by_class_with_datatable_and_widgets(st_scene, ef_scene):
    for scene in (st_scene, ef_scene):
        stats = scene["stats"]
        assert "DataTable" in stats["assets_by_class"]
        assert "DataTable" in stats["entries_by_class"]
        assert sum(stats["assets_by_class"].values()) == stats["assets_with_text"]
        assert sum(stats["entries_by_class"].values()) == stats["text_entries"]
        # UMG widget family: TextBlock (both games) and WG_* widget-BP
        # generated classes (EF) — no real class contains "widget"
        widget_classes = [c for c in stats["entries_by_class"]
                          if c == "TextBlock" or c.startswith("WG_")]
        assert widget_classes, "no UMG widget-family class in the graph"
    assert any(c.startswith("WG_")
               for c in ef_scene["stats"]["entries_by_class"])
    # EF's unresolvable in-package class refs bucket honestly
    assert ef_scene["stats"]["entries_by_class"]["<unknown>"] == 2826


# --------------------------------------------------------------------------
# synthetic, game-independent pins

def _entry(asset, export_index, cls, path, kind="FText-Base", key="K" * 32,
           source="テスト", offset=0):
    return textscan.TextEntry(
        asset=asset, export_index=export_index, class_name=cls,
        property_path=list(path), kind=kind, namespace="", key=key,
        source=source, offset=offset)


def _fake_scan(entries):
    return {"entries": entries, "stats": {"errors": []}}


def test_synthetic_graph_shape_and_json():
    """Two assets, shared + distinct exports/paths; the graph nests
    game->asset->export->property->leaf, every entry is one leaf, the
    whole result is JSON-serializable."""
    entries = [
        _entry("A.uexp", 0, "DataTable", ["<frame>", "10"], source="一"),
        _entry("A.uexp", 0, "DataTable", ["<frame>", "10"], source="二",
               kind="FString", key=""),
        _entry("A.uexp", 2, "WidgetBlueprint", ["Dialog", "0"], source="三"),
        _entry("B.uexp", 1, "DataTable", ["<fstring>", "0"], source="四"),
    ]
    scene = scenegraph.build_scenegraph(_fake_scan(entries))
    graph = scene["graph"]
    assert graph["level"] == "game"
    assert set(graph["assets"]) == {"A.uexp", "B.uexp"}
    a = graph["assets"]["A.uexp"]
    assert a["class_name"] == "DataTable"        # 2 of 3 entries dominate
    assert a["classes"] == {"DataTable": 2, "WidgetBlueprint": 1}
    assert a["kinds"] == {"FText-Base": 2, "FString": 1}
    assert a["entry_count"] == 3
    assert set(a["exports"]) == {"0", "2"}
    assert a["exports"]["0"]["class_name"] == "DataTable"
    assert a["exports"]["2"]["class_name"] == "WidgetBlueprint"
    prop = a["exports"]["0"]["properties"]["<frame>/10"]
    assert prop["property_path"] == ["<frame>", "10"]
    assert [l["entry_index"] for l in prop["leaves"]] == [0, 1]
    assert prop["leaves"][0] == {"entry_index": 0, "kind": "FText-Base",
                                 "namespace": "", "key": "K" * 32,
                                 "source": "一"}
    b = graph["assets"]["B.uexp"]
    assert b["class_name"] == "DataTable" and b["entry_count"] == 1
    seen = [leaf["entry_index"] for *_r, leaf in _leaves(graph)]
    assert sorted(seen) == [0, 1, 2, 3]
    assert scene["stats"]["nodes_by_level"] == {
        "game": 1, "asset": 2, "export": 3, "property": 3, "leaf": 4}
    json.dumps(scene, ensure_ascii=False)        # must not raise


def test_synthetic_class_none_and_tie_determinism():
    """class_name=None buckets as "<unknown>" (never a crash); an
    entry-count tie prefers a REAL class over the fallback bucket, then
    resolves lexicographically."""
    entries = [
        _entry("A.uexp", 0, None, ["<frame>", "0"], source="一"),
        _entry("A.uexp", 1, "Zeta", ["<frame>", "0"], source="二"),
        _entry("A.uexp", 2, "Alpha", ["<frame>", "0"], source="三"),
    ]
    scene = scenegraph.build_scenegraph(_fake_scan(entries))
    a = scene["graph"]["assets"]["A.uexp"]
    assert a["classes"] == {"<unknown>": 1, "Zeta": 1, "Alpha": 1}
    assert a["class_name"] == "Alpha"    # tie: real class beats <unknown>
    assert a["exports"]["0"]["class_name"] is None
    assert scene["stats"]["assets_by_class"] == {"Alpha": 1}
    assert scene["stats"]["entries_by_class"] == {
        "<unknown>": 1, "Zeta": 1, "Alpha": 1}
    assert scene["text_table"][0]["japanese"] == "一"
