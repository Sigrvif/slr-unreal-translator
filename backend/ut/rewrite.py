"""Rewrite orchestration — the Phase 4 export pipeline's kernel.

``plan_rewrite`` validates name→bytes replacements against ONE container
(pak or IoStore pair, chosen by extension) and returns an execution plan;
``execute_rewrite`` runs the Task 4–6 writers into an output directory
that MIRRORS the game-relative layout and returns the game-relative paths
it wrote — the future export zip payload. Game trees are never touched:
every output lands under ``out_dir`` only.
"""
from __future__ import annotations

import os

from . import iostore, pak


def _game_relative(game_root: str, path: str) -> str:
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(game_root))
    if rel.startswith(".."):
        raise ValueError(f"{path} is outside the game root {game_root}")
    return rel.replace(os.sep, "/")


def plan_rewrite(game_root: str, container_path: str,
                 replacements: "dict[str, bytes]",
                 aes_key: "bytes | None" = None) -> dict:
    """Plan the rewrite of one container; no file is written here.

    ``replacements`` keys are pak index names (``.pak``) or container file
    names exactly as the reader produces them (``.utoc`` file map) and ALL
    of them must resolve. Returns a plan dict:

    - ``kind``: ``"pak"`` | ``"iostore"``
    - ``containers``: source path(s) — one for pak, ``[utoc, ucas]`` for iostore
    - ``rel``: game-relative output paths, ``/``-separated (zip convention)
    - ``affected``: sorted replacement names that resolved
    - ``replacements`` / ``aes_key``: writer inputs
    - ``index`` (pak) or ``toc`` + ``chunk_patches`` (iostore): parsed model
    - ``outputs``: ABSOLUTE paths of the written files, filled in by
      ``execute_rewrite`` (empty until then)
    """
    replacements = dict(replacements or {})
    low = container_path.lower()

    if low.endswith(".pak"):
        idx = pak.read_index(container_path, aes_key=aes_key)
        if idx.encrypted:
            # read_index without a key returns the Phase-1 encrypted report
            # (empty entries) — plan cannot resolve anything without it
            raise ValueError(f"{container_path}: pak index is AES-encrypted; "
                             "aes_key is required to plan a rewrite")
        missing = sorted(set(replacements) - {e.name for e in idx.entries})
        if missing:
            raise KeyError(f"replacements not in the pak index: {missing}")
        return {"kind": "pak", "game_root": game_root,
                "containers": [container_path],
                "rel": [_game_relative(game_root, container_path)],
                "affected": sorted(replacements),
                "replacements": replacements, "aes_key": aes_key,
                "index": idx, "outputs": []}

    if low.endswith(".utoc"):
        with open(container_path, "rb") as f:
            blob = f.read()
        head = iostore.parse_toc_header(blob)
        if head.encrypted:
            # parse_toc would refuse it too; say why and that aes_key
            # cannot help — the TOC itself is the encrypted thing
            raise ValueError(f"{container_path}: encrypted IoStore TOC not "
                             "supported (no test game uses one)")
        toc = iostore.parse_toc(blob)
        missing = sorted(set(replacements) - set(toc.file_map))
        if missing:
            raise KeyError(f"replacements not in the container file map: {missing}")
        chunk_patches: "dict[int, bytes]" = {}
        for name, data in replacements.items():
            entry_index = toc.file_map[name]
            if entry_index in chunk_patches:
                raise ValueError(f"{name}: resolves to chunk {entry_index} "
                                 "already claimed by another name")
            chunk_patches[entry_index] = data
        ucas = os.path.splitext(container_path)[0] + ".ucas"
        if not os.path.isfile(ucas):
            raise ValueError(f"missing .ucas sibling: {ucas}")
        return {"kind": "iostore", "game_root": game_root,
                "containers": [container_path, ucas],
                "rel": [_game_relative(game_root, container_path),
                        _game_relative(game_root, ucas)],
                "affected": sorted(replacements),
                "replacements": replacements, "aes_key": None,
                "toc": toc, "chunk_patches": chunk_patches, "outputs": []}

    raise ValueError(f"unsupported container: {container_path} "
                     "(expected .pak or .utoc)")


def execute_rewrite(plan: dict, out_dir: str) -> "list[str]":
    """Run the plan's writer into ``out_dir`` (mirroring the game-relative
    layout); returns the game-relative paths written — the export payload.

    On success ``plan["outputs"]`` is filled with the absolute output paths.
    Writers are atomic per file (tempfile + os.replace); the utoc/ucas pair
    replaces the ucas first (see ``iostore._atomic_writes``).
    """
    outs = [os.path.join(out_dir, *rel.split("/")) for rel in plan["rel"]]
    for out in outs:
        directory = os.path.dirname(out)
        if directory:
            os.makedirs(directory, exist_ok=True)
    kind = plan["kind"]
    if kind == "pak":
        pak.write_pak(plan["containers"][0], plan["index"], outs[0],
                      entries_data=plan["replacements"], aes_key=plan["aes_key"])
    elif kind == "iostore":
        iostore.write_iostore(plan["containers"][0], plan["containers"][1],
                              outs[0], outs[1],
                              chunk_patches=plan["chunk_patches"])
    else:
        raise ValueError(f"unknown plan kind: {kind!r}")
    plan["outputs"] = outs
    return list(plan["rel"])
