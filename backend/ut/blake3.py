"""In-house BLAKE3 (hash mode), written from the published specification.

Source consulted: the BLAKE3 paper, "The BLAKE3 Hash Function" (official spec
repository BLAKE3-team/BLAKE3-specs, blake3.tex). Constants (IV, message
permutation, flag values, 1024-byte chunk size) and the algorithm structure
(7-round compression, G mixing function, chunk/parent tree, root output) were
taken from the spec document; the code below is an original implementation.

Unreal IoStore relevance: the 20-byte ``io_hash`` stored in .utoc chunk metas
is the BLAKE3 digest of the chunk's (decompressed) data truncated to 20 bytes
(proven in Phase 1 against 100/100 real chunks). ``hash20`` reproduces it;
``hash32`` is the standard 32-byte digest (first 32 bytes of the root output
block, which coincide with the packed root chaining value).
"""
from __future__ import annotations

import struct

# --- Spec constants -------------------------------------------------------

# Initialization vector: the SHA-256 IV (spec section 2.1).
IV = (
    0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
    0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19,
)

# Message-word permutation applied after every round except the last
# (spec section 2.3). MSG_PERMUTATION[i] is the source index for word i.
MSG_PERMUTATION = (2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8)

# Block flags (spec section 3.1). Only hash-mode flags are defined here.
CHUNK_START = 1 << 0
CHUNK_END = 1 << 1
PARENT = 1 << 2
ROOT = 1 << 3

BLOCK_LEN = 64    # compression-function message block, bytes
CHUNK_LEN = 1024  # tree leaf (chunk), bytes = 16 blocks

_M32 = 0xFFFFFFFF
_WORDS16 = struct.Struct("<16I")
_CV8 = struct.Struct("<8I")  # packing a chaining value = its 32-byte digest


# --- Compression function (spec section 2.5) ------------------------------

def _g(a: int, b: int, c: int, d: int, mx: int, my: int) -> tuple[int, ...]:
    """One G quarter-round on state positions (a, b, c, d).

    Two message words mx/my; all adds mod 2**32; rotations right by 16, 12,
    8, 7 (spec listing 1). Returns the four updated words (a, b, c, d).
    """
    a = (a + b + mx) & _M32
    t = d ^ a
    d = (t >> 16) | (t << 16) & _M32
    c = (c + d) & _M32
    t = b ^ c
    b = (t >> 12) | (t << 20) & _M32
    a = (a + b + my) & _M32
    t = d ^ a
    d = (t >> 8) | (t << 24) & _M32
    c = (c + d) & _M32
    t = b ^ c
    b = (t >> 7) | (t << 25) & _M32
    return a, b, c, d


def _compress(cv, block_words, counter: int, block_len: int, flags: int):
    """Compress one 64-byte block; returns the 16-word output (spec 2.5).

    State layout: [cv0..cv7, IV0..IV3, counter_lo, counter_hi, block_len,
    flags]. 7 rounds; each round mixes the four columns then the four
    diagonals; the message words are permuted between rounds. Output:
    out[i] = v[i] ^ v[i+8] for i < 8 (the chaining value), and
    out[8+i] = v[8+i] ^ cv[i] (feed-forward).
    """
    v = [cv[0], cv[1], cv[2], cv[3], cv[4], cv[5], cv[6], cv[7],
         IV[0], IV[1], IV[2], IV[3],
         counter & _M32, (counter >> 32) & _M32, block_len, flags]
    m = list(block_words)
    for rnd in range(7):
        # columns
        v[0], v[4], v[8], v[12] = _g(v[0], v[4], v[8], v[12], m[0], m[1])
        v[1], v[5], v[9], v[13] = _g(v[1], v[5], v[9], v[13], m[2], m[3])
        v[2], v[6], v[10], v[14] = _g(v[2], v[6], v[10], v[14], m[4], m[5])
        v[3], v[7], v[11], v[15] = _g(v[3], v[7], v[11], v[15], m[6], m[7])
        # diagonals
        v[0], v[5], v[10], v[15] = _g(v[0], v[5], v[10], v[15], m[8], m[9])
        v[1], v[6], v[11], v[12] = _g(v[1], v[6], v[11], v[12], m[10], m[11])
        v[2], v[7], v[8], v[13] = _g(v[2], v[7], v[8], v[13], m[12], m[13])
        v[3], v[4], v[9], v[14] = _g(v[3], v[4], v[9], v[14], m[14], m[15])
        if rnd != 6:
            m = [m[i] for i in MSG_PERMUTATION]
    return (
        v[0] ^ v[8], v[1] ^ v[9], v[2] ^ v[10], v[3] ^ v[11],
        v[4] ^ v[12], v[5] ^ v[13], v[6] ^ v[14], v[7] ^ v[15],
        v[8] ^ cv[0], v[9] ^ cv[1], v[10] ^ cv[2], v[11] ^ cv[3],
        v[12] ^ cv[4], v[13] ^ cv[5], v[14] ^ cv[6], v[15] ^ cv[7],
    )


# --- Tree hashing (spec section 3.1) --------------------------------------

def _left_subtree_len(n: int) -> int:
    """Bytes in the left child of a subtree covering ``n`` > CHUNK_LEN bytes.

    "Left subtrees are full and big": the left child holds the largest
    power-of-two number of chunks strictly less than the subtree's total.
    """
    chunks = -(-n // CHUNK_LEN)  # ceil
    left_chunks = 1 << ((chunks - 1).bit_length() - 1)
    return left_chunks * CHUNK_LEN


def _chunk_cv(data, start: int, end: int, counter: int, root: bool):
    """Chaining value of the chunk spanning data[start:end].

    ``counter`` is the chunk's index in the whole input. The first block gets
    CHUNK_START, the last gets CHUNK_END (plus ROOT when this chunk is the
    tree root, i.e. the whole input is a single chunk). A partial final block
    is zero-padded; its block_len field carries the true byte count.
    """
    cv = IV
    n = end - start
    blocks = max(1, -(-n // BLOCK_LEN))  # empty chunk = one empty block
    for bi in range(blocks):
        off = start + bi * BLOCK_LEN
        take = min(BLOCK_LEN, end - off)
        flags = 0
        if bi == 0:
            flags |= CHUNK_START
        if bi == blocks - 1:
            flags |= CHUNK_END | (ROOT if root else 0)
        if take == BLOCK_LEN:
            m = _WORDS16.unpack_from(data, off)
        else:
            m = _WORDS16.unpack(data[off:end] + b"\x00" * (BLOCK_LEN - take))
        cv = _compress(cv, m, counter, take, flags)[:8]
    return cv


def _parent_cv(left, right, root: bool):
    """Chaining value of a parent node over two child CVs (spec 3.1)."""
    flags = PARENT | (ROOT if root else 0)
    return _compress(IV, left + right, 0, BLOCK_LEN, flags)[:8]


def _subtree_cv(data, start: int, end: int, chunk_counter: int):
    """Chaining value of the subtree covering data[start:end] (non-root).

    Chunks are leaves; interior nodes hash the pair (left CV, right CV).
    ``chunk_counter`` is the index of the first chunk in this subtree.
    """
    if end - start <= CHUNK_LEN:
        return _chunk_cv(data, start, end, chunk_counter, root=False)
    split = start + _left_subtree_len(end - start)
    left = _subtree_cv(data, start, split, chunk_counter)
    right = _subtree_cv(data, split, end,
                        chunk_counter + (split - start) // CHUNK_LEN)
    return _parent_cv(left, right, root=False)


def _root_cv(data):
    """Root chaining value of the whole input.

    The ROOT flag goes on the final compression (last block of a root chunk,
    or the root parent). Its packed 8-word CV equals the first 32 bytes of
    the extendable output, which is what hash32 returns.
    """
    if len(data) <= CHUNK_LEN:
        return _chunk_cv(data, 0, len(data), 0, root=True)
    split = _left_subtree_len(len(data))
    left = _subtree_cv(data, 0, split, 0)
    right = _subtree_cv(data, split, len(data), split // CHUNK_LEN)
    return _parent_cv(left, right, root=True)


# --- Public API ------------------------------------------------------------

def hash32(data: bytes) -> bytes:
    """BLAKE3 digest of ``data``, 32 bytes (hash mode)."""
    return _CV8.pack(*_root_cv(data))


def hash20(data: bytes) -> bytes:
    """BLAKE3 digest of ``data`` truncated to 20 bytes (IoStore io_hash)."""
    return _CV8.pack(*_root_cv(data))[:20]
