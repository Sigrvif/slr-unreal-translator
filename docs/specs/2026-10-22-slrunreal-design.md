# SLRUnreal — Design Spec

**Date:** 2026-10-22
**Status:** Approved design (all sections user-approved in session; awaiting spec review)
**Product:** SLRUnreal — a self-contained drop-in SLR Translator addon for translation and
demosaic of Unreal Engine games, sibling to SLRUnity (v2.7).

---

## 1. Purpose

Deliver the SLRUnity experience for Unreal Engine games: point SLR Translator at an installed
UE game, scan its containers, extract translatable text with a scene graph for context
quality, translate through SLR's flow, remove mosaics, and export modified game files that
replace the originals — the same file-replacement model SLR uses for Unity games.

**First-release scope (user decision: full parity):** translation + demosaic together,
including defeat of the container-protection class found in both test games (user decision:
protection defeat is a core case, not an edge case).

## 2. Target class & evidence

Two test games, both UE5, both carrying the same container-damage protection:

| | SampleGameA (SampleGameA) | SampleGameB v1.10 |
|---|---|---|
| Path | `<games-root>\SampleGameA` | `<games-root>\SampleGameB` |
| Engine | UE5.5-era (IoStore TOC v8 = ReplaceIoChunkHashWithIoHash; NNE plugin, tbb12.dll) | UE5 ≈5.0–5.3-era (CEF3 EpicWebHelper; built 2023-12/2024-07; pak-only) |
| Containers | `sample_a-Windows.pak` (15.7 MB) + `.utoc/.ucas` (473 KB / 556 MB) + `global.utoc/.ucas` | `SampleGameB-Windows.pak` (3.18 GB, Oodle) only |
| Damage | pak footer magic byte patched (`0x5A6F12E1` vs stock `0x5A6F12F1`), index PLAINTEXT and readable | pak footer magic patched the same way; index **and** entry payloads AES-encrypted (key hunt = IDA workstream) |
| Bodies | plaintext entries visible at offset 0 (bare INI); index at 0xEE3E0A, 1669 named entries | plaintext FPakEntry-style headers at offset 0; unreadable until the AES key lands |

Two corrections of initial misreads, both settled by byte evidence: (1) the ASCII
`-==--==--==--==-` at the start of both `.utoc` files is **the stock IoStore TOC magic
constant** (verified against retoc's `FIoStoreTocHeader::MAGIC`), not a wipe — the
utoc/ucas files parse with a standard, unencrypted reader today. (2) The pak footers
were never "zeroed": both footers are intact 221-byte v11 footers whose **magic
constant carries a one-byte patch** (F1→E1); a narrow tail hexdump only showed the
zero-padded compression-method-name region. The actual protection primitives are the
magic patch (both games) and AES-256 encryption of the SampleGameB index/payloads.

Supporting evidence:

- Launchers (`sample_a.exe`, `SampleGameB.exe`, ~260 KB) are stock UE
  `BootstrapPackagedGame` — **not** the protector. The damaged paks are read by the
  patched shipping exes; that is what the IDA exports are for.
- SampleGameA utoc parses cleanly with a standard reader: version=8, header_size=0x90,
  entry_count=3247, 19,422 compression blocks, 1 method ("Oodle"), 64 KB block size,
  directory index 83,911 B — field-by-field verified against retoc's layout.
- Neither game ships game-level localization (only engine `Engine.locres`); game text is
  FText/FString inside assets. The C# loc-pipeline reference tool does not apply as a
  pipeline.
- Neither game ships a standalone `oo2core` DLL (Oodle statically linked in UE5).
- `Manifest_UFSFiles_Win64.txt` present in both games (4,737 / 17,830 lines) — full
  shipped-file lists usable for entry-name cross-reference.
- IDA exports (user-provided, `*_export_for_ai` folders): `decompile/` (hex-rays C),
  `disassembly/`, `function_index.txt`, `strings.txt`, `pointers.txt`, `imports.txt`,
  `exports.txt`, `memory/` for both shipping exes and both launchers.

## 3. Decisions (user-approved)

1. **Separate addon** `SLRUnreal`, sibling of `SLRUnity` — own folder, own embedded runtime.
2. **First milestone = full parity:** translation + demosaic delivered together.
3. **Protection defeat is core:** repair-read damaged containers; produce output the patched
   engine accepts.
4. **Delivery = Approach B, in-place replacement** (user override of my Approach A
   recommendation, to stay consistent with how SLR handles games): export repacks the game's
   *own* containers — patch-overlay paks are not used. Runtime DLL hooks rejected.
5. **Scene graph is in scope** (user addition): asset-context graph for translation quality.
6. Dev home `<dev-root>/SLR Unreal Translator` mirroring SLRUnity conventions (flat addon
   layout, `_snapshots/`, `.sdd/` ledgers, no git).

## 4. Architecture

```
<dev-root>/SLR Unreal Translator\
├── addon/                          → ships as www/addons/SLRUnreal/ (flat)
│   ├── package.json                name SLRUnreal; load: [UnrealSceneGraph.js, UnrealAddon.js]
│   ├── UnrealAddon.js              UI: New Project slide, options, demosaic panel, export
│   ├── UnrealSceneGraph.js         scene-graph surface (mirrors UnitySceneGraph.js)
│   ├── lib/
│   │   ├── backend.js              embedded-python IPC (same pattern as SLRUnity)
│   │   ├── paths.js                game-dir resolution, container discovery
│   │   ├── detector.js             engine/container/protection fingerprint
│   │   ├── projectBuilder.js       project scaffolding for SLR
│   │   └── sceneGraphCore.js       shared graph building blocks
│   ├── docs.html, assets/help.html, assets/icon.png
├── backend/
│   ├── slr_cli.py                  JSON manifest/result contract v2 (same as SLRUnity)
│   └── ut/                         pure python, zero deps
│       ├── pakfile.py              pak v8/v11 reader/writer (own code)
│       ├── iostore.py              utoc/ucas reader/writer
│       ├── uasset.py               .uasset/.uexp reader/writer (name map, imports/exports, property tags, FText)
│       ├── protection.py           damage fingerprint / repair-read / re-apply (profile-driven)
│       ├── scan_text.py            FText/FString discovery → scene graph + text table
│       ├── scan_demosaic.py        material/texture/post-process candidate detection
│       └── patch.py                apply translation table + demosaic edits, minimal-diff repack
├── build/                          fetch_runtime.py, build_release.py, verify_package.py (SLRUnity pattern, SLRUnreal naming)
├── docs/                           specs, E2E checklist
├── _snapshots/, .sdd/              SLRUnity conventions
└── runtime/win|linux               same embedded python runtimes as SLRUnity
```

Principles:

- **JS is a thin shell; all format knowledge lives in `ut/`.**
- **`protection.py` is data-driven:** the scanner emits a per-game protection profile
  (damage kinds, index-recovery facts); export re-applies exactly what the profile
  describes. Unknown profile ⇒ report, never guess-write.
- **Scene graph:** nodes are asset-context based — text entries carry owning asset, object,
  and property path (widget / data-table row / string-table key / level actor), feeding
  SLR's context grouping the same way UnitySceneGraph did.
- **No vendored third-party code.** References (`rust-u4pak` MPL-2.0, `u4pak` BSD,
  LocalizationUE4) are for reading only; all format code is ours, keeping the addon
  license-clean and fully self-contained.

## 5. Pipeline (data flow)

1. **Detect** — shipping exe, UE version markers, container layout (pak-only vs
   pak+IoStore), protection fingerprint, `Manifest_UFSFiles` presence.
2. **Repair-read** — pak side: read the intact (magic-patched) footers, parse plaintext
   indexes directly (SampleGameA: 1,669 named entries); SampleGameB's AES-encrypted
   index awaits the IDA-derived key (Phase 2 implements decryption; Phase 1 detects and
   reports it). The utoc/ucas side needs **no repair** — stock parsing (verified; see §2
   corrections).
3. **Index & extract** — enumerate entries (names/sizes/offsets); stage
   `.uasset/.uexp/.ubulk` payloads to the backend work area.
4. **Scene graph + text table** — parse assets, walk FText/FString in widgets, data
   tables, string tables, level actors; emit scene graph + SLR text table.
5. **Demosaic scan** — candidates by name/kind over materials/textures/post-process
   (VRM4U-aware for SampleGameA); user-confirmed candidate list in the UI.
6. **Translate** — SLR's translation flow over the table (unchanged model).
7. **Export** — patch from the *pristine* staged extraction (idempotent; never
   double-applied) → repack the game's own containers in original layout → re-apply
   protection damage → verify (re-parse; patched texts present; unmodified compression
   blocks hash-identical where copied verbatim) → game-relative files into SLR's export
   zip.

**Delivery & safety model:** the backend never writes into the game directory; export
always produces files for the zip. Recommended workflow is a copied game dir (unmodded /
modded, as with KousokuJoshi). Known cost, accepted: replacing a 3.2 GB pak means a
multi-GB export zip; IoStore games export far smaller. Documented in help.

## 6. IDA / protection workstream (runs first)

Priority-ordered analysis over the provided exports (offline grep-and-read; live debugging
only with user escalation):

1. SampleGameB pak-index location under a zeroed footer — xrefs off surviving
   `FPakPlatformFile::Initialize` / mount-path strings. Outcome goes into `protection.py`
   as index-recovery facts.
2. SampleGameA's tolerance of the wiped utoc magic — runtime restore vs hardcoded header
   fields; whether its companion `.pak` follows SampleGameB's scheme.
3. Foreign/modified container checks (hash/signature) that a repacked container must
   satisfy — the go/no-go fact for Approach B.
4. Demosaic leads — mosaic-related strings/names to pre-seed `scan_demosaic.py`.

## 7. Testing strategy

- **Round-trip goldens:** parse → serialize each real container; unmodified output
  byte-identical (or hash-identical at compression-block level where blocks are copied
  verbatim).
- **Repair-read goldens:** full entry enumeration of all damaged containers; names
  cross-checked against `Manifest_UFSFiles`.
- **Asset patch units:** FText/FString replace → re-serialize → re-parse → text present;
  length-delta cases included.
- **Damage re-application tests:** repacked+damaged container parses identically to the
  original damaged one.
- **E2E on both games:** scan → translate → export → user launches; checklist discipline as
  SLRUnity (`docs/E2E-CHECKLIST.md` equivalent).

## 8. Risks & mitigations

1. **SampleGameB's AES key** (confirmed encrypted index+payload; the one real
   protection unknown left) → IDA key hunt (Task 8); if the key resists static
   extraction, escalation to the user before any debugger step.
2. **Oodle dependency** — reading ucas payloads AND writing compressed containers both
   need `oo2core` (proprietary; no DLL ships in either game — statically linked in UE5).
   Strategy mirrors repak/retoc: fetch `oo2core_9_win64.dll` on demand from the public
   mirror of Epic's UE Oodle SDK (pinned URL + SHA-256, the same source repak's
   `oodle_loader` uses), cache it outside the shipped package, never redistribute.
   Fallback if the URL dies: user-supplied DLL (any Steam UE game ships one) via a
   documented path/env var. TOC/index/filename work does NOT need the DLL — only payload
   extraction does.
3. **Asset-format drift UE 5.0–5.4** → version-aware `uasset.py` driven by each asset's
   serialized `CustomVersionContainer`; the two test games pin a 5.0-era and 5.3-era
   baseline.
4. **Multi-GB export UX** → accepted cost of the replace model; documented.

## 9. Out of scope (v1)

- Mod-style patch-overlay delivery (`*_P.pak` / patch utoc-ucas) — rejected for
  consistency with SLR's model; the read/repair/write core would support it later if ever
  wanted.
- Runtime DLL hooks / injections.
- Linux-game support beyond the SLRUnity runtime convention (win games are the audience;
  the linux runtime ships for SLR platform parity only if cheap).
- Editing beyond text + demosaic (no asset resurrection, mesh/anim tooling).

## 10. Open spike questions (resolve during implementation, in order)

1. What fraction of SampleGameA ucas chunks are actually Oodle-compressed vs stored
   uncompressed? (Sizing the Oodle dependency for reading.)
2. Where is SampleGameB's AES-256 key, and where do both engines compare the patched
   pak magic 0x5A6F12E1 (the write-back rule for repacked paks)? (IDA spike.)
3. Does SampleGameA's engine mount its companion `.pak`, and is there any signature
   or hash gate beyond the magic patch that a repacked pak must satisfy?
   (Approach B go/no-go for the pak side; utoc/ucas are stock so their go/no-go is
   standard UE behavior.)
4. Do these engines accept uncompressed/zlib-packed paks/ucas when blocks change?
   (Compression fallback validation for writing.)
5. What does the demosaic surface look like per game once assets are readable?

---

## Appendix A — Reference materials

- Test games + IDA exports: `<games-root>\` (see §2 table).
- Reference tools: `<dev-root>/UE\reference tools\` — `rust-u4pak-1.4.0` (MPL-2.0, Rust),
  `u4pak-master` (BSD, single-file Python), `LocalizationUE4-1.7.0` +
  `TranslationEditor_170` (same C# tool, source + binary; UE loc-pipeline editor, useful
  as FText format reference only), and `retoc` (cloned 2026-10-22 from
  https://github.com/trumank/retoc — the current authoritative IoStore read/write
  implementation; its `retoc/src/lib.rs` is the verified source for the utoc/ucas
  layouts in §2 and the porting reference for `ut/iostore.py`).
- SLRUnity (architecture donor): dev `<dev-root>/SLR Unity Translator`, installed
  `<dev-root>/SLR Translator\www\addons\SLRUnity\`.

## Appendix B — Standing constraints (inherited)

- Do not edit SLR Translator; work with the existing addon system; report what doesn't
  work.
- Pre-git development used snapshots in `_snapshots/` and ledgers in `.sdd/` (local, gitignored archives); the repository now uses normal git history.
- Addon fully self-contained (embedded runtime, zero external deps, no vendored GPL/MPL
  code).
- **Key-material hygiene (added after Phase 1, tightened for public release):**
  game-derived secrets (e.g. the SampleGameB AES key — resolved from an env var or
  an untracked local file, see `backend/tests/keys.py`) and the fetched Oodle DLL
  (`runtime/oodle/`) are dev-side only and never committed. Release packages built by a future
  `build_release.py` must exclude `backend/tests/`, `docs/`, `.sdd/`, `_snapshots/`, and
  `runtime/oodle/` — verified by the release verifier, same discipline as SLRUnity's
  no-dev-leakage rules.
