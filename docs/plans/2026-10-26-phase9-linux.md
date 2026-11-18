# Phase 9 — Linux support (translate Windows games on a Linux machine)

User goal (2026-10-26): "it would be enough for the Linux version to
simply be able to work with the Windows ones so the translation can
still be made on a Linux machine regardless where or how it is
ultimately used."

## Audit result (why this phase is small)

The backend only READS game files as bytes — no Windows API anywhere:

- AES: pure-stdlib `aes.py` (no PyCryptodome).
- Oodle: `ctypes.CDLL` over the fetched SDK library — the SAME C
  symbols exist in the Linux `.so` (verified in the ELF dynsym:
  OodleLZ_Decompress / OodleLZ_Compress /
  OodleLZ_GetCompressedBufferSizeNeeded).
- Key discovery scans the Windows shipping exe's PE **bytes**
  (`_pe_sections` parses the file, never the process) — works
  identically on Linux.
- Path handling: `os.walk`/`os.path` are OS-native; the manifest carries
  UTF-8 JSON paths; `os.sep` splits in `find_game` are consistent with
  the host walk that produced them.
- The addon's `lib/backend.js resolveRuntime` ALREADY had the Linux
  branch (`runtime/bin/python3`) from the SLRUnity port — the payload
  just never existed.
- Detector's `-Win64-Shipping.exe` patterns are CORRECT for the
  supported scenario (the game tree is a Windows game copied to Linux).

So the port = ship the Linux runtime + Oodle payloads and build/verify
plumbing. Nothing in backend/ut or addon needed changing for
function; only `oodle.py`'s library resolution was Windows-hardcoded.

## Changes

- `build/fetch_oodle.py`: two pinned payloads — win dll (existing sha)
  + linux `liboo2corelinux64.so.9` from the same OodleUE 2.9.10 mirror
  (ELF x86-64, sha256 ed7e98f7…84e9c, symbols verified in-file). `--os
  win|linux|all`. The .so is chmod 755 (dlopen needs the exec bit; the
  zip does not carry it, so the fetch AND the build stage both chmod).
- `backend/ut/oodle.py`: platform-aware `find_dll` — `sys.platform`
  maps to `runtime/oodle/<os>/<lib>`, resolved relative to BOTH the dev
  tree and the packaged addon layout (backend/ut sits one level deeper
  in the package). Env override `SLRUNREAL_OODLE_DLL` unchanged.
- `build/fetch_runtime.py`: Linux runtime = python-build-standalone
  CPython 3.10.13 x86_64-unknown-linux-gnu (20240107 — the same pinned
  release SLRUnity uses), install-only tar hoisted so
  `runtime/linux/bin/python3` exists (the layout resolveRuntime
  expects). NO pip stage (pure stdlib backend — the SLRUnity pip pins
  are Unity-specific). Verification: run on Linux; on a Windows host a
  structural check (bin/python3 + bin/python3.10 + real
  lib/python3.10 stdlib files) — the functional gate is the first run
  on a Linux machine.
- `build/build_release.py`: `--os win|linux|all`; per-OS Oodle payload
  staged beside the runtime (linux .so chmod 755); smoke scan
  generalized (win runs it, linux skips — the interpreter cannot run
  on a Windows host, SLRUnity build_release.py:180 precedent).
- `build/verify_package.py`: `RUNTIME_MARKER["linux"] =
  runtime/bin/python3`; cross-contamination rules both directions
  (win package must not carry linux payload and vice versa); each OS's
  package MUST carry its Oodle library.

## Validation

- Offline (this Windows host): fetch_oodle both targets idempotent +
  sha-pinned; fetch_runtime linux structural check green; oodle.py
  win round-trip unchanged; ELF dynsym symbol check; full backend
  suite (the oodle.py rewrite is the only backend change — regression
  must stay 207/207); build_release --os linux produces a verified
  package; verify rules reject a linux package without the .so.
- Linux host (user gate): unzip the package into www/addons/, create a
  project for a Windows game copy, export. The checklist carries the
  exact steps.

## Out of scope

- Linux-native UE game binaries (`*-Linux-Shipping`) as key-scan
  targets — ELF key discovery is a different research problem; the
  user's scenario (Windows games) needs only PE scanning, which the
  game tree already provides.
- ARM Linux.
