# -*- coding: utf-8 -*-
"""Phase 8c: the IoStore patch container — a mount-priority ``_SLR_P``
utoc/ucas pair carrying only the patched zen packages, built entirely
from stage metadata.

How it works (layout facts pinned against the real SampleGameA files):

- A zen game resolves packages through the PACKAGE STORE: each
  container's TOC carries a container-header chunk (type 6; chunk id =
  container_id[8] + 00 00 + 06) whose payload is the v5
  FIoContainerHeader — package ids + per-package store entries
  (imported packages, shader map hashes). At mount the engine registers
  those packages; a LATER-mounted container claiming the same package
  id supersedes the base (the same patch semantics pak ``_P`` files
  use; the mount order is the Paks folder's sorted file order, and
  ``_SLR_P`` sorts after the base stem).
- Chunk RESOLUTION is by chunk id: the patched package keeps its exact
  12-byte chunk id (package id + index + type 1), so the dispatcher's
  TOC lookup in the patch container wins over the base container's.
- One zen package = one type-1 chunk holding the whole package (ST:
  1,359 packages, 1,359 type-1 entries), so shipping the patched
  package bytes as one chunk is complete.

The patch container (:func:`write_patch_iostore`):

- a NEW container id (8 random nonzero bytes; the header chunk id
  derives from it exactly as the source's does);
- TOC entries: the container header + one entry per patched payload
  (same chunk id, meta flags; blake3 io_hash; logical offset =
  first-block index * block size);
- blocks: each patched chunk cut at the source block size,
  Oodle-compressed (fallback stored), 16-aligned absolute offsets from
  ucas offset 0 (the source ucas has NO file header — first chunk
  starts at 0);
- no perfect-hash seeds: every entry listed in
  chunks_without_perfect_hash (valid for small containers);
- an EMPTY directory index (packaged games resolve via the package
  store, not the directory index — the boot test is the arbiter);
- the container-header payload reuses the source header's captured
  store entries (same imported-package lists, same shader hashes) under
  the patch container id, with a minimal tail (all optional sections
  empty, zero-size soft references — the grammar
  :func:`iostore.parse_container_header` walks).

:func:`verify_patch_iostore` re-parses the written pair with the
ordinary reader and byte-compares every patched chunk. Reader
acceptance is the strongest offline check; MOUNT precedence is proven
only by a boot test on the real game.
"""
from __future__ import annotations

import os
import struct

from . import blake3, iostore, oodle

__all__ = ["write_patch_iostore", "verify_patch_iostore",
           "patch_container_names"]

_EMPTY_TAIL = (b"\x00\x00\x00\x00"       # optional segment package ids
               + b"\x00\x00\x00\x00"     # optional segment store entries
               + b"\x00\x00\x00\x00"     # empty name batch
               + b"\x00\x00\x00\x00"     # localized packages
               + b"\x00\x00\x00\x00"     # package redirects
               + struct.pack("<qq", 0, 0))   # soft package references


def patch_container_names(source_utoc: str) -> "tuple[str, str]":
    """``sample_a-Windows.utoc`` → (``…_SLR_P.utoc``, ``…_SLR_P.ucas``)."""
    stem, _ = os.path.splitext(os.path.basename(source_utoc))
    return f"{stem}_SLR_P.utoc", f"{stem}_SLR_P.ucas"


def _new_container_id() -> int:
    import random
    while True:
        cid = random.getrandbits(64)
        if cid not in (0,):
            return cid


def _store_entry_of(captured: dict) -> iostore.ContainerStoreEntry:
    return iostore.ContainerStoreEntry(
        [int(x, 16) for x in captured.get("imported", [])],
        [bytes.fromhex(s) for s in captured.get("shaders", [])])


def _container_header_chunk(new_cid: int, patched: "list[dict]") -> bytes:
    """The v5 FIoContainerHeader payload for the patch container."""
    pids = sorted(p["package_id"] for p in patched)
    if len(set(pids)) != len(pids):
        raise ValueError("duplicate package ids in the patch set")
    by_pid = {p["package_id"]: p for p in patched}
    entries = [_store_entry_of(by_pid[pid]["store_entry"]) for pid in pids]
    ch = iostore.ContainerHeader(
        version=iostore.CHV_SOFT_PACKAGE_REFERENCES_OFFSET,
        container_id=new_cid, package_ids=pids,
        store_entries=entries, tail=_EMPTY_TAIL)
    return iostore.serialize_container_header(ch)


def _compress_chunk(dll, bs: int, data: bytes, base: int):
    """Block tuples + payload spans for one chunk, mirroring
    write_iostore's shape: Oodle per block-sized piece with a stored
    fallback, 16-aligned ABSOLUTE offsets continuing from ``base`` (block
    tuples carry absolute ucas offsets — the reader seeks them directly)."""
    pieces = [data[i:i + bs] for i in range(0, len(data), bs)] or [b""]
    tuples: list = []
    spans: "list[tuple[int, bytes]]" = []
    cur = base
    for piece in pieces:
        comp = oodle.compress(dll, piece)
        if len(comp) < len(piece):
            payload, method = comp, 1
        else:
            payload, method = piece, 0
        cur = (cur + 15) & ~15
        spans.append((cur, payload))
        tuples.append((cur, len(payload), len(piece), method))
        cur += len(payload)
    return tuples, spans


def write_patch_iostore(model: dict, patched: "dict[str, bytes]",
                        out_utoc: str, out_ucas: str) -> "tuple[str, str]":
    """Build the patch utoc/ucas pair from stage metadata + patched
    package bytes. ``patched`` maps payload names (game-relative) to the
    full new package bytes. Returns (out_utoc, out_ucas)."""
    tocs = (model.get("io") or {}).get("tocs") or {}
    holding = [t for t in tocs.values()
               if any(n in t["entries"] for n in patched)]
    if not holding:
        raise KeyError(
            "no staged IoStore container holds any of the patched payloads "
            "— a patch container only carries files captured at create time")
    if len(holding) > 1:
        raise NotImplementedError(
            "patched payloads span multiple source containers — one patch "
            "pair per source is needed (no test game does this)")
    t = holding[0]
    h = t["header"]
    bs = h["compression_block_size"]

    new_cid = _new_container_id()
    dll = oodle.get_dll()

    # ---- the patched entries, in stable (sorted payload name) order
    patch_list = []
    for name in sorted(patched):
        meta = t["entries"].get(name)
        if meta is None:
            raise KeyError(f"{name}: not captured in the stage metadata — "
                           "re-create the project")
        chunk_id = bytes.fromhex(meta["chunk_id"])
        pid = int.from_bytes(chunk_id[:8], "little")
        store = t.get("store_entries", {}).get(chunk_id[:8].hex())
        if store is None:
            raise KeyError(f"{name}: no store entry captured for package "
                           f"{pid:#x} — the patch container cannot register it")
        patch_list.append({"name": name, "chunk_id": chunk_id,
                           "meta_flags": meta["meta_flags"],
                           "file_map_name": meta.get(
                               "file_map_name", name),
                           "package_id": pid, "store_entry": store,
                           "data": patched[name]})

    # ---- chunk payloads: container header first, then the packages
    ch_data = _container_header_chunk(new_cid, patch_list)
    ch_tuples, ch_spans = _compress_chunk(dll, bs, ch_data, 0)
    blocks: list = list(ch_tuples)
    # chunk id is 12 bytes: package id (8) + index (2) + type u16 whose
    # LOW byte at index 11 is the chunk type (real ST container-header id:
    # cid + 00 00 00 06)
    entries = [iostore.ChunkEntry(
        chunk_id=new_cid.to_bytes(8, "little") + b"\x00\x00" + b"\x00\x06",
        offset=0, length=len(ch_data), meta_flags=0,
        io_hash=blake3.hash20(ch_data))]
    cur_block = len(blocks)
    cur_end = ch_tuples[-1][0] + ch_tuples[-1][1] if ch_tuples else 0
    for p in patch_list:
        tuples, spans = _compress_chunk(dll, bs, p["data"], cur_end)
        entries.append(iostore.ChunkEntry(
            chunk_id=p["chunk_id"], offset=cur_block * bs,
            length=len(p["data"]), meta_flags=p["meta_flags"],
            io_hash=blake3.hash20(p["data"])))
        blocks.extend(tuples)
        p["_spans"] = spans
        cur_block = len(blocks)
        cur_end = tuples[-1][0] + tuples[-1][1]

    # ---- the TOC: captured header fields, new counts, and a REAL
    # directory index naming the patched files. The empty-index patch
    # pair mounted but never overrode anything (boot 14): retoc — whose
    # containers provably load — always writes a real index, and with
    # the Indexed flag an empty one can fail the environment init and
    # silently drop the whole container. Names come from the captured
    # stage metadata (file_map_name + mount point of the source TOC).
    mount_point = h.get("mount_point")
    if mount_point is None:
        raise KeyError(
            "stage metadata has no TOC mount point (pre-ZenName stage) — "
            "re-create the project")
    file_map = {}
    for entry_index, p in enumerate(patch_list, start=1):
        file_map[p["file_map_name"]] = entry_index
    dir_blob = iostore.serialize_dir_index(mount_point, file_map)
    header = iostore.TocHeader(
        version=h["version"], header_size=iostore.TOC_HEADER_SIZE,
        entry_count=len(entries),
        compressed_block_entry_count=len(blocks),
        compressed_block_entry_size=h.get("compressed_block_entry_size", 12),
        compression_method_name_count=h.get(
            "compression_method_name_count", len(h["compression_methods"])),
        compression_method_name_length=h.get(
            "compression_method_name_length", 32),
        compression_block_size=bs,
        directory_index_size=len(dir_blob), partition_count=1,
        container_id=new_cid,
        encryption_key_guid=bytes.fromhex(h["encryption_key_guid"]),
        container_flags=h["container_flags"],
        perfect_hash_seeds_count=0,
        partition_size=h.get("partition_size", 0),
        chunks_without_perfect_hash_count=len(entries))
    toc = iostore.Toc(
        header=header, entries=entries,
        perfect_hash_seeds=[],
        chunks_without_perfect_hash=list(range(len(entries))),
        blocks=blocks,
        compression_methods=list(h["compression_methods"]),
        mount_point=mount_point, file_map=file_map,
        raw_tail=b"", _dir_entries=[], _file_entries=[],
        _string_table=[], _directory_blob=dir_blob)

    utoc_bytes = iostore.serialize_toc(toc)

    def _write_ucas(dst) -> None:
        pos = 0
        for off, payload in ch_spans:
            dst.write(b"\x00" * (off - pos))
            dst.write(payload)
            pos = off + len(payload)
        for p in patch_list:
            for off, payload in p["_spans"]:
                dst.write(b"\x00" * (off - pos))
                dst.write(payload)
                pos = off + len(payload)

    iostore._atomic_writes([(out_ucas, _write_ucas),
                            (out_utoc, lambda dst: dst.write(utoc_bytes))])
    stem = os.path.basename(out_utoc)
    for suffix in ("_SLR_P.utoc", "_SLRDM_P.utoc", ".utoc"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    _write_anchor_pak(os.path.splitext(out_utoc)[0] + ".pak",
                      _anchor_facts(model, stem))
    return out_utoc, out_ucas


def _write_anchor_pak(out_pak: str, facts: dict) -> None:
    """The mount anchor: UE registers IoStore containers through the pak
    platform file — a .utoc/.ucas pair without a companion .pak is never
    enumerated. (Boot 14/14b: a structurally valid pair sat in Paks and
    the engine never consulted it. Boot 14d: the game's OWN pak as the
    anchor made the patch pair load — so the synthesized anchor must
    mirror the game pak's footer: this engine build uses the patched-e1
    pak magic, and its open path rejects the stock magic. The footer
    facts travel in the stage set, so export stays game-folder-free.)

    The pak carries one 0-byte placeholder file (a root-level name that
    collides with nothing) so the index shape matches the proven EF
    patch pak: real full-directory index, one entry, non-degenerate.
    """
    from . import pak
    f = facts["footer"]
    mount_raw = bytes.fromhex(facts.get("mount_raw") or "")
    # mount_raw must be a FULL FString (i32 length + chars + NUL) — the
    # stage capture keeps the parser's verbatim bytes; anything malformed
    # falls back to the game pak's standard ../../../ mount
    ok = len(mount_raw) >= 4
    if ok:
        (mlen,) = struct.unpack_from("<i", mount_raw, 0)
        ok = 0 < mlen <= len(mount_raw) - 4 and mount_raw[-1:] == b"\x00"
    if not ok:
        mount_raw = struct.pack("<i", 10) + b"../../../\x00"
    # footer method names are a fixed 5 x 32-byte block; short captures
    # (or none) pad out — a short block would displace the whole footer
    methods = bytes.fromhex(f.get("method_names") or "")
    methods = methods[:160] + b"\x00" * (160 - len(methods))
    footer = pak.Footer(
        encryption_guid=b"\x00" * 16, encrypted_index=False,
        magic=f["magic"], version=f["version"],
        index_offset=0, index_size=0, index_sha1=b"\x00" * 20,
        method_names=methods,
    )
    placeholder = "SLRUnreal_mount_anchor.txt"
    entry = pak.PakEntry(
        name=placeholder, offset=0, size=0, uncompressed_size=0,
        method=0, sha1=b"\x00" * 20, block_count=0, blocks=[],
        flags=0, block_size=0, data_offset=0, encoded_pos=0)
    idx = pak.PakIndex(
        mount_point="", entries=[entry], encrypted=False, footer=footer,
        path_hash_seed=facts.get("path_hash_seed", 0), file_count=0,
        mount_raw=mount_raw,
        phi_offset=-1, phi_size=0, fdi_offset=-1, fdi_size=0)
    pak.write_pak(None, idx, out_pak,
                  entries_data={placeholder: b""})


def _anchor_facts(model: dict, stem: str) -> dict:
    """Footer facts of the game pak whose name-stem matches the patch
    container's (sample_a-Windows.pak for sample_a-Windows.utoc).
    Refuses pre-facts stage sets with the re-create message."""
    paks = (model.get("pak") or {}).get("paks") or {}
    for rel, meta in paks.items():
        base = os.path.basename(rel)
        if os.path.splitext(base)[0] == stem:
            return meta
    if len(paks) == 1:
        return next(iter(paks.values()))
    raise KeyError(
        f"stage metadata has no pak footer facts for stem {stem!r} "
        "(pre-anchor stage) — re-create the project")


def verify_patch_iostore(out_utoc: str, out_ucas: str,
                         patched: "dict[str, bytes]", model: dict) -> None:
    """Re-parse the patch pair with the ordinary reader and byte-compare
    every patched chunk (mapped payload name → chunk id)."""
    with open(out_utoc, "rb") as f:
        toc = iostore.parse_toc(f.read())
    tocs = (model.get("io") or {}).get("tocs") or {}
    chunk_of = {}
    for t in tocs.values():
        for name, meta in t["entries"].items():
            chunk_of[name] = bytes.fromhex(meta["chunk_id"])
    by_chunk = {bytes.fromhex(
        t2["entries"][n]["chunk_id"]): n
        for t2 in tocs.values() for n in patched if n in t2["entries"]}
    dll = oodle.get_dll()
    problems = []
    for name in sorted(patched):
        cid = chunk_of.get(name)
        idx = next((i for i, e in enumerate(toc.entries)
                    if e.chunk_id == cid), None)
        if idx is None:
            problems.append(f"{name}: chunk id {cid.hex()} not in the patch "
                            "TOC")
            continue
        got = iostore.read_entry(toc, out_ucas, idx, dll=dll)
        if got != patched[name]:
            problems.append(f"{name}: bytes read back from the patch "
                            f"container differ ({len(got)} vs "
                            f"{len(patched[name])} bytes)")
    if problems:
        raise ValueError("patch container verify failed:\n"
                         + "\n".join(problems))
