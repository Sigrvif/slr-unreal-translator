"""Game discovery + container damage fingerprint. The test games' pak damage
is a one-byte footer-magic patch (0x5A6F12E1 vs stock 0x5A6F12F1); SampleGameA's index is plaintext, SampleGameB's is AES-encrypted. The utoc files
are stock (the ASCII magic is genuine)."""
from __future__ import annotations

import os

from . import pak as pak_mod
from .iostore import parse_toc_header


def find_game(game_root: str) -> dict:
    exes, paks, utocs, ucas, ufs = [], [], [], [], []
    for base, _dirs, files in os.walk(game_root):
        # Ruling (Task 7 fix round 1): UE containers only under Content\Paks —
        # CEF3 ships Chromium resource .pak files elsewhere in the tree.
        parts = {p.lower() for p in base.split(os.sep)}
        under_content_paks = "paks" in parts and "content" in parts
        for name in files:
            full = os.path.join(base, name)
            low = name.lower()
            if low.endswith("-win64-shipping.exe"):
                exes.append(full)
            elif under_content_paks and low.endswith(".pak"):
                paks.append(full)
            elif under_content_paks and low.endswith(".utoc"):
                utocs.append(full)
            elif under_content_paks and low.endswith(".ucas"):
                ucas.append(full)
            elif low == "manifest_ufsfiles_win64.txt":
                ufs.append(full)
    return {"exes": exes, "paks": sorted(paks), "utocs": sorted(utocs),
            "ucas": sorted(ucas), "ufs": ufs}


def fingerprint(game_root: str) -> dict:
    found = find_game(game_root)
    containers = []
    for p in found["paks"]:
        try:
            f = pak_mod.read_footer(p)
            containers.append({"path": p, "kind": "pak",
                               "footer": f.magic_variant,
                               "index_encrypted": f.encrypted_index, "entries": None})
        except ValueError as e:
            containers.append({"path": p, "kind": "pak", "footer": f"bad: {e}",
                               "index_encrypted": None, "entries": None})
    for u in found["utocs"]:
        try:
            with open(u, "rb") as f:
                h = parse_toc_header(f.read(0x1000))
            containers.append({"path": u, "kind": "utoc", "header": "ok",
                               "version": h.version, "entry_count": h.entry_count,
                               "encrypted": h.encrypted, "signed": h.signed})
        except ValueError as e:
            containers.append({"path": u, "kind": "utoc", "header": f"bad: {e}"})
    return {"engine": "ue5", "iostore": bool(found["utocs"]), "containers": containers,
            "ufs": found["ufs"]}
