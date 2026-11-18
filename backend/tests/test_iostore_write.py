import os
import tempfile

import pytest

from backend.ut import blake3, iostore, oodle

PAKS = os.path.join("sample_a", "Content", "Paks")


@pytest.mark.slow
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


@pytest.mark.slow
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


def test_patch_container_header_rejected(game_paths, tmp_path):
    # Task-6 review hardening: the container-header chunk (chunk_type 6) is
    # backed by the physical header at ucas offset 0; patching its utoc entry
    # would desync the utoc from that header. Must raise BEFORE any write.
    up = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.utoc")
    cp = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.ucas")
    with open(up, "rb") as f:
        toc = iostore.parse_toc(f.read())
    idx = next(i for i, e in enumerate(toc.entries) if e.chunk_type == 6)
    with pytest.raises(ValueError, match="container-header"):
        iostore.write_iostore(up, cp, os.path.join(tmp_path, "o.utoc"),
                              os.path.join(tmp_path, "o.ucas"),
                              chunk_patches={idx: b"x"})
    assert os.listdir(str(tmp_path)) == []      # nothing written, no temp left


def test_write_pair_both_temps_then_ucas_first(game_paths, tmp_path, monkeypatch):
    # Task-6 review hardening: BOTH temps must be fully written before
    # either output is replaced, and the UCAS is replaced first, the utoc
    # LAST — a crash between the two replaces leaves a stale utoc beside a
    # new ucas (appended blocks the old utoc never references — the benign,
    # pre-existing failure), never a new utoc pointing at a
    # not-yet-replaced ucas. A tiny fake ucas exercises the pair mechanics
    # without the 556 MB copy (an unmodified rebuild copies it verbatim).
    up = os.path.join(game_paths["sample_a"], PAKS, "sample_a-Windows.utoc")
    fake = os.path.join(tmp_path, "fake.ucas")
    with open(fake, "wb") as f:
        f.write(b"\x00" * 65536)

    real_replace = os.replace
    calls, at_first = [], {"files": None}

    def recording_replace(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            at_first["files"] = sorted(os.listdir(os.path.dirname(os.path.abspath(dst))))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    outd = os.path.join(tmp_path, "ok")
    os.makedirs(outd)
    iostore.write_iostore(up, fake, os.path.join(outd, "o.utoc"),
                          os.path.join(outd, "o.ucas"))
    assert [os.path.basename(c) for c in calls] == ["o.ucas", "o.utoc"]
    # at the FIRST replace both temps already exist on disk
    assert any(f.startswith("o.ucas.") and f.endswith(".tmp") for f in at_first["files"])
    assert any(f.startswith("o.utoc.") and f.endswith(".tmp") for f in at_first["files"])
    assert not [f for f in os.listdir(outd) if f.endswith(".tmp")]
    assert os.path.getsize(os.path.join(outd, "o.ucas")) == 65536

    # injected crash between the two replaces: ucas already replaced, utoc
    # absent (stale/none), failed temp cleaned up
    def crashing_replace(src, dst):
        if os.path.basename(dst) == "o.utoc":
            raise OSError("injected crash between pair replaces")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crashing_replace)
    outd2 = os.path.join(tmp_path, "crash")
    os.makedirs(outd2)
    with pytest.raises(OSError, match="injected crash"):
        iostore.write_iostore(up, fake, os.path.join(outd2, "o.utoc"),
                              os.path.join(outd2, "o.ucas"))
    assert os.path.isfile(os.path.join(outd2, "o.ucas"))
    assert not os.path.exists(os.path.join(outd2, "o.utoc"))
    assert not [f for f in os.listdir(outd2) if f.endswith(".tmp")]
