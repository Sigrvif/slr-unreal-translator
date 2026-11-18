# E2E checklist — Phase 9 (Linux support)

Build under test: `dist/SLRUnreal-linux.zip` (44,343,588 B, 2,354
entries, verify OK). All offline gates on the Windows build host are
green; the functional Linux gate needs a Linux machine (the ELF
interpreter cannot run on Windows, so the smoke scan was skipped —
this is the same class of cross-build gap SLRUnity documents).

## Offline gates (PASS, 2026-10-26)

- [x] test_linux_port.py 9/9 (platform map, find_dll both layouts +
      unmapped error, oodle round-trip, fetch pins, ELF magic/symbols,
      runtime structural check, verify rules both directions).
- [x] Oodle-dependent regression 13/13 (the only backend change,
      oodle.py's library resolution, is behavior-neutral on Windows).
- [x] Full backend suite (216 expected: 207 + 9).
- [x] Linux package independent audit: runtime/bin/python3, pinned
      liboo2corelinux64.so.9 (sha256 byte-exact, ELF x86-64),
      lib/python3.10 stdlib, no win payload leakage.
- [x] verify_package rules reject: linux package without the .so; win
      package carrying runtime/bin/ or the linux payload.

## Linux-host gates (user)

- [ ] Install: unzip `SLRUnreal-linux.zip` into `www/addons/` of the
      Linux SLR Translator, restart. The New Project dialog must show
      the SLRUnreal entry (runtime resolution found
      `runtime/bin/python3`).
- [ ] Create: point it at a COPY of a Windows UE game (the whole
      folder — exe, Content/Paks, everything). Key discovery (EF) must
      confirm the key from the Windows exe's bytes. Expect the usual
      census (ST 1,092 / EF 10,645 rows) and the "Staging set" line.
- [ ] Translate a row or two, then export WITHOUT the game folder
      present elsewhere (the portable patch export) and/or normally.
- [ ] Demosaic standalone on ST if convenient.
- [ ] Confirm the .so loads (any Oodle-compressed game — EF/ST both
      are; a census matching Windows numbers proves it, since most
      chunks are Oodle-compressed and a failed dlopen degrades the
      scan to a handful of stored-only assets).

## Failure triage

- "runtime not found" in SLR: check `www/addons/SLRUnreal/
  runtime/bin/python3` exists and is executable after unzip (some
  archive tools drop the exec bit — `chmod +x` it and the .so: `chmod
  755 runtime/bin/python3* runtime/oodle/linux/*.so.*`).
- Census far below Windows numbers: the .so failed to load — run
  `runtime/bin/python3 backend/slr_cli.py` by hand with a scan
  manifest and read backend.log's traceback (usually the exec bit or a
  glibc mismatch on very old distros; python-build-standalone
  20240107 needs glibc ≥ 2.17, which every 2014+ distro has).
- Key discovery fails on EF: nothing platform-specific (PE bytes);
  compare backend.log against the Windows run's.
