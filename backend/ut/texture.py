"""Texture2D extraction & re-import for SEP image translation (Phase 5a).

SLRUnity-parity round-trip: dump every translatable Texture2D as PNG into
``<GameRoot>_unreal_img`` (plus manifest.json), the user translates the
images in an SEP project over that folder, and the rendered PNGs are
written back into rebuilt containers.

Cooked UTexture2D layout (UE 5.6, byte-verified on both test games — see
docs/plans/2026-10-23-phase5a-sep-images.md):

- The leading region of an export has VARIABLE layout (tagged-prop-like
  data mixed with the cooked header) and is never parsed structurally.
- M (mip count) == len(package bulk_map) == i32 @ (first inline mip
  payload - 8), with 0 at -12 and -4.
- Pixel format: the last ``PF_*`` FString (i32 length + NUL-terminated
  ASCII) before the first inline payload.
- Mip payloads: bulk_map[k] — entry flags 0x40 = inline (offset relative
  to the EXPORT's data slice), 0x100 = .ubulk file (offset inside the
  .ubulk payload, sequential).
- Dims: the 16-byte gap before each inline mip payload k>=1 is
  [SX SY SZ=1 bulkidx=k] holding mip k-1's dims; the 24 bytes after the
  last inline payload are [SX SY SZ][0][0][0] of the last mip; dims_k =
  2 x dims_{k+1} upward. mip sizes must equal the format's byte formula.
- Re-import with unchanged dimensions produces identical payload sizes:
  pure byte swaps, no structural edits, no bulk_map changes.

EF (legacy pak packages) has no bulk_map — legacy parsing is NOT
implemented yet (report, then extend after the EF probe).
"""
from __future__ import annotations

import json
import os
import struct

from . import bcdec, payload, pngio, uasset, zen
from .textscan import _script_objects

FLAG_INLINE = 0x40
_PF_SCAN_LIMIT = 65536      # the PF_ FString lives in the leading region
                            # (< 200B observed); bound the candidate scan so
                            # multi-MB inline payloads are never swept
FLAG_SEP_FILE = 0x100
_ALPHA_FORMATS = {"PF_B8G8R8A8", "PF_R8G8B8A8", "PF_DXT3", "PF_DXT5",
                  "PF_BC7", "PF_A8"}


class TextureError(ValueError):
    pass


# --------------------------------------------------------------------------
# parsing

def _mip_bytes(fmt: str, w: int, h: int) -> "int | None":
    try:
        bb, bpp = bcdec.format_info(fmt)
    except bcdec.BCError:
        return None
    if bb is not None:
        return ((w + 3) // 4) * ((h + 3) // 4) * bb
    return max(1, w) * max(1, h) * bpp


def _halve(w, h):
    return max(1, w // 2), max(1, h // 2)


def _pf_strings(data: bytes, limit: int) -> "list[tuple[int, str]]":
    """PF_* FStrings in the leading region. FString length prefixes here
    sit at arbitrary byte alignment, so candidate discovery jumps straight
    to the b"PF_" occurrences (bytes.find) instead of scanning every byte
    offset — export data with multi-MB inline mip payloads made the old
    byte-wise sweep 84% of total extraction time."""
    out = []
    lim = min(limit, len(data))
    start = 0
    while True:
        i = data.find(b"PF_", start, lim)
        if i < 0:
            return out
        start = i + 1
        p = i - 4                              # i32 length prefix
        if p < 0:
            continue
        (n,) = struct.unpack_from("<i", data, p)
        if 4 <= n <= 32 and p + 4 + n <= lim:
            s = data[p + 4:p + 4 + n]
            if s.endswith(b"\x00"):
                try:
                    out.append((p, s[:-1].decode("ascii")))
                except UnicodeDecodeError:
                    pass


def parse_texture(ex: "zen.ZenExport", bulk_map: list) -> dict:
    """Parse one cooked Texture2D export into

    ``{"export_index", "name", "fmt", "w", "h", "mips": [{storage, offset,
    size, w, h}], "data_len"}``.

    Cooked layout, byte-verified on UE 5.6 zen packages (both test games):

        [variable leading region: prop-like data + cooked header]
        [FString "PF_<FORMAT>"]                        <- pixel format
        i32 0 ; i32 M ; i32 first                      <- M == len(bulk_map)
        then for k = 0..M-1, in order:
            [inline payload of mip k, iff bulk_map[k] is inline]
            i32 SX_k ; i32 SY_k ; i32 SZ_k(=1) ; i32 next(=k+1, 0 at end)
        i32 0 ; i32 0                                  <- terminator

    Everything cross-validates (payload offsets == bulk_map serial_offsets,
    sizes == the format's byte formula, chain ends at data end), so the
    walk IS the parser: try each PF_ candidate as the format and keep the
    first walk that validates. Raises TextureError otherwise.
    """
    data = ex.data
    if not bulk_map:
        raise TextureError("no bulk map")
    m = len(bulk_map)
    fmts = _pf_strings(data, _PF_SCAN_LIMIT)
    if not fmts:
        raise TextureError("no PF_ format string")
    reason = "no validating walk"
    for _pos, fmt in reversed(fmts):
        if fmt not in bcdec._FORMATS:
            reason = f"unsupported format {fmt}"
            continue
        try:
            return _walk_texture(fmts, data, fmt, bulk_map, ex.object_name)
        except TextureError as e:
            reason = str(e)
    raise TextureError(reason)


def _walk_texture(fmts, data: bytes, fmt: str, bulk_map: list, name: str) -> dict:
    m = len(bulk_map)
    # walk from the END-most candidate that names this fmt
    starts = [p + 4 + len(f) + 1 for p, f in fmts if f == fmt]
    if not starts:
        raise TextureError(f"format {fmt} not found")
    last_err = "walk failed"
    for pos0 in reversed(starts):
        try:
            return _walk_from(data, pos0, fmt, bulk_map, name)
        except TextureError as e:
            last_err = str(e)
    raise TextureError(last_err)


# legacy (single-file pak packages, e.g. SampleGameB) -----------------------

def parse_texture_legacy(ex, ubulk_size: "int | None" = None) -> dict:
    """Parse one cooked Texture2D export of a LEGACY package (no bulk map).

    The byte grammar after the PF_ FString is the same as zen
    ([0][M][first] + per-mip [inline payload][SX SY SZ nxt] + [0][0]),
    but which mips are inline is NOT recorded — UE's cook puts the big
    mips in the .ubulk file and the small tail inline, so the split is
    the monotonic pair (j, M-j) for some j. The parser brute-forces j
    and keeps the split whose walk validates to end-of-data with exact
    size formulas. .ubulk payloads are sequential from offset 0; the
    caller passes ``ubulk_size`` and it must equal the sum of the ubulk
    mip sizes (checked when given, required before any byte patch).
    """
    data = ex.data
    fmts = _pf_strings(data, _PF_SCAN_LIMIT)
    if not fmts:
        raise TextureError("no PF_ format string")
    reason = "no validating walk"
    for _pos, fmt in reversed(fmts):
        if fmt not in bcdec._FORMATS:
            reason = f"unsupported format {fmt}"
            continue
        try:
            return _legacy_walk(fmts, data, fmt, ex.object_name, ubulk_size)
        except TextureError as e:
            reason = str(e)
    raise TextureError(reason)


def _legacy_walk(fmts, data: bytes, fmt: str, name: str,
                 ubulk_size: "int | None") -> dict:
    last_err = "no header triple"
    for pe, f2 in fmts:
        if f2 != fmt:
            continue
        end = pe + 4 + len(f2) + 1
        for off in range(0, 40, 4):
            if end + off + 12 > len(data):
                break
            z, m, first = struct.unpack_from("<iii", data, end + off)
            if z != 0 or first != 0 or not 0 < m <= 16:
                continue
            valid = []
            first_err = None
            for j in range(0, m + 1):
                try:
                    info = _legacy_walk_j(data, end + off, fmt, m, j, name,
                                          ubulk_size)
                    valid.append(info)
                except TextureError as e:
                    if first_err is None:
                        first_err = e
                    last_err = str(e)
            if len(valid) == 1:
                return valid[0]
            if len(valid) > 1:
                raise TextureError(
                    f"ambiguous layout: {len(valid)} mip splits validate")
            if first_err is not None:
                last_err = first_err
    raise TextureError(last_err)


def _legacy_walk_j(data: bytes, pos0: int, fmt: str, m: int, j: int,
                   name: str, ubulk_size: "int | None") -> dict:
    """Validate one (j, M) split by walking BACKWARD from the terminator.

    Forward walking cannot skip an inline payload (its size comes from the
    entry AFTER it), but backward every field is known: the terminator
    [0][0] anchors the last entry, each entry [SX SY SZ nxt] names the mip
    (nxt == k+1), and each inline payload sits immediately before its own
    entry with size = formula(entry dims). The walk must land exactly on
    the header triple.
    """
    pos = len(data)
    if pos < 8 + 12:
        raise TextureError("data too short")
    t1, t2 = struct.unpack_from("<ii", data, pos - 8)
    if t1 != 0 or t2 != 0:
        raise TextureError(f"bad terminator ({t1}, {t2})")
    pos -= 8
    entries = []
    inline_off = {}
    for k in range(m - 1, -1, -1):
        pos -= 16
        if pos < 0:
            raise TextureError("walk past data start")
        sx, sy, sz, nxt = struct.unpack_from("<iiii", data, pos)
        if sz != 1 or nxt != (k + 1 if k + 1 < m else 0):
            raise TextureError(f"mip {k}: bad entry ({sx}, {sy}, {sz}, {nxt})")
        if not 0 < sx <= 65536 or not 0 < sy <= 65536:
            raise TextureError(f"mip {k}: bad dims {sx}x{sy}")
        size = _mip_bytes(fmt, sx, sy)
        if size is None:
            raise TextureError(f"mip {k}: unsupported format {fmt}")
        if k >= j:                          # inline: payload precedes entry
            pos -= size
            if pos < 0:
                raise TextureError(f"mip {k}: payload out of data")
            inline_off[k] = pos
        entries.append((sx, sy, size))
    pos -= 12
    if pos != pos0:
        raise TextureError(f"walk landed at {pos}, triple at {pos0}")
    z, mc, first = struct.unpack_from("<iii", data, pos)
    if z != 0 or mc != m or first != 0:
        raise TextureError(f"bad header triple ({z}, {mc}, {first})")

    mips = []
    ubulk_off = 0
    for k, (sx, sy, size) in enumerate(reversed(entries)):
        if k < j:
            mips.append({"index": k, "storage": "ubulk", "offset": ubulk_off,
                         "size": size, "w": sx, "h": sy})
            ubulk_off += size
        else:
            mips.append({"index": k, "storage": "inline",
                         "offset": inline_off[k], "size": size,
                         "w": sx, "h": sy})
    if ubulk_size is not None and ubulk_off != ubulk_size:
        raise TextureError(f"ubulk size {ubulk_size} != sum of ubulk mips "
                           f"{ubulk_off}")
    return {"export_index": None, "name": name, "fmt": fmt,
            "w": mips[0]["w"], "h": mips[0]["h"], "mips": mips,
            "data_len": len(data)}


def _walk_from(data: bytes, pos: int, fmt: str, bulk_map: list, name: str) -> dict:
    m = len(bulk_map)

    def i32(p):
        if p + 4 > len(data):
            raise TextureError("walk past data end")
        return struct.unpack_from("<i", data, p)[0], p + 4

    z, pos = i32(pos)
    mip_count, pos = i32(pos)
    first, pos = i32(pos)
    if z == 0 and mip_count == 0:
        raise TextureError("virtual texture (zero-mip layout)")
    if z != 0 or mip_count != m or first != 0:
        raise TextureError(f"bad header triple ({z}, {mip_count}, {first})")
    mips = []
    for k in range(m):
        e = bulk_map[k]
        if e.flags & FLAG_INLINE:
            if pos != e.serial_offset:
                raise TextureError(
                    f"mip {k}: payload at {e.serial_offset}, walk at {pos}")
            if pos + e.serial_size > len(data):
                raise TextureError(f"mip {k}: payload out of data")
            storage = "inline"
        elif e.flags & FLAG_SEP_FILE:
            storage = "ubulk"
        else:
            raise TextureError(f"mip {k}: unknown bulk flags {e.flags:#x}")
        if storage == "inline":
            pos += e.serial_size
        sx, pos = i32(pos)
        sy, pos = i32(pos)
        sz, pos = i32(pos)
        nxt, pos = i32(pos)
        if sz != 1 or nxt != (k + 1 if k + 1 < m else 0):
            raise TextureError(f"mip {k}: bad entry ({sx}, {sy}, {sz}, {nxt})")
        if not 0 < sx <= 65536 or not 0 < sy <= 65536:
            raise TextureError(f"mip {k}: bad dims {sx}x{sy}")
        want = _mip_bytes(fmt, sx, sy)
        if want != e.serial_size:
            raise TextureError(f"mip {k}: size {e.serial_size} != formula "
                               f"{want} ({fmt} {sx}x{sy})")
        mips.append({"index": k, "storage": storage,
                     "offset": e.serial_offset, "size": e.serial_size,
                     "w": sx, "h": sy})
    t1, pos = i32(pos)
    t2, pos = i32(pos)
    if t1 != 0 or t2 != 0 or pos != len(data):
        raise TextureError(f"bad terminator or trailing bytes "
                           f"({t1}, {t2}, {len(data) - pos} left)")
    return {"export_index": None, "name": name, "fmt": fmt,
            "w": mips[0]["w"], "h": mips[0]["h"], "mips": mips,
            "data_len": len(data)}


def _has_alpha(fmt: str) -> bool:
    return fmt in _ALPHA_FORMATS


def _is_candidate(w: int, h: int, fmt: str) -> bool:
    """SLRUnity's likely-UI-text heuristic, ported to UE format names."""
    if w <= 0 or h <= 0:
        return False
    if w * h > 1024 * 1024:
        return False
    if w == h and w > 0 and (w & (w - 1)) == 0:
        return False
    return _has_alpha(fmt)


def _sanitize(s: str, limit: int = 140) -> str:
    base = "".join(c if c.isalnum() or c in "-_." else "_" for c in (s or ""))
    return (base[:limit] or "x")


def png_name(pkg: str, name: str, export_index: int) -> str:
    return f"{_sanitize(pkg)}__{_sanitize(name)}__{export_index}.png"


# --------------------------------------------------------------------------
# extraction

def _iter_textures(src: payload.PayloadSources, progress=None):
    """Yield (pkg_name, pkg, export_index, ex, info) for every parseable
    Texture2D export. Zen packages walk with the bulk map; legacy packages
    (EF: .uasset/.uexp pairs in a pak) brute-force the inline/ubulk split
    and verify it against the .ubulk file size."""
    names = [f for f in payload.list_files(src)
             if f.lower().endswith(".uasset")]
    script = _script_objects(src) if src.get("io_pairs") else None
    total = len(names)
    for done, n in enumerate(names):
        if progress:
            progress(done, total, n)
        try:
            if src.get("io_pairs"):
                pkg = zen.parse_zen(payload.read_file(src, n), n,
                                    script_objects=script)
            else:
                stem = n[:-len(".uasset")]
                pkg = uasset.parse_legacy(payload.read_file(src, n),
                                          payload.read_file(src,
                                                            stem + ".uexp"),
                                          stem + ".uexp")
        except Exception:
            continue
        for i, ex in enumerate(pkg.exports):
            if ex.class_name != "Texture2D":
                continue
            try:
                if src.get("io_pairs"):
                    info = parse_texture(ex, pkg.bulk_map)
                else:
                    ub = n[:-len(".uasset")] + ".ubulk"
                    ub_size = payload.file_size(src, ub) if _has(src, ub) \
                        else 0
                    info = parse_texture_legacy(ex, ub_size)
            except TextureError:
                continue
            yield n, pkg, i, ex, info


def _has(src: payload.PayloadSources, name: str) -> bool:
    return name in payload._ensure(src)


def extract_images(game_root: str, out_dir: str, aes_key=None, progress=None,
                   max_area: "int | None" = None) -> dict:
    """Write one PNG per Texture2D mip-0 + manifest.json into out_dir."""
    src = payload.find_sources(game_root, aes_key=aes_key)
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    errors = []
    for n, pkg, i, ex, info in _iter_textures(src, progress):
        if max_area and info["w"] * info["h"] > max_area:
            continue
        try:
            rgba = read_mip(src, n, ex, info, 0)
        except TextureError as e:
            errors.append(f"{n}#{i}: {e}")
            continue
        png = pngio.encode_png(info["w"], info["h"], bytes(rgba))
        pname = png_name(n, info["name"], i)
        with open(os.path.join(out_dir, pname), "wb") as f:
            f.write(png)
        manifest.append({
            "pkg": n, "export_index": i, "name": info["name"],
            "type": "Texture2D", "width": info["w"], "height": info["h"],
            "format": info["fmt"], "mips": len(info["mips"]),
            "png": pname,
            "candidate": _is_candidate(info["w"], info["h"], info["fmt"]),
        })
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return {"count": len(manifest), "errors": errors, "out": out_dir}


def read_mip(src: payload.PayloadSources, pkg_name: str, ex: "zen.ZenExport",
             info: dict, mip_index: int) -> bytearray:
    """Decode one mip to RGBA."""
    m = info["mips"][mip_index]
    if m["storage"] == "inline":
        blob = ex.data[m["offset"]:m["offset"] + m["size"]]
    else:
        ub = pkg_name[:-len(".uasset")] + ".ubulk"
        raw = payload.read_file(src, ub)
        blob = raw[m["offset"]:m["offset"] + m["size"]]
        if len(blob) != m["size"]:
            raise TextureError(f"{pkg_name}: ubulk mip {mip_index} short "
                               f"({len(blob)} < {m['size']})")
    return bcdec.decode_texture(info["fmt"], m["w"], m["h"], blob)


# --------------------------------------------------------------------------
# re-import

def _box_down(rgba: bytes, w: int, h: int) -> bytes:
    """2x2 box downsample to the next mip (UE's simple average). Interior
    pixels take a hoisted fast path; odd edges average the surviving
    pixels only. Same floor-division averages as before."""
    dw, dh = _halve(w, h)
    out = bytearray(dw * dh * 4)
    w4 = w * 4
    for y in range(dh):
        y2 = y * 2
        full_y = y2 + 1 < h
        r0 = y2 * w4
        r1 = r0 + w4
        o = y * dw * 4
        for x in range(dw):
            x2 = x * 2
            if full_y and x2 + 1 < w:
                i0 = r0 + x2 * 4
                i1 = i0 + 4
                i2 = r1 + x2 * 4
                i3 = i2 + 4
                out[o] = (rgba[i0] + rgba[i1] + rgba[i2] + rgba[i3]) >> 2
                out[o + 1] = (rgba[i0 + 1] + rgba[i1 + 1] + rgba[i2 + 1]
                              + rgba[i3 + 1]) >> 2
                out[o + 2] = (rgba[i0 + 2] + rgba[i1 + 2] + rgba[i2 + 2]
                              + rgba[i3 + 2]) >> 2
                out[o + 3] = (rgba[i0 + 3] + rgba[i1 + 3] + rgba[i2 + 3]
                              + rgba[i3 + 3]) >> 2
            else:
                rs = gs = bs = asn = n = 0
                for yy in range(y2, min(h, y2 + 2)):
                    row = yy * w4
                    for xx in range(x2, min(w, x2 + 2)):
                        i = row + xx * 4
                        rs += rgba[i]
                        gs += rgba[i + 1]
                        bs += rgba[i + 2]
                        asn += rgba[i + 3]
                        n += 1
                out[o] = rs // n
                out[o + 1] = gs // n
                out[o + 2] = bs // n
                out[o + 3] = asn // n
            o += 4
    return bytes(out)


def import_images(game_root: str, manifest_dir: str, png_dir: str,
                  aes_key=None, progress=None) -> dict:
    """Stage patched package payloads for every manifest entry whose PNG
    exists in ``png_dir`` (SEP's render folder — force-import semantics).

    Returns ``{"staged": {payload name: bytes}, "imported", "skipped",
    "errors"}`` — staging only; the caller rebuilds containers
    (``_stage_groups`` + ``rewrite``, same as text export). Rendered PNGs
    must match the extracted dimensions exactly.
    """
    src = payload.find_sources(game_root, aes_key=aes_key)
    legacy = not src.get("io_pairs")
    with open(os.path.join(manifest_dir, "manifest.json"), "r",
              encoding="utf-8") as f:
        manifest = json.load(f)

    by_pkg: "dict[str, list[dict]]" = {}
    for e in manifest:
        p = os.path.join(png_dir, e["png"])
        if os.path.isfile(p):
            by_pkg.setdefault(e["pkg"], []).append(e)

    script = _script_objects(src)
    staged: "dict[str, bytes]" = {}
    imported = skipped = 0
    errors: "list[str]" = []
    pkgs = sorted(by_pkg)
    for done, pkg_name in enumerate(pkgs):
        if progress:
            progress(done, len(pkgs), pkg_name)
        entries = by_pkg[pkg_name]
        stem = pkg_name[:-len(".uasset")]
        try:
            if legacy:
                raw = payload.read_file(src, pkg_name)          # .uasset
                uexp = payload.read_file(src, stem + ".uexp")
                pkg = uasset.parse_legacy(raw, uexp, stem + ".uexp")
            else:
                raw = payload.read_file(src, pkg_name)
                pkg = zen.parse_zen(raw, pkg_name, script_objects=script)
        except Exception as exc:
            errors.append(f"{pkg_name}: {exc!r}")
            continue
        patched_pkg = bytearray(raw)
        if legacy:
            patched_uexp = bytearray(uexp)
        touched = False
        for e in entries:
            i = e["export_index"]
            if not 0 <= i < len(pkg.exports):
                errors.append(f"{pkg_name}#{i}: export index out of range")
                continue
            ex = pkg.exports[i]
            if ex.class_name != "Texture2D":
                errors.append(f"{pkg_name}#{i}: not a Texture2D any more")
                continue
            try:
                if legacy:
                    if pkg.total_header_size != len(raw):
                        raise TextureError(
                            "export data layout not pinned (total header "
                            "size != len(.uasset)) — refusing to patch")
                    ub = stem + ".ubulk"
                    ub_size = payload.file_size(src, ub) if _has(src, ub) else 0
                    info = parse_texture_legacy(ex, ub_size)
                else:
                    info = parse_texture(ex, pkg.bulk_map)
            except TextureError as exc:
                errors.append(f"{pkg_name}#{i}: {exc}")
                continue
            try:
                with open(os.path.join(png_dir, e["png"]), "rb") as f:
                    pw, ph, rgba = pngio.decode_png(f.read())
            except (OSError, pngio.PngError) as exc:
                errors.append(f"{e['png']}: {exc}")
                continue
            if (pw, ph) != (info["w"], info["h"]):
                errors.append(
                    f"{e['png']}: rendered {pw}x{ph} does not match the "
                    f"texture {info['w']}x{info['h']} — resize it to the "
                    "original dimensions")
                continue
            # base = position of the export's data slice inside the package
            if legacy:
                base = ex.serial_offset - pkg.total_header_size
                if base < 0 or base + len(ex.data) > len(uexp):
                    errors.append(f"{pkg_name}#{i}: export slice out of .uexp")
                    continue
                cur = patched_uexp
            else:
                base = pkg.header_size + ex.serial_offset
                if base + len(ex.data) > len(raw):
                    errors.append(f"{pkg_name}#{i}: export slice out of package")
                    continue
                cur = patched_pkg
            ok = True
            px = rgba
            lw, lh = info["w"], info["h"]
            for m in info["mips"]:
                if m["index"] > 0:
                    px = _box_down(px, lw, lh)
                    lw, lh = _halve(lw, lh)
                if (lw, lh) != (m["w"], m["h"]):
                    errors.append(f"{pkg_name}#{i} mip {m['index']}: dims "
                                  f"chain broke ({lw}x{lh} vs {m['w']}x{m['h']})")
                    ok = False
                    break
                enc = bcdec.encode_texture(info["fmt"], m["w"], m["h"], px)
                if len(enc) != m["size"]:
                    errors.append(f"{pkg_name}#{i} mip {m['index']}: "
                                  f"encoded {len(enc)}B != {m['size']}B")
                    ok = False
                    break
                if m["storage"] == "inline":
                    # zen: payload lives in the package payload at
                    # base+offset; legacy: in the .uexp at base+offset
                    dst = base + m["offset"]
                    if dst + m["size"] > len(cur):
                        errors.append(f"{pkg_name}#{i}: payload out of package")
                        ok = False
                        break
                    cur[dst:dst + m["size"]] = enc
                else:
                    ub = stem + ".ubulk"
                    ubase = bytearray(staged.get(ub)
                                      or payload.read_file(src, ub))
                    if m["offset"] + m["size"] > len(ubase):
                        errors.append(f"{pkg_name}#{i}: ubulk too small")
                        ok = False
                        break
                    ubase[m["offset"]:m["offset"] + m["size"]] = enc
                    staged[ub] = bytes(ubase)
            if not ok:
                continue
            touched = True
            imported += 1
        if touched:
            if legacy:
                staged[stem + ".uexp"] = bytes(patched_uexp)
            else:
                staged[pkg_name] = bytes(patched_pkg)
        else:
            skipped += len(entries)
    return {"staged": staged, "imported": imported, "skipped": skipped,
            "errors": errors}


def write_containers(game_root: str, staged: "dict[str, bytes]", out_dir: str,
                     aes_key=None) -> "list[str]":
    """Rebuild the containers holding ``staged`` payloads into ``out_dir``
    (mirrored game-relative layout, atomic writers — same machinery as the
    text export). Returns the absolute output paths."""
    from . import rewrite
    from .exporttext import _stage_groups      # private-seam precedent
    os.makedirs(out_dir, exist_ok=True)
    src = payload.find_sources(game_root, aes_key=aes_key)
    containers: "list[str]" = []
    for key, group in sorted(_stage_groups(src, staged).items()):
        if key[0] == "pak":
            plan = rewrite.plan_rewrite(game_root, key[1],
                                        group["replacements"], aes_key=aes_key)
        else:
            plan = rewrite.plan_rewrite(game_root, key[1],
                                        group["replacements"])
        rewrite.execute_rewrite(plan, out_dir)
        containers.extend(plan["outputs"])
    return containers
