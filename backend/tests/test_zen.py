"""Task 3: zen package parser (SampleGameA, UE 5.6.1 IoStore).

Invariants pinned against the real game and the Task 2 discovery fixture
(``text_discovery.json`` is ground truth):
  - ALL 1,359 ST packages (1,355 .uasset + 4 .umap) parse without
    exceptions and every package name-batch entry decodes.
  - Golden ``DT_学校会話``: exports carry resolved class names (DataTable)
    and the union of export data slices covers its 473 FText frames
    (>= 470 required; the Task 5 bridge).
  - ST-wide: FText frames found INSIDE export slices total exactly 992
    (the fixture's frame census).
  - Every export with serial_size > 0 has a full-length non-empty data
    slice; a no-export package parses to an empty export list.

The FText frame detector below is TEST-ONLY (the discovery grammar:
flags u32=1 | history u8=0 | 32-UPPERCASE-hex key | negative-length
UTF-16LE source); production detection belongs to Task 5.
"""
import re
import struct

import pytest

from backend.ut import iostore, payload, zen

CHUNKTYPE_SCRIPT_OBJECTS = 5
ST_TOTAL_PACKAGES = 1359            # fixture: packages_scanned
ST_TOTAL_FRAMES = 992               # fixture: ftext_frames_jp (frame census)
GOLDEN_SUFFIX = "Content/common/データ/DT_学校会話.uasset"
WBP_SUFFIX = "Content/common/電車/WBP_痴漢システム.uasset"

# flags u32=1 | history u8=0 | FString len 33 | 32 UPPER hex | NUL | i32 len
_FRAME = re.compile(
    rb"\x01\x00\x00\x00\x00\x21\x00\x00\x00([0-9A-F]{32})\x00(....)", re.S)


def _ftext_frames(data: bytes) -> "list[tuple[int, int]]":
    """(start, end) byte ranges of FText-Base frames in a raw payload."""
    out = []
    for m in _FRAME.finditer(data):
        (ln,) = struct.unpack_from("<i", m.group(2), 0)
        if ln >= 0:                       # source must be negative-length
            continue
        end = m.end() + 2 * (-ln)         # -ln UTF-16 units incl NUL
        out.append((m.start(), end))
    return out


@pytest.fixture(scope="session")
def st_src(game_paths):
    return payload.find_sources(game_paths["sample_a"])


@pytest.fixture(scope="session")
def script_objects(st_src):
    """The game's global ScriptObjects chunk (type 5; ST ships it in
    global.utoc/global.ucas with an all-zero chunk id)."""
    for utoc_path, ucas_path in st_src["io_pairs"]:
        with open(utoc_path, "rb") as f:
            toc = iostore.parse_toc(f.read())
        for i, e in enumerate(toc.entries):
            if e.chunk_type == CHUNKTYPE_SCRIPT_OBJECTS:
                return zen.parse_script_objects(iostore.read_entry(toc, ucas_path, i))
    pytest.fail("no ScriptObjects chunk in any IoStore container pair")


@pytest.fixture(scope="session")
def sweep(st_src, script_objects):
    """Parse every ST package once; return per-package stats + a small
    payload cache for the per-asset assertions."""
    names = [n for n in payload.list_files(st_src)
             if n.lower().endswith((".uasset", ".umap"))]
    golden = next(n for n in names if n.endswith(GOLDEN_SUFFIX))
    wbp = next(n for n in names if n.endswith(WBP_SUFFIX))
    stats = {
        "packages": {},              # name -> dict(exports, frames_in, frames_total)
        "names_decoded": 0,
        "total_exports": 0,
        "unresolved_script_classes": 0,
        "errors": [],
        "bad_slices": [],
        "empty_export_packages": [],
        "cache": {golden: None, wbp: None},
    }
    for n in names:
        try:
            data = payload.read_file(st_src, n)
            pkg = zen.parse_zen(data, n, script_objects=script_objects)
            stats["names_decoded"] += len(pkg.names)
            stats["total_exports"] += len(pkg.exports)
            for ex in pkg.exports:
                if ex.serial_size > 0 and len(ex.data) != ex.serial_size:
                    stats["bad_slices"].append((n, ex.object_name))
                if ex.class_name is None and (ex.class_index >> 62) == 1:
                    stats["unresolved_script_classes"] += 1
            if not pkg.exports:
                stats["empty_export_packages"].append(n)
            slices = [(pkg.header_size + ex.serial_offset,
                       pkg.header_size + ex.serial_offset + ex.serial_size)
                      for ex in pkg.exports]
            frames = _ftext_frames(data)
            inside = sum(1 for s, e in frames
                         if any(a <= s and e <= b for a, b in slices))
            stats["packages"][n] = {"exports": len(pkg.exports),
                                    "frames_total": len(frames),
                                    "frames_in": inside}
            if n in stats["cache"]:
                stats["cache"][n] = (data, pkg)
        except Exception as exc:            # noqa: BLE001 - recorded, asserted
            stats["errors"].append((n, repr(exc)))
    return stats


def test_all_packages_parse(sweep):
    assert len(sweep["packages"]) + len(sweep["errors"]) == ST_TOTAL_PACKAGES
    assert sweep["errors"] == []
    assert sweep["names_decoded"] > 0
    umaps = [n for n in sweep["packages"] if n.lower().endswith(".umap")]
    assert len(umaps) == 4                    # fixture: 4 map packages
    # every package name batch decodes to plain strings (parse would have
    # raised otherwise); the golden's own name is IN its batch
    data, pkg = sweep["cache"][next(n for n in sweep["cache"]
                                    if n.endswith(GOLDEN_SUFFIX))]
    assert pkg.package_name == "/Game/common/データ/DT_学校会話"
    assert "DT_学校会話" in pkg.names


def test_golden_dt_export_classes(script_objects, sweep):
    _, pkg = sweep["cache"][next(n for n in sweep["cache"]
                                 if n.endswith(GOLDEN_SUFFIX))]
    assert pkg.exports
    assert any(ex.class_name == "DataTable" for ex in pkg.exports)
    _, wbp = sweep["cache"][next(n for n in sweep["cache"]
                                 if n.endswith(WBP_SUFFIX))]
    # the widget blueprint's own generated class (observed; there is no
    # export OF class WidgetBlueprint itself in this asset's export map)
    assert any(ex.class_name == "WidgetBlueprintGeneratedClass" for ex in wbp.exports)
    # every script-import class ref in ALL 1,359 packages resolves
    assert sweep["unresolved_script_classes"] == 0
    # script-import resolution actually resolves (store sanity)
    assert script_objects.resolve(0x71E24A29987BD1ED) == "/Script/Engine.DataTable"


def test_golden_frame_coverage(sweep):
    """The golden's export data slices must cover its FText frames (Task 5
    bridge): every frame lands wholly inside one export slice."""
    golden = next(n for n in sweep["cache"] if n.endswith(GOLDEN_SUFFIX))
    data, pkg = sweep["cache"][golden]
    slices = [(pkg.header_size + ex.serial_offset,
               pkg.header_size + ex.serial_offset + ex.serial_size)
              for ex in pkg.exports]
    frames = _ftext_frames(data)
    assert len(frames) == 473                  # discovery: 473 frames
    covered = [f for f in frames if any(a <= f[0] and f[1] <= b for a, b in slices)]
    assert len(covered) >= 470                 # task floor


def test_stwide_frames_inside_slices(sweep):
    total_in = sum(s["frames_in"] for s in sweep["packages"].values())
    total = sum(s["frames_total"] for s in sweep["packages"].values())
    assert total == ST_TOTAL_FRAMES            # census ground truth
    assert total_in == ST_TOTAL_FRAMES         # every frame inside a slice


def test_export_slices_sized(sweep):
    """Every export with serial_size > 0 has a full-length data slice
    (parse_zen raises on out-of-bounds slices, so surviving packages with
    no bad_slices entries satisfy the invariant; observed: 6,762 exports
    across 1,359 packages, zero bad)."""
    assert sweep["bad_slices"] == []
    assert sweep["total_exports"] > 6000


def test_synthetic_no_export_package():
    """A no-export package parses to an empty export list. No REAL ST
    package has zero exports (observed minimum is 1), so the branch is
    pinned with a minimal synthetic payload in the exact 5.6.1 layout:
    summary + cell offsets + one-name batch + empty import-package-name
    batch + trailing package tag, all map sections collapsed to zero
    size."""
    name_batch = (struct.pack("<IIQ", 1, 5, 0xC1640000)
                  + struct.pack("<Q", 0) + struct.pack(">h", 5) + b"Dummy")
    maps_at = 52 + 8 + len(name_batch)             # after cell offsets
    ipn_at = maps_at                # all map sections zero-size; ipn batch follows
    total = ipn_at + 4 + 4          # empty name batch (u32 0) + package tag
    summary = struct.pack(
        "<IIIIIIiiiiiii",
        0, total, 0, 0, 0, 0,                      # has_vi, header_size, name, flags, cooked
        maps_at, maps_at, maps_at, maps_at, maps_at, maps_at, ipn_at)
    payload_bytes = (summary
                     + struct.pack("<ii", maps_at, maps_at)   # cell offsets
                     + name_batch
                     + struct.pack("<I", 0)
                     + struct.pack("<I", 0x9E2A83C1))         # trailing tag
    assert len(payload_bytes) == total
    pkg = zen.parse_zen(payload_bytes, "synthetic.uasset")
    assert pkg.exports == []
    assert pkg.imports == []
    assert pkg.names == ["Dummy"]
    assert pkg.package_name == "Dummy"


def test_bad_payload_raises_named_error():
    with pytest.raises(zen.ZenError):
        zen.parse_zen(b"\x00" * 64, "garbage.uasset")


def test_synthetic_versioned_package():
    """The versioning-info path (no real ST package is versioned — all
    1,359 parse with has_versioning_info == 0 — so pin the branch with a
    synthetic payload: versioning info sits between summary and cell
    offsets)."""
    vi = struct.pack("<Iiii", 3, 522, 1017, 0) + struct.pack("<I", 0)  # + 0 customs
    name_batch = (struct.pack("<IIQ", 1, 5, 0xC1640000)
                  + struct.pack("<Q", 0) + struct.pack(">h", 5) + b"Dummy")
    maps_at = 52 + len(vi) + 8 + len(name_batch)
    ipn_at = maps_at
    total = ipn_at + 4 + 4                          # empty ipn batch + package tag
    summary = struct.pack(
        "<IIIIIIiiiiiii",
        1, total, 0, 0, 0, 0,
        maps_at, maps_at, maps_at, maps_at, maps_at, maps_at, ipn_at)
    pkg = zen.parse_zen(summary + vi + struct.pack("<ii", maps_at, maps_at)
                        + name_batch + struct.pack("<I", 0)
                        + struct.pack("<I", 0x9E2A83C1), "v.uasset")
    assert not pkg.is_unversioned
    assert pkg.version_info.zen_version == 3
    assert (pkg.version_info.file_version_ue4,
            pkg.version_info.file_version_ue5) == (522, 1017)
    assert pkg.version_info.custom_versions == []
