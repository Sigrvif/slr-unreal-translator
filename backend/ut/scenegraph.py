"""Scene graph + flat text table over a textscan scan (Phase 3, Task 6).

:func:`build_scenegraph` groups a scan's TextEntries into a JSON-ready
scene graph

    game -> asset (class, kinds) -> export -> property path -> leaves

Every entry is reachable from exactly one asset node — ``TextEntry.asset``
is the single grouping key — and appears as exactly one leaf. Asset node
``class_name`` is the dominant class among the asset's entries (most
entries; ties prefer a real class over the ``"<unknown>"`` fallback,
then resolve lexicographically for determinism); the full
per-class breakdown rides along in ``classes``. ``class_name=None``
entries (EF's in-package class refs, unresolvable from imports) group
under the honest ``"<unknown>"`` bucket.

The flat ``text_table`` MIRRORS SLRUnity's translator table — strings.csv
as written by SLRUnity's ``backend/ut/discovery.py::export_report``,
whose columns are exactly ``japanese, english, context, field, file,
path_id, hits`` (verified against the installed source at
``<slr-unity backend>``):

- ``japanese`` — the source string (the column to translate; identical
  role in SLRUnity)
- ``english`` — "" (blank for the translator; identical role)
- ``context`` — scene-graph node ref ``<asset>/<export_index>/<field>``
  (SLRUnity: the owning GameObject hierarchy path, e.g. Canvas/Text)
- ``field`` — property path joined with "/" (SLRUnity: the
  human-readable field path; both games cook tagless so real paths are
  the honest ``["<frame>", N]`` fallback from textscan)
- ``file`` — payload file name (SLRUnity: the game-relative asset file)
- ``path_id`` — the export index, the per-package object identifier
  (Unity ``path_id``'s direct analog: unique within one file)
- ``hits`` — always 1: rows are per-entry, not per-unique-string
  (SLRUnity dedups strings.csv to unique strings; Phase 3 pins
  rows == entries so every patchable offset keeps its own row)

plus ONE SLRUnreal extension: ``entry_index`` — back-ref into
``scan["entries"]`` (the entry carries ``offset``, the Phase 4 patch
address). Scan-only — nothing writes, games stay read-only.

Second extension: ``tag`` — the SLR risk tag for ZenName rows (the zen
name batch mixes display labels with internal identifiers; entries carry
"yellow"/"red" from the collector's pattern rules so the client can tag
rows instead of dropping them). "" on every other row.
"""
from __future__ import annotations

__all__ = ["TABLE_FIELDS", "build_scenegraph"]

# SLRUnity strings.csv columns + the SLRUnreal entry_index back-ref +
# the SLR risk tag (ZenName rows only; "" elsewhere)
TABLE_FIELDS = ("japanese", "english", "context", "field", "file",
                "path_id", "hits", "entry_index", "tag")

_UNKNOWN = "<unknown>"       # bucket for class_name=None entries


def _field(property_path: list) -> str:
    """Property path as the row/table field string ('/'-joined)."""
    return "/".join(str(p) for p in property_path)


def build_scenegraph(scan: dict) -> dict:
    """Build the scene graph + text table for one :func:`textscan.scan_game`
    result.

    Returns ``{"graph": {"level": "game", "assets": {name: node}},
    "text_table": [rows...], "stats": {...}}``. Node shapes:

    - asset: ``{asset, class_name, classes, kinds, entry_count, exports}``
    - export: ``{export_index, class_name, properties}``
    - property: ``{property_path, leaves}``
    - leaf: ``{entry_index, kind, namespace, key, source}``

    Rows carry exactly :data:`TABLE_FIELDS` keys, one per entry, in
    entry order. All structures are JSON-serializable.
    """
    entries = list(scan.get("entries") or [])

    assets: dict = {}
    for idx, e in enumerate(entries):
        node = assets.get(e.asset)
        if node is None:
            node = assets[e.asset] = {
                "asset": e.asset, "class_name": None, "classes": {},
                "kinds": {}, "entry_count": 0, "exports": {},
            }
        node["entry_count"] += 1
        node["kinds"][e.kind] = node["kinds"].get(e.kind, 0) + 1
        cls = e.class_name or _UNKNOWN
        node["classes"][cls] = node["classes"].get(cls, 0) + 1
        ex_key = str(e.export_index)
        ex = node["exports"].get(ex_key)
        if ex is None:
            ex = node["exports"][ex_key] = {
                "export_index": e.export_index,
                "class_name": e.class_name, "properties": {},
            }
        path_key = _field(e.property_path)
        prop = ex["properties"].get(path_key)
        if prop is None:
            prop = ex["properties"][path_key] = {
                "property_path": list(e.property_path), "leaves": [],
            }
        prop["leaves"].append({
            "entry_index": idx, "kind": e.kind, "namespace": e.namespace,
            "key": e.key, "source": e.source,
        })

    # deterministic primary class: most entries; ties prefer a real
    # class over the "<unknown>" fallback, then lexicographic smallest
    for node in assets.values():
        node["class_name"] = min(
            sorted(node["classes"]),
            key=lambda c: (-node["classes"][c], c == _UNKNOWN, c))

    text_table = []
    for idx, e in enumerate(entries):
        field = _field(e.property_path)
        text_table.append({
            "japanese": e.source,
            "english": "",
            "context": f"{e.asset}/{e.export_index}/{field}",
            "field": field,
            "file": e.asset,
            "path_id": e.export_index,
            "hits": 1,
            "entry_index": idx,
            "tag": (e.meta or {}).get("risk", "")
            if getattr(e, "meta", None) else "",
        })

    entries_by_class: dict = {}
    entries_by_kind: dict = {}
    assets_by_class: dict = {}
    n_exports = 0
    n_props = 0
    for node in assets.values():
        primary = node["class_name"]
        assets_by_class[primary] = assets_by_class.get(primary, 0) + 1
        for cls, n in node["classes"].items():
            entries_by_class[cls] = entries_by_class.get(cls, 0) + n
        for kind, n in node["kinds"].items():
            entries_by_kind[kind] = entries_by_kind.get(kind, 0) + n
        n_exports += len(node["exports"])
        n_props += sum(len(ex["properties"])
                       for ex in node["exports"].values())

    return {
        "graph": {"level": "game", "assets": assets},
        "text_table": text_table,
        "stats": {
            "text_entries": len(entries),
            "table_rows": len(text_table),
            "assets_with_text": len(assets),
            "nodes_by_level": {
                "game": 1, "asset": len(assets), "export": n_exports,
                "property": n_props, "leaf": len(entries),
            },
            "entries_by_class": entries_by_class,
            "entries_by_kind": entries_by_kind,
            "assets_by_class": assets_by_class,
            "scan_errors": len((scan.get("stats") or {}).get("errors") or []),
        },
    }
