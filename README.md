# SLRUnreal

An **SLR Translator** addon that adds an **Unreal Engine (UE4 / UE5)** game
engine, so Unreal games can be translated with SLR's DSLR engine, glossaries
and QC pipeline the same way RPG Maker games are.

It reads the game's containers directly — legacy `.pak` and IoStore
`.utoc`/`.ucas` pairs — and writes translations back as **patch containers**.
No runtime hooks, nothing installed into the game:

- **Scan** finds every translatable Japanese string: FText and FString
  properties in every package (legacy and Zen formats), strings inside
  compiled UI script bytecode (same-size in-place patching), and — on
  IoStore games — name-table strings that games display as UI text (DataTable
  row keys, option labels), pre-tagged by risk. AES-encrypted pak indexes are
  supported; the key is discovered **offline from the game's own executable**
  and never bundled.
- **Translate** in SLR as usual (DSLR batch, glossary, manual rows; blank
  rows are never written). While an Unreal project is open, DSLR
  automatically uses Unreal text-protection patterns (rich-text markup,
  FText format arguments) instead of its RPG Maker ones — editable in
  Options → General → SLRUnreal, and never touching the user's DSLR config.
- **Export** a small patch container built entirely from the project's own
  staging set (`<name>_SLR_P.pak`, or `.utoc`/`.ucas` on IoStore) — copy it
  into the game's `Content\Paks` folder, delete it to uninstall. Optional:
  full container rebuild from the original game folder, font replacement
  with SLR's own faces, demosaic (image-overlay material swaps), and a
  standalone SEP round-trip for text drawn into images.

User-facing help lives inside the addon: [`addon/docs.html`](addon/docs.html)
(served by SLR's documentation system; [`addon/assets/help.html`](addon/assets/help.html)
is the standalone twin).

## Layout

```
addon/                  JS addon (what ships to SLR users)
  UnrealAddon.js        engine wiring: New Project dialog entry, detector,
                        createProject, exportHandler, export option fields,
                        the DSLR pattern switch + its addon options
                        (eval'd by SLR's addon loader; the only file touching
                        NW/SLR globals)
  lib/                  pure CommonJS: backend runner, detector + game-root
                        resolver, project builder, path helpers, the Unreal
                        pattern sets and the DSLR pattern-switch installer
                        (no NW globals; requireable from plain Node)
  assets/               help.html (standalone help), icon.png
  tests/                node --test suite (lib units + loader-eval harness;
                        skips cleanly without a built stage)
backend/                Python backend (scan / stage / extract / patch)
  slr_cli.py            single CLI entry point (JSON contract v2)
  ut/                   implementation modules (pak, iostore, zen, textscan,
                        patchtext, patchio, cityhash, ...)
  tests/                pytest suite (runs against the reference games when
                        present; skips otherwise)
build/                  build scripts
  fetch_runtime.py      downloads + verifies the per-OS embedded Python
                        runtime (build-time only; no downloads at install)
  fetch_oodle.py        fetches the Oodle data-compression library used by
                        IoStore containers
  build_release.py      stages the addon tree + runtime, writes
                        dist/SLRUnreal-<version>-<os>.zip AND .7z, verifies both,
                        smoke-tests the staged win runtime against the
                        reference game
  build_source.py       writes the minimal source archive
                        (dist/SLRUnreal-<version>-source.zip)
  verify_package.py     single source of truth for the package structure
runtime/                fetched payloads (win/, linux/) — not committed
                        anywhere; produced by the fetch scripts
dist/                   build output: SLRUnreal-<version>-<os>.zip / .7z
                        + the source archive (+ stage trees)
docs/                   E2E checklists, plans and specs
                        (development material)
```

## Building

```
python build/fetch_runtime.py --os win     # + linux (payloads land in runtime/)
python build/fetch_oodle.py
python build/build_release.py              # win + linux zips/7zs into dist/
python build/build_source.py               # minimal source archive into dist/
```

The addon itself is plain JavaScript with no npm dependencies; the backend is
standard-library Python (everything non-trivial — CityHash64, container
formats — implemented from scratch in-tree, zero third-party code vendored).

## Testing

- Backend: `python -m pytest backend/tests` (from the repo root). The suite
  pins the formats against two reference games when they are present and
  skips cleanly otherwise.
- Addon: `node --test addon/tests` — pure-lib units plus a loader-contract
  harness that evals `UnrealAddon.js` exactly the way SLR's addon loader
  does. The loader tests need a built stage (`dist/stage-win`); they skip
  without one.

## Installing

Extract the release archive for your platform into SLR Translator's
`www/addons/` folder (so it becomes `www/addons/SLRUnreal/`) and restart SLR
Translator. Remove the folder to uninstall. Details:
[`addon/docs.html`](addon/docs.html#installing).

## Constraints honored (for contributors)

- Zero modification of SLR Translator — everything goes through the documented
  addon surface (`Engines.registerDetector`, `engines.addHandler`,
  `addProperty("exportOptionFields", ...)`, `common.initStaging`, the
  translator-engine option resolver). The DSLR pattern switch installs a
  per-option backend resolver on the live engine's pattern options at
  runtime; no SLR file or stored setting is ever touched.
- `addon/lib/*.js` stay pure CommonJS (plain-Node testable); NW/SLR globals
  live only in `UnrealAddon.js`.
- Runtime payloads (embedded Python, Oodle) are fetched at build time only —
  installs and users never download anything.
- No game keys are bundled: discovered AES keys are cached locally in
  `backend/keys.json` inside the addon folder and re-discovered from the
  game's executable on any new machine.
- Zero third-party code vendored: CityHash64, the pak/IoStore/zen package
  formats and every patcher are implemented from scratch in
  `backend/ut/`.

## License

Copyright (C) 2026 Shisaye.

GPLv3 or later — see [LICENSE](LICENSE); the components bundled into release
packages (CPython runtime, CityHash/DirectXTex-derived codec code) are
listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The Oodle Core
library is **not bundled** (Epic's UE EULA does not permit it outside UE
products): the backend discovers it at scan time from the game being
processed or a local Unreal Engine install. Runtime payloads are fetched at
build time by `build/fetch_*.py`, never committed.
