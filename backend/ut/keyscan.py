# -*- coding: utf-8 -*-
"""Offline AES key discovery for encrypted-index UE paks.

The pak is its own oracle: an encrypted v11 index decrypts (AES-256-ECB)
to a plaintext whose first block is a mount-point FString (typically
``../../../`` — length i32 10, then ``../``), and whose FULL plaintext
SHA-1 matches the footer's stored IndexHash. So: slide 32-byte windows
over the shipping exe's constant data as key candidates, keep the ones
whose block-0 decrypt looks like a mount FString (a strong multi-byte
validator), then confirm survivors against the full-index SHA-1 — zero
false positives by construction.

Discovery order (cheapest first): 16-aligned windows, then 8, then 4 in
the PE constant-data sections; progress callbacks land every ~250k
candidates so the runner can stream SLR progress lines. No key material
ships with the addon — results are cached per game in ``keys.json`` by
the caller (slr_cli).
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import time

from . import aes, pak


class KeyNotFoundError(ValueError):
    pass


# ---------------------------------------------------------------------------
# keys.json cache (user-side; created on first save; tolerant of edits)

def load_keys(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_key(path: str, game_tag: str, key_hex: str, source: str = "manual") -> None:
    path = os.fspath(path)
    store = load_keys(path)
    store[game_tag] = {"key_hex": key_hex.lower(), "source": source,
                       "added": time.strftime("%Y-%m-%d")}
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# candidate validation

def _plausible_index_block(key: bytes, block0: bytes) -> bool:
    """Cheap multi-byte validator on the DECRYPTED first index block.

    UE writes the mount point as a TString first: i32 length (incl. NUL)
    then the bytes. The stock mount is ``../../../`` -> length 10 then
    ``../``; accept any sane FString whose first bytes look path-like.
    >=32-bit acceptance bar (length field + 3 path chars).
    """
    try:
        plain = aes.decrypt_ecb(key, block0[:16])
    except ValueError:
        return False
    (n,) = struct.unpack("<i", plain[:4])
    if not (0 < n <= 1024):
        return False
    if n >= 4:
        head = plain[4:7]
        return head == b"../" or head == b"..\\"
    return True


# ---------------------------------------------------------------------------
# PE constant-data ranges (stdlib only)

def _pe_sections(data: bytes) -> "list[tuple[int, int]]":
    """[(file_offset, size)] of the constant-ish sections (.rdata/.data),
    falling back to the whole header-bounded image when names are exotic."""
    if len(data) < 0x400 or data[:2] != b"MZ":
        return [(0, len(data))]
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\x00\x00":
        return [(0, len(data))]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    optsz = struct.unpack_from("<H", data, pe + 20)[0]
    table = pe + 24 + optsz
    out = []
    for i in range(nsec):
        off = table + i * 40
        name = bytes(data[off:off + 8]).split(b"\x00")[0].lower()
        size = struct.unpack_from("<I", data, off + 16)[0]
        rawptr = struct.unpack_from("<I", data, off + 20)[0]
        if size and rawptr + size <= len(data):
            if name in (b".rdata", b".data"):
                out.append((rawptr, size))
    if not out:
        out.append((0, len(data)))
    return out


def _candidate_offsets(data: bytes, ranges, alignment: int, cap: "int | None"):
    total = 0
    for off, size in ranges:
        end = min(off + size, len(data)) - 32
        for pos in range(off, end, alignment):
            yield pos
            total += 1
            if cap is not None and total >= cap:
                return


# ---------------------------------------------------------------------------
# candidate generators beyond the contiguous windows
#
# Compilers rarely lay a baked key as one 32-byte global. The observed shape
# (SampleGameB, verified on real bytes): the key is built field-by-field —
#   C7 45 d0 <imm32>   mov dword ptr [rbp-0x30], imm32      (7 bytes each,
#   C7 45 d4 <imm32>   ...                                   disp8 steps 4)
# sometimes with unrelated instructions interleaved. Two more shapes worth
# covering generally: movabs chains (48/49 B8-BF + imm64, 8-byte frags) and
# plain dword runs. Each generator yields 32-byte candidates built from
# address/disp-ordered fragments; the pak oracle does the rest.

def _imm32_chain_candidates(data: bytes):
    """Reconstruct 32-byte candidates from `C7 <modrm 0x40-0x77> <disp8>
    <imm32>` mov chains: group imms by proximity, order by disp, require a
    by-4 disp run of >= 8 entries."""
    n = len(data)
    pos = data.find(b"\xc7")
    sites = []
    while pos != -1 and pos + 7 <= n:
        modrm = data[pos + 1]
        if 0x40 <= modrm <= 0x77 and (modrm & 0xC0) == 0x40:
            disp = data[pos + 2]
            imm = bytes(data[pos + 3:pos + 7])
            sites.append((pos, modrm & 0x80, disp, imm))
        pos = data.find(b"\xc7", pos + 1)
    # proximity groups: sites within 512 bytes sharing the same modrm reg-bit
    group: "list[tuple]" = []
    for site in sorted(sites, key=lambda s: s[0]):
        if group and (site[0] - group[-1][0] > 512 or site[1] != group[0][1]):
            yield from _emit_disp_run(group)
            group = []
        group.append(site)
    yield from _emit_disp_run(group)


def _emit_disp_run(group: "list[tuple]"):
    if len(group) < 8:
        return
    by_disp = {}
    for _pos, _regbit, disp, imm in group:
        by_disp.setdefault(disp, imm)
    disps = sorted(by_disp)
    for start in range(0, len(disps) - 7):
        run = disps[start:start + 8]
        if all(run[i + 1] - run[i] == 4 for i in range(7)):
            yield b"".join(by_disp[d] for d in run)


def _movabs_candidates(data: bytes):
    """Reconstruct 32-byte candidates from movabs chains: REX.W + B8-BF +
    imm64; 4 ascending 8-byte fragments within 512 bytes."""
    n = len(data)
    frags: "list[tuple[int, bytes]]" = []
    pos = 0
    while pos + 10 <= n:
        pos = data.find(b"\x48\xb8", pos)
        if pos == -1:
            break
        if b"\x48\xb9" == data[pos:pos+2] or True:
            frags.append((pos, bytes(data[pos + 2:pos + 10])))
        pos += 2
    frags.sort()
    group: "list[tuple[int, bytes]]" = []
    for fr in frags:
        if group and fr[0] - group[-1][0] > 512:
            if len(group) >= 4:
                for i in range(0, len(group) - 3):
                    yield b"".join(g[1] for g in group[i:i + 4])
            group = []
        group.append(fr)
    if len(group) >= 4:
        for i in range(0, len(group) - 3):
            yield b"".join(g[1] for g in group[i:i + 4])


# ---------------------------------------------------------------------------
# discovery

def _find_encrypted_pak(game_root: str) -> "tuple[str, object] | None":
    found = pak and None
    from . import protection
    for path in protection.find_game(game_root)["paks"]:
        try:
            footer = pak.read_footer(path)
        except ValueError:
            continue
        if footer.encrypted_index:
            return path, footer
    return None


def discover_key(game_root: str, progress=None, max_candidates: "int | None" = None,
                 alignments: "tuple[int, ...]" = (16, 8, 4)) -> dict:
    """Find the AES-256 key for the game's encrypted-index pak.

    Returns ``{key_hex, source: "exe-scan", exe, offset, candidates}``.
    Raises KeyNotFoundError when the game has no encrypted-index pak (that
    is a nothing-to-do, not a failure of scan) or when the exe sweep
    exhausts without a SHA-1-confirmed hit.
    """
    hit = _find_encrypted_pak(game_root)
    if hit is None:
        raise KeyNotFoundError(
            "no encrypted-index pak found — the game needs no key "
            "(or its protection is not the encrypted-index kind)")
    pak_path, footer = hit
    with open(pak_path, "rb") as f:
        f.seek(footer.index_offset)
        block0 = f.read(16)
        f.seek(footer.index_offset)
        index_full = f.read(footer.index_size)   # the SHA-1 gate is over the FULL plaintext

    from . import protection
    exes = [p for p in protection.find_game(game_root)["exes"]
            if os.path.getsize(p) > 1 << 20]
    if not exes:
        raise KeyNotFoundError("no shipping exe found for the candidate scan")

    seen: "set[bytes]" = set()
    tried = 0
    t_last = 0.0

    def _try(cand: bytes) -> "dict | None":
        nonlocal tried, t_last
        if cand in seen:
            return None
        seen.add(cand)
        tried += 1
        hit = None
        if _plausible_index_block(cand, block0):
            plain = aes.decrypt_ecb(cand, index_full)
            if hashlib.sha1(plain).hexdigest() == footer.index_sha1.hex():
                hit = cand
        now = time.time()
        if progress and now - t_last > 0.5:
            t_last = now
            progress(tried)
        if max_candidates is not None and tried >= max_candidates:
            raise KeyNotFoundError(
                f"candidate cap ({max_candidates}) reached without a "
                f"confirmed key — try a manual key entry")
        return hit

    for exe in exes:
        with open(exe, "rb") as f:
            data = f.read()
        ranges = _pe_sections(data)
        # Cheapest-to-enumerate shapes first: the compiler chain shapes emit
        # only a handful of candidates each, while the contiguous sweep is
        # millions — order matters when the caller caps the budget.
        # shape 1: compiler mov-imm32 chains (field-by-field key setup)
        for cand in _imm32_chain_candidates(data):
            hit = _try(cand)
            if hit:
                return {"key_hex": hit.hex(), "source": "exe-scan",
                        "exe": exe, "offset": -1, "candidates": tried}
        # shape 2: movabs chains
        for cand in _movabs_candidates(data):
            hit = _try(cand)
            if hit:
                return {"key_hex": hit.hex(), "source": "exe-scan",
                        "exe": exe, "offset": -1, "candidates": tried}
        # shape 3: contiguous windows, cheapest alignments first
        for alignment in alignments:
            for pos in _candidate_offsets(data, ranges, alignment, None):
                cand = bytes(data[pos:pos + 32])
                hit = _try(cand)
                if hit:
                    return {"key_hex": hit.hex(), "source": "exe-scan",
                            "exe": exe, "offset": pos, "candidates": tried}
    raise KeyNotFoundError(
        "exe sweep exhausted without a confirmed key — the key may be "
        "derived/obfuscated at runtime; use a manual key entry "
        "(community tools/lists for this game)")
