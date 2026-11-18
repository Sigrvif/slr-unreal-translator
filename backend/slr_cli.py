"""SLRUnreal backend CLI — manifest/result JSON envelope (contract v2).

Mirrors SLRUnity's backend/slr_cli.py envelope (read from the installed
source at ``<slr-unity backend>\\slr_cli.py``). The
Phase 4 addon will spawn

    python slr_cli.py --manifest job.json --result result.json

and read result.json for the outcome. All paths travel inside the
manifest file (non-ASCII-safe — Japanese game dirs never touch argv);
each run appends the manifest and any traceback to ``backend.log`` next
to the manifest. The result envelope is ALWAYS written — on a hard
error it is ``{"ok": false, "version": 2, "error": "TypeName: msg",
"traceback": <format_exc>}`` — and the process still exits 0: the
caller only ever parses the result file.

Mirrored envelope elements (cite-line for the ledger): CONTRACT_VERSION
constant + unsupported-version gate, ``--manifest``/``--result`` argv,
``{"ok": True, "version": CONTRACT_VERSION, **payload}`` success shape,
``{"ok": False, ...error/traceback}`` failure shape with
``f"{type(e).__name__}: {e}"`` error formatting, unknown-command gate,
utf-8 manifest read / result write with ``ensure_ascii=False``,
script-dir sys.path insert (embedded-runtime support), backend.log
append, exit 0 in every outcome.

NOT mirrored (deliberate): SLRUnity's stdout progress-line stream and
_SwallowStdout — its addon consumes a line-oriented JSON stream on
stdout, while no SLRUnreal Phase 3 consumer exists yet and nothing this
CLI imports prints to stdout. Per-asset soft errors collect into
``stats["errors"]`` via textscan's never-abort contract and do NOT
touch the envelope's ``ok``.

Commands (Phase 3):

- ``scan`` — manifest ``{"version": 2, "command": "scan", "game_dir":
  ...}`` → payload ``{"stats": {files_scanned, assets_scanned,
  text_entries, by_kind, warnings, errors, by_kind_jp},
  "containers": protection.fingerprint(game_dir), "text_preview":
  first 50 entries}``.
- ``extract`` — manifest + ``"out": path`` → runs the same scan, builds
  the scene graph + text table (Task 6), and writes them ATOMICALLY
  (tempfile in the target dir + os.replace) as one utf-8 JSON file;
  the envelope carries the merged stats + the out path.

Commands (Phase 4a, Task 3):

- ``export`` — manifest + ``"translations"`` (path to the extracted
  table JSON with english filled — or an inline table / ready
  ``{asset: {index: english}}`` mapping; see
  ``ut.exporttext.load_translations``) and ``"out": dir`` → patches the
  translated assets (blank english = skipped), rebuilds the touched
  containers into ``out`` (mirrored game-relative layout, atomic
  writers) and VERIFIES them (re-parse, re-extract, re-scan); the
  envelope carries ``{"stats": {rows_patched, rows_skipped, assets,
  verify}, "containers": [...], "out": dir}``. Games stay read-only.

AES KEY SEAM (Phase 4): dev/testing loads the AES key from the fixture
path carried in the manifest parameter ``aes_key_fixture`` (a JSON file
with ``"aes_key_hex"`` — backend/tests/fixtures/
sample_b_index_facts.json for EF). Phase 4 replaces this with the
shipped key-management UX; the seam lives entirely in _sources().
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback

# Embedded-runtime support (mirrors SLRUnity): the Windows embeddable
# Python ships with a python310._pth file forcing isolated mode — the
# script's own directory is then NOT on sys.path and `import ut` inside
# the command functions would raise ModuleNotFoundError. No-op under a
# normal interpreter (dev / backend tests).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

CONTRACT_VERSION = 2

try:
    from ut import keyscan as keyscan_mod  # normal interpreter (dev/tests)
except ImportError:                        # embedded runtime: import AFTER sys.path insert
    keyscan_mod = None
import importlib as _importlib
if keyscan_mod is None:
    keyscan_mod = _importlib.import_module("ut.keyscan")


KEYS_JSON = os.path.join(_HERE, "keys.json")


def _aes_key(job: dict) -> "bytes | None":
    """AES key resolution order (Phase 4b): manifest inline "aes_key"
    (manual entry, hex) > "aes_key_fixture" (dev/testing JSON) > keys.json
    cache (per-game entries written after a successful discovery)."""
    inline = job.get("aes_key")
    if inline:
        return bytes.fromhex(inline)
    fixture = job.get("aes_key_fixture")
    if fixture:
        with open(fixture, encoding="utf-8") as f:
            return bytes.fromhex(json.load(f)["aes_key_hex"])
    from ut import protection, pak
    game_dir = job.get("game_dir")
    if game_dir and os.path.isdir(game_dir):
        for path in protection.find_game(game_dir)["paks"]:
            try:
                if pak.read_footer(path).encrypted_index:
                    cached = keyscan_mod.load_keys(KEYS_JSON)
                    for tag, entry in cached.items():
                        try:
                            return bytes.fromhex(entry["key_hex"])
                        except (KeyError, ValueError):
                            continue
                    break
            except ValueError:
                continue
    return None


def _discover_and_cache(job: dict) -> "bytes | None":
    """Offline exe-scan discovery for an encrypted-index pak; persists the
    confirmed key to keys.json. Returns None when there is no encrypted pak
    (nothing to do) and propagates KeyNotFoundError as a contract error."""
    from ut import keyscan as ks
    game_dir = job["game_dir"]
    hit = ks._find_encrypted_pak(game_dir)
    if hit is None:
        return None
    result = ks.discover_key(
        game_dir,
        progress=lambda done: _progress_line(done, done, "key discovery"))
    import re as _re
    tag = _re.sub(r"[^A-Za-z0-9_-]", "_",
                  os.path.basename(os.path.normpath(game_dir))) or "game"
    ks.save_key(KEYS_JSON, tag, result["key_hex"], source="exe-scan")
    return bytes.fromhex(result["key_hex"])


def _progress_line(done: int, total: int, file: str) -> None:
    """SLRUnity-format stdout progress line (backend.js parses these)."""
    print(json.dumps({"type": "progress", "done": done, "total": total,
                      "file": file}), flush=True)


def _sources(job: dict) -> dict:
    """payload.find_sources over the manifest's game_dir + AES key.

    Hard contract gates (envelope carries error+traceback): game_dir
    must exist, a bad aes_key fixture/inline value is a hard error, and
    an encrypted-index pak with no resolvable key triggers offline
    discovery (manifest "discover_keys": false opts out) — cache hit,
    then exe scan; the confirmed key is cached to keys.json.
    """
    from ut import payload
    game_dir = job["game_dir"]
    if not os.path.isdir(game_dir):
        raise FileNotFoundError(f"game_dir not found: {game_dir!r}")
    key = _aes_key(job)
    if key is None and job.get("discover_keys", True):
        key = _discover_and_cache(job)
    return payload.find_sources(game_dir, aes_key=key)


def _scan_stats(src: dict, scan: dict) -> dict:
    """Envelope stats: textscan's stats dict + files_scanned (the
    payload layer's total name count — containers beyond packages)."""
    from ut import payload
    stats = dict(scan["stats"])
    stats["files_scanned"] = len(payload.list_files(src))
    return stats


def cmd_scan(job: dict) -> dict:
    from ut import protection, textscan
    src = _sources(job)
    scan = textscan.scan_game(
        src, progress=lambda done, total, file: _progress_line(done, total, file))
    return {
        "stats": _scan_stats(src, scan),
        "containers": protection.fingerprint(job["game_dir"]),
        "text_preview": [
            {"asset": e.asset, "kind": e.kind, "key": e.key,
             "source": e.source[:40]}
            for e in scan["entries"][:50]
        ],
    }


def cmd_extract(job: dict) -> dict:
    from ut import scenegraph, textscan
    src = _sources(job)
    scan = textscan.scan_game(
        src, progress=lambda done, total, file: _progress_line(done, total, file))
    scene = scenegraph.build_scenegraph(scan)
    stats = _scan_stats(src, scan)
    stats.update(scene["stats"])
    data = {
        "version": CONTRACT_VERSION,
        "command": "extract",
        "game_dir": job["game_dir"],
        "graph": scene["graph"],
        "text_table": scene["text_table"],
        "stats": stats,
    }
    out = job["out"]
    # Atomic write: tempfile in the TARGET directory (same volume, so
    # os.replace is atomic) then replace — a reader either sees the old
    # file or the complete new one, never a partial JSON.
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(out)),
        prefix=".slr-extract-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {"stats": stats, "out": out}


def _fonts(job: dict) -> "tuple[bytes | None, bytes | None]":
    """(dialogue_font, ui_font) bytes from the job's ``fonts`` paths —
    None per role when absent (Phase 5b: read+validate lives here so both
    font-capable commands share one validation point)."""
    spec = job.get("fonts") or {}
    if not spec.get("dialogue") and not spec.get("ui"):
        return None, None
    from ut import fonts as fonts_mod
    return (fonts_mod.read_font(spec["dialogue"]) if spec.get("dialogue")
            else None,
            fonts_mod.read_font(spec["ui"]) if spec.get("ui") else None)


def cmd_export(job: dict) -> dict:
    from ut import exporttext
    game_dir = job["game_dir"]
    if not os.path.isdir(game_dir):
        raise FileNotFoundError(f"game_dir not found: {game_dir!r}")
    # export_translations owns the ONE translations normalization point
    # (it accepts every load_translations shape directly) — passing the
    # raw manifest value avoids a second normalization (fix round 1).
    result = exporttext.export_translations(
        game_dir, job["translations"], aes_key=_aes_key(job), out_dir=job["out"],
        demosaic_game=("sample_a" if (job.get("demosaic") or {}).get("sample_a") else None),
        demosaic_include_medium=bool((job.get("demosaic") or {}).get("sample_b_medium")),
        fonts=_fonts(job))
    return {
        "stats": {k: result[k] for k in
                  ("rows_patched", "rows_skipped", "assets", "verify")},
        "fonts_swapped": result["fonts_swapped"],
        "skipped_detail": result["skipped_detail"][:20],
        "containers": result["containers"],
        "out": job["out"],
    }


def cmd_stage(job: dict) -> dict:
    """Phase 8a: one-pass capture — scan + extract table + staging tree.

    Manifest: ``{"version": 2, "command": "stage", "game_dir": ...,
    "stage_dir": ..., "out": extract.json path (optional)}``. The key
    resolution mirrors _sources (discovery + keys.json cache), because
    the stage tree must carry the confirmed key for a portable export.
    """
    from ut import stage
    game_dir = job["game_dir"]
    if not os.path.isdir(game_dir):
        raise FileNotFoundError(f"game_dir not found: {game_dir!r}")
    key = _aes_key(job)
    if key is None and job.get("discover_keys", True):
        key = _discover_and_cache(job)
    result = stage.capture_stage(
        game_dir, aes_key=key, stage_dir=job["stage_dir"],
        extract_out=job.get("out"),
        progress=lambda done, total, file: _progress_line(done, total, file))
    return {"stats": result["stats"], "stage": result["stage"],
            "out": result["out"], "stage_dir": result["stage_dir"]}


def cmd_export_patch(job: dict) -> dict:
    """Phase 8a: portable patch export from the staging tree.

    Manifest: ``{"version": 2, "command": "export_patch", "stage_dir":
    ..., "translations": path, "out": dir, "fonts": {...}}`` — no
    game_dir; the patch pak builds purely from the stage set.
    """
    from ut import exporttext
    if not os.path.isdir(job["stage_dir"]):
        raise FileNotFoundError(f"stage_dir not found: {job['stage_dir']!r}")
    result = exporttext.export_patch(
        job["stage_dir"], job["translations"], out_dir=job["out"],
        fonts=_fonts(job))
    return {
        "stats": {k: result[k] for k in
                  ("rows_patched", "rows_skipped", "assets", "verify")},
        "fonts_swapped": result["fonts_swapped"],
        "skipped_detail": result["skipped_detail"][:20],
        "containers": result["containers"],
        "patch": True,
        "out": job["out"],
    }


def cmd_stage_fingerprint(job: dict) -> dict:
    """Phase 8a: has the game changed since the stage was captured?

    Manifest: ``{"version": 2, "command": "stage_fingerprint",
    "stage_dir": ..., "game_dir": ...}`` → ``{"changed": [...]}`` ([] =
    unchanged; each entry names the container and what changed).
    """
    from ut import stage
    model = stage.load_model(job["stage_dir"])
    changed = stage.check_fingerprint(job["game_dir"], model)
    return {"changed": changed}


def cmd_demosaic_patch(job: dict) -> dict:
    """Phase 8b: the standalone demosaic patch container.

    Manifest: ``{"version": 2, "command": "demosaic_patch", "stage_dir":
    ..., "out": dir, "include_medium": bool (optional)}`` — portable like
    export_patch: built purely from the staging set, no game_dir.
    """
    from ut import demosaicpatch
    if not os.path.isdir(job["stage_dir"]):
        raise FileNotFoundError(f"stage_dir not found: {job['stage_dir']!r}")
    result = demosaicpatch.export_demosaic_patch(
        job["stage_dir"], out_dir=job["out"],
        include_medium=bool(job.get("include_medium")))
    return {
        "ops_applied": result["ops_applied"],
        "ops_skipped": result["ops_skipped"],
        "skipped_detail": result["skipped_detail"][:20],
        "containers": result["containers"],
        "verify": result["verify"],
        "patch": True,
        "out": job["out"],
    }


def cmd_extract_images(job: dict) -> dict:
    from ut import texture
    src = _sources(job)                     # key gate before any extraction
    del src
    out = job["out"]
    res = texture.extract_images(
        job["game_dir"], out, aes_key=_aes_key(job),
        progress=lambda done, total, file: _progress_line(done, total, file))
    return {"stats": {"images": res["count"], "errors": res["errors"][:20]},
            "out": out}


def cmd_insert_images(job: dict) -> dict:
    from ut import texture
    res = texture.import_images(
        job["game_dir"], job["textures_manifest_dir"], job["textures_dir"],
        aes_key=_aes_key(job),
        progress=lambda done, total, file: _progress_line(done, total, file))
    # Font swap (Phase 5b): .ufont entries join the SAME staged set so the
    # single container rebuild carries textures + fonts (SLRUnity parity:
    # Insert carries its own "Replace game fonts" checkbox).
    dialogue, ui = _fonts(job)
    fonts_swapped = 0
    if dialogue or ui:
        from ut import fonts as fonts_mod
        font_staged, _report = fonts_mod.stage_font_swaps(
            _sources(job), dialogue, ui)
        for name, data in font_staged.items():
            if name in res["staged"]:
                raise ValueError(
                    f"{name}: font swap collides with a staged payload")
            res["staged"][name] = data
        fonts_swapped = len(font_staged)
    containers = texture.write_containers(
        job["game_dir"], res["staged"], job["out"], aes_key=_aes_key(job))
    return {"stats": {"imported": res["imported"], "skipped": res["skipped"],
                      "errors": res["errors"][:20]},
            "fonts_swapped": fonts_swapped,
            "containers": containers, "out": job["out"]}


COMMANDS = {"scan": cmd_scan, "extract": cmd_extract, "export": cmd_export,
            "stage": cmd_stage, "export_patch": cmd_export_patch,
            "stage_fingerprint": cmd_stage_fingerprint,
            "demosaic_patch": cmd_demosaic_patch,
            "extract_images": cmd_extract_images,
            "insert_images": cmd_insert_images}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--result", required=True)
    args = ap.parse_args(argv)

    log_path = os.path.join(os.path.dirname(os.path.abspath(args.manifest)),
                            "backend.log")
    result = {"ok": False, "version": CONTRACT_VERSION}
    try:
        with open(args.manifest, encoding="utf-8") as f:
            job = json.load(f)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps(job, ensure_ascii=False) + "\n")
            if isinstance(job.get("game_dir"), str):
                try:
                    from ut import oodle
                    oodle.add_search_root(job["game_dir"])
                except Exception:
                    pass  # discovery hint only; find_dll errors carry the details
            if job.get("version") != CONTRACT_VERSION:
                result["error"] = (
                    f"unsupported contract version {job.get('version')!r} "
                    f"(this backend speaks v{CONTRACT_VERSION})")
            else:
                fn = COMMANDS.get(job.get("command"))
                if fn is None:
                    result["error"] = f"unknown command {job.get('command')!r}"
                else:
                    payload = fn(job)
                    result = {"ok": True, "version": CONTRACT_VERSION,
                              **payload}
    except Exception as e:  # noqa: BLE001 — envelope must never crash without a result
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(result["traceback"] + "\n")
    with open(args.result, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
