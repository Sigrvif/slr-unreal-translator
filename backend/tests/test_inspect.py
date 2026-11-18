import json
import os
import subprocess
import sys

DEV = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_inspect_runs_on_both_games(game_paths):
    for game in ("sample_a", "sample_b"):
        out = os.path.join(DEV, "backend", "tests", "fixtures", f"inspect-{game}.json")
        r = subprocess.run([sys.executable, "-m", "backend.ut.inspect",
                            game_paths[game], "--json", out],
                           cwd=DEV, capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stderr[-2000:]
        with open(out, encoding="utf-8") as f:
            report = json.load(f)
        assert report["oodle"] is True
        assert report["fingerprint"]["iostore"] == (game == "sample_a")
        if game == "sample_a":
            utoc = next(u for u in report["utocs"] if u["entries"] == 3247)
            assert utoc["named_files"] == 1708
            assert utoc["extracted_sample"]["fail"] == 0
            assert report["pak"]["entries"] == 1669
            assert report["pak"]["name_coverage"] > 0.9
        else:
            assert report["pak"]["index_encrypted"] is True   # awaits Task 8 key
