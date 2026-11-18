"""Font replacement (Phase 5b): the SLRUnity "Replace game fonts"
contract on UE games — a two-role swap of the games' packaged font
sidecars, with no game asset structure touched.

UE-side shape (discovery 2026-10-24, both reference games, plan doc
docs/plans/2026-10-25-phase5b-fonts.md):

- Cooked FontFace assets carry NO font bytes (the exports are 48-190B of
  editor-time source paths + flags, tagless) and NO RuntimeData glyph
  cache — the game rasterizes glyphs at runtime via FreeType. The actual
  font files ship as `.ufont` sidecars (UE 5.4+ packaged font files),
  one per FontFace, keyed by package name. Swapping a sidecar's bytes
  therefore changes what the game renders — the UE analog of SLRUnity's
  embedded-font swap, and the TMP atlas re-bake has no UE counterpart
  because there are no frozen atlases.
- `.ufont` layout, detected from the original bytes and preserved on
  replacement (the writer never assumes one variant):
  ``raw``      — the font file (sfnt: TTF/OTF/TTC) from byte 0 (EF);
  ``prefixed`` — u32 LE size + font + u32 trailer 0 (ST; the font starts
  at byte 4 — verified across all 8 ST sidecars, trailer is zero in
  every shipped file, so zeros are accepted by construction).
  Anything else (e.g. a future compressed variant) is skipped with a
  logged reason rather than repacked blindly.
- Roles (SLRUnity parity): game-owned faces (package path outside
  /Engine/) get the dialogue font (SLR's CJK-capable face — untranslated
  Japanese must keep rendering); engine faces get the UI font (SLR's
  narrow Latin face) EXCEPT DroidSansFallback, the engine's CJK fallback,
  which gets the dialogue font for the same tofu reason.

Staging only: :func:`stage_font_swaps` returns payload-layer entries for
:func:`texture.write_containers` / ``exporttext``'s single container
rebuild (Phase 4a ruling — separate rebuilds of one container overwrite
each other, so fonts ride the flow's ONE rebuild).
"""
from __future__ import annotations

from . import payload

__all__ = ["FontError", "read_font", "scan_fonts", "stage_font_swaps"]

_SFNT_MAGICS = (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"true")


class FontError(ValueError):
    """Named rejection: unreadable font file or un-swappable .ufont."""


def _is_sfnt(b: bytes) -> bool:
    return b[:4] in _SFNT_MAGICS


def read_font(path: str) -> bytes:
    """Read + validate a user-supplied (or SLR-default) font file.

    Validation is deliberately shallow — sfnt magic + sane size; the game
    owns everything deeper (and FreeType rejects broken fonts per-face at
    runtime, falling back along the composite chain)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise FontError(f"font file unreadable: {path}: {e}") from None
    if len(data) < 1000:
        raise FontError(f"font file too small ({len(data)}B): {path}")
    if not _is_sfnt(data):
        raise FontError(
            f"not a .ttf/.otf/.ttc font (bad sfnt magic "
            f"{data[:4].hex()}): {path}")
    return data


def _detect_ufont_format(data: bytes) -> "str | None":
    """``"raw"`` / ``"prefixed"`` / None (unknown — never repacked)."""
    if _is_sfnt(data):
        return "raw"
    if len(data) >= 12:
        size = int.from_bytes(data[:4], "little")
        if size == len(data) - 8 and _is_sfnt(data[4:8]):
            return "prefixed"
    return None


def _role(ufont_name: str) -> str:
    low = ufont_name.replace("\\", "/").lower()
    if "droidsansfallback" in low:
        return "dialogue"                 # engine's CJK fallback
    if low.startswith("engine/"):
        return "ui"
    return "dialogue"                     # game-owned face


def scan_fonts(src: payload.PayloadSources) -> "list[dict]":
    """One row per `.ufont` sidecar in the game:

    ``{package, role, ufont, size, format}`` — ``package`` is the ufont
    path minus the extension (the FontFace package it belongs to),
    ``format`` is the detected sidecar layout (None = unknown)."""
    rows = []
    for name in payload.list_files(src):
        if not name.lower().endswith(".ufont"):
            continue
        data = payload.read_file(src, name)
        rows.append({"package": name[:-len(".ufont")], "role": _role(name),
                     "ufont": name, "size": len(data),
                     "format": _detect_ufont_format(data)})
    return sorted(rows, key=lambda r: r["ufont"])


def _repackage(fmt: str, font: bytes) -> bytes:
    if fmt == "prefixed":
        return (len(font).to_bytes(4, "little") + font
                + b"\x00\x00\x00\x00")
    return font


def stage_font_swaps(src: payload.PayloadSources,
                     dialogue_font: "bytes | None" = None,
                     ui_font: "bytes | None" = None,
                     progress=None) -> "tuple[dict, dict]":
    """Stage a `.ufont` replacement for every face whose role has a font.

    Returns ``(staged, report)`` — ``staged`` maps payload-layer ufont
    names to the replacement bytes (format-preserving; entry sizes change
    freely, both container writers re-store/append), ``report`` is
    ``{"swapped": [rows], "skipped": [rows]}`` with one row per face for
    the console summary. Roles without a font (or unknown sidecar
    formats, or faces when BOTH fonts are None) are skipped, never fatal.
    """
    staged: "dict[str, bytes]" = {}
    swapped: "list[dict]" = []
    skipped: "list[dict]" = []
    faces = scan_fonts(src)
    for done, row in enumerate(faces):
        if progress:
            progress(done, len(faces), row["ufont"])
        font = dialogue_font if row["role"] == "dialogue" else ui_font
        if font is None:
            skipped.append({"ufont": row["ufont"], "role": row["role"],
                            "reason": f"no {row['role']} font provided"})
            continue
        if row["format"] is None:
            skipped.append({"ufont": row["ufont"], "role": row["role"],
                            "reason": "unknown .ufont layout (compressed "
                                      "or non-sfnt) — left original"})
            continue
        data = _repackage(row["format"], font)
        staged[row["ufont"]] = data
        swapped.append({"ufont": row["ufont"], "role": row["role"],
                        "package": row["package"], "format": row["format"],
                        "old_size": row["size"], "new_size": len(data)})
    return staged, {"swapped": swapped, "skipped": skipped}
