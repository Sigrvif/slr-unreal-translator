# SLRUnreal Phase 2 E2E checklist

Automated (pytest, real games, read-only on originals; all output to temp) — DONE:
`python -m pytest backend/tests -q` → **41 passed** in 40.6 s (five tests carry the
`slow` marker; dominated by the EF pak pair: 19.6 s golden + 4.2–4.7 s modified
round-trip).

- AES bcrypt/pure cross-check (NIST KAT + 64 oracle blocks + the full 1,190,592 B
  index decrypt) + EF index decrypt == Phase 1 oracle artifact
  `task8_index_decrypted.bin` (SHA-1 7ccf6481…1f7c).
- EF full index parse (17,829 entries, separately-encrypted FDI verified) +
  payload read with key (270/270 sampled entries exact-size, 0 failures).
- EF manifest reconciliation (unmatched line named:
  `SampleGameB/Content/Paks/SampleGameB-Windows.pak` — the pak itself; the UFS
  staging manifest lists the container, which cannot index itself; reverse
  coverage 17,829/17,829 exact).
- blake3 vs 20 real io_hashes (20/20 match, incl. a 2.79 MB deep-tree entry).
- pak writer: ST byte-identical rebuild (whole-file); ST modified-entry scoped
  delta (exactly `Engine/Config/BaseEngine.ini` differs, magic stays patched-e1);
  EF byte-identical rebuild (slow; 3,183,813,419 B sha256-identical); EF
  encrypted-entry modification round-trip (slow).
- utoc/ucas writer: unmodified identical (utoc AND ucas whole-file); one-chunk
  patch with blake3 + block alignment (neighbors byte-identical).
- rewrite orchestration end-to-end on ST pak (plan → execute → read-back), plus
  the iostore arm and validation arms (KeyError / unsupported container /
  keyless-encrypted ValueError).

Manual (user-run, on COPIED game dirs — originals never touched) — PENDING USER:

1. SampleGameA copy: replace `sample_a-Windows.pak` with the UNMODIFIED
   rebuild → game must boot to title (proves writer acceptance: magic + index
   hash).
2. SampleGameA copy: replace with the BaseEngine.ini-modified rebuild → game
   boots (proves modified-container acceptance).
3. SampleGameB copy: replace `SampleGameB-Windows.pak` with the modified
   rebuild → game boots (proves AES/Oodle re-encryption acceptance).
4. SampleGameA copy: replace the utoc/ucas PAIR with the patched-chunk rebuild
   → game boots (proves IoStore writer acceptance).

Any failure here is a Phase 3 blocker, not a Phase 2 code regression — report
symptoms (boot log, crash offset, which of the four).

## Regenerating the four artifacts

The rebuilt files come straight from the tests' rebuild functions
(`backend/ut/pak.py::write_pak`, `backend/ut/iostore.py::write_iostore`). All
four were generated into `%TEMP%` on 2026-10-22 and read-back-verified; any of
them can be regenerated with a one-liner run from the repo root
(`<dev-root>/SLR Unreal Translator`; outputs go to `%TEMP%` — substitute any folder
you prefer):

1. ST unmodified rebuild (byte-identical golden; sha256
   eb7078cf873a61a0f48dd9b17de585a6f8ea7b5e01273ecd6d5c18ce89763094):
```bash
python -c "import os; from backend.ut import pak; src=r'<games-root>\SampleGameA\sample_a\Content\Paks\sample_a-Windows.pak'; out=os.path.join(os.environ['TEMP'],'st-unmodified.pak'); pak.write_pak(src, pak.read_index(src), out); print(out)"
```

2. ST BaseEngine.ini-modified rebuild (same probe line as the test):
```bash
python -c "import os; from backend.ut import pak; src=r'<games-root>\SampleGameA\sample_a\Content\Paks\sample_a-Windows.pak'; idx=pak.read_index(src); t=next(e for e in idx.entries if e.name.endswith('BaseEngine.ini')); out=os.path.join(os.environ['TEMP'],'st-modified.pak'); pak.write_pak(src, idx, out, entries_data={t.name: pak.read_entry_data(src,t)+b'\r\n; slrunreal roundtrip probe\r\n'}); print(out)"
```

3. EF modified rebuild (first encrypted Oodle entry,
   `Engine/GlobalShaderCache-PCD3D_ES3_1.bin`, + `\x00slrunreal-probe`; key from
   the dev-side fixture):
```bash
python -c "import os,json; from backend.ut import pak; src=r'<games-root>\SampleGameB\SampleGameB\Content\Paks\SampleGameB-Windows.pak'; key=bytes.fromhex(json.load(open(r'backend\tests\fixtures\sample_b_index_facts.json',encoding='utf-8'))['aes_key_hex']); idx=pak.read_index(src,aes_key=key); t=next(e for e in idx.entries if e.flags&1 and e.method!=0); out=os.path.join(os.environ['TEMP'],'ef-modified.pak'); pak.write_pak(src, idx, out, entries_data={t.name: pak.read_entry_data(src,t,aes_key=key)+b'\x00slrunreal-probe'}, aes_key=key); print(out)"
```

4. ST patched utoc/ucas pair (same first small type-1 chunk + probe as the test;
   both files are needed, replace BOTH):
```bash
python -c "import os; from backend.ut import iostore; d=r'<games-root>\SampleGameA\sample_a\Content\Paks'; up=d+r'\sample_a-Windows.utoc'; cp=d+r'\sample_a-Windows.ucas'; toc=iostore.parse_toc(open(up,'rb').read()); i=next(i for i,e in enumerate(toc.entries) if e.length and 0<e.length<4096 and e.chunk_type==1); patched=iostore.read_entry(toc,cp,i)+b'slrunreal-probe'; t=os.environ['TEMP']; iostore.write_iostore(up,cp,os.path.join(t,'st-patched.utoc'),os.path.join(t,'st-patched.ucas'),chunk_patches={i:patched}); print(os.path.join(t,'st-patched.utoc'), os.path.join(t,'st-patched.ucas'))"
```
