"""Length-aware text patching (Phase 4a, Task 1): rewrite FText/FString
frames in ONE asset's payload — re-write the negative length fields and
splice grown/shrunk UTF-16LE bodies, never same-length hacks (Phase 4a
global constraint). Scan-only code stays in ``textscan``; this module
consumes its TextEntry rows and produces patched bytes + a report.

Patch geometry (Phase 3 final review, verbatim; offsets are
payload-absolute for ST zen chunks, .uexp-relative for EF — the layer is
offset-agnostic: one asset's payload + that asset's entries):

- ``FText-Base`` — length i32 at ``offset+42`` (store ``-(chars+1)``),
  UTF-16LE+NUL body at ``offset+46``, patchable range
  ``[offset+42, offset+46+2*(chars+1))``.
- ``FString`` — length i32 at ``offset``, body at ``offset+4``, range
  ``[offset, offset+4+2*(chars+1))``.
- ``BcString`` (Phase 6) — Kismet bytecode string constant,
  ``entry.offset`` = the opcode byte: ``[34][UTF-16LE text][00 00][1f
  32-hex key][00]``. NUL-terminated with NO length field — and execution
  resumes immediately after the NUL (the key const is the NEXT opcode),
  so there is no dead tail: the patch is a SAME-SIZE EXACT-FILL
  overwrite. The translation is space-padded to the original slot so the
  terminator lands exactly where the original one was; every changed
  byte stays inside the string operand, no jump/skip relocation,
  ``shift`` is always 0. Two write modes: UTF-16 in place (op preserved,
  padded to the original unit count), or ANSI + opcode swap ``34``→``1f``
  (this engine's own EX_StringConst form, 1 byte/char, padded to
  ``2*units+1`` chars) when UTF-16 does not fit and the translation is
  pure ASCII. Anything longer is skipped with a reason, never truncated.
  The key framing is validated and never written.

``chars`` counts UTF-16 CODE UNITS (the FString length domain): astral
translation characters count 2, exactly as the game stores them.

Contract (:func:`patch_payload`):

- ``entries`` are ONE asset's TextEntry rows (mixed assets = caller bug,
  ValueError); ``translations`` maps entry_index -> text. ``None``/``""``
  means skip (blank-English = not-patched, the Phase 3 contract) and is
  reported under ``skipped`` — rows absent from ``translations`` are
  simply untouched.
- Validated BEFORE any byte is written, so a rejection leaves the caller's
  payload untouched: unsupported kinds (ValueError naming the kind),
  translations with unpaired surrogates or embedded NULs (cannot round-trip
  a UTF-16 FString), translation keys outside ``entries``, and each entry's
  frame re-verified against the payload (grammar prefix, stored length,
  NUL terminator) — a stale entry/payload pair is a named error, not
  silent corruption.
- Two patchable ranges that intersect = hard ValueError: the entries share
  ONE payload buffer and a double-written range is corruption by
  construction. Back-to-back ranges (a chained FString ending exactly
  where the next begins) are fine.
- Patches apply in DESCENDING offset order (splice from the back, earlier
  offsets stay valid). Null patch (``{}``) returns the payload unchanged —
  the Task 2 per-format null-patch goldens build on that.

Report: ``{"patched": [{entry_index, offset, old_chars, new_chars, shift,
new_source}], "skipped": [{entry_index, reason}]}`` — ``patched`` sorted
by offset; ``offset`` is the entry's ORIGINAL frame offset; ``shift`` is
this patch's byte delta (positive = growth), i.e. how far content AFTER
the patchable range moves.

:func:`rebase_entries` maps an entry list onto the patched payload: each
offset moves by the sum of shifts of patches at STRICTLY lower offsets
(patches never move their own frame start). Patched rows also carry their
``new_source`` so the rebased list is consistent with the patched bytes;
``property_path`` is left as-is (it is an export-data-relative diagnostic,
not a patch address — fresh re-scans produce fresh paths).
"""
from __future__ import annotations

import dataclasses
import struct

__all__ = ["patch_payload", "rebase_entries"]

_SUPPORTED = ("FText-Base", "FText-Ascii", "FString", "BcString", "ZenName")

# (length-field delta, UTF-16 body delta) from the entry's frame offset
_GEOMETRY = {"FText-Base": (42, 46), "FText-Ascii": (42, 46), "FString": (0, 4)}

_BCHEX = frozenset(b"0123456789ABCDEF")


def bc_write_mode(entry, text: str) -> "str | None":
    """``"utf16"`` / ``"ansi"`` / None (doesn't fit) for a BcString
    translation — the single source of the same-size fit rule, shared by
    :func:`patch_payload` and the exporter's pre-filter.

    EXACT-FILL semantics (boot test 9 lesson): in bytecode, execution
    resumes immediately after a string constant's NUL — there is no dead
    tail. A translation is therefore SPACE-PADDED to the original slot so
    the terminator lands exactly where the original one was; every
    changed byte stays inside the string operand and the following
    bytecode (the key const et al.) keeps its exact address."""
    units = len(entry.source)
    if len(text.encode("utf-16-le")) // 2 <= units:
        return "utf16"
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return None
    if len(text) <= 2 * units + 1:
        return "ansi"
    return None


def _verify_bc_site(payload: bytes, entry, entry_index: int) -> "tuple[int, int]":
    """(region_start, region_end) for a BcString entry: the ``29 01``
    prefix + opcode byte must sit exactly where the scan said, the decoded
    text must equal the scan's source, and the key framing must be intact
    and match ``entry.key``."""
    off = entry.offset
    if off < 2 or off >= len(payload) or payload[off] != 0x34 \
            or payload[off - 2:off] != b"\x29\x01":
        raise ValueError(
            f"entry {entry_index}: BcString opcode byte missing/changed at "
            f"offset {off} — entry does not match this payload")
    units = len(entry.source)
    end = off + 1 + 2 * (units + 1)
    if end + 34 > len(payload) or payload[end - 2:end] != b"\x00\x00":
        raise ValueError(
            f"entry {entry_index}: BcString body/terminator mismatch at "
            f"offset {off} — entry does not match this payload")
    if payload[off + 1:end - 2].decode("utf-16-le") != entry.source:
        raise ValueError(
            f"entry {entry_index}: BcString text mismatch at offset {off} "
            f"— entry does not match this payload")
    keyb = payload[end + 1:end + 33]
    if payload[end] != 0x1F or payload[end + 33] != 0 \
            or not all(c in _BCHEX for c in keyb) \
            or keyb.decode("ascii") != entry.key:
        raise ValueError(
            f"entry {entry_index}: BcString key framing mismatch at "
            f"offset {off} — entry does not match this payload")
    return off, end


def _verify_ascii_site(payload: bytes, entry, len_pos: int, old_end: int,
                       entry_index: int) -> None:
    """FText-Ascii frame verification: grammar prefix (flags/history/key),
    POSITIVE stored length ``len(source)+1``, single-byte NUL terminator."""
    if entry.offset < 0 or len_pos + 4 > len(payload):
        raise ValueError(
            f"entry {entry_index}: patchable range outside the "
            f"{len(payload)}-byte payload — entry/payload mismatch")
    try:
        key_ascii = entry.key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"entry {entry_index}: non-ASCII FText key {entry.key!r} "
            f"— malformed entry") from exc
    expected = (b"\x01\x00\x00\x00\x00" + struct.pack("<i", 33)
                + key_ascii + b"\x00")
    if payload[entry.offset:len_pos] != expected:
        raise ValueError(
            f"entry {entry_index}: FText grammar/key mismatch at offset "
            f"{entry.offset} — entry does not match this payload")
    (stored,) = struct.unpack_from("<i", payload, len_pos)
    if stored != len(entry.source) + 1:
        raise ValueError(
            f"entry {entry_index} (FText-Ascii at offset {entry.offset}): "
            f"stored length {stored} != {len(entry.source) + 1} — entry "
            f"does not match this payload")
    if old_end > len(payload) or payload[old_end - 1:old_end] != b"\x00":
        raise ValueError(
            f"entry {entry_index}: missing NUL terminator — entry does "
            f"not match this payload")


def _check_translatable(entry_index: int, text: str) -> None:
    """Reject texts that cannot round-trip a UTF-16 FString: unpaired
    surrogates (lone half, two highs, high at end) and embedded NULs (the
    terminator is appended by the writer; an inner NUL would truncate the
    string in-game)."""
    prev_high = False
    for pos, ch in enumerate(text):
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDBFF:               # high surrogate
            if prev_high:
                break
            prev_high = True
            continue
        if 0xDC00 <= cp <= 0xDFFF:               # low surrogate
            if not prev_high:
                break
            prev_high = False
            continue
        if cp == 0:
            raise ValueError(
                f"translation for entry {entry_index} contains an embedded "
                f"NUL at position {pos} — cannot be stored in an FString")
        prev_high = False
    else:
        if prev_high:
            raise ValueError(
                f"translation for entry {entry_index} ends with an unpaired "
                f"surrogate — cannot be encoded as UTF-16")
        return
    raise ValueError(
        f"translation for entry {entry_index} contains an unpaired "
        f"surrogate at position {pos} — cannot be encoded as UTF-16")


def _verify_site(payload: bytes, entry, len_pos: int, old_units: int,
                 old_end: int, entry_index: int) -> None:
    """The entry's frame must still be exactly where the scan said: the
    FText grammar prefix (flags/history/key), the stored negative length
    ``-(chars+1)``, and the NUL terminator. Anything else means the entry
    list does not belong to this payload — named error, no splicing."""
    if entry.offset < 0 or old_end > len(payload):
        raise ValueError(
            f"entry {entry_index} ({entry.kind} at offset {entry.offset}): "
            f"patchable range [{len_pos}, {old_end}) outside the "
            f"{len(payload)}-byte payload — entry/payload mismatch")
    if entry.kind == "FText-Base":
        try:
            key_ascii = entry.key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"entry {entry_index}: non-ASCII FText key {entry.key!r} "
                f"— malformed entry") from exc
        expected = (b"\x01\x00\x00\x00\x00" + struct.pack("<i", 33)
                    + key_ascii + b"\x00")
        if payload[entry.offset:len_pos] != expected:
            raise ValueError(
                f"entry {entry_index}: FText grammar/key mismatch at offset "
                f"{entry.offset} — entry does not match this payload")
    (stored,) = struct.unpack_from("<i", payload, len_pos)
    if stored != -(old_units + 1):
        raise ValueError(
            f"entry {entry_index} ({entry.kind} at offset {entry.offset}): "
            f"stored length {stored} != -({old_units}+1) — entry does not "
            f"match this payload")
    if struct.unpack_from("<H", payload, old_end - 2)[0] != 0:
        raise ValueError(
            f"entry {entry_index} ({entry.kind} at offset {entry.offset}): "
            f"missing NUL terminator — entry does not match this payload")


def patch_payload(payload: bytes, entries: list,
                  translations: "dict[int, str]") -> "tuple[bytes, dict]":
    """Patch ONE asset's payload. Returns ``(new_payload, report)`` — see
    the module docstring for the report schema and the validation
    contract. Raises ValueError (named, before any splicing) on unsupported
    kinds, unpaired surrogates / embedded NULs, out-of-range translation
    keys, mixed-asset entry lists, entry/payload mismatches, and
    overlapping patchable ranges."""
    assets = {e.asset for e in entries}
    if len(assets) > 1:
        raise ValueError(
            f"patch_payload patches one asset; got entries for "
            f"{sorted(assets)}")
    for i in translations:
        if not isinstance(i, int) or isinstance(i, bool) \
                or not 0 <= i < len(entries):
            raise ValueError(
                f"translations key {i!r} is not an entry index into this "
                f"asset's 0..{len(entries) - 1} rows")

    ops: list = []
    skipped: list = []
    for i, text in translations.items():
        if text is None or text == "":
            skipped.append({"entry_index": i, "reason": "blank translation"})
            continue
        entry = entries[i]
        if entry.kind not in _SUPPORTED:
            raise ValueError(
                f"entry {i}: unsupported kind {entry.kind!r} "
                f"(supported: {_SUPPORTED}) — no pinned patch geometry")
        _check_translatable(i, text)
        if entry.kind == "ZenName":
            if "/" in entry.source:
                skipped.append({
                    "entry_index": i,
                    "reason": "name contains a path separator — package "
                              "identity, never patched"})
                continue
            # zen name-batch storage: the string bytes ONLY (no inline
            # length, no NUL — the length header and CityHash live in
            # side tables that zen.patch_zen_package rewrites). ASCII
            # translations store as ascii (1 B/char), anything else as
            # UTF-16LE — the length header's sign follows.
            try:
                new_bytes = text.encode("ascii")
            except UnicodeEncodeError:
                new_bytes = text.encode("utf-16-le")
            ops.append({
                "entry_index": i, "entry": entry, "offset": entry.offset,
                "new_chars": len(text), "new_bytes": new_bytes,
                "new_source": text,
            })
            continue
        if entry.kind == "BcString":
            ops.append({
                "entry_index": i, "entry": entry, "offset": entry.offset,
                "bc_text": text, "new_bytes": None,      # geometry pass
                "new_source": text,
            })
            continue
        if entry.kind == "FText-Ascii":
            # source FString is sign-encoded: ASCII stays ANSI (positive
            # length); a non-ASCII translation flips the body to UTF-16
            # (negative length) — the game's FString reader takes either
            try:
                ascii_body = text.encode("ascii") + b"\x00"
                new_bytes = struct.pack("<i", len(ascii_body)) + ascii_body
            except UnicodeEncodeError:
                body = text.encode("utf-16-le") + b"\x00\x00"
                new_bytes = struct.pack("<i", -(len(body) // 2)) + body
            ops.append({
                "entry_index": i, "entry": entry, "offset": entry.offset,
                "new_chars": len(text), "new_bytes": new_bytes,
                "new_source": text,
            })
            continue
        body = text.encode("utf-16-le") + b"\x00\x00"
        ops.append({
            "entry_index": i,
            "entry": entry,
            "offset": entry.offset,
            "new_chars": len(body) // 2 - 1,            # units incl NUL - 1
            "new_bytes": struct.pack(
                "<i", -(len(body) // 2)) + body,        # -(units incl NUL)
            "new_source": text,
        })

    for op in ops:                       # geometry + site check vs ORIGINAL
        entry = op["entry"]
        if entry.kind == "BcString":
            start, end = _verify_bc_site(payload, entry, op["entry_index"])
            region = end - start
            mode = bc_write_mode(entry, op["bc_text"])
            if mode == "utf16":
                # space-pad to the ORIGINAL unit count: the terminator must
                # land exactly where the original one was (execution
                # resumes right after it — no dead tail exists)
                units = len(entry.source)
                padded = op["bc_text"] + " " * (units - len(op["bc_text"]))
                head = b"\x34" + padded.encode("utf-16-le") + b"\x00\x00"
            elif mode == "ansi":
                budget = 2 * len(entry.source) + 1
                padded = op["bc_text"] + " " * (budget - len(op["bc_text"]))
                head = b"\x1f" + padded.encode("ascii") + b"\x00"
            else:
                skipped.append({
                    "entry_index": op["entry_index"],
                    "reason": "translation too long for the bytecode "
                              "string slot (same-size rule)"})
                continue
            assert len(head) == region, "exact-fill invariant"
            op["len_pos"] = start
            op["old_end"] = end
            op["new_bytes"] = head
            op["shift"] = 0
            op["mode"] = mode
            op["old_chars"] = len(entry.source)
            op["new_chars"] = len(op["bc_text"])
            continue
        if entry.kind == "ZenName":
            # site re-verification: the entry's own geometry (charset +
            # byte length from the scan) must decode back to the scanned
            # source at exactly the recorded offset
            meta = entry.meta or {}
            enc, nbytes = meta.get("encoding"), meta.get("bytes")
            if enc not in ("ascii", "wide") or not isinstance(nbytes, int) \
                    or nbytes <= 0:
                raise ValueError(
                    f"entry {op['entry_index']}: ZenName entry carries no "
                    f"name-batch geometry — malformed entry")
            if entry.offset < 0 or entry.offset + nbytes > len(payload):
                raise ValueError(
                    f"entry {op['entry_index']}: ZenName range "
                    f"[{entry.offset:#x}, {entry.offset + nbytes:#x}) "
                    f"outside the {len(payload)}-byte payload")
            raw = payload[entry.offset:entry.offset + nbytes]
            try:
                current = raw.decode("ascii" if enc == "ascii"
                                     else "utf-16-le")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"entry {op['entry_index']}: ZenName bytes at "
                    f"{entry.offset:#x} do not decode as {enc} — entry "
                    f"does not match this payload") from exc
            if current != entry.source:
                raise ValueError(
                    f"entry {op['entry_index']}: ZenName content "
                    f"{current!r} != scanned {entry.source!r} — entry "
                    f"does not match this payload")
            op["old_chars"] = len(entry.source)
            op["len_pos"] = entry.offset
            op["old_end"] = entry.offset + nbytes
            op["shift"] = len(op["new_bytes"]) - nbytes
            continue
        len_d, body_d = _GEOMETRY[entry.kind]
        old_units = len(entry.source)                # scanned unit strings
        len_pos = entry.offset + len_d
        if entry.kind == "FText-Ascii":
            old_end = entry.offset + body_d + old_units + 1   # ANSI: 1B/char
            _verify_ascii_site(payload, entry, len_pos, old_end,
                               op["entry_index"])
        else:
            old_end = entry.offset + body_d + 2 * (old_units + 1)
            _verify_site(payload, entry, len_pos, old_units, old_end,
                         op["entry_index"])
        op["old_chars"] = old_units
        op["len_pos"] = len_pos
        op["old_end"] = old_end
        op["shift"] = len(op["new_bytes"]) - (old_end - len_pos)

    ops = [op for op in ops if op.get("new_bytes") is not None]

    ranges = sorted((op["len_pos"], op["old_end"], op["entry_index"])
                    for op in ops)
    for (s1, e1, i1), (s2, e2, i2) in zip(ranges, ranges[1:]):
        if s2 < e1:
            raise ValueError(
                f"patchable ranges overlap: entries {i1} [{s1:#x}, {e1:#x}) "
                f"and {i2} [{s2:#x}, {e2:#x}) share one payload")

    result = payload
    for op in sorted(ops, key=lambda o: -o["len_pos"]):
        result = result[:op["len_pos"]] + op["new_bytes"] \
            + result[op["old_end"]:]
    patched = sorted(
        ({"entry_index": op["entry_index"], "offset": op["offset"],
          "old_chars": op["old_chars"], "new_chars": op["new_chars"],
          "shift": op["shift"], "new_source": op["new_source"],
          **({"mode": op["mode"]} if "mode" in op else {})}
         for op in ops), key=lambda r: r["offset"])
    return result, {"patched": patched, "skipped": skipped}


def patch_span(entry, row: dict) -> "tuple[int, int, int]":
    """``(start, end, shift)`` of one patched row's rewrite region — the
    shared span math for the container fixup wrappers (zen/legacy export-
    map rewrites) and :func:`rebase_entries`. BcString regions include the
    opcode byte and never shift; FText-Ascii bodies are 1 byte/char;
    ZenName regions are the raw name bytes (entry.meta carries them)."""
    if entry.kind == "BcString":
        return (row["offset"],
                row["offset"] + 1 + 2 * (row["old_chars"] + 1),
                row["shift"])
    if entry.kind == "ZenName":
        return (row["offset"],
                row["offset"] + (entry.meta or {}).get("bytes", 0),
                row["shift"])
    len_d, body_d = _GEOMETRY[entry.kind]
    width = 1 if entry.kind == "FText-Ascii" else 2
    return (row["offset"] + len_d,
            row["offset"] + body_d + width * (row["old_chars"] + 1),
            row["shift"])


def rebase_entries(entries: list, report: dict) -> list:
    """New TextEntry list addressed to the patched payload: each offset
    moves by the sum of ``shift`` of patches at strictly lower offsets;
    patched rows also take their ``new_source``. Input list untouched."""
    patches = [(p["offset"], p["shift"]) for p in report.get("patched", ())]
    by_index = {p["entry_index"]: p for p in report.get("patched", ())}
    out = []
    for i, e in enumerate(entries):
        delta = sum(shift for off, shift in patches if off < e.offset)
        p = by_index.get(i)
        updates = {"offset": e.offset + delta}
        if p is not None and "new_source" in p:
            updates["source"] = p["new_source"]
        out.append(dataclasses.replace(e, **updates))
    return out
