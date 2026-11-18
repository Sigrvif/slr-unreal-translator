# -*- coding: utf-8 -*-
"""Phase 8a: the patch pak — a mount-priority sibling container carrying
ONLY the patched entries, built entirely from stage metadata.

Why a patch pak instead of a full rebuild: a legacy pak (EF, 2.95 GiB)
must be complete to mount, so a portable export would otherwise need
the original container. Unreal's own patch mechanism avoids that: a
pak named ``<stem>_P.pak`` mounts AFTER the base pak and its entries
shadow the originals by mount-point-relative name (the engine sorts
pak files so later names win — the FPakPlatformFile mount chain, the
same mechanism Epic's own hot-patch paks use). The export product
becomes a few MB, uninstall becomes "delete one file", and the game
tree is never touched.

Layout (:func:`write_patch_pak`): synthesize a v11 PakIndex from
stage.json's per-entry metadata (method shape and per-entry encryption
flags preserved; the patch index itself is encrypted iff the source
pak's was — mirroring the source means zero new engine behaviors to
trust) with ``encoded_pos`` assigned in the source's encoded-blob
order (write_pak's ordering key), a Footer with ``index_offset = 0``
(empty data region), NO path-hash/full-directory extents (``-1`` — a
patch pak legitimately has none; the engine reads patch paks with
empty phi/fdi), and ``file_count = 0`` (v11 = no legacy records). Then
``pak.write_pak(src_path=None, ...)`` — every entry rides
``entries_data``, the writer appends records+payloads at offset 0 and
builds the index fresh.

The pak magic: the source's magic byte-for-byte (patched-e1 for EF —
the game accepts its own variant; stock magic would be a NEW behavior
we have no boot test for).

Verify (:func:`verify_patch_pak`): parse the output with the ordinary
reader (a one-pak PayloadSources), read every patched entry back and
byte-compare against the staged patch bytes. Reader acceptance is the
strongest offline check available; MOUNT precedence is proven only by
a boot test on the real game.
"""
from __future__ import annotations

import os

from . import pak, payload

__all__ = ["write_patch_pak", "verify_patch_pak", "patch_pak_name"]


def patch_pak_name(source_pak_path: str) -> str:
    """``SampleGameB-Windows.pak`` -> ``SampleGameB-Windows_SLR_P.pak``.

    The trailing ``_P`` is the engine's patch-suffix convention (higher
    mount priority than the base container); the ``_SLR`` infix keeps
    the addon's patch paks identifiable and uninstallable at a glance.
    """
    stem, ext = os.path.splitext(os.path.basename(source_pak_path))
    return f"{stem}_SLR_P{ext or '.pak'}"


def _synth_index(pak_meta: dict, patched: "dict[str, bytes]") -> "pak.PakIndex":
    """A PakIndex over ONLY the patched entries, in source blob order."""
    entries = []
    for payload_name, data in patched.items():
        meta = pak_meta["entries"].get(payload_name)
        if meta is None:
            raise KeyError(
                f"{payload_name}: not captured in the stage metadata — the "
                "stage set was built at create time and this payload was "
                "not part of it (re-create the project)")
        e = pak.PakEntry(
            name=meta["index_name"],
            offset=0, size=0,
            uncompressed_size=len(data),
            method=meta["method"],
            sha1=b"\x00" * 20,             # recomputed by the writer
            block_count=0, blocks=[], flags=meta["flags"],
            block_size=0, data_offset=0,
            encoded_pos=meta["order"],
        )
        entries.append(e)
    if len({e.name for e in entries}) != len(entries):
        raise ValueError("duplicate index names in the patch set")
    entries.sort(key=lambda e: e.encoded_pos)
    f = pak_meta["footer"]
    mount_raw = bytes.fromhex(pak_meta["mount_raw"])
    if not mount_raw:
        # a stage model with no mount bytes still needs a VALID FString
        # (i32 length 0) — the writer round-trips mount_raw verbatim, and
        # an empty buffer would shift the whole primary index
        mount_raw = b"\x00\x00\x00\x00"
    footer = pak.Footer(
        encryption_guid=bytes.fromhex(f["encryption_guid"]),
        encrypted_index=bool(f["encrypted_index"]),
        magic=f["magic"],
        version=f["version"],
        index_offset=0,                    # empty data region
        index_size=0,
        index_sha1=b"\x00" * 20,           # recomputed by the writer
        method_names=bytes.fromhex(f["method_names"]),
    )
    return pak.PakIndex(
        mount_point="",                    # unused by the writer
        entries=entries,
        encrypted=False,                   # report shape; footer governs
        footer=footer,
        path_hash_seed=pak_meta["path_hash_seed"],
        file_count=0,                      # v11: no legacy records
        mount_raw=mount_raw,
        phi_offset=-1, phi_size=0,         # a patch pak has no phi/fdi
        fdi_offset=-1, fdi_size=0,
    )


def write_patch_pak(stage_model: dict, stage_dir: str,
                    patched: "dict[str, bytes]",
                    out_path: str) -> str:
    """Build the patch pak from stage metadata + patched payload bytes.

    ``patched`` maps PAYLOAD names (game-relative) to their new bytes;
    the stage metadata bridges them to the source pak's index names.
    Returns ``out_path``. Exactly one source pak must hold the staged
    files (EF: the game's single big pak; a multi-pak legacy layout
    would need one patch pak per source — none observed).
    """
    paks = (stage_model.get("pak") or {}).get("paks") or {}
    holding = [p for p, meta in paks.items()
               if any(n in meta["entries"] for n in patched)]
    if not holding:
        raise KeyError(
            "no staged pak holds any of the patched payloads — a patch pak "
            "only carries files captured at create time")
    if len(holding) > 1:
        raise NotImplementedError(
            f"patched payloads span multiple source paks ({holding}) — "
            "one patch pak per source pak is needed; no test game does this")
    pak_meta = paks[holding[0]]

    aes_key = bytes.fromhex(stage_model["aes_key_hex"]) \
        if stage_model.get("aes_key_hex") else None
    if pak_meta["footer"]["encrypted_index"] and aes_key is None:
        raise ValueError("source pak index is encrypted; aes_key required")

    # entries_data speaks INDEX names (write_pak's contract)
    entries_data = {pak_meta["entries"][n]["index_name"]: data
                    for n, data in patched.items()}
    idx = _synth_index(pak_meta, patched)
    pak.write_pak(None, idx, out_path, entries_data=entries_data,
                  aes_key=aes_key)
    return out_path


def verify_patch_pak(out_path: str, patched: "dict[str, bytes]",
                     stage_model: dict) -> None:
    """Re-parse the patch pak with the ordinary reader and byte-compare
    every patched entry. Raises ValueError listing every mismatch."""
    paks = (stage_model.get("pak") or {}).get("paks") or {}
    index_names = {}
    for meta in paks.values():
        for payload_name, e in meta["entries"].items():
            index_names[e["index_name"]] = payload_name

    aes_key = bytes.fromhex(stage_model["aes_key_hex"]) \
        if stage_model.get("aes_key_hex") else None
    src: payload.PayloadSources = {
        "paks": [out_path], "io_pairs": [], "aes_key": aes_key,
        "_files": None,
    }
    files = payload._ensure(src)
    problems = []
    for payload_name, want in sorted(patched.items()):
        got = payload.read_file(src, payload_name)
        if got != want:
            problems.append(f"{payload_name}: bytes read back from the "
                            f"patch pak differ from the staged patch "
                            f"({len(got)} vs {len(want)} bytes)")
    if problems:
        raise ValueError("patch pak verify failed:\n" + "\n".join(problems))
