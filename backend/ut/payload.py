"""Payload layer: a uniform (name -> decompressed bytes) view over one
game's containers — pak, IoStore, or both — as pure composition of the
Phase 1-2 readers (no new format logic).

Names are game-relative, ``/``-separated, with the mount point's leading
``../`` run stripped (``manifest.normalize_pak_name``): ``Engine/...``,
``SampleGameB/Config/DefaultEngine.ini``,
``sample_a/Content/....uasset``. Discovery is ``protection.find_game``
(UE containers only under Content\\Paks, manifests at the game root).

Lazy by design: ``find_sources`` resolves container paths only; indexes and
tocs are parsed on first use (``list_files``/``read_file``) and cached in
the handle; payloads are never preloaded — each ``read_file`` reads and
decompresses exactly one entry.

EF pairing rule (pinned in test_payload.py): every package file (.uasset
and .umap alike) ships its export stream as a SEPARATE pak entry with the
exact stem and the extension swapped to .uexp — the payload layer needs no
special case because each half is just another entry.
"""
from __future__ import annotations

import os

from . import iostore, pak, protection
from .manifest import normalize_pak_name

# Opaque handle: callers get it from find_sources and pass it back; the
# layout (container paths, aes_key, lazily-built name table) is private.
PayloadSources = dict


def find_sources(game_root: str, aes_key: "bytes | None" = None) -> PayloadSources:
    from . import oodle
    oodle.add_search_root(game_root)
    """Resolve a game root to its payload containers. No index parse yet."""
    found = protection.find_game(game_root)
    ucas_by_stem = {os.path.splitext(u)[0]: u for u in found["ucas"]}
    io_pairs = [(u, ucas_by_stem[stem])
                for u in sorted(found["utocs"])
                if (stem := os.path.splitext(u)[0]) in ucas_by_stem]
    return {
        "paks": sorted(found["paks"]),   # pak paths (sorted)
        "io_pairs": io_pairs,            # (utoc_path, ucas_path) pairs (sorted)
        "aes_key": aes_key,
        "_files": None,                  # name -> handler, built lazily
    }


def _ensure(src: PayloadSources) -> dict:
    """Build (once) and return the game-relative name -> handler table."""
    if src["_files"] is not None:
        return src["_files"]
    files: dict = {}
    for p in src["paks"]:
        footer = pak.read_footer(p)
        if footer.encrypted_index and src["aes_key"] is None:
            raise ValueError(f"{p}: pak index is encrypted; aes_key required")
        idx = pak.read_index(p, footer, aes_key=src["aes_key"])
        for e in idx.entries:
            name = normalize_pak_name(idx.mount_point, e.name)
            # first container wins if names ever collide (none observed in
            # either game: ST's pak and utoc name sets are disjoint)
            files.setdefault(name, ("pak", p, e))
    for utoc_path, ucas_path in src["io_pairs"]:
        with open(utoc_path, "rb") as f:
            toc = iostore.parse_toc(f.read())
        for raw_name, entry_index in toc.file_map.items():
            name = normalize_pak_name(toc.mount_point, raw_name)
            files.setdefault(name, ("io", toc, ucas_path, entry_index))
    src["_files"] = files
    return files


def list_files(src: PayloadSources) -> "list[str]":
    """All payload names, game-relative and ``/``-separated, sorted."""
    return sorted(_ensure(src))


def read_file(src: PayloadSources, name: str) -> bytes:
    """Decompressed payload of one file (pak entry or IoStore chunk)."""
    files = _ensure(src)
    if name not in files:
        raise KeyError(f"payload name not found: {name!r} "
                       f"({len(files)} files known)")
    handler = files[name]
    if handler[0] == "pak":
        _, pak_path, entry = handler
        return pak.read_entry_data(pak_path, entry, aes_key=src["aes_key"])
    if handler[0] == "stage":
        return handler[1]        # Phase 8a: pre-built staging handle
    _, toc, ucas_path, entry_index = handler
    return iostore.read_entry(toc, ucas_path, entry_index)


def file_size(src: PayloadSources, name: str) -> int:
    """Decompressed size of one file without reading it. Pak entries carry
    the size in the index; IoStore entries would have to be read (only the
    legacy-path ubulk size guard calls this, and legacy games are pak)."""
    files = _ensure(src)
    if name not in files:
        raise KeyError(f"payload name not found: {name!r} "
                       f"({len(files)} files known)")
    handler = files[name]
    if handler[0] == "pak":
        return handler[2].uncompressed_size
    return len(read_file(src, name))
