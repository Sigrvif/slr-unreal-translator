# -*- coding: utf-8 -*-
"""Phase 8a: portable-project staging — the slrproj-carried export base.

SLR's Save Project (.slrproj) packs the project's entire staging cache,
so a project is portable exactly when the cache carries every byte the
export needs (the SLRUnity model). For Unreal the full containers are
far too big to embed (EF 2.95 GiB), but the export only ever READS the
text-bearing packages — measured on the real games: EF 541 assets /
70.3 MiB, ST 49 assets / 1.4 MiB. :func:`capture_stage` writes exactly
those (plus the .ufont originals the font swap reads, the demosaic
candidate assets, and the container metadata a PATCH container needs)
under a stage directory the addon keeps inside the project cache;
:func:`sources` turns the directory back into a payload-layer handle so
the export pipeline runs unchanged.

What lives on disk after capture:

- ``files/<game-rel>`` — raw decompressed payload bytes of every
  text-bearing package (legacy EF pairs stage BOTH halves under their
  payload names), every .ufont (the font swap reads + replaces whole
  files), and every demosaic candidate asset.
- ``script_objects.bin`` — the zen ScriptObjects chunk (ST only; zen
  class names — BcString emission keys off them — so a stage-mode scan
  needs the same store the game scan used).
- ``stage.json`` —
  - ``containers``: per source container the path, size and sha1 — the
    fingerprint for "the game updated since create" warnings.
  - ``aes_key_hex``: the confirmed pak key (EF; null for ST).
  - ``zen_mode``: ST-style single-file packages vs EF legacy pairs.
  - ``pak``: everything the patch-pak writer needs — per source pak the
    footer fields (version, magic, encrypted_index, encryption guid,
    method names), mount_raw, path_hash_seed, and per NEEDED file the
    index name + method + flags + uncompressed size + encoded-blob
    order. "Needed" = staged ∪ ufonts ∪ demosaic assets — never the
    whole 17,829-entry index (EF stage.json stays ~100 KB).
  - ``io``: per NEEDED file the source utoc path + file-map name +
    entry index, plus the utoc header fields (consumed by the Phase 8c
    patch-container writer).
  - ``staged_files`` / ``ufonts`` / ``demosaic_assets``: the sets.

One pass: with ``extract_out`` given, capture_stage ALSO builds the
extract table, so project creation runs scan → stage (the stage pass
replaces the extract pass — same reads, same scan, plus the writes).
"""
from __future__ import annotations

import hashlib
import json
import os

from . import demosaic, iostore, pak, payload, scenegraph, textscan, uasset, zen

__all__ = ["capture_stage", "load_model", "sources", "check_fingerprint",
           "stage_file_path", "STAGE_MANIFEST", "SCRIPT_OBJECTS_FILE"]

STAGE_MANIFEST = "stage.json"
SCRIPT_OBJECTS_FILE = "script_objects.bin"

# --------------------------------------------------------------------------
# capture

def _sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _container_rel(path: str) -> str:
    """Container path as recorded in stage.json — keyed by REAL path so
    export-from-stage never needs the game tree to resolve siblings."""
    return os.path.normpath(path).replace(os.sep, "/")


def _game_name(game_root: str) -> str:
    return os.path.basename(os.path.normpath(game_root))


def _candidate_names() -> set:
    """Demosaic candidate payload names (fixture-backed; the wall entry
    carries a descriptive asset string that simply never resolves)."""
    try:
        per_game = demosaic.load_candidates()
    except Exception:                      # noqa: BLE001 - optional stage set
        return set()
    return {c["asset"] for blk in per_game.values()
            for c in blk.get("candidates", [])}


def _script_objects_bytes(src: payload.PayloadSources) -> "bytes | None":
    """The raw ScriptObjects chunk (zen only; chunk type 5)."""
    try:
        for utoc_path, ucas_path in src.get("io_pairs") or ():
            with open(utoc_path, "rb") as f:
                toc = iostore.parse_toc(f.read())
            for i, e in enumerate(toc.entries):
                if e.chunk_type == 5:
                    return iostore.read_entry(toc, ucas_path, i)
    except Exception:                      # noqa: BLE001 - optional
        return None
    return None


def _pak_meta(src: payload.PayloadSources, needed: set) -> dict:
    """The pak half of stage.json, restricted to the needed names.

    Per source pak: footer fields + mount bytes + per-entry metadata
    with the payload name → index name mapping (the patch writer's
    entries_data keys are INDEX names, but the export pipeline speaks
    payload names — the map bridges the two).

    Paks holding NONE of the staged names still contribute footer-level
    facts ("entries": {}): the zen patch trio's anchor .pak must mirror
    the game pak's footer — magic, compression-method names, mount
    point — or the engine rejects it at open and never mounts the
    companion .utoc (boot 14/14b/14d: the game's own pak as anchor
    worked, our stock-magic synthesized one did not).
    """
    files = payload._ensure(src)
    by_pak: dict = {}
    for name in needed:
        handler = files.get(name)
        if handler is None or handler[0] != "pak":
            continue
        by_pak.setdefault(handler[1], []).append((name, handler[2]))
    paks: dict = {}
    for pak_path in src["paks"]:
        rel = _container_rel(pak_path)
        pairs = by_pak.get(pak_path) or ()
        try:
            idx = pak.read_index(pak_path, aes_key=src["aes_key"])
        except Exception:                   # noqa: BLE001 - facts-only fallback
            f = pak.read_footer(pak_path)
            paks[rel] = {
                "footer": {
                    "version": f.version, "magic": f.magic,
                    "encrypted_index": f.encrypted_index,
                    "encryption_guid": f.encryption_guid.hex(),
                    "method_names": f.method_names.hex(),
                },
                "mount_raw": "", "path_hash_seed": 0, "entries": {},
            }
            continue
        order = {e.name: i for i, e in
                 enumerate(sorted(idx.entries, key=lambda e: e.encoded_pos))}
        entries = {}
        for payload_name, entry in pairs:
            entries[payload_name] = {
                "index_name": entry.name,
                "method": entry.method, "flags": entry.flags,
                "uncompressed_size": entry.uncompressed_size,
                "order": order[entry.name],
            }
        paks[rel] = {
            "footer": {
                "version": idx.footer.version,
                "magic": idx.footer.magic,
                "encrypted_index": idx.footer.encrypted_index,
                "encryption_guid": idx.footer.encryption_guid.hex(),
                "method_names": idx.footer.method_names.hex(),
            },
            "mount_raw": idx.mount_raw.hex(),
            "path_hash_seed": idx.path_hash_seed,
            "entries": entries,
        }
    return {"paks": paks} if paks else {}


def _io_meta(src: payload.PayloadSources, needed: set) -> dict:
    """The IoStore half of stage.json, restricted to the needed names.

    Phase 8c additions: per-entry chunk ids + meta flags and the source
    container header's store entries for the staged packages (imported
    packages + shader map hashes) — everything a patch utoc/ucas needs
    to re-register those packages under a new container id, captured
    once at create so export never touches the game.
    """
    files = payload._ensure(src)
    utoc_of = {os.path.normpath(ucas): utoc
               for utoc, ucas in src.get("io_pairs", [])}
    by_ucas: dict = {}
    for name in needed:
        handler = files.get(name)
        if handler is None or handler[0] != "io":
            continue
        by_ucas.setdefault(handler[2], []).append((name, handler[1], handler[3]))
    if not by_ucas:
        return {}
    tocs: dict = {}
    for ucas_path, triples in by_ucas.items():
        toc = triples[0][1]
        h = toc.header
        raw_of = {}
        for raw in toc.file_map:
            raw_of[payload.normalize_pak_name(toc.mount_point, raw)] = raw
        entries = {}
        for payload_name, toc_ref, entry_index in triples:
            raw = raw_of.get(payload_name)
            if raw is None:
                raise KeyError(f"{payload_name}: no file-map name resolves")
            chunk = toc_ref.entries[entry_index]
            entries[payload_name] = {
                "file_map_name": raw,
                "entry_index": entry_index,
                "chunk_id": chunk.chunk_id.hex(),
                "meta_flags": chunk.meta_flags,
            }
        # container-header store entries for the staged packages (the
        # patch container re-registers them; the engine's package store
        # needs the same imported-package lists and shader hashes)
        store_by_pkg = {}
        chi = next((i for i, e in enumerate(toc_ref.entries)
                    if e.chunk_type == 6), None)
        if chi is not None:
            from . import oodle
            ch = _parse_ch(iostore.read_entry(toc_ref, ucas_path, chi,
                                              dll=oodle.get_dll()))
            staged_pkgs = {v["chunk_id"][:16] for v in entries.values()}
            for pid, se in zip(ch.package_ids, ch.store_entries):
                pid_hex = pid.to_bytes(8, "little").hex()
                if pid_hex in staged_pkgs:
                    store_by_pkg[pid_hex] = {
                        "imported": [hex(p) for p in se.imported_packages],
                        "shaders": [s.hex() for s in se.shader_map_hashes],
                    }
        utoc_path = utoc_of[os.path.normpath(ucas_path)]
        tocs[_container_rel(utoc_path)] = {
            "ucas": _container_rel(ucas_path),
            "header": {
                "version": h.version, "container_id": h.container_id,
                "container_flags": h.container_flags,
                "compression_block_size": h.compression_block_size,
                "compressed_block_entry_size": h.compressed_block_entry_size,
                "compression_method_name_count":
                    h.compression_method_name_count,
                "compression_method_name_length":
                    h.compression_method_name_length,
                "compression_methods": list(toc.compression_methods),
                "mount_point": toc.mount_point,
                "encryption_key_guid": h.encryption_key_guid.hex(),
                "partition_size": h.partition_size,
            },
            "store_entries": store_by_pkg,
            "entries": entries,
        }
    return {"tocs": tocs}


def _parse_ch(data: bytes):
    """Container header parse with the oodle dll contract handled here so
    _io_meta stays linear (late import, same seam as write_iostore)."""
    return iostore.parse_container_header(data)


def capture_stage(game_root: str, aes_key: "bytes | None" = None,
                  stage_dir: str = "stage",
                  extract_out: "str | None" = None,
                  progress=None) -> dict:
    """One pass: scan the game, write the extract table (when
    ``extract_out``), and capture the staging set under ``stage_dir``.

    Returns ``{"stats", "stage": {files, bytes, ufonts, candidates},
    "out": extract path | None, "stage_dir"}``. The stats shape matches
    cmd_extract's (the addon reads it the same way).
    """
    src = payload.find_sources(game_root, aes_key=aes_key)
    files = payload.list_files(src)                 # key gate up front
    names = [n for n in files if n.lower().endswith((".uasset", ".umap"))]
    zen_mode = bool(src.get("io_pairs"))
    script_objects = textscan._script_objects(src) if zen_mode else None
    stems = textscan.asset_stems(src) if zen_mode else None
    name_freq: dict = {}             # JP batch text -> #packages (zen)

    ufonts = sorted(n for n in files if n.lower().endswith(".ufont"))
    candidates = _candidate_names() & set(files)

    os.makedirs(os.path.join(stage_dir, "files"), exist_ok=True)
    entries: list = []
    errors: list = []
    warnings: dict = {}
    by_kind: dict = {}
    by_kind_jp: dict = {}
    assets_scanned = 0
    staged: dict = {}                 # payload name -> bytes (text set)
    total = len(names)
    for done, n in enumerate(names):
        if progress:
            progress(done, total, n)
        try:
            if zen_mode:
                data = payload.read_file(src, n)
                pkg = zen.parse_zen(data, n, script_objects=script_objects)
                halves = (data,)
            else:
                stem = n[:-len(".uasset")] if n.endswith(".uasset") \
                    else n[:-len(".umap")]
                uexp_name = stem + ".uexp"
                ua = payload.read_file(src, n)
                ux = payload.read_file(src, uexp_name)
                pkg = uasset.parse_legacy(ua, ux, uexp_name)
                halves = (ua, ux)
        except Exception as exc:             # noqa: BLE001 - recorded below
            errors.append(f"{n}: {exc!r}")
            continue
        assets_scanned += 1
        pkg_entries, pkg_warnings = textscan.scan_text(pkg, stems)
        entries.extend(pkg_entries)
        if zen_mode:
            # shared-identifier frequency (see textscan
            # .upgrade_zenname_risk) — same set-per-package domain
            for t in {ne.text for ne in pkg.name_entries
                      if textscan._has_jp(ne.text)}:
                name_freq[t] = name_freq.get(t, 0) + 1
        for e in pkg_entries:
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
            if textscan._has_jp(e.source):
                by_kind_jp[e.kind] = by_kind_jp.get(e.kind, 0) + 1
        for w in pkg_warnings:
            cls, _, cnt = w.partition(":")
            warnings[cls.strip()] = warnings.get(cls.strip(), 0) + int(cnt)
        if pkg_entries:
            if zen_mode:
                staged[n] = halves[0]
            else:
                # both halves under their REAL payload names (the export
                # stage-grouping keys by payload name)
                staged[n] = halves[0]
                staged[stem + ".uexp"] = halves[1]

    # ufonts: staged as bytes — the font swap reads the original through
    # the payload handle, so a stage-mode swap needs them in the tree.
    for name in ufonts:
        staged[name] = payload.read_file(src, name)

    # demosaic candidates: staged as single payloads
    for name in sorted(candidates):
        if name not in staged:
            staged[name] = payload.read_file(src, name)

    # write the staged bytes
    stage_bytes = 0
    for name, data in sorted(staged.items()):
        dst = os.path.join(stage_dir, "files", *name.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)
        stage_bytes += len(data)

    # shared-identifier promotion (zen only) — must land BEFORE the
    # extract table rows are built so their tags match scan_game's
    if zen_mode:
        textscan.upgrade_zenname_risk(entries, name_freq)

    # ScriptObjects chunk (zen only) — a stage-mode scan must see the same
    # class-name store the create-time scan saw, or BcString rows vanish.
    so_bytes = _script_objects_bytes(src) if zen_mode else None
    if so_bytes is not None:
        with open(os.path.join(stage_dir, SCRIPT_OBJECTS_FILE), "wb") as f:
            f.write(so_bytes)

    # container fingerprint + patch-writer metadata (needed names only)
    needed = set(staged)
    containers = []
    for p in src["paks"]:
        containers.append({"path": _container_rel(p), "kind": "pak",
                           "size": os.path.getsize(p),
                           "sha1": _sha1_file(p)})
    for utoc_path, ucas_path in src.get("io_pairs", []):
        containers.append({"path": _container_rel(utoc_path), "kind": "io",
                           "size": os.path.getsize(utoc_path),
                           "sha1": _sha1_file(utoc_path)})
        containers.append({"path": _container_rel(ucas_path), "kind": "io",
                           "size": os.path.getsize(ucas_path),
                           "sha1": _sha1_file(ucas_path)})

    stage_meta = {
        "version": 1,
        "game_root_name": _game_name(game_root),
        "aes_key_hex": aes_key.hex() if aes_key else None,
        "zen_mode": zen_mode,
        "script_objects": bool(so_bytes is not None),
        "containers": containers,
        "pak": _pak_meta(src, needed),
        "io": _io_meta(src, needed),
        "staged_files": sorted(staged),
        "ufonts": ufonts,
        "demosaic_assets": sorted(candidates),
    }
    _atomic_json(os.path.join(stage_dir, STAGE_MANIFEST), stage_meta)

    stats = {
        "assets_scanned": assets_scanned,
        "text_entries": len(entries),
        "by_kind": by_kind,
        "by_kind_jp": by_kind_jp,
        "warnings": warnings,
        "errors": errors,
        "files_scanned": len(files),
    }
    out = None
    if extract_out:
        scan = {"entries": entries, "stats": {
            "assets_scanned": assets_scanned, "text_entries": len(entries),
            "by_kind": by_kind, "by_kind_jp": by_kind_jp,
            "warnings": warnings, "errors": errors}}
        scene = scenegraph.build_scenegraph(scan)
        stats.update(scene["stats"])
        data = {
            "version": 2,
            "command": "extract",
            "game_dir": game_root,
            "graph": scene["graph"],
            "text_table": scene["text_table"],
            "stats": stats,
        }
        _atomic_json(extract_out, data)
        out = extract_out
    return {"stats": stats,
            "stage": {"files": len(staged), "bytes": stage_bytes,
                      "ufonts": len(ufonts), "candidates": len(candidates)},
            "out": out, "stage_dir": stage_dir}


def _atomic_json(path: str, data: dict) -> None:
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               prefix=".slr-stage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

# --------------------------------------------------------------------------
# load + consume

def load_model(stage_dir: str) -> dict:
    with open(os.path.join(stage_dir, STAGE_MANIFEST), encoding="utf-8") as f:
        return json.load(f)


def stage_file_path(stage_dir: str, name: str) -> str:
    return os.path.join(stage_dir, "files", *name.split("/"))


def sources(stage_dir: str) -> payload.PayloadSources:
    """A payload-layer handle over the staging tree.

    ``_files`` is PRE-BUILT with ``("stage", bytes)`` handlers, so
    payload.read_file/list_files work unchanged. ``zen_mode`` replaces
    the io_pairs presence test the export pipeline uses (a stage handle
    has no real containers); ``_script_objects_pre`` carries the parsed
    ScriptObjects store so a stage-mode scan matches the create-time
    scan. The aes_key rides along for the patch-pak writer.
    """
    model = load_model(stage_dir)
    staged = {}
    for name in model["staged_files"]:
        with open(stage_file_path(stage_dir, name), "rb") as f:
            staged[name] = ("stage", f.read())
    so_pre = None
    if model.get("script_objects"):
        so_path = os.path.join(stage_dir, SCRIPT_OBJECTS_FILE)
        if os.path.isfile(so_path):
            with open(so_path, "rb") as f:
                so_pre = zen.parse_script_objects(f.read())
    return {
        "paks": [],
        "io_pairs": [],
        "aes_key": bytes.fromhex(model["aes_key_hex"])
        if model.get("aes_key_hex") else None,
        "_files": staged,
        "_stage": {"dir": os.path.abspath(stage_dir), "model": model},
        "_zen_flag": bool(model.get("zen_mode")),
        "_script_objects_pre": so_pre,
    }


def check_fingerprint(game_root: str, model: dict) -> "list[str]":
    """Compare the live containers against the create-time fingerprint.

    Returns the changed/missing container paths ([] = unchanged). The
    caller decides whether to warn; a changed game never blocks a
    staging-based export (the patch targets the staged bytes), but a
    full-rebuild export against a changed game is refused by the addon.
    """
    changed = []
    for c in model.get("containers", []):
        rel = c["path"]
        if os.path.isabs(rel):
            live = rel
        else:
            live = os.path.join(game_root, *rel.split("/"))
        if not os.path.isfile(live):
            changed.append(c["path"] + " (missing)")
            continue
        if os.path.getsize(live) != c["size"]:
            changed.append(c["path"] + " (size)")
            continue
        if _sha1_file(live) != c["sha1"]:
            changed.append(c["path"] + " (sha1)")
    return changed
