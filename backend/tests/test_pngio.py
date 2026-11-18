# -*- coding: utf-8 -*-
"""Phase 5a: stdlib PNG codec tests."""
import struct
import zlib

import pytest

from backend.ut import pngio


def _chunk(tag: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))


def _png(width, height, depth, ctype, raw, plte=None, trns=None):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, depth, ctype, 0, 0, 0)
    out = sig + _chunk(b"IHDR", ihdr)
    if plte is not None:
        out += _chunk(b"PLTE", plte)
    if trns is not None:
        out += _chunk(b"tRNS", trns)
    out += _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")
    return out


def test_rgba_round_trip():
    import random
    random.seed(5)
    w, h = 33, 17
    buf = bytes(random.randrange(256) for _ in range(w * h * 4))
    dec = pngio.decode_png(pngio.encode_png(w, h, buf))
    assert (dec[0], dec[1]) == (w, h)
    assert bytes(dec[2]) == buf


def test_grey8_decode():
    raw = b"\x00" + bytes(range(11))          # filter 0 + 11 grey bytes
    w, h, rgba = pngio.decode_png(_png(11, 1, 8, 0, raw))
    assert (w, h) == (11, 1)
    for x in range(11):
        assert rgba[x * 4:x * 4 + 4] == bytes((x, x, x, 255))


def test_rgb8_decode():
    row = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255))
    w, h, rgba = pngio.decode_png(_png(3, 1, 8, 2, b"\x00" + row))
    assert bytes(rgba) == bytes((255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255))


def test_palette_and_trns():
    plte = bytes((10, 20, 30, 200, 100, 50))
    raw = b"\x00" + bytes((0, 1))             # indices 0, 1
    w, h, rgba = pngio.decode_png(_png(2, 1, 8, 3, raw, plte=plte, trns=b"\x40\x00"))
    assert bytes(rgba) == bytes((10, 20, 30, 0x40, 200, 100, 50, 0))


def test_sub_filter_decode():
    # filter 1 (Sub): first pixel (10,20,30,40), second = first + (1,2,3,4)
    raw = b"\x01" + bytes((10, 20, 30, 40, 1, 2, 3, 4))
    w, h, rgba = pngio.decode_png(_png(2, 1, 8, 6, raw))
    assert bytes(rgba) == bytes((10, 20, 30, 40, 11, 22, 33, 44))


def test_up_average_paeth_decode():
    base = bytes((10, 10, 10, 10, 200, 200, 200, 200))
    row0 = b"\x00" + base
    # row 1: Up (+1 each), gives (11,11,11,11, 201,201,201,201)
    row1 = b"\x02" + bytes((1, 1, 1, 1, 1, 1, 1, 1))
    # row 2: Average against row1
    row2_vals = bytes((1, 1, 1, 1, 1, 1, 1, 1))
    row2 = b"\x03" + row2_vals
    w, h, rgba = pngio.decode_png(_png(2, 3, 8, 6, row0 + row1 + row2))
    assert bytes(rgba[0:8]) == base
    assert bytes(rgba[8:16]) == bytes((11,) * 4 + (201,) * 4)
    # Average: raw + floor((left+up)/2); px0: up=11 -> 5+1; px1: (11+201)/2+1
    assert bytes(rgba[16:24]) == bytes((6,) * 4 + (104,) * 4)


def test_grey16_truncated():
    raw = b"\x00" + bytes((0x12, 0x34, 0x56, 0x78))
    w, h, rgba = pngio.decode_png(_png(1, 1, 16, 0, raw))
    assert bytes(rgba) == bytes((0x34, 0x34, 0x34, 255))


def test_grey4_packed():
    raw = b"\x00" + bytes((0x0F,))            # two 4-bit pixels: 0 and 15
    w, h, rgba = pngio.decode_png(_png(2, 1, 4, 0, raw))
    assert bytes(rgba) == bytes((0, 0, 0, 255, 255, 255, 255, 255))


def test_rejects_bad_inputs():
    with pytest.raises(pngio.PngError):
        pngio.decode_png(b"not a png")
    with pytest.raises(pngio.PngError):
        pngio.encode_png(0, 10, b"")
    interlaced = bytearray(_png(1, 1, 8, 6, b"\x00" + b"\x00" * 4))
    interlaced[28] = 1                       # IHDR interlace byte
    with pytest.raises(pngio.PngError):
        pngio.decode_png(bytes(interlaced))
