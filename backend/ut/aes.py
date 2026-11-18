"""AES-256-ECB. Windows: BCrypt via ctypes (OS API, not third-party code).
Fallback + cross-check: in-house pure-python block implementation."""
from __future__ import annotations

import ctypes
import os
import threading

_LOCK = threading.Lock()
_BCRYPT = None  # bcrypt.dll handle once loaded (key handles are per-call)
_BACKENDS = None  # available_backends() result, resolved once (Task 2 pre-step:
#               # find_library walk cost ~886 us/call; encrypt_ecb/decrypt_ecb
#               # consult it per call, so full-index work paid 20-35 s in probes)


def available_backends() -> list[str]:
    global _BACKENDS
    if _BACKENDS is None:
        out = []
        try:
            import ctypes.util
            if ctypes.util.find_library("bcrypt") or os.name == "nt":
                out.append("bcrypt")
        except Exception:
            pass
        out.append("pure")
        _BACKENDS = out
    return list(_BACKENDS)    # copy: callers cannot mutate the cache


def _check_ecb_args(key: bytes, data: bytes) -> None:
    if len(key) != 32:
        raise ValueError(f"AES-256 key must be 32 bytes, got {len(key)}")
    if len(data) % 16 != 0:
        raise ValueError(f"ECB data length must be a multiple of 16, "
                         f"got {len(data)}")


# ---------------------------------------------------------------------------
# Windows BCrypt path (OS crypto API). Call sequence:
#   BCryptOpenAlgorithmProvider(&hAlg, "AES", None, 0)
#   BCryptSetProperty(hAlg, "ChainingMode", L"ChainingModeECB", 34, 0)
#   BCryptGenerateSymmetricKey(hAlg, &hKey, None, 0, key, 32, 0)
#   BCryptEncrypt/Decrypt(hKey, pbInput, cbInput, None, None, 0,
#                         pbOutput, cbOutput, &cbResult, 0)
# No BCRYPT_BLOCK_PADDING flag — inputs are whole 16-byte blocks (callers
# zero-pad), so cbResult must equal cbInput (asserted below).
# ---------------------------------------------------------------------------

def _load_bcrypt() -> ctypes.CDLL:
    global _BCRYPT
    with _LOCK:
        if _BCRYPT is not None:
            return _BCRYPT
        dll = ctypes.WinDLL("bcrypt")
        for name in ("BCryptOpenAlgorithmProvider", "BCryptSetProperty",
                     "BCryptGenerateSymmetricKey", "BCryptEncrypt",
                     "BCryptDecrypt", "BCryptDestroyKey",
                     "BCryptCloseAlgorithmProvider"):
            getattr(dll, name).restype = ctypes.c_long          # NTSTATUS
        dll.BCryptOpenAlgorithmProvider.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_wchar_p,
            ctypes.c_wchar_p, ctypes.c_ulong]
        dll.BCryptSetProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_char_p,
            ctypes.c_ulong, ctypes.c_ulong]
        dll.BCryptGenerateSymmetricKey.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p,
            ctypes.c_ulong, ctypes.c_ulong]
        for name in ("BCryptEncrypt", "BCryptDecrypt"):
            getattr(dll, name).argtypes = [
                ctypes.c_void_p,                     # hKey
                ctypes.c_char_p, ctypes.c_ulong,     # pbInput, cbInput
                ctypes.c_void_p,                     # pPaddingInfo (none)
                ctypes.c_void_p, ctypes.c_ulong,     # pbIV, cbIV (none: ECB)
                ctypes.c_char_p, ctypes.c_ulong,     # pbOutput, cbOutput
                ctypes.POINTER(ctypes.c_ulong),      # pcbResult
                ctypes.c_ulong]                      # dwFlags (no padding)
        dll.BCryptDestroyKey.argtypes = [ctypes.c_void_p]
        dll.BCryptCloseAlgorithmProvider.argtypes = [ctypes.c_void_p,
                                                     ctypes.c_ulong]
        _BCRYPT = dll
        return _BCRYPT


def _bcrypt_ecb(key: bytes, data: bytes, encrypt: bool) -> bytes:
    dll = _load_bcrypt()
    h_alg = ctypes.c_void_p()
    status = dll.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0)
    if status != 0:
        raise OSError(f"BCryptOpenAlgorithmProvider failed: "
                      f"{status & 0xFFFFFFFF:#x}")
    try:
        mode = "ChainingModeECB\x00".encode("utf-16-le")    # wide literal, 34 B
        status = dll.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0)
        if status != 0:
            raise OSError(f"BCryptSetProperty(ChainingModeECB) failed: "
                          f"{status & 0xFFFFFFFF:#x}")
        h_key = ctypes.c_void_p()
        status = dll.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key),
                                                None, 0, key, len(key), 0)
        if status != 0:
            raise OSError(f"BCryptGenerateSymmetricKey failed: "
                          f"{status & 0xFFFFFFFF:#x}")
        try:
            src = ctypes.create_string_buffer(bytes(data), len(data))
            dst = ctypes.create_string_buffer(len(data))
            cb_result = ctypes.c_ulong(0)
            fn = dll.BCryptEncrypt if encrypt else dll.BCryptDecrypt
            status = fn(h_key, src, len(data), None, None, 0,
                        dst, len(data), ctypes.byref(cb_result), 0)
            if status != 0:
                raise OSError(f"BCrypt{'Encrypt' if encrypt else 'Decrypt'} "
                              f"failed: {status & 0xFFFFFFFF:#x}")
            if cb_result.value != len(data):    # no padding flag: 1:1 blocks
                raise OSError(f"BCrypt cbResult {cb_result.value} != "
                              f"cbInput {len(data)}")
            return dst.raw[:cb_result.value]
        finally:
            dll.BCryptDestroyKey(h_key)
    finally:
        dll.BCryptCloseAlgorithmProvider(h_alg, 0)


# ---------------------------------------------------------------------------
# In-house pure-python AES-256 (fallback backend, always importable so the
# OS-API path can be cross-checked against it — see backend/tests/test_aes.py).
# Standard Rijndael: s-box/inv-s-box computed at import from the GF(2^8)
# definition (inverses via the 0x03 generator walk + affine transform),
# 256-bit key schedule (Nk=8, Nr=14), ECB block loops.
# State convention: flat 16-byte list, flat[r + 4*c] = state row r, col c
# (i.e. input byte order — FIPS-197 s[r][c] = in[r + 4c]).
# ---------------------------------------------------------------------------

def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _xtime(a: int) -> int:                       # multiply by x in GF(2^8)
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _gf_mul(a: int, b: int) -> int:              # peasant multiplication
    out = 0
    while b:
        if b & 1:
            out ^= a
        b >>= 1
        a = _xtime(a)
    return out


def _build_sboxes() -> "tuple[list[int], list[int]]":
    """Rijndael s-box from the field definition: p walks all 255 nonzero
    elements (multiplying by the generator 0x03) while q simultaneously
    tracks p^-1 (multiplying by 0x03^-1); the s-box is the affine transform
    of the inverse, and 0 maps to 0."""
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF   # p *= 0x03
        q = (q ^ (q << 1)) & 0xFF                              # q *= 0xF6
        q = (q ^ (q << 2)) & 0xFF                              # (== p^-1)
        q = (q ^ (q << 4)) & 0xFF
        if q & 0x80:
            q ^= 0x09
        affine = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4)
        sbox[p] = affine ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


_SBOX, _INV_SBOX = _build_sboxes()
assert (_SBOX[0], _SBOX[1], _SBOX[0x53], _INV_SBOX[0x63]) == \
    (0x63, 0x7C, 0xED, 0x00), "derived s-box disagrees with FIPS-197"

_MUL2 = [_gf_mul(2, x) for x in range(256)]
_MUL3 = [_gf_mul(3, x) for x in range(256)]
_MUL9 = [_gf_mul(9, x) for x in range(256)]
_MUL11 = [_gf_mul(11, x) for x in range(256)]
_MUL13 = [_gf_mul(13, x) for x in range(256)]
_MUL14 = [_gf_mul(14, x) for x in range(256)]


def _key_expansion(key: bytes) -> "list[list[int]]":
    """AES-256 (Nk=8): 60 round-key words of 4 bytes each."""
    words = [list(key[4 * i:4 * i + 4]) for i in range(8)]
    rcon = 1
    for i in range(8, 60):
        t = list(words[i - 1])
        if i % 8 == 0:
            t = t[1:] + t[:1]                    # RotWord
            t = [_SBOX[b] for b in t]            # SubWord
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % 8 == 4:
            t = [_SBOX[b] for b in t]            # SubWord (AES-256 extra)
        words.append([a ^ b for a, b in zip(words[i - 8], t)])
    return words


def _add_round_key(s: list, w: "list[list[int]]", rnd: int) -> None:
    for c in range(4):
        word = w[4 * rnd + c]
        s[4 * c] ^= word[0]
        s[4 * c + 1] ^= word[1]
        s[4 * c + 2] ^= word[2]
        s[4 * c + 3] ^= word[3]


def _shift_rows(s: list) -> None:
    for r in range(1, 4):
        row = [s[r], s[r + 4], s[r + 8], s[r + 12]]
        for c in range(4):
            s[r + 4 * c] = row[(c + r) % 4]      # rotate row r left by r


def _inv_shift_rows(s: list) -> None:
    for r in range(1, 4):
        row = [s[r], s[r + 4], s[r + 8], s[r + 12]]
        for c in range(4):
            s[r + 4 * c] = row[(c - r) % 4]


def _mix_columns(s: list) -> None:
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
        s[i] = _MUL2[a0] ^ _MUL3[a1] ^ a2 ^ a3
        s[i + 1] = a0 ^ _MUL2[a1] ^ _MUL3[a2] ^ a3
        s[i + 2] = a0 ^ a1 ^ _MUL2[a2] ^ _MUL3[a3]
        s[i + 3] = _MUL3[a0] ^ a1 ^ a2 ^ _MUL2[a3]


def _inv_mix_columns(s: list) -> None:
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
        s[i] = _MUL14[a0] ^ _MUL11[a1] ^ _MUL13[a2] ^ _MUL9[a3]
        s[i + 1] = _MUL9[a0] ^ _MUL14[a1] ^ _MUL11[a2] ^ _MUL13[a3]
        s[i + 2] = _MUL13[a0] ^ _MUL9[a1] ^ _MUL14[a2] ^ _MUL11[a3]
        s[i + 3] = _MUL11[a0] ^ _MUL13[a1] ^ _MUL9[a2] ^ _MUL14[a3]


def _encrypt_block(w: "list[list[int]]", block: bytes) -> bytes:
    s = list(block)
    _add_round_key(s, w, 0)
    for rnd in range(1, 14):
        s = [_SBOX[b] for b in s]                # SubBytes
        _shift_rows(s)
        _mix_columns(s)
        _add_round_key(s, w, rnd)
    s = [_SBOX[b] for b in s]
    _shift_rows(s)
    _add_round_key(s, w, 14)
    return bytes(s)


def _decrypt_block(w: "list[list[int]]", block: bytes) -> bytes:
    s = list(block)
    _add_round_key(s, w, 14)
    for rnd in range(13, 0, -1):
        _inv_shift_rows(s)
        s = [_INV_SBOX[b] for b in s]
        _add_round_key(s, w, rnd)
        _inv_mix_columns(s)
    _inv_shift_rows(s)
    s = [_INV_SBOX[b] for b in s]
    _add_round_key(s, w, 0)
    return bytes(s)


class _PureEcb:
    """The in-house block cipher exposed under the same pair of names
    (``aes._pure_ecb``), used as fallback and as the bcrypt cross-check."""

    @staticmethod
    def encrypt_ecb(key: bytes, data: bytes) -> bytes:
        _check_ecb_args(key, data)
        w = _key_expansion(key)
        return b"".join(_encrypt_block(w, data[i:i + 16])
                        for i in range(0, len(data), 16))

    @staticmethod
    def decrypt_ecb(key: bytes, data: bytes) -> bytes:
        _check_ecb_args(key, data)
        w = _key_expansion(key)
        return b"".join(_decrypt_block(w, data[i:i + 16])
                        for i in range(0, len(data), 16))


_pure_ecb = _PureEcb()


def encrypt_ecb(key: bytes, data: bytes) -> bytes:
    """AES-256-ECB encrypt; bcrypt backend preferred, in-house fallback."""
    _check_ecb_args(key, data)
    if not data:
        return b""
    if "bcrypt" in available_backends():
        try:
            return _bcrypt_ecb(key, data, True)
        except OSError:
            pass        # degrade to the in-house backend (it is cross-checked)
    return _pure_ecb.encrypt_ecb(key, data)


def decrypt_ecb(key: bytes, data: bytes) -> bytes:
    """AES-256-ECB decrypt; bcrypt backend preferred, in-house fallback."""
    _check_ecb_args(key, data)
    if not data:
        return b""
    if "bcrypt" in available_backends():
        try:
            return _bcrypt_ecb(key, data, False)
        except OSError:
            pass
    return _pure_ecb.decrypt_ecb(key, data)
