# SLRUnreal Phase 2 — Round-Trip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Round-trip both games' containers — decrypt SampleGameB's index and payloads with the recovered AES key, and build container *writers* whose output the patched engines accept: rebuild-unmodified must be byte-identical (goldens), and a modified entry must change only the expected regions with all hashes/magic reproduced.

**Architecture:** Pure-Python `ut/` package continues; new modules: `aes.py` (Windows BCrypt via ctypes + pure-python fallback), `blake3.py` (in-house, spec-derived), writer paths in `pak.py`/`iostore.py` + a `rewrite.py` orchestration layer. OS APIs via ctypes are not third-party code. Unchanged payload bytes are always copied verbatim (no decompress/recompress), so rebuilds are deterministic and byte-stable.

**Tech Stack:** Python 3.10+, pytest, ctypes (bcrypt.dll, oo2core_9_win64.dll).

**Spec:** `<dev-root>/SLR Unreal Translator\docs\specs\2026-10-22-slrunreal-design.md`
**Phase 1 ledger (facts this plan builds on):** `.sdd/2026-10-22-phase1-repair-read/progress.md`

## Global Constraints

- Dev home: `<dev-root>/SLR Unreal Translator`. No git — ledger at `.sdd/2026-10-22-phase2-roundtrip/progress.md` (create in Task 1); source-only snapshot at the end (Task 8).
- Zero third-party code vendored. References (READ-ONLY): `<dev-root>/UE\reference tools\repak\` (oodle signatures/enums), `retoc\` (utoc writer), `rust-u4pak-1.4.0\src\pack.rs` + `record.rs` + `index.rs` (pak writer). The blake3 implementation is written from the published algorithm spec — no copying reference implementations.
- **Key-material hygiene (spec Appendix B):** the AES key lives in `backend/tests/fixtures/sample_b_index_facts.json` (already dev-side). Never in shipped paths. The fetched DLL stays in `runtime/oodle/win/`.
- Test games are READ-ONLY. All writer output goes to `tempfile` scratch (never into the game dirs).
- Writer outputs for these games must carry the PATCHED magic `0x5A6F12E1` (never stock `0x5A6F12F1`) — parameterized, driven by the source footer.
- **Atomic writes everywhere:** any script that rewrites a project file writes `tempfile` + `os.replace` (Phase 1 incident rule). Writer modules also build containers in temp paths and move on success.
- Real-game tests skip cleanly when folders absent. Slow tests (full 3.2 GB rebuild) take `pytest.mark.slow` and stay in the default run (~1–3 min, IO-bound).
- Windows: Python 3.10.6, pytest 8.3.5, Git Bash, run from `<dev-root>/SLR Unreal Translator`. `PYTHONIOENCODING=utf-8` for Japanese output.

## Verified facts added by Phase 1 (beyond the Phase 1 plan's facts section)

- **EF AES-256 key:** withheld — the game's key is not ours to publish (tests resolve it from `SLRUNREAL_SAMPLE_B_AES_KEY` or an untracked local file, see `backend/tests/keys.py`) (fixture `sample_b_index_facts.json` → `aes_key_hex`). AES-256-**ECB**, zero-padded plaintext to 16 B (index size 1,190,592 = 74,412 blocks, divides evenly). Decrypted-index oracle artifact: `.sdd/2026-10-22-phase1-repair-read/sdd/task8_index_decrypted.bin` (mount `../../../`, 17,829 entries). EF footer: index_offset `0xBDA5C59E`, IndexHash `7ccf6481bea84423a9d2c4e1ee25622fd53e1f7c` (= SHA-1 of decrypted plaintext).
- **EF manifest question to close:** decrypted index = 17,829 entries vs 17,830 UFS lines — Task 2 must name the unmatched line.
- **Pak write-back gates (both games):** footer magic must be `0x5A6F12E1`; footer IndexHash = SHA-1 over the (decrypted) index plaintext; signature object unwired (zero GUID fine); no other gate found.
- **Entry sha1 semantics:** SHA-1 of the ON-DISK payload bytes (pre-decompression). Encrypted payloads are 16-byte aligned; encrypted single-block encoded entries consume one u32 block size (the `> 0` decoder rule, fixed in Phase 1's final wave).
- **OodleLZ_Compress (2.9.10, verified export of our DLL):** `fn(compressor: i32, raw: *u8, rawLen: usize, compBuf: *u8, level: i32, opts: null, dict: null, lrm: null, scratch: null, scratchSize: 0) -> isize` (−1 = fail); compBuf sized via `OodleLZ_GetCompressedBufferSizeNeeded(compressor, rawLen)`. Enums: Kraken=8, Mermaid=9, Selkie=11, Leviathan=13; levels: Normal=4, Optimal1=5…Optimal5=9. Writer default: **Kraken, Normal** for modified chunks (byte-stability not required — originals are copied verbatim).
- **utoc write path:** `serialize_toc` already round-trips byte-identically (Phase 1 golden). Chunk `io_hash` = BLAKE3 truncated to 20 bytes of the chunk's uncompressed data (100/100 oracle); metas must be updated for modified chunks.
- **Windows BCrypt (OS API, fine to use):** `BCryptOpenAlgorithmProvider("AES")` → `BCryptSetProperty(ChainingMode="ChainingModeECB")` → `BCryptGenerateSymmetricKey` → `BCryptEncrypt/BCryptDecrypt` (no IV for ECB; `BCRYPT_BLOCK_PADDING` flag NOT set — we pad ourselves with zeros).

---

### Task 1: AES layer + EF index/payload decryption

**Files:**
- Create: `backend/ut/aes.py`
- Modify: `backend/ut/pak.py` (aes_key paths in `read_index`/`read_entry_data`)
- Test: `backend/tests/test_aes.py`, extend `backend/tests/test_pak_scan.py`
- Create: `.sdd/2026-10-22-phase2-roundtrip/progress.md`

**Interfaces:**
- Consumes: Phase 1 `pak.read_footer`/`read_index`/`PakEntry`; the oracle artifact path; the key from the EF fixture JSON (load via `json`, dev-side).
- Produces:
  - `backend/ut/aes.py`: `decrypt_ecb(key: bytes, data: bytes) -> bytes` and `encrypt_ecb(key: bytes, data: bytes) -> bytes` (len(data) % 16 == 0 enforced; 32-byte key enforced). Win implementation via bcrypt.dll ctypes; fallback `_pure_ecb` (in-house AES block cipher, ~150 lines, used when bcrypt unavailable AND always importable for cross-checks). `available_backends() -> list[str]`.
  - `pak.read_index(path, footer=None, aes_key: bytes | None = None)` — when `footer.encrypted_index`: require `aes_key`, read `index_size` bytes at `index_offset`, decrypt, SHA-1-verify against `footer.index_sha1`, parse with the existing plaintext path (decrypt → identical machinery).
  - `pak.read_entry_data(path, entry, dll=None, aes_key=None)` — for `flags & 1` entries: decrypt each block's bytes (AES-ECB, then trim to the block's compressed size) before the stored/Oodle dispatch.

- [ ] **Step 1: Failing tests**

`backend/tests/test_aes.py`:
```python
import json
import os
import unittest.mock

import pytest

from backend.ut import aes

DEV = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEY_FIXTURE = os.path.join(DEV, "backend", "tests", "fixtures", "sample_b_index_facts.json")
ORACLE = os.path.join(DEV, ".sdd", "2026-10-22-phase1-repair-read", "sdd",
                      "task8_index_decrypted.bin")
EF_PAK = r"<games-root>\SampleGameB\SampleGameB\Content\Paks\SampleGameB-Windows.pak"


def _key() -> bytes:
    with open(KEY_FIXTURE, encoding="utf-8") as f:
        return bytes.fromhex(json.load(f)["aes_key_hex"])


def test_roundtrip_random_blocks():
    key = bytes(range(32))
    data = bytes((i * 7 + 3) & 0xFF for i in range(16 * 100))
    assert aes.encrypt_ecb(key, data) != data
    assert aes.decrypt_ecb(key, aes.encrypt_ecb(key, data)) == data


def test_pure_backend_matches_bcrypt_on_samples():
    if "bcrypt" not in aes.available_backends():
        pytest.skip("bcrypt unavailable")
    if not os.path.isfile(ORACLE):
        pytest.skip("Phase 1 oracle artifact absent")
    key = _key()
    with open(ORACLE, "rb") as f:
        data = f.read(16 * 64)
    enc = aes.encrypt_ecb(key, data)
    pure = aes._pure_ecb
    assert pure.decrypt_ecb(key, enc) == data
    assert pure.encrypt_ecb(key, data) == enc


def test_decrypt_ef_index_oracle():
    if not (os.path.isfile(ORACLE) and os.path.isfile(EF_PAK)):
        pytest.skip("oracle artifact or game absent")
    key = _key()
    from backend.ut import pak
    footer = pak.read_footer(EF_PAK)
    with open(EF_PAK, "rb") as f:
        f.seek(footer.index_offset)
        blob = f.read(footer.index_size)
    plain = aes.decrypt_ecb(key, blob)
    with open(ORACLE, "rb") as f:
        assert plain == f.read()
```

Extend `backend/tests/test_pak_scan.py`:
```python
def test_read_index_encrypted_with_key_sample_b(game_paths):
    import json as _json
    fx = os.path.join(os.path.dirname(__file__), "fixtures", "sample_b_index_facts.json")
    with open(fx, encoding="utf-8") as f:
        key = bytes.fromhex(_json.load(f)["aes_key_hex"])
    p = _pak(game_paths, "sample_b", ("SampleGameB", "Content", "Paks",
                                          "SampleGameB-Windows.pak"))
    idx = pak.read_index(p, aes_key=key)
    assert not idx.encrypted
    assert len(idx.entries) == 17829
    assert idx.mount_point == "../../../"
    # every entry is encrypted payload (Phase 1 sampled flags=1); reading one without key raises
    e0 = min(idx.entries, key=lambda e: e.offset)
    with pytest.raises(ValueError):
        pak.read_entry_data(p, e0)
    data = pak.read_entry_data(p, e0, aes_key=key)
    assert len(data) == e0.uncompressed_size
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError: backend.ut.aes` / encrypted index returns encrypted=True path).

- [ ] **Step 3: Implement**

`backend/ut/aes.py` — bcrypt path:
```python
"""AES-256-ECB. Windows: BCrypt via ctypes (OS API, not third-party code).
Fallback + cross-check: in-house pure-python block implementation."""
from __future__ import annotations

import ctypes
import os
import threading

_LOCK = threading.Lock()
_BCRYPT = None  # (handle, key_handle) cache per key is NOT thread-cached; keep simple


def available_backends() -> list[str]:
    out = []
    try:
        import ctypes.util
        if ctypes.util.find_library("bcrypt") or os.name == "nt":
            out.append("bcrypt")
    except Exception:
        pass
    out.append("pure")
    return out
```
Then: `encrypt_ecb`/`decrypt_ecb` dispatch on backend (bcrypt preferred). BCrypt calls: `BCryptOpenAlgorithmProvider(&hAlg, "AES", None, 0)`, `BCryptSetProperty(hAlg, "ChainingMode", "ChainingModeECB\0", 34, 0)`, `BCryptGenerateSymmetricKey(hAlg, &hKey, None, 0, key, 32, 0)`, `BCryptEncrypt/Decrypt(hKey, pbInput, cbInput, None, None, 0, pbOutput, cbOutput, &cbResult, 0)` — no padding flag; assert `cbResult == cbInput`. Buffer types via `ctypes.create_string_buffer`. Pure fallback: standard AES (s-box/inv-s-box computed at import from the GF(2^8) definition, key expansion for 256-bit, encrypt/decrypt block loops) exposed as module `_pure_ecb` with the same two functions.

`pak.py` changes: thread `aes_key` through `read_index` (decrypt-then-parse; on missing key keep the Phase 1 encrypted-report behavior) and `read_entry_data` (per-block: `blob = aes.decrypt_ecb(aes_key, raw16)` where `raw16 = raw[:align16(len(raw))]`, then use `blob[:csize]`).

- [ ] **Step 4: Run** `python -m pytest backend/tests -q` → 25 passed (21 + 4). The oracle test is the hard gate.
- [ ] **Step 5: Ledger** — create `.sdd/2026-10-22-phase2-roundtrip/progress.md` with the identity line `# SDD ledger — plan: <dev-root>/SLR Unreal Translator\docs\plans\2026-10-22-phase2-roundtrip.md`, then the Task 1 block (backends available, oracle pass, EF entry read result).

---

### Task 2: EF manifest reconciliation + full EF read

**Files:**
- Modify: `backend/ut/manifest.py` (nothing new needed if coverage works), `backend/tests/test_manifest.py`
- Test: extend `backend/tests/test_manifest.py`

**Interfaces:**
- Produces: a test proving EF index-name ↔ manifest coverage and NAMING the unmatched line (closes the Phase 1 pinned question). `intersect_names` reused as-is.

- [ ] **Step 1: Failing test**
```python
def test_ef_index_manifest_reconciliation(game_paths):
    import json as _json
    from backend.ut import pak, manifest as M
    fx = os.path.join(os.path.dirname(__file__), "fixtures", "sample_b_index_facts.json")
    with open(fx, encoding="utf-8") as f:
        key = bytes.fromhex(_json.load(f)["aes_key_hex"])
    p = os.path.join(game_paths["sample_b"], "SampleGameB", "Content", "Paks",
                     "SampleGameB-Windows.pak")
    idx = pak.read_index(p, aes_key=key)
    ufs = M.load_ufs(os.path.join(game_paths["sample_b"], "Manifest_UFSFiles_Win64.txt"))
    names = {M.normalize_pak_name(idx.mount_point, e.name) for e in idx.entries}
    missing = [u for u in ufs if u not in names and not u.lower().endswith(
        (".pak", ".utoc", ".ucas", ".sig", ".assign"))]
    assert len(names) == 17829
    assert len(missing) <= 2, missing
    # Record the unmatched line(s) in the report; the assertion forces the explanation.
```
- [ ] **Step 2: FAIL → Step 3: run the comparison manually once**, record the unmatched line(s) and why (expected candidates: the pak file itself, or a manifest-only staged file), then adjust the filter so the assert holds with the explanation documented in the test as a comment.
- [ ] **Step 4:** `python -m pytest backend/tests -q` → 26 passed. **Step 5: Ledger** with the named unmatched line.

---

### Task 3: In-house blake3 (20-byte io_hash)

**Files:**
- Create: `backend/ut/blake3.py`
- Test: `backend/tests/test_blake3.py`

**Interfaces:**
- Produces: `hash20(data: bytes) -> bytes` (BLAKE3 of data, first 20 bytes) and `hash32(data: bytes) -> bytes`. Pure python, in-house from the published spec (consult the official BLAKE3 paper/spec page via WebFetch for the compression function constants and flag values — write ORIGINAL code; do not copy any reference implementation).

- [ ] **Step 1: Failing test (oracle = Phase 1's proven hashes)**
```python
import os

import pytest

from backend.ut import blake3, iostore, oodle

PAKS = os.path.join("sample_a", "Content", "Paks")


def test_known_small_vectors():
    # self-consistency + documented properties; the real gate is the game oracle below
    assert blake3.hash32(b"").hex().startswith("af1349b9f5f9a1a6a0404dea36dcc949")
    assert len(blake3.hash20(b"x")) == 20


def test_matches_real_io_hashes(game_paths):
    with open(os.path.join(game_paths["sample_a"], PAKS,
                           "sample_a-Windows.utoc"), "rb") as f:
        toc = iostore.parse_toc(f.read())
    ucas = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.ucas")
    dll = oodle.get_dll()
    checked = 0
    for i, e in enumerate(toc.entries):
        if e.length == 0 or e.io_hash == b"\x00" * 20:
            continue
        data = iostore.read_entry(toc, ucas, i, dll=dll)
        assert blake3.hash20(data) == e.io_hash, f"entry {i}"
        checked += 1
        if checked >= 20:
            break
    assert checked >= 10
```
NOTE: the `af1349b9…` prefix in `test_known_small_vectors` is the well-known BLAKE3 empty-string digest — if it fails while the game oracle passes, fix the vector, not the implementation, and record it.

- [ ] **Step 2: FAIL → Step 3: Implement** per the spec: 32-byte state, IV = SHA-256 IV, MSG_PERMUTATION, chunk size 1024, CV stack chaining, flags (CHUNK_START/CHUNK_END/PARENT/ROOT), `hash32` = root output; `hash20` = first 20 bytes. Implement incremental or whole-buffer (whole-buffer acceptable — chunks can be large but one-off).
- [ ] **Step 4:** `python -m pytest backend/tests -q` → 28 passed. **Step 5: Ledger.**

---

### Task 4: pak v11 writer — plaintext path (SampleGameA golden)

**Files:**
- Modify: `backend/ut/pak.py` (append writer)
- Test: `backend/tests/test_pak_write.py`

**Interfaces:**
- Consumes: `PakIndex`/`PakEntry`/`Footer` from `read_index`; original file bytes (payloads copied verbatim by slicing).
- Produces:
  - `write_pak(src_path: str, idx: PakIndex, out_path: str, entries_data: dict[str, bytes] | None = None, aes_key: bytes | None = None, magic: int | None = None) -> None` — rebuilds the container: entry records + payloads in original offsets where unmodified (when `entries_data` is None: slice original regions — pure copy-through); modified entries (name → new bytes) are stored/compressed per their original method shape (stored stays stored; compressed re-Oodles Kraken/Normal via `oodle.compress`) and appended at the end of the data region with rebuilt records; index re-encoded exactly as parsed; footer written with `magic or src footer magic` (patched-e1 for these games), recomputed `index_offset/index_size`, SHA-1 of the plaintext index in `index_sha1`, method names + guid copied. `aes_key` set → index plaintext zero-padded to 16 and encrypted; modified compressed+encrypted payloads re-encrypted.
  - `backend/ut/oodle.py` gains `compress(dll, data: bytes, compressor: int = 8, level: int = 4) -> bytes` (Kraken/Normal defaults; compBuf via `OodleLZ_GetCompressedBufferSizeNeeded`; −1 → ValueError).

- [ ] **Step 1: Failing test**
```python
import hashlib
import os
import tempfile

import pytest

from backend.ut import pak


def _st_pak(game_paths):
    return os.path.join(game_paths["sample_a"], "sample_a", "Content", "Paks",
                        "sample_a-Windows.pak")


def test_rebuild_unmodified_byte_identical_sample_a(game_paths):
    src = _st_pak(game_paths)
    idx = pak.read_index(src)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out)
        with open(src, "rb") as a, open(out, "rb") as b:
            assert os.path.getsize(out) == os.path.getsize(src)
            while True:
                ca, cb = a.read(1 << 20), b.read(1 << 20)
                assert ca == cb
                if not ca:
                    break


def test_rebuild_with_modified_entry(game_paths):
    src = _st_pak(game_paths)
    idx = pak.read_index(src)
    target = next(e for e in idx.entries if e.name.endswith("BaseEngine.ini"))
    original = pak.read_entry_data(src, target)
    modified = original + b"\r\n; slrunreal roundtrip probe\r\n"
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out, entries_data={target.name: modified})
        reread = pak.read_index(out)
        assert pak.read_entry_data(out, next(e for e in reread.entries
                                             if e.name == target.name)) == modified
        # every OTHER entry still byte-identical
        src_map = {e.name: e for e in idx.entries}
        diff_names = [e.name for e in reread.entries
                      if pak.read_entry_data(out, e) != pak.read_entry_data(src, src_map[e.name])]
        assert diff_names == [target.name]
        assert pak.read_footer(out).magic == pak.PAK_MAGIC_PATCHED
```
- [ ] **Step 2: FAIL → Step 3: Implement.** Writer model: iterate entries in INDEX ORDER for record layout; data region = original offsets preserved (copy via seek/read of source ranges in offset order; modified entries appended after the original data region end, records updated); re-encode the v11 index exactly mirroring `_decode_encoded_entry` (bit packing: method<<23, encrypted<<22, blocks<<6, block-size encoding per the reference, offsets union); footer last. If byte-identity fails, bisect: compare the rebuilt index blob against the original slice at `footer.index_offset` FIRST (this is where encoding drift shows), fix field-by-field with hexdump evidence — never mask a mismatch.
- [ ] **Step 4:** `python -m pytest backend/tests -q` → 30 passed. **Step 5: Ledger** (byte-identity confirmed; modified-entry delta scope).

---

### Task 5: pak writer — encrypted path (SampleGameB golden + modification)

**Files:**
- Modify: `backend/ut/pak.py`
- Test: extend `backend/tests/test_pak_write.py`

**Interfaces:**
- Consumes: Task 1 `aes`; Task 4 `write_pak`.
- Produces: `write_pak(..., aes_key=...)` covering: encrypted index (decrypt → re-encode → pad → encrypt → footer sha1 of plaintext), unchanged encrypted payloads copied VERBATIM (never decrypt/recompress unchanged data), modified encrypted payloads (decrypt → Oodle-decompress → replace → Oodle Kraken/Normal → pad to 16 → encrypt → new block ranges with 16-byte alignment).

- [ ] **Step 1: Failing test** (mark `@pytest.mark.slow`):
```python
@pytest.mark.slow
def test_rebuild_unmodified_byte_identical_sample_b(game_paths):
    import json as _json
    fx = os.path.join(os.path.dirname(__file__), "fixtures", "sample_b_index_facts.json")
    with open(fx, encoding="utf-8") as f:
        key = bytes.fromhex(_json.load(f)["aes_key_hex"])
    src = os.path.join(game_paths["sample_b"], "SampleGameB", "Content", "Paks",
                       "SampleGameB-Windows.pak")
    idx = pak.read_index(src, aes_key=key)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out, aes_key=key)
        a_size, b_size = os.path.getsize(src), os.path.getsize(out)
        assert b_size == a_size
        ha, hb = hashlib.sha256(), hashlib.sha256()
        with open(src, "rb") as a, open(out, "rb") as b:
            for chunk_a, chunk_b in zip(iter(lambda: a.read(1 << 22), b""),
                                        iter(lambda: b.read(1 << 22), b"")):
                ha.update(chunk_a); hb.update(chunk_b)
        assert ha.hexdigest() == hb.hexdigest()


@pytest.mark.slow
def test_rebuild_ef_modified_entry(game_paths):
    import json as _json
    fx = os.path.join(os.path.dirname(__file__), "fixtures", "sample_b_index_facts.json")
    with open(fx, encoding="utf-8") as f:
        key = bytes.fromhex(_json.load(f)["aes_key_hex"])
    src = os.path.join(game_paths["sample_b"], "SampleGameB", "Content", "Paks",
                       "SampleGameB-Windows.pak")
    idx = pak.read_index(src, aes_key=key)
    target = next(e for e in idx.entries if e.flags & 1 and e.method != 0)
    original = pak.read_entry_data(src, target, aes_key=key)
    modified = original + b"\x00slrunreal-probe"
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "rebuild.pak")
        pak.write_pak(src, idx, out, entries_data={target.name: modified}, aes_key=key)
        reread = pak.read_index(out, aes_key=key)
        got = pak.read_entry_data(out, next(e for e in reread.entries
                                            if e.name == target.name), aes_key=key)
        assert got == modified
```
- [ ] **Step 2: FAIL → Step 3: Implement** per Interfaces. The 3.2 GB copy is IO-bound (~1–3 min); stream everything (never hold the file in memory).
- [ ] **Step 4:** `python -m pytest backend/tests -q` → 32 passed (slow pair included). **Step 5: Ledger** (durations; encryption round-trip notes).

---

### Task 6: utoc/ucas writer — modification path (SampleGameA)

**Files:**
- Modify: `backend/ut/iostore.py` (append writer)
- Test: `backend/tests/test_iostore_write.py`

**Interfaces:**
- Consumes: `parse_toc`/`serialize_toc`/`read_entry`; Task 3 `blake3.hash20`; `oodle.compress`.
- Produces: `write_iostore(utoc_path: str, ucas_path: str, out_utoc: str, out_ucas: str, chunk_patches: dict[int, bytes] | None = None) -> None` — `chunk_patches` maps TOC entry index → new chunk data. Unmodified: ucas copied verbatim, utoc re-serialized (byte-identical by the Phase 1 golden). Modified: new chunk data Oodle-compressed (Kraken/Normal) into new blocks APPENDED to the ucas (16-byte... 0x10000-block alignment preserved: new chunk offsets must be block_size-aligned; pad ucas to a block boundary before appending), that entry's offset/length updated, its block-range entries extended/rewritten (existing blocks of OTHER entries untouched), `io_hash` recomputed via `blake3.hash20(new_data)`.

- [ ] **Step 1: Failing test**
```python
import os
import tempfile

from backend.ut import blake3, iostore, oodle

PAKS = os.path.join("sample_a", "Content", "Paks")


def test_rebuild_unmodified_identical(game_paths):
    up = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.utoc")
    cp = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.ucas")
    with open(up, "rb") as f:
        toc = iostore.parse_toc(f.read())
    with tempfile.TemporaryDirectory() as tmp:
        ou, oc = os.path.join(tmp, "o.utoc"), os.path.join(tmp, "o.ucas")
        iostore.write_iostore(up, cp, ou, oc)
        assert open(ou, "rb").read() == open(up, "rb").read()
        assert open(oc, "rb").read() == open(cp, "rb").read()


def test_patch_one_chunk(game_paths):
    up = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.utoc")
    cp = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.ucas")
    with open(up, "rb") as f:
        toc = iostore.parse_toc(f.read())
    dll = oodle.get_dll()
    idx = next(i for i, e in enumerate(toc.entries)
               if e.length and 0 < e.length < 4096 and e.chunk_type == 1)
    original = iostore.read_entry(toc, cp, idx, dll=dll)
    patched = original + b"slrunreal-probe"
    with tempfile.TemporaryDirectory() as tmp:
        ou, oc = os.path.join(tmp, "o.utoc"), os.path.join(tmp, "o.ucas")
        iostore.write_iostore(up, cp, ou, oc, chunk_patches={idx: patched})
        with open(ou, "rb") as f:
            toc2 = iostore.parse_toc(f.read())
        assert iostore.read_entry(toc2, oc, idx, dll=dll) == patched
        assert toc2.entries[idx].io_hash == blake3.hash20(patched)
        # unmodified entry still reads identical
        j = next(j for j in range(len(toc.entries)) if j != idx and toc.entries[j].length)
        assert iostore.read_entry(toc2, oc, j, dll=dll) == iostore.read_entry(toc, cp, j, dll=dll)
```
- [ ] **Step 2: FAIL → Step 3: Implement** — copy ucas bytes verbatim, then align-append new blocks; update `blocks` list (append new block tuples; entry's offset points at the first new block-aligned offset; length = len(new data)); rebuild `serialize_toc` from the updated model (metas updated only for patched entries).
- [ ] **Step 4:** `python -m pytest backend/tests -q` → 34 passed. **Step 5: Ledger.**

---

### Task 7: rewrite orchestration + writer acceptance model

**Files:**
- Create: `backend/ut/rewrite.py`
- Test: `backend/tests/test_rewrite.py`

**Interfaces:**
- Consumes: Tasks 4–6 writers; `protection.fingerprint`.
- Produces: `plan_rewrite(game_root: str, container_path: str, replacements: dict[str, bytes], aes_key: bytes | None = None) -> dict` — chooses pak vs iostore by extension, returns an execution plan (entries/chunks affected, output paths); `execute_rewrite(plan, out_dir: str) -> list[str]` — runs the writer into `out_dir`, returns written game-relative file paths (the future export zip payload). This is the Phase 4 export pipeline's kernel.

- [ ] **Step 1: Failing test**
```python
def test_rewrite_pak_and_iostore_end_to_end(game_paths, tmp_path):
    import json as _json
    from backend.ut import pak, rewrite
    st = game_paths["sample_a"]
    src = os.path.join(st, "sample_a", "Content", "Paks", "sample_a-Windows.pak")
    idx = pak.read_index(src)
    name = next(e.name for e in idx.entries if e.name.endswith("BaseEngine.ini"))
    plan = rewrite.plan_rewrite(st, src, {name: b"; probe\r\n"})
    assert plan["kind"] == "pak" and len(plan["affected"]) == 1
    written = rewrite.execute_rewrite(plan, str(tmp_path))
    assert len(written) == 1 and written[0].endswith(".pak")
    out = plan["outputs"][0]
    reread = pak.read_index(out)
    assert pak.read_entry_data(out, next(e for e in reread.entries
                                         if e.name == name)) == b"; probe\r\n"
```
- [ ] **Step 2: FAIL → Step 3: Implement** thin orchestration (validation: replacements must all resolve; missing key for encrypted containers → informative error).
- [ ] **Step 4:** `python -m pytest backend/tests -q` → 35 passed. **Step 5: Ledger.**

---

### Task 8: E2E checklist + snapshot + phase close

**Files:**
- Create: `docs/E2E-CHECKLIST-PHASE2.md`
- Modify: `.sdd/2026-10-22-phase2-roundtrip/progress.md`

- [ ] **Step 1: Write the checklist**
```markdown
# SLRUnreal Phase 2 E2E checklist

Automated (pytest, real games, read-only on originals; all output to temp):
- AES bcrypt/pure cross-check + EF index decrypt == Phase 1 oracle artifact.
- EF full index parse (17,829 entries) + payload read with key.
- EF manifest reconciliation (unmatched line named: <fill>).
- blake3 vs 20 real io_hashes.
- pak writer: ST byte-identical rebuild; ST modified-entry scoped delta; EF byte-identical
  rebuild (slow); EF encrypted-entry modification round-trip (slow).
- utoc/ucas writer: unmodified identical; one-chunk patch with blake3 + block alignment.
- rewrite orchestration end-to-end on ST pak.

Manual (user-run, on COPIED game dirs — originals never touched):
1. SampleGameA copy: replace sample_a-Windows.pak with the UNMODIFIED rebuild ->
   game must boot to title (proves writer acceptance: magic + index hash).
2. SampleGameA copy: replace with the BaseEngine.ini-modified rebuild -> game boots
   (proves modified-container acceptance).
3. SampleGameB copy: replace SampleGameB-Windows.pak with the modified rebuild ->
   game boots (proves AES/Oodle re-encryption acceptance).
4. SampleGameA copy: replace utoc/ucas pair with the patched-chunk rebuild -> game boots
   (proves IoStore writer acceptance).
Any failure here is a Phase 3 blocker, not a Phase 2 code regression — report symptoms.
```
- [ ] **Step 2: Full suite** `python -m pytest backend/tests -q` → 35 passed; record durations of the two slow tests.
- [ ] **Step 3: Snapshot** (source-only; exclude runtime/, caches, .sdd, _snapshots, fixtures >1 MB):
```bash
cd "/e/Dev/SLR Unreal Translator"
python - <<'EOF'
import os, tempfile, zipfile
root = os.getcwd()
out = os.path.join("_snapshots", "phase2-roundtrip.zip")
prune = {"__pycache__", ".pytest_cache", "runtime", "dist", "_snapshots", ".sdd"}
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in prune]
        for f in sorted(files):
            if f.endswith(".pyc"):
                continue
            full = os.path.join(base, f)
            if f.endswith(".json") and f"fixtures{os.sep}" in base and os.path.getsize(full) > 1 << 20:
                continue
            z.write(full, os.path.relpath(full, root))
print(out)
EOF
```
- [ ] **Step 4: Final ledger entry** (suite count, slow durations, E2E checklist status: automated done / manual pending user).

---

## Self-review notes

- Spec coverage: §5 stage 7's "repack the game's own containers in their original layout, re-apply protection damage, verify" = Tasks 4–6; EF decryption (§8 risk 1 resolved) = Tasks 1–2; in-house crypto (blake3, AES-pure fallback) = Tasks 1/3. Approach B's go/no-go is settled by the Task 8 manual E2E.
- Soft facts with resolution protocols: blake3 KAT prefix (fix vector not code if oracle passes); EF unmatched manifest line (name it in Task 2); byte-identity bisect rule (index blob first).
- Type consistency: `write_pak(src, idx, out, entries_data, aes_key, magic)` used identically in Tasks 4/5/7; `write_iostore(utoc, ucas, out_utoc, out_ucas, chunk_patches)` in Tasks 6/7; `oodle.compress(dll, data, compressor=8, level=4)` in Tasks 4–6.
