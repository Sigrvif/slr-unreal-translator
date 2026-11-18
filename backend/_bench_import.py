import os
import os, sys, json, time, shutil, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ut import texture

# Simulate the user flow: pick 30 extracted images (mixed sizes) as the
# "SEP rendered" folder, then import them back.
def bench(tag, game, key, boot_dir, pick=30):
    man = json.load(open(os.path.join(boot_dir, "manifest.json"), encoding="utf-8"))
    # deterministic mix: prefer candidates, cap size, take every Nth
    rows = [m for m in man if m["candidate"]][:pick]
    if len(rows) < pick:
        rows += [m for m in man if m not in rows][:pick - len(rows)]
    rd = os.path.join(os.environ["TEMP"], "slr-render-" + tag)
    shutil.rmtree(rd, ignore_errors=True)
    os.makedirs(rd)
    import filecmp
    for m in rows:
        shutil.copy(os.path.join(boot_dir, m["png"]), os.path.join(rd, m["png"]))
    t0 = time.time()
    res = texture.import_images(game, boot_dir, rd, aes_key=key)
    t1 = time.time()
    biggest = max((m["width"] * m["height"] for m in rows), default=0)
    print(f"{tag}: imported {res['imported']} of {len(rows)} picks in {t1-t0:.1f}s "
          f"(errors {len(res['errors'])}, staged {len(res['staged'])}, "
          f"biggest pick {biggest//1024}Kpx)")
    for e in res["errors"][:3]:
        print("  err:", e)
    # container rebuild on the staged set
    out = os.path.join(os.environ["TEMP"], "slr-ctrb-" + tag)
    shutil.rmtree(out, ignore_errors=True)
    t2 = time.time()
    ctrs = texture.write_containers(game, res["staged"], out, aes_key=key)
    t3 = time.time()
    print(f"{tag}: containers rebuilt in {t3-t2:.1f}s -> {len(ctrs)} files "
          f"({sum(os.path.getsize(c) for c in ctrs)//1024//1024} MB)")
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    return res, ctrs

# key from env or fixtures/local/sample_b.key (never committed - see tests/keys.py)
import os as _os
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games
_KEY_HEX = _os.environ.get("SLRUNREAL_SAMPLE_B_AES_KEY", "").strip() or open("tests/fixtures/local/sample_b.key").read().strip()
KEY = bytes.fromhex(_KEY_HEX)
bench("EF", os.path.join(_GAMES_ROOT, "SampleGameB"), KEY,
      os.path.join(os.environ.get("SLRUNREAL_IMG_OUT", ""), "SampleGameB_unreal_img"))
bench("ST", os.path.join(_GAMES_ROOT, "SampleGameA"), None,
      os.path.join(os.environ.get("SLRUNREAL_IMG_OUT", ""), "SampleGameA_unreal_img"))
