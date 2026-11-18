# -*- coding: utf-8 -*-
"""Phase 4a Task 5: demosaic patch planning + application.

Candidates come from the Phase 4a Task 4 discovery fixture
(``backend/tests/fixtures/demosaic_discovery.json``); every edit is a
same-size byte substitution at a byte-located offset (material-slot or
texture reference swaps), applied through the Phase 4a Task 3 export
machinery (stage -> rewrite -> verify). Baked-pixel mosaic (EF event CG
textures) is information-destroyed and is NOT a candidate here — see the
fixture's "wall" entry.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import payload
from . import exporttext

_FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tests", "fixtures", "demosaic_discovery.json")

# per-game which container family the candidates patch (drives aes-key need)
_GAME_IS_IO = {"sample_a": True, "sample_b": False}


@dataclass
class PatchOp:
    asset: str          # payload name (zen package name / .uexp name)
    offset: int         # byte offset inside the primary payload
    old: bytes          # expected current bytes (must match at apply time)
    new: bytes          # same-size replacement
    kind: str           # "material-slot" | "texture-ref"
    confidence: str     # "high" | "medium" | "low"
    why: str
    game: str


def load_candidates(fixture_path: "str | None" = None) -> "dict[str, list[dict]]":
    with open(fixture_path or _FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _game_of(src: "dict") -> str:
    return "sample_a" if src.get("io_pairs") else "sample_b"


def plan_demosaic(game_root: str, aes_key: "bytes | None" = None,
                  selection: "list[str] | None" = None,
                  include_medium: bool = False, include_low: bool = False,
                  fixture_path: "str | None" = None) -> "list[PatchOp]":
    """Build the viable PatchOp list for one game.

    Defaults: HIGH candidates only (the MEDIUM widget swap is opt-in via
    ``include_medium`` — its blueprint display path is untraced; boot-test
    gated). LOW candidates are never planned unless explicitly named in
    ``selection`` AND ``include_low`` (the discovery fixture's LOW entries
    carry no pinned edit). ``selection`` filters by exact payload name.
    """
    src = payload.find_sources(game_root, aes_key=aes_key)
    game = _game_of(src)
    viable = {"high"}
    if include_medium:
        viable.add("medium")
    ops: "list[PatchOp]" = []
    for cand in load_candidates(fixture_path).get(game, {}).get("candidates", []):
        conf = cand.get("confidence", "low")
        hint = cand.get("edit_hint", "")
        m = _parse_edit(hint)
        if m is None:
            if selection and cand["asset"] in selection:
                raise ValueError(f"{cand['asset']}: candidate has no pinned edit "
                                 f"(confidence {conf}) — cannot plan")
            continue
        offset, old, new = m
        if conf not in viable and not (include_low and conf == "low"):
            continue
        if selection is not None and cand["asset"] not in selection:
            continue
        ops.append(PatchOp(cand["asset"], offset, old, new,
                           "material-slot" if game == "sample_a" else "texture-ref",
                           conf, cand.get("why", ""), game))
    if selection:
        planned = {op.asset for op in ops}
        missing = [s for s in selection if s not in planned]
        if missing:
            raise ValueError(f"selection names not in the viable plan: {missing}")
    return ops


def _parse_edit(hint: str) -> "tuple[int, bytes, bytes] | None":
    """Extract (offset, old, new) from an edit_hint of the discovery
    fixture's pinned form: two ``XX YY ZZ``-style hex byte groups around a
    ``->`` with the offset as the first ``0x…`` token."""
    import re
    if "->" not in hint:
        return None
    off_m = re.search(r"payload @0x([0-9A-Fa-f]+)|uexp @0x([0-9A-Fa-f]+)", hint)
    if not off_m:
        return None
    offset = int(off_m.group(1) or off_m.group(2), 16)
    # old bytes: the i32 hex group right after the offset token; new bytes:
    # the first hex byte group after the arrow
    seg_old, seg_new = hint.split("->", 1)
    pre = hint[:hint.index("->")]
    om = re.findall(r"\b([0-9A-Fa-f]{2}(?: [0-9A-Fa-f]{2})*)\b", pre[pre.lower().index("@0x"):])
    nm = re.findall(r"\b([0-9A-Fa-f]{2}(?: [0-9A-Fa-f]{2})*)\b", seg_new)
    if not om or not nm:
        return None
    old = bytes.fromhex(om[0].replace(" ", ""))
    new = bytes.fromhex(nm[0].replace(" ", ""))
    if len(old) != len(new):
        raise ValueError(f"edit hint is not same-size: {old.hex()} -> {new.hex()}")
    return offset, old, new


def apply_demosaic(plan: "list[PatchOp]", game_root: str,
                   aes_key: "bytes | None" = None,
                   out_dir: "str | None" = None) -> dict:
    """Apply the plan through the export machinery and verify.

    Reuses exporttext's stage -> rewrite -> verify pipeline with raw byte
    substitutions instead of text patches. Every op's ``old`` bytes are
    verified at its offset BEFORE anything is written; the rebuilt
    containers are re-parsed and each patched payload read back must carry
    ``new`` at the offset (and not ``old``). The game tree is never
    touched.
    """
    if not plan:
        return {"containers": [], "ops_applied": 0, "verify": "ok",
                "out_dir": out_dir or ""}
    game = plan[0].game
    if any(op.game != game for op in plan):
        raise ValueError("plan mixes games")
    src = payload.find_sources(game_root, aes_key=aes_key)
    payload.list_files(src)

    staged_files: "dict[str, bytes]" = {}
    ops_applied = 0
    for op in sorted(plan, key=lambda o: o.asset):
        if _game_of(src) != op.game:
            raise ValueError(f"{op.asset}: plan/game mismatch")
        if src.get("io_pairs"):
            data = bytearray(payload.read_file(src, op.asset))
            _sub(data, op)
            staged_files[op.asset] = bytes(data)
        else:
            uexp = bytearray(payload.read_file(src, op.asset))
            _sub(uexp, op)
            header = payload.read_file(src, exporttext._header_sibling(src, op.asset))
            staged_files[op.asset] = bytes(uexp)
            staged_files[exporttext._header_sibling(src, op.asset)] = header
        ops_applied += 1

    out_root = out_dir
    containers: "list[str]" = []
    verify_paks: "list[str]" = []
    verify_io: "list[tuple]" = []
    from . import rewrite
    groups = exporttext._stage_groups(src, staged_files)
    for key in sorted(groups):
        if key[0] == "pak":
            repl = {k: v for k, v in groups[key]["replacements"].items()}
            plan_r = rewrite.plan_rewrite(game_root, key[1], repl, aes_key=aes_key)
        else:
            plan_r = rewrite.plan_rewrite(game_root, key[1],
                                          groups[key]["replacements"])
        rewrite.execute_rewrite(plan_r, out_root)
        containers.extend(plan_r["outputs"])
        if key[0] == "pak":
            verify_paks.append(plan_r["outputs"][0])
        else:
            verify_io.append((plan_r["outputs"][0], plan_r["outputs"][1]))

    # ---- verify: read each patched payload back from the OUTPUT containers
    verify_src = {"paks": verify_paks, "io_pairs": verify_io,
                  "aes_key": aes_key, "_files": None}
    problems: "list[str]" = []
    for op in plan:
        data = payload.read_file(verify_src, op.asset)
        got = data[op.offset:op.offset + len(op.new)]
        if got != op.new:
            problems.append(f"{op.asset}@0x{op.offset:x}: reads {got.hex()}, "
                            f"expected {op.new.hex()}")
    if problems:
        raise ValueError("demosaic verify failed: " + "; ".join(problems[:10]))

    return {"containers": containers, "ops_applied": ops_applied,
            "verify": "ok", "out_dir": out_root}


def _sub(data: bytearray, op: PatchOp) -> None:
    if op.offset + len(op.new) > len(data):
        raise ValueError(f"{op.asset}@0x{op.offset:x}: beyond payload end")
    current = bytes(data[op.offset:op.offset + len(op.old)])
    if current == op.new:
        raise ValueError(f"{op.asset}@0x{op.offset:x}: already patched")
    if current != op.old:
        raise ValueError(f"{op.asset}@0x{op.offset:x}: expected {op.old.hex()}, "
                         f"found {current.hex()}")
    data[op.offset:op.offset + len(op.new)] = op.new
