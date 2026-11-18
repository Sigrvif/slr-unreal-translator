"""Phase 1 CLI: read-only container inspection over a game directory."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import manifest as manifest_mod
from . import oodle, pak as pak_mod
from . import protection
from .iostore import parse_toc, read_entry


def _utoc_report(utoc_path: str, ucas_path: str | None) -> dict:
    with open(utoc_path, "rb") as f:
        toc = parse_toc(f.read())
    by_method: dict[str, int] = {}
    for _off, _cs, _us, mi in toc.blocks:
        by_method[str(mi)] = by_method.get(str(mi), 0) + 1
    rep = {"path": utoc_path, "entries": len(toc.entries),
           "named_files": len(toc.file_map), "methods": toc.compression_methods,
           "blocks_by_method": by_method, "extracted_sample": None}
    if ucas_path and os.path.isfile(ucas_path):
        dll = oodle.get_dll()
        ok = fail = 0
        for i, e in enumerate(toc.entries):
            if e.length == 0:
                continue
            try:
                read_entry(toc, ucas_path, i, dll=dll)  # length verified inside
                ok += 1
            except Exception:
                fail += 1
            if ok + fail >= 25:
                break
        rep["extracted_sample"] = {"ok": ok, "fail": fail}
    return rep


def _pak_report(game_root: str, pak_path: str, cache: str | None) -> dict:
    footer = pak_mod.read_footer(pak_path)
    idx = pak_mod.read_index(pak_path, footer)
    ufs_files = protection.find_game(game_root)["ufs"]
    coverage = None
    manifest_count = 0
    if ufs_files and idx.entries:
        ufs = manifest_mod.load_ufs(ufs_files[0])
        manifest_count = len(ufs)
        coverage = manifest_mod.intersect_names(idx.entries, ufs)
    return {"path": pak_path, "footer_magic": footer.magic_variant,
            "index_encrypted": idx.encrypted, "entries": len(idx.entries),
            "manifest_count": manifest_count, "name_coverage": coverage}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("game_root")
    ap.add_argument("--json", help="also write the report to this path")
    args = ap.parse_args(argv)

    fp = protection.fingerprint(args.game_root)
    report = {"game": args.game_root, "fingerprint": fp, "utocs": [], "pak": None,
              "oodle": False}
    try:
        oodle.get_dll()
        report["oodle"] = True
    except FileNotFoundError:
        report["oodle"] = False

    for u in fp["containers"]:
        if u["kind"] == "utoc" and u["header"] == "ok":
            ucas = u["path"][:-5] + ".ucas"
            report["utocs"].append(_utoc_report(u["path"], ucas if os.path.isfile(ucas) else None))
    biggest = max((c["path"] for c in fp["containers"] if c["kind"] == "pak"),
                  key=os.path.getsize, default=None)
    if biggest:
        report["pak"] = _pak_report(args.game_root, biggest, None)

    text = json.dumps(report, indent=1, ensure_ascii=False)
    print(text)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
