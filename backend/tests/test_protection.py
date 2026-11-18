from backend.ut import protection


def test_fingerprint_sample_a(game_paths):
    fp = protection.fingerprint(game_paths["sample_a"])
    assert fp["engine"] == "ue5"
    assert fp["iostore"] is True
    paks = [c for c in fp["containers"] if c["kind"] == "pak"]
    utocs = [c for c in fp["containers"] if c["kind"] == "utoc"]
    assert all(p["footer"] == "patched-e1" for p in paks)
    assert all(not p["index_encrypted"] for p in paks)
    assert len(utocs) == 2
    assert all(u["header"] == "ok" and not u["encrypted"] for u in utocs)
    assert any(u["entry_count"] == 3247 for u in utocs)


def test_fingerprint_sample_b(game_paths):
    fp = protection.fingerprint(game_paths["sample_b"])
    assert fp["iostore"] is False
    paks = [c for c in fp["containers"] if c["kind"] == "pak"]
    assert len(paks) == 1 and paks[0]["footer"] == "patched-e1"
    assert paks[0]["index_encrypted"] is True
