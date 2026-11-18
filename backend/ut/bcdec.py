"""Block-compression codecs, stdlib-only: BC1/BC2/BC3/BC4/BC5/BC7 + raw.

Decode follows DirectXTex (microsoft/DirectXTex BC6HBC7.cpp / BC.cpp) and
the Microsoft BC7 mode reference; BC7 partition/fixup tables live in
_bc7tables.py (auto-generated from the same source). Encoders are
first-party deterministic approximations (palette build + nearest-index
assignment) — round-trip fidelity is judged on decode(encode(decode(x))),
not byte identity with Epic/DirectXTex output.

All decode/encode operate on 4x4 pixel blocks in RGBA byte order
(r,g,b,a per pixel). Entry points:
  decode_texture(fmt_name, w, h, data) -> bytearray (w*h*4)
  encode_texture(fmt_name, w, h, rgba) -> bytes
  mip_dims(w, h, level), format_info(fmt_name) -> (bytes_per_4x4_block
  or None for raw, bytes_per_pixel)
"""
from __future__ import annotations

from ._bc7tables import FIXUP_TABLE, PARTITION_TABLE

WEIGHTS2 = (0, 21, 43, 64)
WEIGHTS3 = (0, 9, 18, 27, 37, 46, 55, 64)
WEIGHTS4 = (0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64)


class BCError(ValueError):
    pass


# format name -> (block bytes for compressed, bytes/pixel for raw; other is None)
_FORMATS = {
    "PF_DXT1": (8, None),
    "PF_DXT3": (16, None),
    "PF_DXT5": (16, None),
    "PF_BC4": (8, None),
    "PF_BC5": (16, None),
    "PF_BC7": (16, None),
    "PF_B8G8R8A8": (None, 4),
    "PF_R8G8B8A8": (None, 4),
    "PF_G8": (None, 1),
    "PF_A8": (None, 1),
}


def format_info(fmt: str) -> "tuple[int | None, int | None]":
    if fmt not in _FORMATS:
        raise BCError(f"unsupported pixel format {fmt!r}")
    return _FORMATS[fmt]


def mip_dims(w: int, h: int, level: int) -> "tuple[int, int]":
    return max(1, w >> level), max(1, h >> level)


def decode_texture(fmt: str, w: int, h: int, data: bytes) -> bytearray:
    bb, bpp = format_info(fmt)
    out = bytearray(w * h * 4)
    if bb is None:
        _decode_raw(fmt, w, h, data, out)
        return out
    bw, bh = (w + 3) // 4, (h + 3) // 4
    dec = _DECODERS[fmt]
    # interior blocks write one joined 16-byte run per output row instead of
    # 16 per-pixel slices — the same bytes, ~3x fewer Python operations.
    # Edge blocks keep the clamped per-pixel path.
    for by in range(bh):
        y0 = by * 4
        row_base = y0 * w * 4
        full_y = y0 + 4 <= h
        for bx in range(bw):
            off = (by * bw + bx) * bb
            if off + bb > len(data):
                raise BCError(f"block data short at {off}")
            px = dec(data[off:off + bb])
            if full_y and bx * 4 + 4 <= w:
                o = row_base + bx * 16
                out[o:o + 16] = px[0] + px[1] + px[2] + px[3]
                o += w * 4
                out[o:o + 16] = px[4] + px[5] + px[6] + px[7]
                o += w * 4
                out[o:o + 16] = px[8] + px[9] + px[10] + px[11]
                o += w * 4
                out[o:o + 16] = px[12] + px[13] + px[14] + px[15]
                continue
            for py in range(4):
                yy = y0 + py
                if yy >= h:
                    break
                row = yy * w
                for pxx in range(4):
                    xx = bx * 4 + pxx
                    if xx >= w:
                        break
                    i = (row + xx) * 4
                    out[i:i + 4] = px[py * 4 + pxx]
    return out


def encode_texture(fmt: str, w: int, h: int, rgba: bytes) -> bytes:
    bb, bpp = format_info(fmt)
    if bb is None:
        return _encode_raw(fmt, w, h, rgba)
    if len(rgba) != w * h * 4:
        raise BCError("rgba buffer size mismatch")
    bw, bh = (w + 3) // 4, (h + 3) // 4
    enc = _ENCODERS[fmt]
    out = bytearray()
    w4 = w * 4
    for by in range(bh):
        y0 = by * 4
        row_base = y0 * w4
        full_y = y0 + 4 <= h
        for bx in range(bw):
            if full_y and bx * 4 + 4 <= w:
                # interior: 4 row slices, 16 subslices, no clamping math
                o = row_base + bx * 16
                r0 = rgba[o:o + 16]
                r1 = rgba[o + w4:o + w4 + 16]
                r2 = rgba[o + 2 * w4:o + 2 * w4 + 16]
                r3 = rgba[o + 3 * w4:o + 3 * w4 + 16]
                px = (r0[0:4], r0[4:8], r0[8:12], r0[12:16],
                      r1[0:4], r1[4:8], r1[8:12], r1[12:16],
                      r2[0:4], r2[4:8], r2[8:12], r2[12:16],
                      r3[0:4], r3[4:8], r3[8:12], r3[12:16])
            else:
                px = []
                for py in range(4):
                    for pxx in range(4):
                        xx = min(w - 1, bx * 4 + pxx)
                        yy = min(h - 1, y0 + py)
                        i = (yy * w + xx) * 4
                        px.append(rgba[i:i + 4])
            out += enc(px)
    return bytes(out)


# --- raw formats -----------------------------------------------------------

def _decode_raw(fmt, w, h, data, out):
    n = w * h
    if len(data) < n * _FORMATS[fmt][1]:
        raise BCError(f"raw data short: {len(data)} < {n * _FORMATS[fmt][1]}")
    if fmt == "PF_B8G8R8A8":
        # strided slice swap: C speed instead of a per-pixel Python loop
        out[0::4] = data[2::4]
        out[1::4] = data[1::4]
        out[2::4] = data[0::4]
        out[3::4] = data[3::4]
    elif fmt == "PF_R8G8B8A8":
        out[:n * 4] = data[:n * 4]
    elif fmt == "PF_G8":
        out[0::4] = data[:n]
        out[1::4] = data[:n]
        out[2::4] = data[:n]
        out[3::4] = b"\xff" * n
    else:                                            # PF_A8: alpha only
        out[0::4] = b"\x00" * n
        out[1::4] = b"\x00" * n
        out[2::4] = b"\x00" * n
        out[3::4] = data[:n]


def _encode_raw(fmt, w, h, rgba):
    n = w * h
    if len(rgba) != n * 4:
        raise BCError("rgba buffer size mismatch")
    if fmt == "PF_B8G8R8A8":
        out = bytearray(n * 4)
        out[0::4] = rgba[2::4]
        out[1::4] = rgba[1::4]
        out[2::4] = rgba[0::4]
        out[3::4] = rgba[3::4]
        return bytes(out)
    if fmt == "PF_R8G8B8A8":
        return bytes(rgba[:n * 4])
    if fmt == "PF_G8":
        return bytes(rgba[0::4])
    return bytes(rgba[3::4])                          # PF_A8


# --- BC1/2/3/4/5 -----------------------------------------------------------

def _rgb565(v):
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _to565(r, g, b):
    return (((r * 31 + 127) // 255) << 11) | (((g * 63 + 127) // 255) << 5) \
        | ((b * 31 + 127) // 255)


def _decode_bc1_alpha(block):
    c0 = block[0] | (block[1] << 8)
    c1 = block[2] | (block[3] << 8)
    p0, p1 = _rgb565(c0), _rgb565(c1)
    if c0 > c1:                                    # 4-color (opaque) mode
        r0, g0, b0 = p0
        r1, g1, b1 = p1
        table = (bytes((r0, g0, b0, 255)), bytes((r1, g1, b1, 255)),
                 bytes(((2 * r0 + r1) // 3, (2 * g0 + g1) // 3,
                        (2 * b0 + b1) // 3, 255)),
                 bytes(((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3,
                        (b0 + 2 * b1) // 3, 255)))
    else:                                          # 3-color (punchthrough)
        r0, g0, b0 = p0
        r1, g1, b1 = p1
        table = (bytes((r0, g0, b0, 255)), bytes((r1, g1, b1, 255)),
                 bytes(((r0 + r1) >> 1, (g0 + g1) >> 1, (b0 + b1) >> 1, 255)),
                 bytes((0, 0, 0, 0)))
    idx = int.from_bytes(block[4:8], "little")
    return [table[(idx >> (i * 2)) & 3] for i in range(16)]


_SQ = [i * i for i in range(-255, 256)]     # squared-difference LUT


def _encode_bc1_color_block(px, allow_three):
    """16 RGBA tuples -> 8-byte BC1 color block.

    The nearest-palette search is unrolled with locals over a
    squared-difference LUT (no per-pixel generator/list churn) — this is
    the hot loop of re-importing big compressed textures. Tie-breaking
    matches the original loop: the FIRST palette index with a strictly
    smaller distance wins.
    """
    has_alpha = False
    seen_opaque = False
    rmin = gmin = bmin = 255
    rmax = gmax = bmax = 0
    for p in px:
        r, g, b, a = p
        if a < 128:
            has_alpha = True
            continue
        seen_opaque = True
        if r < rmin: rmin = r
        if r > rmax: rmax = r
        if g < gmin: gmin = g
        if g > gmax: gmax = g
        if b < bmin: bmin = b
        if b > bmax: bmax = b
    if not seen_opaque:                  # all transparent: bbox = black
        rmin = gmin = bmin = rmax = gmax = bmax = 0
    c0v = _to565(rmax, gmax, bmax)
    c1v = _to565(rmin, gmin, bmin)
    if c0v == c1v:                       # flat block: single color, idx 0
        # c0 == c1 decodes as 3-color mode but every index is 0, which is
        # endpoint 0 in both modes — harmless. Transparent pixels still
        # get index 3 when the 3-color mode is available.
        idx = 0
        if allow_three and has_alpha:
            for i, p in enumerate(px):
                if p[3] < 128:
                    idx |= 3 << (i * 2)
        return bytes((c0v & 0xFF, c0v >> 8, c1v & 0xFF, c1v >> 8,
                      idx & 0xFF, (idx >> 8) & 0xFF, (idx >> 16) & 0xFF,
                      (idx >> 24) & 0xFF))
    three = allow_three and has_alpha
    if three and c0v < c1v:              # 3-color mode requires c0 >= c1
        c0v, c1v = c1v, c0v
    elif not three and c0v < c1v:        # 4-color mode requires c0 > c1
        c0v, c1v = c1v, c0v
    r0, g0, b0 = _rgb565(c0v)
    r1, g1, b1 = _rgb565(c1v)
    SQ = _SQ
    O = 255                              # LUT bias for negative differences
    idx = 0
    shift = 0
    if three:
        r2, g2, b2 = (r0 + r1) >> 1, (g0 + g1) >> 1, (b0 + b1) >> 1
        for p in px:
            r, g, b, a = p
            if a < 128:
                k = 3
            else:
                d0 = SQ[r - r0 + O] + SQ[g - g0 + O] + SQ[b - b0 + O]
                d1 = SQ[r - r1 + O] + SQ[g - g1 + O] + SQ[b - b1 + O]
                d2 = SQ[r - r2 + O] + SQ[g - g2 + O] + SQ[b - b2 + O]
                best, k = d0, 0
                if d1 < best:
                    best, k = d1, 1
                if d2 < best:
                    k = 2
            idx |= k << shift
            shift += 2
    else:
        r2, g2, b2 = (2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3
        r3, g3, b3 = (r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3
        for p in px:
            r, g, b, a = p
            d0 = SQ[r - r0 + O] + SQ[g - g0 + O] + SQ[b - b0 + O]
            d1 = SQ[r - r1 + O] + SQ[g - g1 + O] + SQ[b - b1 + O]
            d2 = SQ[r - r2 + O] + SQ[g - g2 + O] + SQ[b - b2 + O]
            d3 = SQ[r - r3 + O] + SQ[g - g3 + O] + SQ[b - b3 + O]
            best, k = d0, 0
            if d1 < best:
                best, k = d1, 1
            if d2 < best:
                best, k = d2, 2
            if d3 < best:
                k = 3
            idx |= k << shift
            shift += 2
    return bytes((c0v & 0xFF, c0v >> 8, c1v & 0xFF, c1v >> 8,
                  idx & 0xFF, (idx >> 8) & 0xFF, (idx >> 16) & 0xFF,
                  (idx >> 24) & 0xFF))


def _encode_bc1_block(px):
    return _encode_bc1_color_block(px, allow_three=True)


def _decode_alpha8(block):
    """BC3/BC5-style 8-byte alpha block -> 16 alpha values."""
    a0, a1 = block[0], block[1]
    if a0 > a1:
        pal = [a0, a1] + [(a0 * (8 - i) + a1 * i) // 8 for i in range(1, 7)]
    else:
        pal = [a0, a1] + [(a0 * (6 - i) + a1 * i) // 6 for i in range(1, 5)]
        pal += [0, 255]
    idx = int.from_bytes(block[2:8], "little")      # 48 bits
    return [pal[(idx >> (i * 3)) & 7] for i in range(16)]


def _encode_alpha8(values):
    """16 alphas -> 8-byte block (a0=max, a1=min, 8-level mode).
    Unrolled 8-way nearest search; tie-breaking matches the original
    loop (first strictly smaller wins)."""
    a0, a1 = max(values), min(values)
    block = bytearray(8)
    block[0], block[1] = a0, a1
    if a0 > a1:
        p0, p1 = a0, a1
        p2 = (7 * a0 + a1) >> 3
        p3 = (6 * a0 + 2 * a1) >> 3
        p4 = (5 * a0 + 3 * a1) >> 3
        p5 = (4 * a0 + 4 * a1) >> 3
        p6 = (3 * a0 + 5 * a1) >> 3
        p7 = (2 * a0 + 6 * a1) >> 3
        pal = (p0, p1, p2, p3, p4, p5, p6, p7, -1)
    else:
        pal = (a0,) * 9                             # flat: every index -> a0
    idx = 0
    shift = 0
    for v in values:
        best = 1 << 30
        k = 0
        for j in range(8):
            d = v - pal[j]
            if d < 0:
                d = -d
            if d < best:
                best, k = d, j
        idx |= k << shift
        shift += 3
    block[2:8] = idx.to_bytes(6, "little")
    return bytes(block)


def _decode_bc3(block):
    alphas = _decode_alpha8(block[:8])
    colors = _decode_bc1_alpha(block[8:16])
    return [bytes((c[0], c[1], c[2], a)) for c, a in zip(colors, alphas)]


def _encode_bc3_block(px):
    out = _encode_alpha8([p[3] for p in px])
    opaque = [(p[0], p[1], p[2], 255) for p in px]
    return out + _encode_bc1_color_block(opaque, allow_three=False)


def _decode_bc2(block):
    idx = int.from_bytes(block[0:8], "little")
    colors = _decode_bc1_alpha(block[8:16])
    out = []
    for i in range(16):
        a = (idx >> (i * 4)) & 0xF
        c = colors[i]
        out.append(bytes((c[0], c[1], c[2], (a << 4) | a)))
    return out


def _encode_bc2_block(px):
    aidx = 0
    for i, p in enumerate(px):
        aidx |= ((p[3] >> 4) & 0xF) << (i * 4)
    opaque = [(p[0], p[1], p[2], 255) for p in px]
    return aidx.to_bytes(8, "little") + _encode_bc1_color_block(
        opaque, allow_three=False)


def _decode_bc4(block):
    vals = _decode_alpha8(block)
    return [bytes((v, v, v, 255)) for v in vals]


def _encode_bc4_block(px):
    return _encode_alpha8([p[0] for p in px])


def _decode_bc5(block):
    rs = _decode_alpha8(block[:8])
    gs = _decode_alpha8(block[8:16])
    return [bytes((r, g, 0, 255)) for r, g in zip(rs, gs)]


def _encode_bc5_block(px):
    return (_encode_alpha8([p[0] for p in px])
            + _encode_alpha8([p[1] for p in px]))


# --- BC7 -------------------------------------------------------------------

# (uPartitions, uPartitionBits, uPBits, uRotationBits, uIndexModeBits,
#  uIndexPrec, uIndexPrec2, precQ RGBA, precWithP RGBA)
_BC7_MODES = (
    (2, 4, 6, 0, 0, 3, 0, (4, 4, 4, 0), (5, 5, 5, 0)),   # mode 0
    (1, 6, 2, 0, 0, 3, 0, (6, 6, 6, 0), (7, 7, 7, 0)),   # mode 1
    (2, 6, 0, 0, 0, 2, 0, (5, 5, 5, 0), (5, 5, 5, 0)),   # mode 2
    (1, 6, 4, 0, 0, 2, 0, (7, 7, 7, 0), (8, 8, 8, 0)),   # mode 3
    (0, 0, 0, 2, 1, 2, 3, (5, 5, 5, 6), (5, 5, 5, 6)),   # mode 4
    (0, 0, 0, 2, 0, 2, 2, (7, 7, 7, 8), (7, 7, 7, 8)),   # mode 5
    (0, 0, 2, 0, 0, 4, 0, (7, 7, 7, 7), (8, 8, 8, 8)),   # mode 6
    (1, 6, 4, 0, 0, 2, 0, (5, 5, 5, 5), (6, 6, 6, 6)),   # mode 7
)


class _Bits:
    """LSB-first bit reader over a 16-byte block."""
    __slots__ = ("b", "p")

    def __init__(self, block):
        self.b = block
        self.p = 0

    def get(self, n):
        if n == 0:
            return 0
        v = 0
        for k in range(n):
            if self.p >= 128:
                raise BCError("BC7 block overrun")
            v |= ((self.b[self.p >> 3] >> (self.p & 7)) & 1) << k
            self.p += 1
        return v


def _unquantize(comp, prec):
    if prec <= 0:
        return 255
    comp = (comp << (8 - prec)) & 0xFF
    return comp | (comp >> prec)


def _interp(c0, c1, wc, wa, wcprec, waprec):
    wct = (WEIGHTS2, WEIGHTS3, WEIGHTS4)[wcprec - 2][wc]
    wat = (WEIGHTS2, WEIGHTS3, WEIGHTS4)[waprec - 2][wa]
    return tuple(
        (c0[i] * (64 - wct) + c1[i] * wct + 32) >> 6 if i < 3
        else (c0[3] * (64 - wat) + c1[3] * wat + 32) >> 6
        for i in range(4))


def _decode_bc7(block):
    bits = _Bits(block)
    first = 0
    while first < 128 and not ((block[first >> 3] >> (first & 7)) & 1):
        first += 1
    mode = first
    if mode > 7:
        return [b"\x00\x00\x00\x00"] * 16
    uPart, uPartBits, uPBits, uRotBits, uIdxModeBits, idxPrec, idxPrec2, \
        precQ, precWP = _BC7_MODES[mode]

    bits.p = mode + 1                  # header: mode bits are consumed
    shape = bits.get(uPartBits)
    rotation = bits.get(uRotBits)
    index_mode = bits.get(uIdxModeBits)
    n_ep = (uPart + 1) << 1
    c = []
    for ch in range(4):                          # R, G, B, A order
        vals = []
        for _ in range(n_ep):
            if precQ[ch]:
                vals.append(bits.get(precQ[ch]))
            else:
                vals.append(255)
        c.append(vals)
    # apply P-bits (pi = i * uPBits // n_ep), then unquantize
    pbits = [bits.get(1) for _ in range(uPBits)]
    for i in range(n_ep):
        pi = (i * uPBits) // n_ep
        for ch in range(4):
            if precQ[ch] != precWP[ch]:
                c[ch][i] = (c[ch][i] << 1) | pbits[pi]
    eps = []
    for i in range(n_ep):
        eps.append(bytes((_unquantize(c[0][i], precWP[0]),
                          _unquantize(c[1][i], precWP[1]),
                          _unquantize(c[2][i], precWP[2]),
                          _unquantize(c[3][i], precWP[3]) if precWP[3] else 255)))

    fix = FIXUP_TABLE[uPart * 64 + shape]
    w1 = []
    for i in range(16):
        nbits = idxPrec - 1 if i in fix[:uPart + 1] else idxPrec
        w1.append(bits.get(nbits))
    w2 = []
    if idxPrec2:
        for i in range(16):
            w2.append(bits.get(idxPrec2 - 1 if i == 0 else idxPrec2))

    part = PARTITION_TABLE[uPart * 64 + shape]
    out = []
    for i in range(16):
        reg = part[i]
        if idxPrec2 == 0:
            px = _interp(eps[reg << 1], eps[(reg << 1) + 1],
                         w1[i], w1[i], idxPrec, idxPrec)
        elif index_mode == 0:
            px = _interp(eps[reg << 1], eps[(reg << 1) + 1],
                         w1[i], w2[i], idxPrec, idxPrec2)
        else:
            px = _interp(eps[reg << 1], eps[(reg << 1) + 1],
                         w2[i], w1[i], idxPrec2, idxPrec)
        r, g, b, a = px
        if rotation == 1:
            r, a = a, r
        elif rotation == 2:
            g, a = a, g
        elif rotation == 3:
            b, a = a, b
        out.append(bytes((r, g, b, a)))
    return out


def _encode_bc7_block(px):
    return _encode_bc7_mode6(px)


def _encode_bc7_mode6(px):
    """Mode 6: 1 subset, RGBA 7-bit+P endpoints, 4-bit indices.

    The two P-bits are per ENDPOINT and apply to every channel of that
    endpoint: stored = (q << 1) | p.  q is v>>1; p is picked per endpoint
    so a majority of odd channels stay exact (alpha is one of four
    channels — worst case an endpoint is off by 1 in some channels).
    Mode 6 is used for every block: its 16-entry palette carries both
    opaque and alpha content; mode 5's 4-entry color palette visibly
    bands text blocks.
    """
    mins = [min(p[i] for p in px) for i in range(4)]
    maxs = [max(p[i] for p in px) for i in range(4)]
    ql = [v >> 1 for v in mins]
    qh = [v >> 1 for v in maxs]
    pl = 1 if sum(v & 1 for v in mins) >= 3 else 0
    ph = 1 if sum(v & 1 for v in maxs) >= 3 else 0
    e0 = bytes(((ql[ch] << 1) | pl) for ch in range(4))
    e1 = bytes(((qh[ch] << 1) | ph) for ch in range(4))
    pal = [_interp(e0, e1, w, w, 4, 4) for w in range(16)]
    idx = []
    for p in px:
        best, k = 1 << 30, 0
        for j, c in enumerate(pal):
            d = ((p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2
                 + (p[2] - c[2]) ** 2 + (p[3] - c[3]) ** 2)
            if d < best:
                best, k = d, j
        idx.append(k)
    if idx[0] >= 8:
        # pixel 0 stores only 3 bits (MSB=0): swap endpoints and invert
        # the index scale so its code lands below 8.
        ql, qh = qh, ql
        pl, ph = ph, pl
        e0, e1 = e1, e0
        pal = [_interp(e0, e1, w, w, 4, 4) for w in range(16)]
        idx = [15 - k for k in idx]
    bits = _BitWriter()
    bits.put(6, 0)                    # six 0 bits, then mode-6 '1'
    bits.put(1, 1)
    for ch in range(4):               # R,G,B,A; endpoint 0 then 1
        bits.put(7, ql[ch])
        bits.put(7, qh[ch])
    bits.put(1, pl)
    bits.put(1, ph)
    for i in range(16):               # pixel 0 stores 3 bits (MSB=0)
        bits.put(3 if i == 0 else 4, idx[i] & (0x7 if i == 0 else 0xF))
    return bits.bytes()


class _BitWriter:
    __slots__ = ("buf", "p")

    def __init__(self):
        self.buf = bytearray(16)
        self.p = 0

    def put(self, n, v):
        for k in range(n):
            if (v >> k) & 1:
                self.buf[self.p >> 3] |= 1 << (self.p & 7)
            self.p += 1

    def bytes(self):
        return bytes(self.buf)


_DECODERS = {
    "PF_DXT1": _decode_bc1_alpha,
    "PF_DXT3": _decode_bc2,
    "PF_DXT5": _decode_bc3,
    "PF_BC4": _decode_bc4,
    "PF_BC5": _decode_bc5,
    "PF_BC7": _decode_bc7,
}

_ENCODERS = {
    "PF_DXT1": _encode_bc1_block,
    "PF_DXT3": _encode_bc2_block,
    "PF_DXT5": _encode_bc3_block,
    "PF_BC4": _encode_bc4_block,
    "PF_BC5": _encode_bc5_block,
    "PF_BC7": _encode_bc7_block,
}
