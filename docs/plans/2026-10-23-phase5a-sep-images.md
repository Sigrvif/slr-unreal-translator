# Phase 5a — SEP image pipeline (UE textures)

Goal: SLRUnity-parity image round-trip. Extract every translatable
Texture2D as PNG into `<GameRoot>_unreal_img` (manifest.json alongside),
user runs an SEP project on that folder, then a standalone insert tool
writes SEP's rendered PNGs back into rebuilt containers.

## SLRUnity contract (<dev-root>/SLR Unity Translator, ut/textures.py)
- extract: one PNG per Texture2D, `<assetfile>__<name>__<path_id>.png`,
  manifest rows {file, rel, path_id, name, type, width, height, format,
  png, candidate}; manifest.json in the output folder.
- insert: 4 folder picks (game, extract dir, SEP render dir, output);
  every manifest entry whose PNG exists in the render dir is force-
  imported (no mtime check in render-dir mode); output = full game copy
  with patched files; `textures_manifest_dir` + `textures_dir` CLI keys.
- SEP side: SEP is an SLR Translator engine scanning a folder of images
  (.png/.jpg/...); rendered output keeps the same filenames. Filename is
  the join key. No addon-internal SEP API is used.

## UE-side discovery (2026-10-23, _texscan.py probes; ST=zen, EF=legacy)
Census: ST 259 Texture2D; EF 4544 Texture2D (+3806 LightMap +768
ShadowMap — skipped: class filter is exactly "Texture2D").
Formats seen in ST leading FStrings: PF_DXT1, PF_B8G8R8A8 (full census
pending in texture.py tests).

Cooked UTexture2D export layout (UE 5.6, both games, byte-verified):
- leading region is VARIABLE layout (tagged-prop-like data + cooked
  header interleaved; no fixed offsets — do not parse it structurally).
- reliable anchors:
  - M (mip count) == len(pkg.bulk_map) == i32 @ (first_inline_payload - 8);
    i32 @ -12 and -4 are 0.
  - pixel format: the last `PF_*` FString (i32 len + NUL-terminated ASCII)
  - per-mip payload: pkg.bulk_map[k] — zen: i32 dataIndex headers resolved
    by parse_zen; entry flags 0x40 = inline (payload inside the export's
    data slice at serial_offset), 0x100 = .ubulk (payload at serial_offset
    INSIDE the .ubulk package file; ubulk offsets are sequential).
  - dims: mip k>=1 dims appear in the 16B gap before inline payload k as
    [SX SY SZ=1 bulkidx=k]; consecutive-chain check + payload-size formula
    disambiguate; dims_0 = 2 x dims_1 (or leading-scan for M==1).
  - mip sizes: BC = ceil(w/4)*ceil(h/4)*blockbytes; uncompressed 4B/px
    (1B for G8/A8). Validated exact on all inspected mips.
- re-import with unchanged dims => identical payload sizes => byte-swap
  only; no structural edits, no bulk_map changes, no length propagation.

## Runtime constraint
Embeddable python is stdlib-only (no pip): PNG codec + BC1/2/3/5/7 codecs
are first-party implementations in backend/ut/ (pngio.py, bcdec.py).
No third-party code vendored; no new runtime fetches.

## Work breakdown
1. ut/pngio.py — RGBA <-> PNG (encode: filter 0 rows, zlib; decode: all
   5 filter types, color types 0/2/3/4/6, bit depths 8 (+16 -> 8)).
2. ut/bcdec.py — decode/encode BC1/BC2/BC3/BC4/BC5/BC7 + raw formats.
   Deterministic encoders (exact-solution search per block for 3-color
   BC1 / 4-color BC3 alpha; PCA-lite endpoints for BC7) — round-trip
   fidelity judged on decode(encode(decode(x))) PSNR, not byte identity.
3. ut/texture.py — parse_texture(pkg, ex) -> TextureInfo (name, dims,
   fmt, mips[] {storage, offset, size, dims}); extract_images(src, out,
   progress) -> manifest; import_images(src, manifest_dir, png_dir, ...)
   -> rebuilt container files (reuses rewrite.execute_rewrite staging).
4. slr_cli.py — commands `extract_images` {game_dir, out_dir} and
   `insert_images` {game_dir, out_dir, textures_manifest_dir,
   textures_dir}; insert writes rebuilt containers game-relative into
   out_dir (parity with text export), NOT a full game copy (containers
   are the only mutable part of a UE install; documented in help).
5. Addon: slide actions "Extract images" and "Insert translated images"
   (SLRUnity e2e-fix6 pattern), docs.html section.
6. Tests: synthetic zen packages (tiny BC1/BGRA textures built by hand,
   127grey/WhiteSquare-style ground truth: encode->parse->decode->assert
   uniform), manifest round-trip, same-dims guard.
7. Boot test: extract ST images folder + verify counts; user runs SEP.

## Rulings
- Same-dimensions rule: rendered PNGs must match extracted dims exactly;
  otherwise the entry is rejected with a clear error (SEP renders same
  canvas; SLRUnity silently re-encoded, UE mip chains make silent resize
  unsafe).
- LightMap/ShadowMap/TextureCube are skipped (class_name filter).
- Candidate flag mirrors SLRUnity heuristic (small, non-square-pow2,
  alpha formats).
