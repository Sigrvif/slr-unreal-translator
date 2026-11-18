# -*- coding: utf-8 -*-
"""Phase 5a: block-compression codec tests (decode correctness + encode
idempotency). Fidelity bar: decode(encode(decode(x))) == decode(x) at high
PSNR — encoders are deterministic approximations, not Epic-identical."""
import math
import random

import pytest

from backend.ut import bcdec


def _psnr(a, b):
    se = sum((a[i] - b[i]) ** 2 for i in range(len(a)))
    if se == 0:
        return 99.0
    return 10 * math.log10(255 * 255 / (se / len(a)))


def _strip(w, h, seed=13):
    """Gradient + alpha stripes; BC4 gets full alpha (it only encodes R)."""
    px = bytearray()
    for y in range(h):
        for x in range(w):
            r, g, b = x * 4, y * 4, (x * y) & 255
            a = 255 if ((y // 8 + x // 8) % 2) else 40
            px += bytes((r, g, b, a))
    return bytes(px)


@pytest.mark.parametrize("fmt,bb", [("PF_DXT1", 8), ("PF_DXT3", 16),
                                    ("PF_DXT5", 16), ("PF_BC4", 8),
                                    ("PF_BC5", 16), ("PF_BC7", 16)])
def test_round_trip_idempotent(fmt, bb):
    w = h = 64
    px = _strip(w, h)
    enc = bcdec.encode_texture(fmt, w, h, px)
    assert len(enc) == bb * (w // 4) * (h // 4)
    dec = bytes(bcdec.decode_texture(fmt, w, h, enc))
    enc2 = bcdec.encode_texture(fmt, w, h, dec)
    dec2 = bytes(bcdec.decode_texture(fmt, w, h, enc2))
    assert _psnr(dec, dec2) > 38.0


@pytest.mark.parametrize("fmt", ["PF_B8G8R8A8", "PF_R8G8B8A8"])
def test_raw_exact(fmt):
    random.seed(3)
    w = h = 8
    px = bytes(random.randrange(256) for _ in range(w * h * 4))
    assert bytes(bcdec.decode_texture(fmt, w, h,
                                      bcdec.encode_texture(fmt, w, h, px))) == px


def test_g8_decodes_grey():
    grey = bytes((0, 128, 255))
    rgba = bytes(bcdec.decode_texture("PF_G8", 3, 1, grey))
    assert rgba == bytes((0, 0, 0, 255, 128, 128, 128, 255, 255, 255, 255, 255))


@pytest.mark.parametrize("fmt,v", [("PF_DXT1", 127), ("PF_DXT5", 200),
                                   ("PF_BC7", 255), ("PF_BC7", 7)])
def test_flat_block_stays_flat(fmt, v):
    px = bytes((v, v, v, 255)) * 4096
    dec = bytes(bcdec.decode_texture(
        "PF_DXT1" if fmt == "PF_DXT1" else fmt, 64, 64,
        bcdec.encode_texture(fmt, 64, 64, px)))
    tol = 8 if fmt == "PF_DXT1" else (4 if fmt == "PF_DXT5" else 1)
    for i in range(1024):
        r, g, b, a = dec[i * 4:i * 4 + 4]
        assert abs(r - v) <= tol and abs(g - v) <= tol and abs(b - v) <= tol
        assert a == 255


def test_dxt1_punchthrough():
    px = bytearray()
    for y in range(16):
        for x in range(16):
            px += bytes((200, 100, 50, 0 if (x // 4 + y // 4) % 2 else 255))
    dec = bytes(bcdec.decode_texture(
        "PF_DXT1", 16, 16, bcdec.encode_texture("PF_DXT1", 16, 16, bytes(px))))
    assert all(dec[i * 4 + 3] in (0, 255) for i in range(256))


def test_unsupported_format_raises():
    with pytest.raises(bcdec.BCError):
        bcdec.decode_texture("PF_FloatRGBA", 4, 4, b"\x00" * 16)
    with pytest.raises(bcdec.BCError):
        bcdec.encode_texture("PF_ASTC_4x4", 4, 4, b"\x00" * 64)


def test_odd_mip_dims_sizes():
    # 56x46 must downsample cleanly through the whole chain (edge clamps)
    px = _strip(56, 46, seed=7)
    for fmt, size in (("PF_DXT1", 8), ("PF_BC7", 16)):
        enc = bcdec.encode_texture(fmt, 56, 46, px)
        # encode_texture covers ONE level: 14x12 blocks for 56x46
        assert len(enc) == 14 * 12 * size
