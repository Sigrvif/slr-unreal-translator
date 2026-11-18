"""Pure-python CityHash64 — UE5 zen name-batch hashes.

UE's cooked zen package name batches store one CityHash64 per name
(see ``ut.zen``); patching a name string means recomputing its hash.
This is a from-scratch port of Google CityHash v1.0.3's ``city.cc``
(64-bit little-endian path only — the only one the name batch uses),
kept dependency-free like every backend module. Correctness is pinned
by known-answer vectors from a reference implementation
(``tests/test_cityhash.py``) and by the real ST name batches.
"""
from __future__ import annotations

import struct

__all__ = ["cityhash64"]

_MASK = (1 << 64) - 1
_K0 = 0xC3A5C85C97CB3127
_K1 = 0xB492B66FBE98F273
_K2 = 0x9AE16A3B2F90404F
_KMUL = 0x9DDFEA08EB382D69          # Hash128to64's multiplier


def _rot(v: int, s: int) -> int:
    return ((v >> s) | (v << (64 - s))) & _MASK


def _shift_mix(v: int) -> int:
    return (v ^ (v >> 47)) & _MASK


def _hash_len16_mul(u: int, v: int, mul: int) -> int:
    a = ((u ^ v) * mul) & _MASK
    a ^= a >> 47
    b = ((v ^ a) * mul) & _MASK
    b ^= b >> 47
    return (b * mul) & _MASK


def _hash_len16(u: int, v: int) -> int:
    return _hash_len16_mul(u, v, _KMUL)


def _hash_len0to16(s: bytes) -> int:
    n = len(s)
    if n >= 8:
        mul = (_K2 + n * 2) & _MASK
        a = (_fetch64(s, 0) + _K2) & _MASK
        b = _fetch64(s, n - 8)
        c = (_rot(b, 37) * mul + a) & _MASK
        d = ((_rot(a, 25) + b) * mul) & _MASK
        return _hash_len16_mul(c, d, mul)
    if n >= 4:
        mul = (_K2 + n * 2) & _MASK
        a = _fetch32(s, 0)
        return _hash_len16_mul((n + (a << 3)) & _MASK,
                               _fetch32(s, n - 4), mul)
    if n > 0:
        a = s[0]
        b = s[n >> 1]
        c = s[n - 1]
        y = (a + (b << 8)) & 0xFFFFFFFF
        z = (n + (c << 2)) & 0xFFFFFFFF
        return (_shift_mix(((y * _K2) & _MASK) ^ ((z * _K0) & _MASK))
                * _K2) & _MASK
    return _K2


def _hash_len17to32(s: bytes) -> int:
    n = len(s)
    mul = (_K2 + n * 2) & _MASK
    a = (_fetch64(s, 0) * _K1) & _MASK
    b = _fetch64(s, 8)
    c = (_fetch64(s, n - 8) * mul) & _MASK
    d = (_fetch64(s, n - 16) * _K2) & _MASK
    return _hash_len16_mul(
        (_rot((a + b) & _MASK, 43) + _rot(c, 30) + d) & _MASK,
        (a + _rot((b + _K2) & _MASK, 18) + c) & _MASK, mul)


def _hash_len33to64(s: bytes) -> int:
    n = len(s)
    mul = (_K2 + n * 2) & _MASK
    a = (_fetch64(s, 0) * _K2) & _MASK
    b = _fetch64(s, 8)
    c = _fetch64(s, n - 24)
    d = _fetch64(s, n - 32)
    e = (_fetch64(s, 16) * _K2) & _MASK
    f = (_fetch64(s, 24) * 9) & _MASK
    g = _fetch64(s, n - 8)
    h = (_fetch64(s, n - 16) * mul) & _MASK
    u = (_rot((a + g) & _MASK, 43) + (_rot(b, 30) + c) * 9) & _MASK
    v = (((a + g) & _MASK ^ d) + f + 1) & _MASK
    w = (_bswap64((u + v) * mul & _MASK) + h) & _MASK
    x = (_rot((e + f) & _MASK, 42) + c) & _MASK
    y = ((_bswap64((v + w) * mul & _MASK) + g) * mul) & _MASK
    z = (e + f + c) & _MASK
    a = (_bswap64(((x + z) * mul + y) & _MASK) + b) & _MASK
    b = (_shift_mix(((z + a) * mul + d + h) & _MASK) * mul) & _MASK
    return (b + x) & _MASK


def _weak_len32(s: bytes, a: int, b: int) -> "tuple[int, int]":
    w = _fetch64(s, 0)
    x = _fetch64(s, 8)
    y = _fetch64(s, 16)
    z = _fetch64(s, 24)
    a = (a + w) & _MASK
    b = _rot((b + a + z) & _MASK, 21)
    c = a
    a = (a + x + y) & _MASK
    b = (b + _rot(a, 44)) & _MASK
    return (a + z) & _MASK, (b + c) & _MASK


def _fetch64(s: bytes, i: int) -> int:
    return int.from_bytes(s[i:i + 8], "little")


def _fetch32(s: bytes, i: int) -> int:
    return int.from_bytes(s[i:i + 4], "little")


def _bswap64(v: int) -> int:
    return struct.unpack("<Q", struct.pack(">Q", v & _MASK))[0]


def cityhash64(s: bytes) -> int:
    """CityHash64 of ``s`` (little-endian host order, as UE cooks)."""
    n = len(s)
    if n <= 32:
        if n <= 16:
            return _hash_len0to16(s)
        return _hash_len17to32(s)
    if n <= 64:
        return _hash_len33to64(s)
    # > 64: hash the end first, then 64-byte chunks keeping 56 bytes of
    # state (v, w, x, y, z).
    x = _fetch64(s, n - 40)
    y = (_fetch64(s, n - 16) + _fetch64(s, n - 56)) & _MASK
    z = _hash_len16((_fetch64(s, n - 48) + n) & _MASK, _fetch64(s, n - 24))
    v_first, v_second = _weak_len32(s[n - 64:], n, z)
    w_first, w_second = _weak_len32(s[n - 32:], (y + _K1) & _MASK, x)
    x = (x * _K1 + _fetch64(s, 0)) & _MASK
    pos = 0
    left = (n - 1) & ~63
    while True:
        x = _rot((x + y + v_first + _fetch64(s, pos + 8)) & _MASK,
                 37) * _K1 & _MASK
        y = _rot((y + v_second + _fetch64(s, pos + 48)) & _MASK,
                 42) * _K1 & _MASK
        x ^= w_second
        x &= _MASK
        y = (y + v_first + _fetch64(s, pos + 40)) & _MASK
        z = _rot((z + w_first) & _MASK, 33) * _K1 & _MASK
        v_first, v_second = _weak_len32(
            s[pos:], (v_second * _K1) & _MASK, (x + w_first) & _MASK)
        w_first, w_second = _weak_len32(
            s[pos + 32:], (z + w_second) & _MASK,
            (y + _fetch64(s, pos + 16)) & _MASK)
        z, x = x, z
        pos += 64
        left -= 64
        if left == 0:
            break
    return (_hash_len16(
        (_hash_len16(v_first, w_first) + _shift_mix(y) * _K1 + z) & _MASK,
        (_hash_len16(v_second, w_second) + x) & _MASK))
