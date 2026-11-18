# -*- coding: utf-8 -*-
"""Phase 8b: demosaic as a STANDALONE operation (Extract Images sibling).

Pre-8b the demosaic rode the translation export's checkboxes — a
per-game tag with nothing to customize (the candidate list is
fixture-pinned per game). Standalone, it becomes: one backend command
that reads the staging set (portable — the candidate assets were
captured at create), applies the same-size byte substitutions, and
writes its OWN patch container (``<stem>_SLRDM_P.*`` — disjoint from
the translation patch so both coexist in Paks/), with an honest
per-candidate report: applied N of M, skipped with reasons (the baked
pixel wall included).

The apply/verify logic is :mod:`demosaic`'s (fixture ops, ``old`` bytes
verified at the offset before any write, every patch read back and
byte-checked); the container step is :mod:`patchpak` (legacy pak) or
:mod:`patchio` (zen) — same patch-container machinery as the
translation export, separate output name.
"""
from __future__ import annotations

import os

from . import demosaic, patchio, patchpak, stage as stage_mod

__all__ = ["export_demosaic_patch"]


def _plan_from_stage(model: dict, stage_dir: str,
                     include_medium: bool) -> "list":
    """The viable PatchOp list with the game inferred from the stage
    (zen → sample_a; legacy pak → sample_b) — the same fixture,
    same confidence gating as plan_demosaic, but the verify-then-sub
    targets are the STAGED bytes, not the game's."""
    game = "sample_a" if model.get("zen_mode") else "sample_b"
    ops = []
    for cand in demosaic.load_candidates().get(game, {}).get("candidates", []):
        conf = cand.get("confidence", "low")
        if conf not in ({"high", "medium"} if include_medium else {"high"}):
            continue
        m = demosaic._parse_edit(cand.get("edit_hint", ""))
        if m is None:
            continue                      # the wall: no pinned edit
        offset, old, new = m
        ops.append(demosaic.PatchOp(
            cand["asset"], offset, old, new,
            "material-slot" if game == "sample_a" else "texture-ref",
            conf, cand.get("why", ""), game))
    return ops


def export_demosaic_patch(stage_dir: str, out_dir: "str | None" = None,
                          include_medium: bool = False) -> dict:
    """Build the standalone demosaic patch container from the staging set.

    Returns ``{"containers", "ops_applied", "ops_skipped", "skipped_detail",
    "verify", "out_dir", "patch": True}``. Every candidate's ``old`` bytes
    are checked against the STAGED payload first — a mismatch (game
    version drift) skips THAT candidate with a reason instead of failing
    the export.
    """
    model = stage_mod.load_model(stage_dir)
    src = stage_mod.sources(stage_dir)
    zen_mode = bool(model.get("zen_mode"))
    ops = _plan_from_stage(model, stage_dir, include_medium)

    staged: "dict[str, bytes]" = {}
    skipped: "list[dict]" = []
    applied = 0
    for op in sorted(ops, key=lambda o: o.asset):
        if op.asset not in src["_files"]:
            skipped.append({"asset": op.asset, "reason":
                            "candidate asset not in the staging set "
                            "(re-create the project)"})
            continue
        data = bytearray(src["_files"][op.asset][1])
        try:
            demosaic._sub(data, op)
        except ValueError as e:
            skipped.append({"asset": op.asset, "reason": str(e)})
            continue
        staged[op.asset] = bytes(data)
        applied += 1

    containers: "list[str]" = []
    out_root = out_dir
    if staged:
        if out_root is not None:
            os.makedirs(out_root, exist_ok=True)
        if zen_mode:
            tocs = (model.get("io") or {}).get("tocs") or {}
            holding = [t for t in tocs.values()
                       if any(n in t["entries"] for n in staged)]
            if len(holding) != 1:
                raise NotImplementedError(
                    "demosaic payloads span multiple source containers")
            source_utoc = next(utoc for utoc, t in tocs.items()
                               if t is holding[0])
            out_utoc_name, out_ucas_name = patchio.patch_container_names(
                os.path.basename(source_utoc))
            out_utoc_name = out_utoc_name.replace("_SLR_P", "_SLRDM_P")
            out_ucas_name = out_ucas_name.replace("_SLR_P", "_SLRDM_P")
            out_utoc = os.path.join(out_root, out_utoc_name)
            out_ucas = os.path.join(out_root, out_ucas_name)
            patchio.write_patch_iostore(model, staged, out_utoc, out_ucas)
            patchio.verify_patch_iostore(out_utoc, out_ucas, staged, model)
            # trio: the mount-anchor pak (see patchio._write_anchor_pak)
            containers = [out_utoc, out_ucas,
                          os.path.splitext(out_utoc)[0] + ".pak"]
        else:
            paks = (model.get("pak") or {}).get("paks") or {}
            holding = [p for p, meta in paks.items()
                       if any(n in meta["entries"] for n in staged)]
            if len(holding) != 1:
                raise NotImplementedError(
                    "demosaic payloads span multiple source paks")
            source_pak = holding[0]
            out_name = patchpak.patch_pak_name(
                os.path.basename(source_pak)).replace("_SLR_P", "_SLRDM_P")
            out_path = os.path.join(out_root, out_name)
            patchpak.write_patch_pak(model, stage_dir, staged, out_path)
            patchpak.verify_patch_pak(out_path, staged, model)
            containers = [out_path]

    return {"containers": containers, "ops_applied": applied,
            "ops_skipped": len(skipped), "skipped_detail": skipped,
            "verify": "ok", "out_dir": out_root, "patch": True}
