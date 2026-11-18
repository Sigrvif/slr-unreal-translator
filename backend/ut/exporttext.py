"""Export orchestration (Phase 4a, Task 3): translations in, VERIFIED
rebuilt containers out — pure composition of the Phase 1-3 layers (no new
format logic; every byte written goes through the Phase 2 writers).

:func:`export_translations` per asset WITH translations (>= 1 non-blank):

1. read the asset's payload fresh (+ the .uasset half for EF pairs) and
   RE-SCAN it (never trust stale table offsets — the Task 1/2 patch
   wrappers re-verify each frame against the bytes anyway, so a stale
   table is a named error, not silent corruption);
2. patch via the Task 2 wrappers — ``zen.patch_zen_package`` (ST zen
   chunks) or ``uasset.patch_legacy_pair`` (EF .uasset+.uexp pairs, both
   halves returned);
3. stage the patched bytes grouped by their ACTUAL container (from the
   payload-layer handler): zen packages become iostore chunk patches
   (file-map name -> bytes), EF pairs become pak ``entries_data`` (BOTH
   halves — the .uasset may be rewritten by the export-map propagation);
4. rebuild each container through :mod:`rewrite` (plan + execute: the
   Task 2 writers, atomic per file, output mirroring the game-relative
   layout under ``out_dir`` — or a temp dir when None);
5. VERIFY the hard way: re-parse every output container, re-extract each
   staged file (byte-exact against what was staged) and re-scan each
   patched asset — every translated row present with its new source,
   every skipped row byte-identical, kinds and FText keys preserved.
   One pinned nuance: a FString row translated OUT of Japanese is
   expected to vanish from the re-scan (textscan's chained-FString
   emission is JP-gated by design — an English string is not an
   extraction target; its bytes were already verified exactly). A
   verify failure raises ValueError carrying the details (the
   "details" arm of the plan contract) — a broken chain is never
   reported as a successful export.

Blank/None english = SKIPPED (Phase 3 final-review contract line): the
row keeps its original bytes and is counted under ``rows_skipped``.
Assets whose rows are ALL blank are neither scanned nor rebuilt (a null
patch is the identity — no container is rewritten for them).

Translation indexes are PER-ASSET (the position of the row within that
asset's entry list — exactly what :func:`patchtext.patch_payload`
validates). :func:`load_translations` is the small adapter for the
extracted-table JSON shapes: with the extract graph present it derives
those per-asset indexes from each row's ``entry_index`` minus the
asset's graph base (PRUNE-PROOF — any row subset targets the rows it
names); without the graph it falls back to row order (the extract
table is one-row-per-entry in scan order, so order-based indexes are
correct only for per-asset PREFIXES of that order).

Games are read-only: every write lands under ``out_dir`` only.
"""
from __future__ import annotations

import json
import os
import struct
import tempfile

from . import patchtext, payload, rewrite, stage as stage_mod, textscan, uasset, zen

__all__ = ["export_translations", "export_patch", "load_translations"]


def _zen(src: payload.PayloadSources) -> bool:
    """Zen (single-file package) mode: real containers key off io_pairs
    presence; a STAGE handle (Phase 8a) or a patch-verify handle (Phase
    8c) carries the explicit ``_zen_flag`` instead — one helper so every
    mode switch agrees."""
    if "_zen_flag" in src:
        return bool(src["_zen_flag"])
    return bool(src.get("io_pairs"))


def _stage_script_objects(src: payload.PayloadSources):
    """The ScriptObjects store for one sources handle: the real-container
    scan (io pairs) or the stage handle's pre-parsed store (Phase 8a —
    zen class names must survive the stage round trip or BcString rows
    vanish from the re-scan)."""
    if "_zen_flag" in src:
        return src.get("_script_objects_pre")
    if _zen(src):
        return textscan._script_objects(src)
    return None


def _scan_domain(s: str) -> str:
    """Text in the scanner's unit-string (WTF-16) domain: one ``chr`` per
    UTF-16 code unit, so astral characters appear as their surrogate
    pair — exactly how textscan decodes UTF-16 payloads. Unit-strings
    (scan sources) round-trip identically; translations (Python domain)
    get normalized so verify compares like with like."""
    raw = s.encode("utf-16-le", "surrogatepass")
    return "".join(map(chr, struct.unpack(f"<{len(raw) // 2}H", raw)))


def _expected_after_patch(entries: list, trans: dict) -> "list[tuple]":
    """The (kind, key, source) rows a re-scan of the PATCHED asset must
    produce, in order: every row carries its translation (WTF-16 domain)
    or its original source — except a translated FString, BcString or
    ZenName whose translation no longer reads as Japanese: all three are
    collected JP-gated (an English replacement is not an extraction
    target), so the row is EXPECTED to vanish from the re-scan (its
    bytes are verified separately, byte-exactly)."""
    out = []
    for i, e in enumerate(entries):
        t = trans.get(i)
        if t not in (None, ""):
            if e.kind in ("FString", "BcString", "ZenName") \
                    and not (len(t) >= 2 and textscan._has_jp(t)):
                continue
            kind = e.kind
            if e.kind == "FText-Ascii":
                # the write keeps ASCII bodies ANSI but flips non-ASCII
                # translations to a UTF-16 source FString — which the
                # re-scan reports as a plain FText-Base frame
                try:
                    t.encode("ascii")
                except UnicodeEncodeError:
                    kind = "FText-Base"
            out.append((kind, e.key, _scan_domain(t)))
        else:
            out.append((e.kind, e.key, e.source))
    return out


# --------------------------------------------------------------------------
# translations adapter

def _asset_bases(graph: dict) -> "dict[str, int] | None":
    """{asset: global entry_index of its FIRST scanned entry} from an
    extract scene graph — leaves carry the global scan index and a full
    table's per-asset rows are contiguous, so the smallest leaf index of
    an asset is its base. Empty dict (falsy) when the graph covers no
    assets."""
    bases: dict = {}
    for asset, node in (graph or {}).get("assets", {}).items():
        idxs = [leaf["entry_index"]
                for ex in node.get("exports", {}).values()
                for prop in ex.get("properties", {}).values()
                for leaf in prop.get("leaves", ())]
        if idxs:
            bases[asset] = min(idxs)
    return bases


def _rows_to_translations(rows: list, graph: "dict | None") -> dict:
    """{asset: {per-asset entry index: english|None}} from table rows.

    Index derivation (fix round 1): when every row carries an integer
    ``entry_index`` AND the extract ``graph`` is available, the per-asset
    index is ``entry_index - base(asset)`` with base = the asset's first
    global scan index from the graph — PRUNE-PROOF: any subset of rows
    targets exactly the rows its entry_index fields name (validated:
    per-asset entry_index strictly ascending, never below the base; the
    upper bound is patch_payload's named 0..N-1 range check). Rows
    without the field, or tables without the graph (no base authority),
    fall back to ROW ORDER — the row's position among its file's rows —
    which is only correct for per-asset PREFIXES of scan order.
    """
    bases = _asset_bases(graph) if graph is not None else None
    use_field = bool(bases) and all(
        isinstance(r, dict) and isinstance(r.get("entry_index"), int)
        for r in rows)
    out: dict = {}
    pos: dict = {}
    last_field: dict = {}
    for row in rows:
        asset = row["file"]
        if use_field:
            base = bases.get(asset)
            if base is None:
                raise ValueError(
                    f"table row references asset {asset!r} which the "
                    f"extract graph does not cover")
            raw = row["entry_index"]
            if raw <= last_field.get(asset, base - 1):
                raise ValueError(
                    f"table rows for {asset!r} are not in ascending "
                    f"entry_index order ({raw} after "
                    f"{last_field.get(asset)})")
            last_field[asset] = raw
            i = raw - base
            if i < 0:
                raise ValueError(
                    f"table row for {asset!r} has entry_index {raw} "
                    f"below the graph base {base}")
        else:
            i = pos.get(asset, 0)
            pos[asset] = i + 1
        out.setdefault(asset, {})[i] = row.get("english") or None
    return out


def load_translations(value) -> dict:
    """Normalize a ``translations`` argument to ``{asset: {entry_index:
    english | None}}`` (per-asset indexes; blank english -> None = skipped).

    Accepted shapes:

    - a path (str/bytes/os.PathLike) to a UTF-8 JSON file — the extract
      output, a bare rows list, or a ready asset->index->text mapping;
    - the EXTRACT OUTPUT dict (``{"text_table": rows, "graph": ...}``):
      with the graph, per-asset indexes come from each row's
      ``entry_index`` minus the asset's graph base — prune-proof (any
      row subset targets the rows it names); without a ``graph`` key the
      rows fall back to order-based derivation (prefix-only);
    - a rows list (scenegraph TABLE_FIELDS rows; ``file`` + ``english``
      are consumed; with no graph authority the per-asset index is the
      row's POSITION among its file's rows in table order);
    - a ready ``{asset: {index: english|None}}`` dict — JSON round-trips
      give string keys and ``null`` blanks; both are coerced here.
    """
    if isinstance(value, (str, bytes, os.PathLike)):
        with open(os.fspath(value), encoding="utf-8") as f:
            return load_translations(json.load(f))
    if isinstance(value, dict) and "text_table" in value:
        return _rows_to_translations(value["text_table"],
                                     value.get("graph"))
    if isinstance(value, list):
        return _rows_to_translations(value, None)
    if isinstance(value, dict):
        return {asset: {int(k): (None if v in (None, "") else v)
                        for k, v in (mapping or {}).items()}
                for asset, mapping in value.items()}
    raise TypeError(
        f"cannot read translations from {type(value).__name__} "
        f"(want a JSON path, extract output, rows list, or "
        f"{{asset: {{index: english}}}} dict)")


# --------------------------------------------------------------------------
# per-asset read + fresh scan (mirrors scan_game's inner loop for ONE
# package; the payload layer is read through, never around)

def _header_sibling(src: payload.PayloadSources, name: str) -> str:
    """The ACTUAL header half of a legacy .uexp asset: the exact-stem
    .uasset, or the .umap for .umap-hosted pairs (EF ships 101 .umap
    files) — resolved by payload membership per the EF pairing pin. The
    read path AND the staging loop must resolve the SAME name, or the
    patched header half lands under a payload name that does not exist
    (fix-round-1 bug: staging keyed it unconditionally as .uasset)."""
    files = payload.list_files(src)
    stem = name[:-len(".uexp")]
    sibling = stem + ".uasset"
    if sibling not in files:
        sibling = stem + ".umap"
    return sibling


def _scan_asset(src: payload.PayloadSources, name: str,
                script_objects, stems: "set[str] | None" = None) -> "tuple[tuple, list]":
    """(raw payload halves, fresh TextEntry list) for one asset.

    Zen mode reads the package chunk itself; legacy mode reads the .uexp
    (the asset name, per the scan contract) plus its header sibling
    (.uasset, or .umap for .umap-hosted pairs — :func:`_header_sibling`).
    """
    if _zen(src):
        data = payload.read_file(src, name)
        pkg = zen.parse_zen(data, name, script_objects=script_objects)
        entries, _warnings = textscan.scan_text(pkg, stems)
        return (data,), entries
    uexp = payload.read_file(src, name)
    ua = payload.read_file(src, _header_sibling(src, name))
    pkg = uasset.parse_legacy(ua, uexp, name)
    entries, _warnings = textscan.scan_text(pkg)
    return (ua, uexp), entries


def _patch_asset(src, name: str, raws: tuple, entries: list,
                 translations: dict) -> tuple:
    """Run the right Task 2 wrapper; returns the staged payload halves."""
    if _zen(src):
        return (zen.patch_zen_package(raws[0], entries, translations),)
    ua, uexp = uasset.patch_legacy_pair(raws[0], raws[1], entries,
                                        translations)
    return ua, uexp


# --------------------------------------------------------------------------
# container grouping (the payload handler names each staged file's home)

def _stage_groups(src: payload.PayloadSources, staged_files: "dict[str, bytes]"
                  ) -> "dict[tuple, dict]":
    """Group {payload name: bytes} by container.

    Returns {container key: group} where the key is ``("pak", path)`` or
    ``("io", utoc_path, ucas_path)`` and the group carries the writer's
    replacement map: pak INDEX names (mount-relative, exactly as the
    reader produces them) or utoc FILE-MAP names.
    """
    files = payload._ensure(src)          # same parsed models the reads use
    utoc_of = {ucas: utoc for utoc, ucas in src.get("io_pairs", [])}
    inv_file_map: dict[int, dict[str, str]] = {}   # id(toc) -> {entry: raw}
    groups: dict[tuple, dict] = {}
    for name, data in staged_files.items():
        handler = files.get(name)
        if handler is None:
            raise KeyError(f"payload name not found: {name!r} "
                           f"({len(files)} files known)")
        if handler[0] == "pak":
            _, pak_path, entry = handler
            key = ("pak", pak_path)
            groups.setdefault(key, {"replacements": {}})["replacements"][
                entry.name] = data
            continue
        _, toc, ucas_path, entry_index = handler
        inverse = inv_file_map.get(id(toc))
        if inverse is None:
            inverse = inv_file_map[id(toc)] = {i: n for n, i in
                                               toc.file_map.items()}
        key = ("io", utoc_of[ucas_path], ucas_path)
        groups.setdefault(key, {"replacements": {}})["replacements"][
            inverse[entry_index]] = data
    return groups


# --------------------------------------------------------------------------
# main entry

def export_translations(game_root: str, translations_by_asset: dict,
                        aes_key: "bytes | None" = None,
                        out_dir: "str | None" = None,
                        demosaic_game: "str | None" = None,
                        demosaic_include_medium: bool = False,
                        fonts: "tuple[bytes | None, bytes | None] | None" = None
                        ) -> dict:
    """Export translations into rebuilt game containers.

    ``translations_by_asset`` is ``{asset: {entry_index: english | None}}``
    (asset names exactly as TextEntry.asset / payload names — zen package
    names for ST, .uexp names for EF; indexes per-asset; see
    :func:`load_translations` for the table-JSON shapes). Blank/None
    english = skipped. ``aes_key`` is required for an encrypted-index pak
    (EF). ``out_dir`` receives the rebuilt containers mirroring the
    game-relative layout; None writes into a fresh temp dir instead.
    ``fonts`` is an optional ``(dialogue_font, ui_font)`` byte pair (either
    half None = that role keeps its original faces): the font swap stages
    its .ufont replacements onto the SAME staged payload set so one
    container rebuild carries both (separate rebuilds would overwrite
    each other); an export with fonts and no translated rows is valid.

    Returns ``{"containers": [absolute output paths], "rows_patched",
    "rows_skipped", "assets", "fonts_swapped", "verify": "ok",
    "out_dir"}``. Any validation failure (unknown asset, zero-entry
    asset, out-of-range index, untranslatable text, entry/payload
    mismatch) raises a named error BEFORE anything is written; a verify
    failure raises with the per-row details after the containers are
    written (they live under out_dir/temp only — the game tree is never
    touched).
    """
    translations_by_asset = load_translations(translations_by_asset)
    src = payload.find_sources(game_root, aes_key=aes_key)
    payload.list_files(src)       # parse indexes up front (EF: key gate)
    # script-object store for zen class names (same private-seam precedent
    # as zen.patch_zen_package importing patchtext._GEOMETRY)
    script_objects = _stage_script_objects(src)

    staged_files: "dict[str, bytes]" = {}
    scanned: "dict[str, tuple]" = {}     # asset -> (entries, translations)
    rows_patched = rows_skipped = 0
    assets_patched = 0
    skipped_detail: "list[dict]" = []    # non-blank skips, for the summary

    # ---- stage every patched asset BEFORE any container is written
    for asset in sorted(translations_by_asset):
        trans = dict(translations_by_asset[asset] or {})
        live = {i: t for i, t in trans.items() if t not in (None, "")}
        rows_skipped += len(trans) - len(live)
        if not live:
            continue                    # all-blank asset: nothing to rebuild
        raws, entries = _scan_asset(src, asset, script_objects)
        if not entries:
            raise ValueError(
                f"{asset}: translations given but the fresh scan finds zero "
                f"text entries — nothing to patch")
        # BcString same-size pre-filter (Phase 6): a translation that fits
        # NO write mode is skipped with a reason here — letting it reach
        # the patcher would leave the site untouched and blow up the verify
        # (expected drops the row, the re-scan still finds the original).
        unfit = [i for i, t in live.items()
                 if entries[i].kind == "BcString"
                 and patchtext.bc_write_mode(entries[i], t) is None]
        for i in unfit:
            skipped_detail.append({"asset": asset, "entry_index": i,
                                   "reason": "translation too long for the "
                                             "bytecode string slot"})
            live.pop(i)
        rows_skipped += len(unfit)
        trans = {i: t for i, t in trans.items() if i in live}
        if not live:
            continue                    # nothing left to rebuild
        patched = _patch_asset(src, asset, raws, entries, trans)
        if _zen(src):
            staged_files[asset] = patched[0]
        else:                # both halves under their REAL payload names
            staged_files[asset] = patched[1]                    # .uexp
            staged_files[_header_sibling(src, asset)] = patched[0]
        scanned[asset] = (entries, trans)
        rows_patched += len(live)
        assets_patched += 1

    # ---- demosaic: apply the discovery-fixture ops onto the SAME staged
    # payloads (single-pass composition — Phase 4a Task 6 ruled that separate
    # rebuilds overwrite each other, last-writer-wins).
    demosaic_ops = 0
    if demosaic_game:
        from . import demosaic as demosaic_mod
        ops = demosaic_mod.plan_demosaic(game_root, aes_key=aes_key,
                                         include_medium=demosaic_include_medium)
        for op in ops:
            base = bytearray(staged_files.get(op.asset)
                             or payload.read_file(src, op.asset))
            demosaic_mod._sub(base, op)
            staged_files[op.asset] = bytes(base)
            demosaic_ops += 1

    # ---- font swap (Phase 5b): the same single-rebuild composition —
    # .ufont sidecars join staged_files so fonts ride the ONE rebuild. A
    # fonts-only export (no translated rows) is valid and produces the
    # font-bearing containers.
    fonts_swapped = 0
    if fonts and (fonts[0] or fonts[1]):
        from . import fonts as fonts_mod
        font_staged, _report = fonts_mod.stage_font_swaps(
            src, fonts[0], fonts[1])
        for name, data in font_staged.items():
            if name in staged_files:
                raise ValueError(
                    f"{name}: font swap collides with a staged payload "
                    "(ufont names never carry text patches)")
            staged_files[name] = data
        fonts_swapped = len(font_staged)

    # ---- rebuild each touched container (atomic writers, mirrored layout)
    out_root = tempfile.mkdtemp(prefix="slr-export-") if out_dir is None \
        else out_dir
    containers: "list[str]" = []
    verify_paks: "list[str]" = []          # the OUTPUT containers (not the
    verify_io: "list[tuple]" = []          # game sources!) for the verify
    groups = _stage_groups(src, staged_files)
    for key in sorted(groups):
        if key[0] == "pak":
            plan = rewrite.plan_rewrite(game_root, key[1],
                                        groups[key]["replacements"],
                                        aes_key=aes_key)
        else:
            plan = rewrite.plan_rewrite(game_root, key[1],
                                        groups[key]["replacements"])
        rewrite.execute_rewrite(plan, out_root)
        containers.extend(plan["outputs"])
        if key[0] == "pak":
            verify_paks.append(plan["outputs"][0])
        else:                              # outputs order: utoc, ucas
            verify_io.append((plan["outputs"][0], plan["outputs"][1]))

    # ---- verify: re-parse the outputs, re-extract, re-scan every asset
    verify_src = {
        "paks": verify_paks,
        "io_pairs": verify_io,
        "aes_key": aes_key,
        "_files": None,
    }
    problems: "list[str]" = []
    # 1) byte-exact round-trip: every staged file read back through the
    #    rebuilt containers == the patched bytes that were staged
    for name in sorted(staged_files):
        if payload.read_file(verify_src, name) != staged_files[name]:
            problems.append(f"{name}: bytes read back from the rebuilt "
                            f"container differ from the staged patch")
    # 2) structural re-scan per asset: the rebuilt payload must parse as a
    #    package and re-scan to exactly the expected rows. Translated
    #    FText-Base rows always re-appear (the frame detector is
    #    content-agnostic); a translated FString row re-appears only when
    #    the translation still reads as Japanese — textscan's chained
    #    FString emission is JP-gated by design (an English replacement is
    #    no longer an extraction TARGET; its bytes were checked in 1)).
    for asset in sorted(scanned):
        entries, trans = scanned[asset]
        _raws, ventries = _scan_asset(verify_src, asset, script_objects)
        expected = _expected_after_patch(entries, trans)
        if len(ventries) != len(expected):
            problems.append(f"{asset}: re-scan found {len(ventries)} entries, "
                            f"expected {len(expected)}")
            continue
        for i, (new, want) in enumerate(zip(ventries, expected)):
            kind, key, source = want
            if new.source != source:
                problems.append(f"{asset}[{i}]: source {new.source!r} != "
                                f"expected {source!r}")
            elif new.kind != kind:
                problems.append(f"{asset}[{i}]: kind {new.kind!r} != "
                                f"{kind!r}")
            elif new.key != key:
                problems.append(f"{asset}[{i}]: FText key changed")
    if problems:
        shown = "; ".join(problems[:10])
        more = f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""
        raise ValueError(f"export verify failed: {shown}{more}")

    return {"containers": containers, "rows_patched": rows_patched,
            "rows_skipped": rows_skipped, "assets": assets_patched,
            "demosaic_ops": demosaic_ops, "fonts_swapped": fonts_swapped,
            "skipped_detail": skipped_detail,
            "verify": "ok", "out_dir": out_root}


# --------------------------------------------------------------------------
# Phase 8a: patch export (portable, stage-based)

def export_patch(stage_dir: str, translations_by_asset: dict,
                 out_dir: "str | None" = None,
                 fonts: "tuple[bytes | None, bytes | None] | None" = None
                 ) -> dict:
    """Export translations as a PATCH container built purely from the
    staging tree (portable — the original game folder is not needed).

    Same patch/verify contract as :func:`export_translations` (fresh
    re-scan, per-row pre-filters, full structural verify), with two
    differences:

    - the payload handle is :func:`stage.sources` (the staged bytes),
      so every asset the table names MUST be part of the create-time
      stage set (a missing asset raises before anything is written);
    - the final write is :func:`patchpak.write_patch_pak` (legacy pak
      games) — a ``<stem>_SLR_P.pak`` whose entries shadow the base
      pak's. Zen (IoStore) games route through the Phase 8c patch
      container writer; until it lands a zen stage raises
      NotImplementedError (the fallback is the full-rebuild export).

    Font swaps join the staged set exactly as in the full export (the
    .ufont originals ARE staged — the swap reads them through the
    handle). Demosaic is NOT part of this path (Phase 8b: standalone
    operation with its own output).
    """
    translations_by_asset = load_translations(translations_by_asset)
    src = stage_mod.sources(stage_dir)
    model = src["_stage"]["model"]
    zen_mode = _zen(src)
    script_objects = _stage_script_objects(src)
    staged_files: "dict[str, bytes]" = {}
    scanned: "dict[str, tuple]" = {}
    rows_patched = rows_skipped = 0
    assets_patched = 0
    skipped_detail: "list[dict]" = []

    for asset in sorted(translations_by_asset):
        trans = dict(translations_by_asset[asset] or {})
        live = {i: t for i, t in trans.items() if t not in (None, "")}
        rows_skipped += len(trans) - len(live)
        if not live:
            continue
        raws, entries = _scan_asset(src, asset, script_objects)
        if not entries:
            raise ValueError(
                f"{asset}: translations given but the fresh scan finds zero "
                f"text entries — nothing to patch")
        unfit = [i for i, t in live.items()
                 if entries[i].kind == "BcString"
                 and patchtext.bc_write_mode(entries[i], t) is None]
        for i in unfit:
            skipped_detail.append({"asset": asset, "entry_index": i,
                                   "reason": "translation too long for the "
                                             "bytecode string slot"})
            live.pop(i)
        rows_skipped += len(unfit)
        trans = {i: t for i, t in trans.items() if i in live}
        if not live:
            continue
        patched = _patch_asset(src, asset, raws, entries, trans)
        if zen_mode:
            staged_files[asset] = patched[0]        # single zen package
        else:
            staged_files[asset] = patched[1]                        # .uexp
            staged_files[_header_sibling(src, asset)] = patched[0]  # header
        scanned[asset] = (entries, trans)
        rows_patched += len(live)
        assets_patched += 1

    fonts_swapped = 0
    if fonts and (fonts[0] or fonts[1]):
        from . import fonts as fonts_mod
        font_staged, _report = fonts_mod.stage_font_swaps(
            src, fonts[0], fonts[1])
        for name, data in font_staged.items():
            if name in staged_files:
                raise ValueError(
                    f"{name}: font swap collides with a staged payload "
                    "(ufont names never carry text patches)")
            staged_files[name] = data
        fonts_swapped = len(font_staged)

    if not staged_files:
        raise ValueError(
            "nothing to export: no translated rows and no font swap")

    out_root = tempfile.mkdtemp(prefix="slr-patch-") if out_dir is None \
        else out_dir
    os.makedirs(out_root, exist_ok=True)

    if zen_mode:
        # ---- Phase 8c: the IoStore patch container (patched zen packages
        # re-registered under a new container id; resolution by chunk id)
        from . import patchio
        tocs = (model.get("io") or {}).get("tocs") or {}
        holding = [t for t in tocs.values()
                   if any(n in t["entries"] for n in staged_files)]
        if len(holding) != 1:
            raise NotImplementedError(
                f"patched payloads span {len(holding)} source containers — "
                "one patch pair per source is needed")
        source_utoc = next(utoc for utoc, t in tocs.items() if t is holding[0])
        out_utoc_name, out_ucas_name = patchio.patch_container_names(
            os.path.basename(source_utoc))
        out_utoc = os.path.join(out_root, out_utoc_name)
        out_ucas = os.path.join(out_root, out_ucas_name)
        patchio.write_patch_iostore(model, staged_files, out_utoc, out_ucas)
        patchio.verify_patch_iostore(out_utoc, out_ucas, staged_files, model)
        # structural re-scan: a chunk-id-keyed handle over the patch pair
        # (names map to chunk ids via the stage metadata; the patch TOC's
        # own directory index names the files for the engine's mount)
        with open(out_utoc, "rb") as f:
            from . import iostore as _iostore
            patch_toc = _iostore.parse_toc(f.read())
        name_to_chunk = {}
        for t in tocs.values():
            for n in staged_files:
                if n in t["entries"]:
                    name_to_chunk[n] = bytes.fromhex(t["entries"][n]["chunk_id"])
        verify_files = {}
        for n, cid in name_to_chunk.items():
            idx = next(i for i, e in enumerate(patch_toc.entries)
                       if e.chunk_id == cid)
            verify_files[n] = ("io", patch_toc, out_ucas, idx)
        verify_src = {"paks": [], "io_pairs": [],
                      "aes_key": None, "_files": verify_files,
                      "_zen_flag": True,
                      "_script_objects_pre": script_objects}
        # the trio: utoc + ucas + the mount-anchor pak (UE registers IoStore
        # containers through the pak platform file — no .pak, no container)
        containers = [out_utoc, out_ucas,
                      os.path.splitext(out_utoc)[0] + ".pak"]
    else:
        # ---- Phase 8a: the legacy patch pak
        from . import patchpak
        paks = model["pak"]["paks"]
        holding = {p for p, meta in paks.items()
                   if any(n in meta["entries"] for n in staged_files)}
        if len(holding) != 1:
            raise NotImplementedError(
                f"patched payloads span {len(holding)} source paks — one "
                "patch pak per source is needed (no test game does this)")
        source_pak = next(iter(holding))
        out_name = patchpak.patch_pak_name(os.path.basename(source_pak))
        out_path = os.path.join(out_root, out_name)
        patchpak.write_patch_pak(model, stage_dir, staged_files, out_path)
        patchpak.verify_patch_pak(out_path, staged_files, model)
        # structural re-scan through the patch pak (the reader accepts a
        # one-pak game — the same handle shape the real mount sees)
        verify_src = {"paks": [out_path], "io_pairs": [],
                      "aes_key": src["aes_key"], "_files": None}
        containers = [out_path]

    problems: "list[str]" = []
    for asset in sorted(scanned):
        entries, trans = scanned[asset]
        _raws, ventries = _scan_asset(verify_src, asset, script_objects)
        expected = _expected_after_patch(entries, trans)
        if len(ventries) != len(expected):
            problems.append(f"{asset}: re-scan found {len(ventries)} entries, "
                            f"expected {len(expected)}")
            continue
        for i, (new, want) in enumerate(zip(ventries, expected)):
            kind, key, source = want
            if new.source != source:
                problems.append(f"{asset}[{i}]: source {new.source!r} != "
                                f"expected {source!r}")
            elif new.kind != kind:
                problems.append(f"{asset}[{i}]: kind {new.kind!r} != "
                                f"{kind!r}")
            elif new.key != key:
                problems.append(f"{asset}[{i}]: FText key changed")
    if problems:
        shown = "; ".join(problems[:10])
        more = f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""
        raise ValueError(f"patch export verify failed: {shown}{more}")

    return {"containers": containers, "rows_patched": rows_patched,
            "rows_skipped": rows_skipped, "assets": assets_patched,
            "fonts_swapped": fonts_swapped, "skipped_detail": skipped_detail,
            "verify": "ok", "out_dir": out_root, "patch": True}
