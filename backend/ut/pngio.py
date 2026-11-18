"""Minimal PNG codec (RGBA), stdlib-only (zlib + struct).

The shipped backend runtime has no pip, so image I/O for the SEP
round-trip is implemented here rather than pulled in with Pillow:
  encode_png(width, height, rgba) -> bytes     (8-bit RGBA, filter 0)
  decode_png(data) -> (width, height, rgba bytearray)
Decoding supports the PNG baseline needed for SEP-rendered files: color
types 0 (grey), 2 (RGB), 3 (palette), 4 (grey+alpha), 6 (RGBA); bit
depths 1/2/4/8/16 (sub-8 uninterlaced, 16 truncated to 8); all five
scanline filters; no interlace (Adam7 raises — SEP never emits it).
"""
from __future__ import annotations

import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"


class PngError(ValueError):
    pass


# --- encode ---------------------------------------------------------------

def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """rgba: width*height*4 bytes, row-major, non-premultiplied."""
    if width <= 0 or height <= 0:
        raise PngError(f"bad dimensions {width}x{height}")
    if len(rgba) != width * height * 4:
        raise PngError("rgba buffer size mismatch")
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)                      # filter type 0 (None)
        raw += rgba[y * stride:(y + 1) * stride]
    idat = zlib.compress(bytes(raw), 6)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


# --- decode ---------------------------------------------------------------

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}      # channels per color type


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> "tuple[int, int, bytearray]":
    if not data.startswith(_SIG):
        raise PngError("not a PNG file")
    pos = len(_SIG)
    width = height = bitdepth = colortype = interlace = 0
    idat = bytearray()
    palette: "list[tuple[int, int, int]]" = []
    trns = b""
    seen_ihdr = False
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise PngError("truncated chunk")
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bitdepth, colortype, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body)
            seen_ihdr = True
        elif tag == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if not seen_ihdr:
        raise PngError("missing IHDR")
    if width <= 0 or height <= 0:
        raise PngError(f"bad dimensions {width}x{height}")
    if colortype not in _CHANNELS:
        raise PngError(f"unsupported color type {colortype}")
    if colortype == 3 and not palette:
        raise PngError("palette image without PLTE")
    if interlace:
        raise PngError("interlaced PNG not supported")
    depths = (1, 2, 4, 8, 16) if colortype != 3 else (1, 2, 4, 8)
    if bitdepth not in depths:
        raise PngError(f"unsupported bit depth {bitdepth}")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise PngError(f"IDAT: {e}") from e

    nch = _CHANNELS[colortype]
    # bytes per pixel as stored (>=1; packed sub-byte for depth<8)
    bits_per_px = nch * bitdepth
    stride = (width * bits_per_px + 7) // 8
    bpp = max(1, (bits_per_px + 7) // 8)           # filter unit
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise PngError(f"image data short: {len(raw)} < {expected}")

    out = bytearray(width * height * 4)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:                              # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:                            # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                            # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                            # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                ul = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], ul)) & 0xFF
        elif ftype != 0:
            raise PngError(f"bad filter type {ftype}")
        prev = line
        _emit(line, y, width, bitdepth, colortype, palette, trns, out)
    return width, height, out


def _emit(line, y, width, depth, colortype, palette, trns, out):
    """Expand one filtered scanline into the RGBA output row."""
    base = y * width * 4
    alpha_default = 255

    def trns_at(idx: int) -> int:
        return trns[idx] if idx < len(trns) else alpha_default

    if colortype == 6 and depth == 8:
        out[base:base + width * 4] = line[:width * 4]
        return
    if colortype == 2 and depth == 8:
        for x in range(width):
            o, i = base + x * 4, x * 3
            out[o:o + 3] = line[i:i + 3]
            out[o + 3] = 255
        return
    if colortype == 0 and depth == 8:
        for x in range(width):
            o, v = base + x * 4, line[x]
            out[o] = out[o + 1] = out[o + 2] = v
            out[o + 3] = 255
        return
    if colortype == 4 and depth == 8:
        for x in range(width):
            o, i = base + x * 4, x * 2
            v = line[i]
            out[o] = out[o + 1] = out[o + 2] = v
            out[o + 3] = line[i + 1]
        return
    if colortype == 3:                              # palette 1/2/4/8
        per_byte = 8 // depth
        mask = (1 << depth) - 1
        for x in range(width):
            byte = line[x // per_byte]
            shift = 8 - depth * (x % per_byte + 1)
            idx = (byte >> shift) & mask
            if idx >= len(palette):
                raise PngError(f"palette index {idx} out of range")
            r, g, b = palette[idx]
            o = base + x * 4
            out[o:o + 3] = bytes((r, g, b))
            out[o + 3] = trns_at(idx)
        return
    if depth == 16:                                 # grey/rgb/greyA/rgba 16
        if colortype == 0:
            for x in range(width):
                o, v = base + x * 4, line[x * 2 + 1]
                out[o] = out[o + 1] = out[o + 2] = v
                out[o + 3] = 255
            return
        if colortype == 2:
            for x in range(width):
                o, i = base + x * 4, x * 6
                out[o] = line[i + 1]
                out[o + 1] = line[i + 3]
                out[o + 2] = line[i + 5]
                out[o + 3] = 255
            return
        if colortype == 4:
            for x in range(width):
                o, i = base + x * 4, x * 4
                v = line[i + 1]
                out[o] = out[o + 1] = out[o + 2] = v
                out[o + 3] = line[i + 3]
            return
        for x in range(width):
            o, i = base + x * 4, x * 8
            out[o] = line[i + 1]
            out[o + 1] = line[i + 3]
            out[o + 2] = line[i + 5]
            out[o + 3] = line[i + 7]
        return
    # sub-8-bit grey (types 0 and 4 share packing; 4 handled above for 8)
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    maxv = mask
    for x in range(width):
        byte = line[x // per_byte]
        shift = 8 - depth * (x % per_byte + 1)
        v = ((byte >> shift) & mask) * 255 // maxv
        o = base + x * 4
        out[o] = out[o + 1] = out[o + 2] = v
        out[o + 3] = 255
