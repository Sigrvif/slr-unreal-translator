# SLRUnreal Phase 1 — Repair-Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** See inside both test games' containers — parse the stock IoStore TOCs end-to-end, read the footer-patched paks via their indexes, fingerprint the damage, extract real payloads (Oodle via fetched DLL), and produce IDA-derived facts about how the patched engines read the protected paks.

**Architecture:** Pure-Python `ut/` package (zero dependencies) inside the SLRUnreal dev home, mirroring SLRUnity's backend layout. Reading only — no writers, no addon JS yet. Every format layout in this plan is verified against the real game files or a cited reference source.

**Tech Stack:** Python 3.10+ (system python for tests; embedded runtime comes in a later phase), pytest, ctypes (Oodle DLL).

**Spec:** `<dev-root>/SLR Unreal Translator\docs\specs\2026-10-22-slrunreal-design.md`

## Global Constraints

- Dev home: `<dev-root>/SLR Unreal Translator`. All paths below are relative to it unless absolute.
- **No git.** Instead of commits, each task appends a dated entry to the phase ledger `.sdd/2026-10-22-phase1-repair-read/progress.md`. One source-only snapshot zip at the end of the phase (Task 10) — snapshots must never include `dist/`, `runtime/`, caches, or test-game data.
- **Zero third-party code vendored.** References are read-only: `<dev-root>/UE\reference tools\retoc\retoc\src\lib.rs` (IoStore, verified layouts), `<dev-root>/UE\reference tools\rust-u4pak-1.4.0\` (pak), `<dev-root>/UE\reference tools\u4pak-master\u4pak.py` (pak, BSD).
- Test games (do not modify):
  - `<games-root>\SampleGameA` (SampleGameA; game content in `sample_a\Content\Paks\`)
  - `<games-root>\SampleGameB` (game content in `SampleGameB\Content\Paks\`)
  - IDA exports: `<games-root>\<name>-Win64-Shipping.exe_export_for_ai\` and `<name>.exe_export_for_ai\`
- Tests that need the real games skip cleanly when the folders are absent.
- Oodle DLL is fetched (never committed, never snapshotted) to `runtime/oodle/win/oo2core_9_win64.dll` from the pinned URL in Task 4; env var `SLRUNREAL_OODLE_DLL` overrides the path.
- Windows console: run python with `python` (Git Bash). If a test prints game paths with Japanese characters, prefix `PYTHONIOENCODING=utf-8`.

## Verified format facts (use these; do not re-derive)

**IoStore TOC (`.utoc`) — from retoc `lib.rs` (ported field-for-field) and confirmed against SampleGameA's real bytes:**

- Header, 144 B (0x90), little-endian, `struct "<16s10IQ16sB3xIQII40x"`:
  `magic[16]` = `-==--==--==--==-` (16 bytes, trailing dash), `version u32`, `header_size u32` (=0x90), `entry_count u32`, `compressed_block_entry_count u32`, `compressed_block_entry_size u32` (=12), `compression_method_name_count u32`, `compression_method_name_length u32` (=32), `compression_block_size u32`, `directory_index_size u32`, `partition_count u32`, `container_id u64`, `encryption_key_guid 16B`, `container_flags u8` (1=Compressed 2=Encrypted 4=Signed 8=Indexed), 3 pad bytes, `perfect_hash_seeds_count u32`, `partition_size u64`, `chunks_without_perfect_hash_count u32`, `reserved u32`, `reserved[5] u64`.
- Body, in order: `entry_count ×` chunk ids (12 raw bytes each; chunk type = `id[11] & 0x3F`); `entry_count ×` offset/length (10 B packed big-endian: offset 40-bit then length 40-bit); `seeds_count × i32`; `without_ph_count × i32`; `block_count ×` block entries (12 B packed little-endian: offset 40-bit, compressed_size 24-bit, uncompressed_size 24-bit, method_index u8 — index 0 = stored, else `methods[idx-1]`); `method_count ×` 32-B NUL-padded ASCII names; (signatures only if flag Signed: u32 size, 2×size bytes, block_count×20 B); directory index blob (`directory_index_size` B; AES-256-ECB decrypt per 16 B block if flag Encrypted); `entry_count ×` metas (version ≥ 8: 20 B io_hash + flags u8 + 3 pad = 24 B; older: 20 B + 1 B = 21 B).
- Directory index blob = `mount_point` FString (i32 byte-length incl. NUL, then bytes; negative length = UTF-16LE incl. NUL), directory entries array (i32 count; each: 4× i32 with −1 = none: name-idx, first_child, next_sibling, first_file), file entries array (i32 count; each: name-idx i32, next_file i32 (−1 = none), user_data u32 = TOC entry index; 12 B total), string table array (i32 count; each FString).
- Chunk `io_hash` (v8+) is **BLAKE3 truncated to 20 bytes** (proven on 100/100 real entries; NOT sha1 — no stdlib blake3, so the suite does not verify hashes; Phase 2 writers must decide: in-house blake3 or skip recompute).
- Chunk data in `.ucas` is block-aligned: `first_block = offset // block_size`, `last_block = (align_up(offset+length, block_size) − 1) // block_size`; concatenate those blocks (decompress each per its method), total must equal `length`.
- SampleGameA ground truth (from the real files): `sample_a-Windows.utoc` version 8, entry_count 3247, blocks 19422, methods `["Oodle"]`, block_size 0x10000, directory_index_size 83911, flags 0x09 (Indexed|Compressed, **not** encrypted/signed), seeds 1624, partition_count 1; 1708 named files (directory index); 580/1847 directory strings are UTF-16 Japanese; ContainerHeader chunk (type 6, IoCn) lives in this TOC (entry 309), NOT in global.utoc. `global.utoc`: version 8, entry_count 1, compressed_block_entry_count 41, method_count 0, directory_index_size 0 (file is exactly 686 B; its single entry is chunk type 5). Both files parse with slack 0 (header counts explain the file exactly; raw_tail is empty).

**Pak v11 (both test games) — CORRECTED after Task 5's byte evidence: the footers were never zeroed.**
The protection is a one-byte magic patch in an otherwise intact 221 B footer, plus (SampleGameB only) AES encryption of the index and per-entry payloads:

- Footer at EOF, 221 B: `encryption_guid 16B`, `encrypted_index u8`, `magic u32`, `version u32`, `index_offset u64`, `index_size u64`, `index_sha1 20B`, method names 5×32 B. Stock magic `0x5A6F12F1`; **both test games ship `0x5A6F12E1`** (last byte patched — the patched engine accepts E1; write-back must reproduce it). The trailing 160 B method-name region is zero-padded, which is why a narrow tail hexdump reads as "zeros".
- SampleGameA: `encrypted_index=0`; index at 0xEE3E0A, PLAINTEXT, mount point `../../../`, 1669 entries, entry offsets chain to EOF. Structure per the local references (`rust-u4pak-1.4.0/src/index.rs` for the v11 encoded layout; `u4pak-master/u4pak.py` for legacy records) — implement against the bytes with the chain property as validator.
- SampleGameB: `encrypted_index=1` (AES-256, key unknown until Task 8); sampled entry flags=0x01 (payloads encrypted too). No offline recovery until the key lands; `read_index` reports the encrypted state instead of guessing.
- Inline entry record (at each entry's `Offset`), verified against both real paks:

```
i64 Offset (= record's own start — self-consistency check)
i64 Size            (compressed bytes on disk)
i64 UncompressedSize
u32 MethodIndex     (0 = stored; 1 = first footer method, "Oodle" on these games)
20B Sha1            (of the UNCOMPRESSED data)
if MethodIndex != 0:
    u32 BlockCount
    BlockCount × { u64 Start, u64 End }   (absolute file offsets; End−Start = compressed bytes)
u8 Flags            (bit0 = encrypted)
u32 BlockSize
then data (Size bytes)
```
Evidence anchors: SampleGameA pak entry 0: Offset=0, Size=Uncomp=0x4D, method 0, data (UTF-8-BOM INI) at 0x35. SampleGameB pak entry 0: Offset=0, Size=0x1C0, Uncomp=0x428, method 1, 1 block {0x49, 0x205}, flags 1, blocksize 0x428, data at 0x49.

**Oodle fetch (from repak's `oodle_loader`, same policy):**
URL `https://github.com/WorkingRobot/OodleUE/raw/refs/heads/main/Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/2.9.10/win/redist/oo2core_9_win64.dll`, SHA-256 `6f5d41a7892ea6b2db420f2458dad2f84a63901c9a93ce9497337b16c195f457`.

**IDA export formats:** `strings.txt` lines `0xADDR | len | type | text`; `pointers.txt` lines `Source_Address | Segment | Points_To_Address | Target_Name | Target_Type | Target_Detail` (4M+ rows — always grep, never read whole); `function_index.txt` blocks `Function: sub_X / Address: 0x… / File: decompile/X.c / …`; `decompile/` holds one .c per function (264k files — access by exact name).

---

### Task 1: Dev-home scaffold + pytest harness

**Files:**
- Create: `backend/ut/__init__.py` (empty), `backend/tests/__init__.py` (empty), `backend/tests/conftest.py`, `pytest.ini`, `.sdd/2026-10-22-phase1-repair-read/progress.md`

**Interfaces:**
- Produces: session fixture `game_paths -> dict` with keys `"sample_a"`, `"sample_b"` (absolute game-root paths); helper `game_paths` skips the test module when the folders are missing. All later tasks import nothing from this task's code (fixture only).

- [ ] **Step 1: Create the files**

`backend/tests/conftest.py`:
```python
import os
import pytest

GAMES = {
    "sample_a": r"<games-root>\SampleGameA",
    "sample_b": r"<games-root>\SampleGameB",
}


@pytest.fixture(scope="session")
def game_paths():
    missing = [k for k, v in GAMES.items() if not os.path.isdir(v)]
    if missing:
        pytest.skip(f"test games not present: {missing}")
    return dict(GAMES)
```

`pytest.ini`:
```ini
[pytest]
testpaths = backend/tests
```

`.sdd/2026-10-22-phase1-repair-read/progress.md`:
```markdown
# Phase 1 ledger — repair-read

## 2026-10-22 — Task 1: scaffold
- backend/ut + backend/tests created, pytest.ini at dev root.
- Tests: python -m pytest backend/tests -q → no tests ran (collection OK).
```

- [ ] **Step 2: Verify collection**

Run: `cd "/e/Dev/SLR Unreal Translator" && python -m pytest backend/tests -q`
Expected: `no tests ran` (exit 5) or 0 errors — collection works, conftest imports cleanly.

- [ ] **Step 3: Verify game fixtures resolve**

Run: `python -c "from backend.tests.conftest import GAMES; import os; print({k: os.path.isdir(v) for k, v in GAMES.items()})"`
Expected: `{'sample_a': True, 'sample_b': True}`

---

### Task 2: IoStore TOC header parser

**Files:**
- Create: `backend/ut/iostore.py`
- Test: `backend/tests/test_iostore_header.py`

**Interfaces:**
- Produces: `TOC_MAGIC: bytes`; `TOC_HEADER_SIZE: int = 0x90`;
  `parse_toc_header(data: bytes, offset: int = 0) -> TocHeader`;
  `serialize_toc_header(h: TocHeader) -> bytes`;
  `TocHeader` dataclass with fields exactly as in the Verified format facts section, plus properties `encrypted: bool`, `signed: bool`.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_iostore_header.py`:
```python
import os

import pytest

from backend.ut.iostore import TOC_HEADER_SIZE, parse_toc_header, serialize_toc_header

SUMMER = os.path.join("sample_a", "Content", "Paks")
GLOBAL = os.path.join("sample_a", "Content", "Paks")


def _read(game_paths, rel):
    with open(os.path.join(game_paths["sample_a"], rel), "rb") as f:
        return f.read()


def test_header_fields_and_roundtrip_summer(game_paths):
    data = _read(game_paths, os.path.join(SUMMER, "sample_a-Windows.utoc"))
    h = parse_toc_header(data)
    assert h.version == 8
    assert h.header_size == TOC_HEADER_SIZE
    assert h.entry_count == 3247
    assert h.compressed_block_entry_count == 19422
    assert h.compressed_block_entry_size == 12
    assert h.compression_method_name_count == 1
    assert h.compression_method_name_length == 32
    assert h.compression_block_size == 0x10000
    assert h.directory_index_size == 83911
    assert h.partition_count == 1
    assert h.perfect_hash_seeds_count == 1624
    assert not h.encrypted
    assert not h.signed
    assert serialize_toc_header(h) == data[:TOC_HEADER_SIZE]


def test_header_global(game_paths):
    data = _read(game_paths, os.path.join(GLOBAL, "global.utoc"))
    h = parse_toc_header(data)
    assert h.version == 8
    assert h.entry_count == 1
    assert h.compressed_block_entry_count == 41
    assert h.directory_index_size == 0
    assert not h.encrypted


def test_bad_magic_raises():
    with pytest.raises(ValueError):
        parse_toc_header(b"\x00" * 256)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/e/Dev/SLR Unreal Translator" && python -m pytest backend/tests/test_iostore_header.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.ut.iostore'`

- [ ] **Step 3: Implement**

`backend/ut/iostore.py`:
```python
"""IoStore (.utoc/.ucas) reader. Layouts verified against retoc and the real
SampleGameA files; see docs/plans/2026-10-22-phase1-repair-read.md."""
from __future__ import annotations

import struct
from dataclasses import dataclass

TOC_MAGIC = b"-==--==--==--==-"  # stock Epic magic constant, not damage
TOC_HEADER_SIZE = 0x90

_HEADER = struct.Struct("<16s10IQ16sB3xIQII40x")

FLAG_COMPRESSED = 0b0001
FLAG_ENCRYPTED = 0b0010
FLAG_SIGNED = 0b0100
FLAG_INDEXED = 0b1000


@dataclass
class TocHeader:
    version: int
    header_size: int
    entry_count: int
    compressed_block_entry_count: int
    compressed_block_entry_size: int
    compression_method_name_count: int
    compression_method_name_length: int
    compression_block_size: int
    directory_index_size: int
    partition_count: int
    container_id: int
    encryption_key_guid: bytes
    container_flags: int
    perfect_hash_seeds_count: int
    partition_size: int
    chunks_without_perfect_hash_count: int

    @property
    def encrypted(self) -> bool:
        return bool(self.container_flags & FLAG_ENCRYPTED)

    @property
    def signed(self) -> bool:
        return bool(self.container_flags & FLAG_SIGNED)


def parse_toc_header(data: bytes, offset: int = 0) -> TocHeader:
    if len(data) - offset < TOC_HEADER_SIZE:
        raise ValueError("utoc too small for header")
    (magic, version, header_size, entry_count, block_count, block_entry_size,
     method_count, method_name_len, block_size, dir_index_size, partition_count,
     container_id, key_guid, flags, seeds_count, partition_size, without_ph,
     _reserved7) = _HEADER.unpack_from(data, offset)
    if magic != TOC_MAGIC:
        raise ValueError(f"bad utoc magic: {magic!r}")
    if header_size != TOC_HEADER_SIZE:
        raise ValueError(f"unexpected utoc header size {header_size:#x}")
    return TocHeader(version, header_size, entry_count, block_count, block_entry_size,
                     method_count, method_name_len, block_size, dir_index_size,
                     partition_count, container_id, key_guid, flags, seeds_count,
                     partition_size, without_ph)


def serialize_toc_header(h: TocHeader) -> bytes:
    return _HEADER.pack(
        TOC_MAGIC, h.version, h.header_size, h.entry_count,
        h.compressed_block_entry_count, h.compressed_block_entry_size,
        h.compression_method_name_count, h.compression_method_name_length,
        h.compression_block_size, h.directory_index_size, h.partition_count,
        h.container_id, h.encryption_key_guid, h.container_flags,
        h.perfect_hash_seeds_count, h.partition_size,
        h.chunks_without_perfect_hash_count, 0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/tests/test_iostore_header.py -q`
Expected: 3 passed (or skipped if games absent — must not fail)

- [ ] **Step 5: Ledger**

Append to `.sdd/2026-10-22-phase1-repair-read/progress.md`:
```markdown
## Task 2: utoc header parser
- backend/ut/iostore.py: TocHeader parse/serialize (144 B layout).
- Tests: test_iostore_header.py — real-file field assertions + byte round-trip.
```

---

### Task 3: Full TOC parse — entries, blocks, methods, directory index, round-trip

**Files:**
- Modify: `backend/ut/iostore.py` (append)
- Test: `backend/tests/test_iostore_toc.py`

**Interfaces:**
- Consumes: `TocHeader`, `parse_toc_header`, `serialize_toc_header` (Task 2).
- Produces:
  - `ChunkEntry` dataclass: `chunk_id: bytes` (12), `offset: int`, `length: int`, `meta_flags: int` (1=Compressed, 2=MemoryMapped), `io_hash: bytes` (20; zeros if unavailable)
  - `Toc` dataclass with `header: TocHeader`, `entries: list[ChunkEntry]`, `blocks: list[tuple[int, int, int, int]]` (offset, csize, usize, method_index), `compression_methods: list[str]`, `mount_point: str`, `file_map: dict[str, int]` (mount-relative path → entry index), `raw_tail: bytes`
  - `parse_toc(data: bytes) -> Toc`
  - `serialize_toc(toc: Toc) -> bytes`
  - `Toc.file_names() -> list[str]` (full paths: mount_point + walk)

- [ ] **Step 1: Write the failing test**

`backend/tests/test_iostore_toc.py`:
```python
import os

from backend.ut.iostore import parse_toc, serialize_toc


def _toc_path(game_paths, name):
    return os.path.join(game_paths["sample_a"], "sample_a", "Content", "Paks", name)


def test_full_parse_and_roundtrip_summer(game_paths):
    with open(_toc_path(game_paths, "sample_a-Windows.utoc"), "rb") as f:
        data = f.read()
    toc = parse_toc(data)
    assert len(toc.entries) == 3247
    assert len(toc.blocks) == 19422
    assert toc.compression_methods == ["Oodle"]
    names = toc.file_names()
    assert 3000 < len(names) <= 3247          # files only (non-file chunks have no name)
    assert toc.mount_point.startswith("../../../")
    for name in names:
        assert name.startswith(toc.mount_point)
    for idx in toc.file_map.values():
        assert 0 <= idx < len(toc.entries)
    # every entry's block range stays inside the block table
    bs = toc.header.compression_block_size
    for e in toc.entries:
        if e.length == 0:
            continue
        first = e.offset // bs
        last = (e.offset + e.length - 1) // bs
        assert 0 <= first <= last < len(toc.blocks)
    assert serialize_toc(toc) == data         # golden: byte-identical round-trip
    assert 0 < len(toc.raw_tail) < 4096       # trailing opaque bytes (~128 observed)


def test_full_parse_global(game_paths):
    with open(_toc_path(game_paths, "global.utoc"), "rb") as f:
        data = f.read()
    toc = parse_toc(data)
    assert len(toc.entries) == 1
    assert toc.file_names() == []             # no directory index in global.utoc
    assert serialize_toc(toc) == data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "/e/Dev/SLR Unreal Translator" && python -m pytest backend/tests/test_iostore_toc.py -q`
Expected: FAIL — `ImportError: cannot import name 'parse_toc'`

- [ ] **Step 3: Implement (append to `backend/ut/iostore.py`)**

```python
import io

_OFFSET_LEN = struct.Struct(">5s5s")            # big-endian 40-bit offset + length
_BLOCK = struct.Struct("<5s3s3sB")              # LE: offset 40, csize 24, usize 24, method
_META_V8 = struct.Struct("<20sB3x")
_META_OLD = struct.Struct("<20sB")

CHUNKTYPE_CONTAINER_HEADER = 6


def _u40(be5: bytes) -> int:
    return int.from_bytes(be5, "big")


def _pack40(v: int) -> bytes:
    return v.to_bytes(5, "big")


def _read_fstring(buf: io.BytesIO) -> str:
    (n,) = struct.unpack("<i", buf.read(4))
    if n == 0:
        return ""
    raw = buf.read(n)
    return raw[:-1].decode("utf-8") if raw.endswith(b"\x00") else raw.decode("utf-8")


def _write_fstring(out: io.BytesIO, s: str) -> None:
    if not s:
        out.write(struct.pack("<i", 0))
    else:
        raw = s.encode("utf-8") + b"\x00"
        out.write(struct.pack("<i", len(raw)))
        out.write(raw)


def _read_i32_array(buf: io.BytesIO, count: int) -> list[int]:
    return list(struct.unpack(f"<{count}i", buf.read(4 * count))) if count else []


@dataclass
class ChunkEntry:
    chunk_id: bytes
    offset: int
    length: int
    meta_flags: int
    io_hash: bytes

    @property
    def chunk_type(self) -> int:
        return self.chunk_id[11] & 0x3F


@dataclass
class _DirEntry:
    name: int      # index into string table, -1 = none
    first_child: int
    next_sibling: int
    first_file: int


@dataclass
class _FileEntry:
    name: int
    next_file: int
    user_data: int


@dataclass
class Toc:
    header: TocHeader
    entries: list[ChunkEntry]
    perfect_hash_seeds: list[int]
    chunks_without_perfect_hash: list[int]
    blocks: list[tuple[int, int, int, int]]
    compression_methods: list[str]
    mount_point: str
    file_map: dict[str, int]
    raw_tail: bytes
    _dir_entries: list[_DirEntry]
    _file_entries: list[_FileEntry]
    _string_table: list[str]
    _directory_blob: bytes

    def file_names(self) -> list[str]:
        names = []

        def walk(dir_idx: int, prefix: str) -> None:
            d = self._dir_entries[dir_idx]
            if d.name >= 0:
                prefix = prefix + self._string_table[d.name] + "/"
            f = d.first_file
            while f != -1:
                fe = self._file_entries[f]
                names.append(self.mount_point + prefix + self._string_table[fe.name])
                f = fe.next_file
            c = d.first_child
            while c != -1:
                walk(c, prefix)
                c = self._dir_entries[c].next_sibling

        if self._dir_entries:
            walk(0, "")
        return names


def parse_toc(data: bytes) -> Toc:
    h = parse_toc_header(data)
    pos = TOC_HEADER_SIZE
    need = (h.entry_count * 12 + h.entry_count * 10
            + h.perfect_hash_seeds_count * 4 + h.chunks_without_perfect_hash_count * 4
            + h.compressed_block_entry_count * 12
            + h.compression_method_name_count * h.compression_method_name_length
            + h.directory_index_size
            + h.entry_count * (24 if h.version >= 8 else 21))
    if len(data) - pos < need:
        raise ValueError(f"utoc truncated: need {need} bytes after header, have {len(data) - pos}")

    entries: list[ChunkEntry] = []
    raw_ids = data[pos:pos + h.entry_count * 12]
    pos += h.entry_count * 12
    raw_ol = data[pos:pos + h.entry_count * 10]
    pos += h.entry_count * 10
    seeds = _read_i32_array(io.BytesIO(data[pos:pos + h.perfect_hash_seeds_count * 4]),
                            h.perfect_hash_seeds_count)
    pos += h.perfect_hash_seeds_count * 4
    without_ph = _read_i32_array(
        io.BytesIO(data[pos:pos + h.chunks_without_perfect_hash_count * 4]),
        h.chunks_without_perfect_hash_count)
    pos += h.chunks_without_perfect_hash_count * 4

    blocks = []
    for i in range(h.compressed_block_entry_count):
        off, cs, us, mi = _BLOCK.unpack_from(data, pos + i * 12)
        blocks.append((int.from_bytes(off, "little"),
                       int.from_bytes(cs, "little"),
                       int.from_bytes(us, "little"), mi))
    pos += h.compressed_block_entry_count * 12

    methods = []
    for i in range(h.compression_method_name_count):
        name = data[pos + i * h.compression_method_name_length:
                    pos + (i + 1) * h.compression_method_name_length]
        methods.append(name.split(b"\x00")[0].decode("ascii"))
    pos += h.compression_method_name_count * h.compression_method_name_length

    if h.signed:
        raise ValueError("signed containers not supported yet (no test game uses them)")
    directory_blob = data[pos:pos + h.directory_index_size]
    pos += h.directory_index_size
    if h.encrypted:
        raise ValueError("encrypted TOC not supported (no test game uses it)")

    meta_struct = _META_V8 if h.version >= 8 else _META_OLD
    for i in range(h.entry_count):
        cid = raw_ids[i * 12:(i + 1) * 12]
        o, l = _OFFSET_LEN.unpack_from(raw_ol, i * 10)
        hsh, mflags = meta_struct.unpack_from(data, pos + i * meta_struct.size)
        entries.append(ChunkEntry(cid, _u40(o), _u40(l), mflags, hsh))
    pos += h.entry_count * meta_struct.size

    mount_point = ""
    dir_entries: list[_DirEntry] = []
    file_entries: list[_FileEntry] = []
    string_table: list[str] = []
    if directory_blob:
        buf = io.BytesIO(directory_blob)
        mount_point = _read_fstring(buf)
        (dc,) = struct.unpack("<i", buf.read(4))
        dir_entries = [_DirEntry(*struct.unpack("<4i", buf.read(16))) for _ in range(max(dc, 0))]
        (fc,) = struct.unpack("<i", buf.read(4))
        file_entries = [_FileEntry(*struct.unpack("<3iI", buf.read(16))) for _ in range(max(fc, 0))]
        (sc,) = struct.unpack("<i", buf.read(4))
        string_table = [_read_fstring(buf) for _ in range(max(sc, 0))]

    file_map = {name: fe.user_data
                for name, fe in zip(_collect_paths(mount_point, dir_entries, file_entries, string_table),
                                    _iter_files(dir_entries, file_entries))}
    return Toc(h, entries, seeds, without_ph, blocks, methods, mount_point, file_map,
               data[pos:], dir_entries, file_entries, string_table, directory_blob)


def _iter_files(dir_entries, file_entries):
    def walk(i):
        d = dir_entries[i]
        f = d.first_file
        while f != -1:
            yield file_entries[f]
            f = file_entries[f].next_file
        c = d.first_child
        while c != -1:
            yield from walk(c)
            c = dir_entries[c].next_sibling
    if dir_entries:
        yield from walk(0)


def _collect_paths(mount_point, dir_entries, file_entries, string_table):
    result = []
    def walk(i, prefix):
        d = dir_entries[i]
        p = prefix + (string_table[d.name] + "/" if d.name >= 0 else "")
        f = d.first_file
        while f != -1:
            result.append(mount_point + p + string_table[file_entries[f].name])
            f = file_entries[f].next_file
        c = d.first_child
        while c != -1:
            walk(c, p)
            c = dir_entries[c].next_sibling
    if dir_entries:
        walk(0, "")
    return result


def serialize_toc(toc: Toc) -> bytes:
    h = toc.header
    out = bytearray()
    out += serialize_toc_header(h)
    for e in toc.entries:
        out += e.chunk_id
    for e in toc.entries:
        out += _pack40(e.offset) + _pack40(e.length)
    out += struct.pack(f"<{len(toc.perfect_hash_seeds)}i", *toc.perfect_hash_seeds)
    out += struct.pack(f"<{len(toc.chunks_without_perfect_hash)}i", *toc.chunks_without_perfect_hash)
    for off, cs, us, mi in toc.blocks:
        out += _BLOCK.pack(off.to_bytes(5, "little"), cs.to_bytes(3, "little"),
                           us.to_bytes(3, "little"), mi)
    for m in toc.compression_methods:
        raw = m.encode("ascii")[:31]
        out += raw + b"\x00" * (h.compression_method_name_length - len(raw))
    out += toc._directory_blob
    meta_struct = _META_V8 if h.version >= 8 else _META_OLD
    for e in toc.entries:
        out += meta_struct.pack(e.io_hash, e.meta_flags)
    out += toc.raw_tail
    return bytes(out)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest backend/tests/test_iostore_toc.py backend/tests/test_iostore_header.py -q`
Expected: all pass. If the round-trip assert fails, the byte-accounting is off — compare `serialize_toc` output against `data` to find the first diverging section **before** changing any layout assumption; the sizes in the header must explain the file exactly (`len(data) == need + header + len(raw_tail)`).

- [ ] **Step 5: Ledger**

Append to `.sdd/2026-10-22-phase1-repair-read/progress.md`:
```markdown
## Task 3: full TOC parse
- entries/blocks/methods/directory-index parse + byte-identical round-trip on both utocs.
- raw_tail size observed: <fill in actual number> bytes (sample_a-Windows.utoc).
```

---

### Task 4: Oodle layer + chunk extraction

**Files:**
- Create: `build/fetch_oodle.py`, `backend/ut/oodle.py`
- Modify: `backend/ut/iostore.py` (append `read_entry`)
- Test: `backend/tests/test_oodle_extract.py`

**Interfaces:**
- Consumes: `Toc`, `ChunkEntry`, `TocHeader.compression_block_size` (Tasks 2–3).
- Produces:
  - `build/fetch_oodle.py` — downloads the pinned DLL to `runtime/oodle/win/oo2core_9_win64.dll`, verifies SHA-256, idempotent.
  - `backend/ut/oodle.py`: `find_dll(explicit: str | None = None) -> str` (order: explicit arg → `SLRUNREAL_OODLE_DLL` env → dev-home `runtime/oodle/win/oo2core_9_win64.dll`; raises `FileNotFoundError` listing all tried paths); `get_dll() -> ctypes.CDLL` (cached); `decompress(dll, src: bytes, dst_len: int) -> bytes` (returns exactly `dst_len` bytes or raises).
  - `iostore.read_entry(toc: Toc, ucas_path: str, entry_index: int, dll=None) -> bytes` — concatenates/decompresses the entry's blocks; asserts `offset % block_size == 0` and result length == `entry.length`.

- [ ] **Step 1: Fetch the DLL**

`build/fetch_oodle.py`:
```python
"""Fetch the pinned Oodle SDK decompression DLL (same source/policy as repak's
oodle_loader). Never committed, never shipped inside a package."""
import hashlib
import os
import sys
import urllib.request

URL = ("https://github.com/WorkingRobot/OodleUE/raw/refs/heads/main/"
       "Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/2.9.10/win/redist/oo2core_9_win64.dll")
SHA256 = "6f5d41a7892ea6b2db420f2458dad2f84a63901c9a93ce9497337b16c195f457"
DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "runtime", "oodle", "win", "oo2core_9_win64.dll")


def main() -> int:
    if os.path.isfile(DEST):
        with open(DEST, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() == SHA256:
                print(f"[oodle] already present: {DEST}")
                return 0
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    print(f"[oodle] downloading {URL}")
    with urllib.request.urlopen(URL) as r, open(DEST + ".part", "wb") as f:
        f.write(r.read())
    with open(DEST + ".part", "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != SHA256:
        os.remove(DEST + ".part")
        print(f"ERROR: sha256 mismatch {digest}", file=sys.stderr)
        return 1
    os.replace(DEST + ".part", DEST)
    print(f"[oodle] ok: {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run: `cd "/e/Dev/SLR Unreal Translator" && python build/fetch_oodle.py`
Expected: `[oodle] ok: ...` (or `already present` on re-run).

- [ ] **Step 2: Write the failing test**

`backend/tests/test_oodle_extract.py`:
```python
import os
import struct

import pytest

from backend.ut import iostore, oodle

PAKS = os.path.join("sample_a", "Content", "Paks")


def _dll():
    return oodle.get_dll()


def test_decompress_dll_loads():
    dll = _dll()
    assert dll is not None


def test_extract_container_header_chunk(game_paths):
    # Ruling (Task 4): the ContainerHeader chunk lives in the game container's
    # TOC (sample_a-Windows.utoc), not global.utoc; the magic u32
    # 0x496F436E is LE-serialized (bytes b"nCoI").
    with open(os.path.join(game_paths["sample_a"], PAKS,
                           "sample_a-Windows.utoc"), "rb") as f:
        toc = iostore.parse_toc(f.read())
    ucas = os.path.join(game_paths["sample_a"], PAKS,
                        "sample_a-Windows.ucas")
    idx = next(i for i, e in enumerate(toc.entries)
               if e.chunk_type == iostore.CHUNKTYPE_CONTAINER_HEADER)
    data = iostore.read_entry(toc, ucas, idx, dll=_dll())
    assert struct.unpack("<I", data[:4])[0] == 0x496F436E


def test_extract_sample_lengths(game_paths):
    # Ruling (Task 4): io_hash is BLAKE3 truncated to 20 bytes (proven 100/100
    # entries with a dev-only blake3 install). hashlib has no blake3 and no
    # third-party deps are allowed, so the suite gates integrity on
    # read_entry's internal length check + the LE magic above, and walks both
    # stored and Oodle-compressed entries to exercise both block paths.
    with open(os.path.join(game_paths["sample_a"], PAKS,
                           "sample_a-Windows.utoc"), "rb") as f:
        toc = iostore.parse_toc(f.read())
    ucas = os.path.join(game_paths["sample_a"], PAKS,
                        "sample_a-Windows.ucas")
    dll = _dll()
    bs = toc.header.compression_block_size
    checked = compressed = 0
    for i, e in enumerate(toc.entries):
        if e.length == 0:
            continue
        if toc.blocks[e.offset // bs][3] != 0:
            compressed += 1
        iostore.read_entry(toc, ucas, i, dll=dll)  # raises on length mismatch
        checked += 1
        if checked >= 100:
            break
    assert checked >= 25
    assert compressed >= 5
```

Note: the sha1 assertion encodes the assumption that v8 `io_hash` is SHA-1 of the uncompressed chunk. If it fails while the `IoCn` test passes, inspect before weakening: hash a few entries' data with sha1/sha256/truncated variants and record the real rule in the ledger — only then adjust the test (and note it in the plan copy).

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_oodle_extract.py -q`
Expected: FAIL — `FileNotFoundError` from `find_dll` (module missing).

- [ ] **Step 4: Implement**

`backend/ut/oodle.py`:
```python
"""ctypes binding for the user-side Oodle SDK DLL (fetch via build/fetch_oodle.py)."""
from __future__ import annotations

import ctypes
import os
import threading

_LOCK = threading.Lock()
_DLL = None

_DEV_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "runtime", "oodle", "win", "oo2core_9_win64.dll")


def find_dll(explicit: str | None = None) -> str:
    tried = []
    for cand in (explicit, os.environ.get("SLRUNREAL_OODLE_DLL"), _DEV_DEFAULT):
        if not cand:
            continue
        tried.append(cand)
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        "oo2core DLL not found (run build/fetch_oodle.py or set SLRUNREAL_OODLE_DLL): "
        + "; ".join(tried))


def get_dll() -> ctypes.CDLL:
    global _DLL
    with _LOCK:
        if _DLL is not None:
            return _DLL
        path = find_dll()
        dll = ctypes.CDLL(path)
        # OodleLZ_Decompress 2.9.x signature (15 args); returns decoded byte count
        # or 0 on failure.
        fn = dll.OodleLZ_Decompress
        fn.restype = ctypes.c_longlong
        fn.argtypes = [ctypes.c_char_p, ctypes.c_longlong, ctypes.c_char_p,
                       ctypes.c_longlong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_void_p,
                       ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_ulonglong, ctypes.c_int]
        _DLL = dll
        return _DLL


def decompress(dll: ctypes.CDLL, src: bytes, dst_len: int) -> bytes:
    dst = ctypes.create_string_buffer(dst_len)
    n = dll.OodleLZ_Decompress(src, len(src), dst, dst_len, 1, 0, 0,
                               None, 0, None, 0, None, None, 0, 0)
    if n != dst_len:
        raise ValueError(f"oodle decompress returned {n}, expected {dst_len}")
    return dst.raw[:dst_len]
```

Append to `backend/ut/iostore.py`:
```python
def _align_up(v: int, a: int) -> int:
    return (v + a - 1) & ~(a - 1)


def read_entry(toc: Toc, ucas_path: str, entry_index: int, dll=None) -> bytes:
    e = toc.entries[entry_index]
    bs = toc.header.compression_block_size
    if e.offset % bs != 0:
        raise ValueError(f"entry {entry_index} offset {e.offset:#x} not block-aligned")
    first = e.offset // bs
    last = (_align_up(e.offset + e.length, bs) - 1) // bs
    out = bytearray()
    with open(ucas_path, "rb") as f:
        for bi in range(first, last + 1):
            off, csize, usize, mi = toc.blocks[bi]
            f.seek(off)
            blob = f.read(csize)
            if mi == 0:
                if len(blob) != usize:
                    raise ValueError(f"block {bi}: stored size mismatch")
                out += blob
            else:
                from . import oodle
                method = toc.compression_methods[mi - 1]
                if method != "Oodle":
                    raise ValueError(f"unsupported compression method {method!r}")
                out += oodle.decompress(dll or oodle.get_dll(), blob, usize)
    if len(out) != e.length:
        raise ValueError(f"entry {entry_index}: read {len(out)} bytes, expected {e.length}")
    return bytes(out)
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest backend/tests/test_oodle_extract.py -q`
Expected: 3 passed. The `IoCn` test is the hard gate; if `argtypes` mismatch crashes python, re-check the 15-arg signature against the DLL's export (crash on call = wrong arg count — try the 13-arg variant used by some wrappers and record which one works in the ledger).

- [ ] **Step 6: Ledger**

Append to `.sdd/2026-10-22-phase1-repair-read/progress.md`:
```markdown
## Task 4: oodle + chunk extraction
- build/fetch_oodle.py (pinned URL+sha256), ut/oodle.py ctypes, iostore.read_entry.
- ContainerHeader chunk extracted (IoCn); sample sha1 verification result: <pass/fail + rule>.
- OodleLZ_Decompress arg signature used: <15-arg / 13-arg>.
```

---

### Task 5: Pak v11 footer + index reader (revised after premise correction)

**Files:**
- Create: `backend/ut/pak.py`
- Test: `backend/tests/test_pak_scan.py`

**Interfaces:**
- Consumes: `backend.ut.oodle` (`get_dll`, `decompress`).
- Produces:
  - `PAK_MAGIC_STOCK = 0x5A6F12F1`, `PAK_MAGIC_PATCHED = 0x5A6F12E1`, `FOOTER_SIZE_V11 = 221`
  - `Footer` dataclass: `encryption_guid: bytes`, `encrypted_index: bool`, `magic: int`, `version: int`, `index_offset: int`, `index_size: int`, `index_sha1: bytes`; property `magic_variant` returning `"stock"`, `"patched-e1"`, or `"other"`.
  - `read_footer(path) -> Footer` — raises ValueError unless magic is stock or patched-e1.
  - `PakEntry` dataclass: `name: str` (index name, mount-relative), plus `offset, size, uncompressed_size, method, sha1, block_count, blocks, flags, block_size, data_offset`.
  - `PakIndex` dataclass: `mount_point: str`, `entries: list[PakEntry]`, `encrypted: bool`, `footer: Footer`.
  - `read_index(path, footer=None) -> PakIndex` — plaintext path parses the v11 index (mount point, entry count, per-entry names+metadata, porting the STRUCTURE from the local references, reference-reading only, original code); validate with SampleGameA's observed properties (1669 entries, names sane, min offset 0, no offset beyond `index_offset`); encrypted path returns empty entries with `encrypted=True`.
  - `read_entry_data(path, entry, dll=None, aes_key=None) -> bytes` — seek `entry.offset`, parse the inline record, cross-check against the index metadata, read `entry.size` bytes; stored returns directly; compressed per-block Oodle with per-block uncompressed size `min(block_size, uncompressed_size - produced)`; `flags & 1` (encrypted payload) requires `aes_key` (AES-256-ECB per 16 B before decompression) else raises; verifies sha1 of the final result on the plaintext path.
  - `scan_pak(...) -> ScanResult` — the inline walker from the pre-revision brief, kept as a DIAGNOSTIC only (both games' data regions break it after a few records; never a source of truth).

- [ ] **Step 1: Write the failing test**

`backend/tests/test_pak_scan.py`:
```python
import hashlib
import os

import pytest

from backend.ut import pak


def _pak(game_paths, game, rel):
    return os.path.join(game_paths[game], *rel)


def test_footer_and_index_sample_a(game_paths):
    p = _pak(game_paths, "sample_a", ("sample_a", "Content", "Paks",
                                          "sample_a-Windows.pak"))
    f = pak.read_footer(p)
    assert f.magic == pak.PAK_MAGIC_PATCHED       # the E1 patch IS the damage
    assert f.version == 11
    assert not f.encrypted_index
    idx = pak.read_index(p, f)
    assert not idx.encrypted
    assert idx.mount_point.startswith("../../../")
    assert len(idx.entries) == 1669
    for e in idx.entries:
        assert e.name and not e.name.startswith("/") and "\\" not in e.name
        assert e.offset < f.index_offset
    e0 = min(idx.entries, key=lambda e: e.offset)
    assert e0.offset == 0 and e0.uncompressed_size == 0x4D   # the BOM INI at offset 0
    assert e0.method == 0


def test_read_entry_data_sample_a_first_entry(game_paths):
    p = _pak(game_paths, "sample_a", ("sample_a", "Content", "Paks",
                                          "sample_a-Windows.pak"))
    idx = pak.read_index(p)
    e0 = min(idx.entries, key=lambda e: e.offset)
    data = pak.read_entry_data(p, e0)
    assert data.startswith(b"\xef\xbb\xbf")
    assert hashlib.sha1(data).digest() == e0.sha1


def test_footer_encrypted_index_sample_b(game_paths):
    p = _pak(game_paths, "sample_b", ("SampleGameB", "Content", "Paks",
                                          "SampleGameB-Windows.pak"))
    f = pak.read_footer(p)
    assert f.magic == pak.PAK_MAGIC_PATCHED
    assert f.version == 11
    assert f.encrypted_index                     # index+payload AES — Task 8 hunts the key
    idx = pak.read_index(p, f)
    assert idx.encrypted and idx.entries == []
```

- [ ] **Step 2: Run test to verify it fails** — `python -m pytest backend/tests/test_pak_scan.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `backend/ut/pak.py`**

Footer: read last 221 B, unpack guid/encrypted/magic/version/index_offset/index_size/sha1/names, validate magic. Index: seek `index_offset`, read `index_size` bytes; parse mount FString (length sign selects ASCII vs UTF-16), entry count, then the encoded layout per the references; validate per the Interfaces notes. Encrypted index: return the encrypted PakIndex without reading the blob. `read_entry_data`: per Interfaces; encrypted-without-key raises `ValueError("payload encrypted; AES key required (Task 8)")`.

- [ ] **Step 4: Run tests** — `python -m pytest backend/tests -q`; all pass. If the encoded-index layout resists porting, STOP and report BLOCKED with the first divergent offset — do not redesign the format model.

- [ ] **Step 5: Ledger** — append observed values (entry count, name samples, index layout family used: v11-encoded vs legacy-records).

### Task 6: Manifest cross-reference

**Files:**
- Create: `backend/ut/manifest.py`
- Test: `backend/tests/test_manifest.py`

**Interfaces:**
- Consumes: `PakEntry` list (Task 5).
- Produces: `load_ufs(game_root: str) -> list[str]` (paths from `Manifest_UFSFiles_Win64.txt`, preserving order); `normalize_pak_name(mount_point: str, name: str) -> str` (strip the mount prefix, e.g. `../../../Game/`); `intersect_names(entries, ufs) -> float` (fraction of index entries whose normalized name is present in the UFS list — SampleGameA's plaintext index makes name coverage the primary cross-check); `assign_by_order(entries, ufs) -> tuple[dict[int, str], float]` (exact-count order fallback, unchanged semantics).

- [ ] **Step 1: Write the failing test**

`backend/tests/test_manifest.py`:
```python
import os

from backend.ut import manifest


def _ufs(game_paths, game):
    root = game_paths[game]
    return os.path.join(root, "Manifest_UFSFiles_Win64.txt")


def test_load_ufs_summer(game_paths):
    ufs = manifest.load_ufs(_ufs(game_paths, "sample_a"))
    assert len(ufs) == 4737
    assert any(p.endswith(".ini") for p in ufs)
    assert any("Content/Paks" not in p for p in ufs)   # UFS = staged-for-containers list


def test_intersect_names_and_normalize():
    class E:
        def __init__(self, name):
            self.name = name
    ufs = ["Game/Content/A.ini", "Game/Content/B.uexp", "Engine/Content/C.uasset"]
    entries = [E("../../../Game/Content/A.ini"), E("../../../Game/Content/B.uexp")]
    assert manifest.normalize_pak_name("../../../Game/",
                                       "../../../Game/Content/A.ini") == "Game/Content/A.ini"
    assert manifest.intersect_names(entries, ufs) == 1.0
    assert manifest.intersect_names(entries + [E("../../../Game/Content/X.ini")],
                                    ufs) == pytest.approx(2 / 3)


def test_assign_by_order_exact_and_mismatch():
    entries = [type("E", (), {"__dict__": {}})() for _ in range(3)]
    mapping, conf = manifest.assign_by_order(entries, ["a", "b", "c"])
    assert mapping == {0: "a", 1: "b", 2: "c"} and conf == 1.0
    mapping, conf = manifest.assign_by_order(entries, ["a", "b"])
    assert mapping == {} and conf == 0.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest backend/tests/test_manifest.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

`backend/ut/manifest.py`:
```python
"""Manifest_UFSFiles cross-reference. The UFS manifest lists staged files in pack
order; when the scanned pak entry count matches exactly, order-based name
assignment is safe. Anything else is reported, not guessed."""
from __future__ import annotations


def load_ufs(manifest_path: str) -> list[str]:
    paths: list[str] = []
    with open(manifest_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            path = line.split("\t", 1)[0].strip()
            if path:
                paths.append(path)
    return paths


def assign_by_order(entries, ufs: list[str]) -> tuple[dict[int, str], float]:
    if len(entries) == len(ufs) and entries:
        return {i: p for i, p in enumerate(ufs)}, 1.0
    return {}, 0.0
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest backend/tests/test_manifest.py -q`
Expected: 2 passed.

- [ ] **Step 5: Ledger**

Append to `.sdd/2026-10-22-phase1-repair-read/progress.md`:
```markdown
## Task 6: manifest cross-ref
- load_ufs + strict order-based assignment (exact-count only).
- sample_b count check happens in Task 10 (needs full scan).
```

---


### Task 7: Protection fingerprint

**Files:**
- Create: `backend/ut/protection.py`
- Test: `backend/tests/test_protection.py`

**Interfaces:**
- Consumes: `pak.read_footer` (Task 5), `parse_toc_header` (Task 2).
- Produces: `find_game(game_root: str) -> dict` — locates the shipping exe, all paks/utocs/ucas under `*/Content/Paks`, engine dir, and `Manifest_UFSFiles` files. `fingerprint(game_root: str) -> dict` — per-container damage report: paks get `{path, kind: "pak", footer: "patched-e1"|"stock"|"other"|bad-message, index_encrypted: bool|None, entries: None}`; utocs get `{path, kind: "utoc", header: "ok", version, entry_count, encrypted, signed}`; plus `engine: "ue5"`, `iostore: true|false`. Raises nothing on damaged input — damage is data, not error.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_protection.py`:
```python
from backend.ut import protection


def test_fingerprint_sample_a(game_paths):
    fp = protection.fingerprint(game_paths["sample_a"])
    assert fp["engine"] == "ue5"
    assert fp["iostore"] is True
    paks = [c for c in fp["containers"] if c["kind"] == "pak"]
    utocs = [c for c in fp["containers"] if c["kind"] == "utoc"]
    assert all(p["footer"] == "patched-e1" for p in paks)
    assert all(not p["index_encrypted"] for p in paks)
    assert len(utocs) == 2
    assert all(u["header"] == "ok" and not u["encrypted"] for u in utocs)
    assert any(u["entry_count"] == 3247 for u in utocs)


def test_fingerprint_sample_b(game_paths):
    fp = protection.fingerprint(game_paths["sample_b"])
    assert fp["iostore"] is False
    paks = [c for c in fp["containers"] if c["kind"] == "pak"]
    assert len(paks) == 1 and paks[0]["footer"] == "patched-e1"
    assert paks[0]["index_encrypted"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest backend/tests/test_protection.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

`backend/ut/protection.py`:
```python
"""Game discovery + container damage fingerprint. The test games' pak damage
is a one-byte footer-magic patch (0x5A6F12E1 vs stock 0x5A6F12F1); SampleGameA's index is plaintext, SampleGameB's is AES-encrypted. The utoc files
are stock (the ASCII magic is genuine)."""
from __future__ import annotations

import os

from . import pak as pak_mod
from .iostore import parse_toc_header


def find_game(game_root: str) -> dict:
    exes, paks, utocs, ucas, ufs = [], [], [], [], []
    for base, _dirs, files in os.walk(game_root):
        under_content_paks = os.sep + "Content" + os.sep + "Paks" + os.sep in base + os.sep
        for name in files:
            full = os.path.join(base, name)
            low = name.lower()
            if low.endswith("-win64-shipping.exe"):
                exes.append(full)
            elif under_content_paks and low.endswith(".pak"):
                paks.append(full)        # CEF3/Chromium ships 56 resource .paks that are not UE containers
            elif under_content_paks and low.endswith(".utoc"):
                utocs.append(full)
            elif under_content_paks and low.endswith(".ucas"):
                ucas.append(full)
            elif low == "manifest_ufsfiles_win64.txt":
                ufs.append(full)
    return {"exes": exes, "paks": sorted(paks), "utocs": sorted(utocs),
            "ucas": sorted(ucas), "ufs": ufs}


def fingerprint(game_root: str) -> dict:
    found = find_game(game_root)
    containers = []
    for p in found["paks"]:
        try:
            f = pak_mod.read_footer(p)
            containers.append({"path": p, "kind": "pak",
                               "footer": f.magic_variant,
                               "index_encrypted": f.encrypted_index, "entries": None})
        except ValueError as e:
            containers.append({"path": p, "kind": "pak", "footer": f"bad: {e}",
                               "index_encrypted": None, "entries": None})
    for u in found["utocs"]:
        try:
            with open(u, "rb") as f:
                h = parse_toc_header(f.read(0x1000))
            containers.append({"path": u, "kind": "utoc", "header": "ok",
                               "version": h.version, "entry_count": h.entry_count,
                               "encrypted": h.encrypted, "signed": h.signed})
        except ValueError as e:
            containers.append({"path": u, "kind": "utoc", "header": f"bad: {e}"})
    return {"engine": "ue5", "iostore": bool(found["utocs"]), "containers": containers,
            "ufs": found["ufs"]}
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest backend/tests/test_protection.py -q`
Expected: 2 passed.

- [ ] **Step 5: Ledger**

Append to `.sdd/2026-10-22-phase1-repair-read/progress.md`:
```markdown
## Task 7: protection fingerprint
- find_game/fingerprint; both games: pak footer magic patched-e1; EF index encrypted; utocs stock.
```

---

### Task 8: IDA analysis — SampleGameB pak-index facts (research)

**Files:**
- Create: `docs/ida/sample_b-pak-index.md`, `backend/tests/fixtures/sample_b_index_facts.json`
- Test: `backend/tests/test_ida_fixtures.py`

**Interfaces:**
- Consumes: `<games-root>\SampleGameB-Win64-Shipping.exe_export_for_ai\` (read-only).
- Produces: `sample_b_index_facts.json` schema:
  ```json
  {
    "index_location": "footer | hardcoded-offset | unknown",
    "aes_key_hex": null,
    "aes_key_evidence": "sub_... | null",
    "magic_patch": {"stock": "5A6F12F1", "patched": "5A6F12E1", "engine_refs": "sub_..."},
    "foreign_pak_checks": "none-found | signature | hash | unknown",
    "evidence": [{"claim": "...", "function": "sub_...", "file": "decompile/....c", "notes": "..."}],
    "confidence": "low | medium | high"
  }
  ```
  This fixture is the input contract for the Phase 2 index-decryption implementation. Note the task got MORE concrete after Task 5: the footer is intact with patched magic, the index is AES-encrypted — the primary hunt is the 32-byte AES-256 key (key schedule or constant) in the shipping exe, plus locating the engine's E1-magic comparison (which proves the write-back rule).

- [ ] **Step 1: Write the fixture-schema test first**

`backend/tests/test_ida_fixtures.py`:
```python
import json
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tests", "fixtures", "sample_b_index_facts.json")


def test_sample_b_index_facts_schema():
    if not os.path.isfile(FIXTURES):
        pytest.fail("fixtures/sample_b_index_facts.json missing (Task 8)")
    with open(FIXTURES, encoding="utf-8") as f:
        facts = json.load(f)
    for key in ("index_location", "foreign_pak_checks", "evidence", "confidence"):
        assert key in facts, key
    assert facts["index_location"] in ("footer", "hardcoded-offset", "unknown")
    assert facts["foreign_pak_checks"] in ("none-found", "signature", "hash", "unknown")
    assert facts["confidence"] in ("low", "medium", "high")
    assert len(facts["evidence"]) >= 2
    for ev in facts["evidence"]:
        assert {"claim", "function", "file"} <= set(ev)
```

Run: `python -m pytest backend/tests/test_ida_fixtures.py -q` → FAIL (fixture missing).

- [ ] **Step 2: Xref hunt for the pak-mount path**

Work in `<games-root>\SampleGameB-Win64-Shipping.exe_export_for_ai\`. The workflow is: string address → pointers.txt xref → function → decompile. Concretely:

```bash
cd "/e/Dev/UE/Test Games/SampleGameB-Win64-Shipping.exe_export_for_ai"
# 1. locate the anchor strings in THIS binary first (addresses differ per game):
grep -n "FPakPlatformFile::Initialize\|\*\.pak" strings.txt | head -10
# 2. for each string address found, find code referencing it:
grep -n "0x<ADDR_FROM_STEP_1>" pointers.txt | head -20
```
For each `Source_Address` returned, find the containing function in `function_index.txt` (grep the address; functions are listed with their address ranges via `File:` entries — locate by `grep -n "Address: 0x<addr>" function_index.txt`, else scan `disassembly/` for the nearest function boundary), then read `decompile/<ADDR>.c`.

Follow the call graph around pak mounting. Questions to answer with cited functions:
1. The magic comparison: where does the engine compare against `0x5A6F12E1` (grep the disassembly for `5A6F12E1`; also try `0x147...` immediate patterns)? This proves the patched reader and the write-back rule.
2. The AES key: any AES-256 key schedule or 32-byte constant reachable from the pak-mount path (`grep -in "decrypt\|crypto\|faes" strings.txt | head -20`, AES-NI instruction patterns like `aesdec`/`aesimc` in the disassembly near pak code, or 0x63/0x63-leading S-box constants)? If a key candidate is found, record bytes + address.
3. Any check that would reject a pak we rebuild (hash compare against embedded data, size checks)?

- [ ] **Step 3: Write the findings + fixture**

Fill `docs/ida/sample_b-pak-index.md` with: one section per question, each claim citing `sub_…` + decompile file + the exact evidence (constant, comparison, call). Write the matching `sample_b_index_facts.json`. If a question cannot be answered offline, use the `"unknown"` values and say what's missing — **do not speculate in the fixture**.

- [ ] **Step 4: Run fixture test**

Run: `python -m pytest backend/tests/test_ida_fixtures.py -q`
Expected: PASS.

- [ ] **Step 5: Ledger**

Append to `.sdd/2026-10-22-phase1-repair-read/progress.md`:
```markdown
## Task 8: IDA sample_b index facts
- index_location=<...>, aes key=<present/absent>, magic patch refs=<...>, foreign checks=<...>, confidence=<...>.
- See docs/ida/sample_b-pak-index.md.
```

---

### Task 9: IDA analysis — SampleGameA pak role + demosaic leads (research)

**Files:**
- Create: `docs/ida/sample-a-mount-and-demosaic.md`, `backend/tests/fixtures/sample_a_demosaic_leads.json`
- Test: extend `backend/tests/test_ida_fixtures.py`

**Interfaces:**
- Consumes: `<games-root>\sample_a-Win64-Shipping.exe_export_for_ai\` (read-only).
- Produces: `sample_a_demosaic_leads.json` schema:
  ```json
  {
    "pak_mounted": true,
    "pak_mount_evidence": "sub_...",
    "magic_patch": {"engine_refs": "sub_..."},
    "mosaic_strings": [{"text": "...", "address": "0x..."}],
    "mosaic_functions": [{"function": "sub_...", "why": "..."}],
    "notes": "..."
  }
  ```

- [ ] **Step 1: Extend the fixture test**

Add to `backend/tests/test_ida_fixtures.py`:
```python
DEMOSAIC = os.path.join(os.path.dirname(FIXTURES), "sample_a_demosaic_leads.json")


def test_sample_a_demosaic_leads_schema():
    if not os.path.isfile(DEMOSAIC):
        pytest.fail("fixtures/sample_a_demosaic_leads.json missing (Task 9)")
    with open(DEMOSAIC, encoding="utf-8") as f:
        leads = json.load(f)
    assert isinstance(leads.get("pak_mounted"), bool)
    assert "pak_mount_evidence" in leads
    assert isinstance(leads.get("mosaic_strings"), list)
```

Run: `python -m pytest backend/tests/test_ida_fixtures.py -q` → FAIL (missing fixture).

- [ ] **Step 2: Investigate**

Same xref workflow as Task 8 (strings → pointers.txt → function_index → decompile), in `sample_a-Win64-Shipping.exe_export_for_ai`:

```bash
cd "/e/Dev/UE/Test Games/sample_a-Win64-Shipping.exe_export_for_ai"
grep -in "mosaic\|censor\|hide_body\|hidebody" strings.txt | head -20
# pak mount path: locate the anchor strings first, then xref them:
grep -n "FPakPlatformFile::Initialize\|\*\.pak" strings.txt
# magic-patch proof: grep disassembly for the patched immediate
grep -rn "0x5A6F12E1\|5A6F12E1" disassembly/ | head -10
```

Answer: (1) is the companion `.pak` mounted (its index is plaintext, so this is about priority, not decryption), (2) where the engine compares `0x5A6F12E1` (write-back rule), (3) any strings/functions with mosaic/censor semantics, (4) VRM4U-related hiding logic if present.

- [ ] **Step 3: Write findings + fixture, run tests**

Same discipline as Task 8: claims cited, unknowns marked, no speculation.
Run: `python -m pytest backend/tests/test_ida_fixtures.py -q` → PASS.

- [ ] **Step 4: Ledger**

Append to `.sdd/2026-10-22-phase1-repair-read/progress.md`:
```markdown
## Task 9: IDA sample-a mount + demosaic leads
- pak_mounted=<...>; magic patch refs=<...>; mosaic leads: <count> strings, <count> functions.
```

---

### Task 10: Inspector CLI + full-game verification + snapshot

**Files:**
- Create: `backend/ut/inspect.py`, `backend/tests/fixtures/inspect-sample_a.json`, `backend/tests/fixtures/inspect-sample_b.json`, `docs/E2E-CHECKLIST.md`
- Test: `backend/tests/test_inspect.py`

**Interfaces:**
- Consumes: everything from Tasks 2–9.
- Produces: `python -m backend.ut.inspect <game_root> [--json OUT]` printing a report:
  ```json
  {"game": "...", "fingerprint": {...},
   "utocs": [{"path": "...", "entries": 3247, "named_files": 1708, "methods": ["Oodle"],
              "blocks_by_method": {"0": 0, "1": 19422}, "extracted_sample": {"ok": 25, "fail": 0}}],
   "pak": {"path": "...", "footer_magic": "patched-e1", "index_encrypted": false,
           "entries": 1669, "manifest_count": 4737, "name_coverage": 0.97},
   "oodle": true}
  ```
  `blocks_by_method` answers spike question 1 (how Oodle-dependent reading is); `name_coverage` reports Task 6's intersect_names over the plaintext index (SampleGameA) and is null for encrypted indexes.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_inspect.py`:
```python
import json
import os
import subprocess
import sys

DEV = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_inspect_runs_on_both_games(game_paths):
    for game in ("sample_a", "sample_b"):
        out = os.path.join(DEV, "backend", "tests", "fixtures", f"inspect-{game}.json")
        r = subprocess.run([sys.executable, "-m", "backend.ut.inspect",
                            game_paths[game], "--json", out],
                           cwd=DEV, capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stderr[-2000:]
        with open(out, encoding="utf-8") as f:
            report = json.load(f)
        assert report["oodle"] is True
        assert report["fingerprint"]["iostore"] == (game == "sample_a")
        if game == "sample_a":
            utoc = next(u for u in report["utocs"] if u["entries"] == 3247)
            assert utoc["named_files"] == 1708
            assert utoc["extracted_sample"]["fail"] == 0
            assert report["pak"]["entries"] == 1669
            assert report["pak"]["name_coverage"] > 0.9
        else:
            assert report["pak"]["index_encrypted"] is True   # awaits Task 8 key
```

Note: no full pak scan happens anymore — the footer+index read is constant-time even on the 3.2 GB pak (encrypted indexes are detected, not decrypted).

- [ ] **Step 2: Run to verify it fails** → `No module named backend.ut.inspect`.

- [ ] **Step 3: Implement**

`backend/ut/inspect.py`:
```python
"""Phase 1 CLI: read-only container inspection over a game directory."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import manifest as manifest_mod
from . import oodle, pak as pak_mod
from . import protection
from .iostore import parse_toc, read_entry


def _utoc_report(utoc_path: str, ucas_path: str | None) -> dict:
    with open(utoc_path, "rb") as f:
        toc = parse_toc(f.read())
    by_method: dict[str, int] = {}
    for _off, _cs, _us, mi in toc.blocks:
        by_method[str(mi)] = by_method.get(str(mi), 0) + 1
    rep = {"path": utoc_path, "entries": len(toc.entries),
           "named_files": len(toc.file_map), "methods": toc.compression_methods,
           "blocks_by_method": by_method, "extracted_sample": None}
    if ucas_path and os.path.isfile(ucas_path):
        dll = oodle.get_dll()
        ok = fail = 0
        for i, e in enumerate(toc.entries):
            if e.length == 0:
                continue
            try:
                read_entry(toc, ucas_path, i, dll=dll)  # length verified inside
                ok += 1
            except Exception:
                fail += 1
            if ok + fail >= 25:
                break
        rep["extracted_sample"] = {"ok": ok, "fail": fail}
    return rep


def _pak_report(game_root: str, pak_path: str, cache: str | None) -> dict:
    footer = pak_mod.read_footer(pak_path)
    idx = pak_mod.read_index(pak_path, footer)
    ufs_files = protection.find_game(game_root)["ufs"]
    coverage = None
    manifest_count = 0
    if ufs_files and idx.entries:
        ufs = manifest_mod.load_ufs(ufs_files[0])
        manifest_count = len(ufs)
        coverage = manifest_mod.intersect_names(idx.entries, ufs)
    return {"path": pak_path, "footer_magic": footer.magic_variant,
            "index_encrypted": idx.encrypted, "entries": len(idx.entries),
            "manifest_count": manifest_count, "name_coverage": coverage}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("game_root")
    ap.add_argument("--json", help="also write the report to this path")
    args = ap.parse_args(argv)

    fp = protection.fingerprint(args.game_root)
    report = {"game": args.game_root, "fingerprint": fp, "utocs": [], "pak": None,
              "oodle": False}
    try:
        oodle.get_dll()
        report["oodle"] = True
    except FileNotFoundError:
        report["oodle"] = False

    for u in fp["containers"]:
        if u["kind"] == "utoc" and u["header"] == "ok":
            ucas = u["path"][:-5] + ".ucas"
            report["utocs"].append(_utoc_report(u["path"], ucas if os.path.isfile(ucas) else None))
    biggest = max((c["path"] for c in fp["containers"] if c["kind"] == "pak"),
                  key=os.path.getsize, default=None)
    if biggest:
        report["pak"] = _pak_report(args.game_root, biggest, None)

    text = json.dumps(report, indent=1, ensure_ascii=False)
    print(text)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests + both games**

Run: `python -m pytest backend/tests -q`
Expected: entire suite passes. Record the two reports' headline numbers in the ledger: sample_a named_files, blocks_by_method split, pak name_coverage; sample_b index_encrypted.

- [ ] **Step 5: Write `docs/E2E-CHECKLIST.md`**

```markdown
# SLRUnreal Phase 1 E2E checklist

Automated (pytest, real games): utoc header/TOC parse + byte round-trip on both
utocs; ContainerHeader extraction (LE IoCn magic); extraction walk over stored
+ Oodle blocks; pak footer + plaintext index read (1669 entries, sha1-verified
first entry); encrypted-index detection (SampleGameB); manifest name coverage;
damage fingerprint (patched-e1 magic on both paks).

Manual (user): none yet — Phase 1 is read-only; no game files are modified.

Spike answers recorded: Q1 blocks_by_method=<?>; Q2 see
docs/ida/sample_b-pak-index.md; Q3 see docs/ida/sample-a-mount-and-demosaic.md.
```

- [ ] **Step 6: Snapshot + final ledger entry**

Source-only snapshot (exclude runtime/, caches, fixtures over 1 MB):

```bash
cd "/e/Dev/SLR Unreal Translator"
python - <<'EOF'
import os, zipfile
root = os.getcwd()
out = os.path.join("_snapshots", "phase1-repair-read.zip")
prune = {"__pycache__", ".pytest_cache", "runtime", "dist", "_snapshots", ".sdd"}
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in prune]
        for f in files:
            if f.endswith(".pyc") or (f.endswith(".json") and f"fixtures{os.sep}" in base
                                      and os.path.getsize(os.path.join(base, f)) > 1 << 20):
                continue
            full = os.path.join(base, f)
            z.write(full, os.path.relpath(full, root))
print(out)
EOF
```

Append final ledger entry:
```markdown
## Task 10: inspect CLI + verification + snapshot
- Full suite: <N passed, M skipped>.
- SampleGameA: named_files=<>, blocks_by_method=<>, pak coverage=<>.
- SampleGameB: index_encrypted=true (Task 8 facts: <>).
- Snapshot: _snapshots/phase1-repair-read.zip (<size>).
- Phase 1 complete; Phase 2 (index decryption + writers) planned from these facts.
```

---

## Self-review notes (updated after in-flight corrections)

- Spec coverage vs §5 pipeline: stage 1 (detect) = Task 7; stage 2 (repair-read) = Tasks 3, 5, 8, 9; stage 3 (index & extract) = Tasks 3–5, 10. Stages 4–7 are Phase 3+ by the phased-plan decomposition agreed at plan time. §10 spike questions 1–3 answered by Tasks 4/10, 8, 9.
- In-flight rulings folded into this plan (all evidence-backed by real bytes): 16-byte TOC magic with trailing dash; directory_index_size 83911; global.utoc entry_count 1 / block_count 41; io_hash = BLAKE3[0:20] (suite does not verify hashes); ContainerHeader in game TOC with LE magic; pak footers intact with patched magic 0x5A6F12E1, ST index plaintext (1669 entries), EF index+payload AES-encrypted.
- Remaining soft facts each carry an explicit empirical resolution protocol in their tasks — no silent assumptions.
