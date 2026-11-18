import os
import os, sys, json, time, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ut import payload, texture

# key from env or fixtures/local/sample_b.key (never committed - see tests/keys.py)
import os as _os
_GAMES_ROOT = os.environ.get("SLRUNREAL_GAMES_ROOT", "")  # dir holding the local test games
_KEY_HEX = _os.environ.get("SLRUNREAL_SAMPLE_B_AES_KEY", "").strip() or open("tests/fixtures/local/sample_b.key").read().strip()
KEY = bytes.fromhex(_KEY_HEX)
GAME = os.path.join(_GAMES_ROOT, "SampleGameB")
OUT = os.path.join(os.environ.get("SLRUNREAL_IMG_OUT", ""), "SampleGameB_unreal_img")
t0 = time.time()
res = texture.extract_images(GAME, OUT, aes_key=KEY,
                             progress=lambda d, t, f: (
                                 print(f"{d}/{t} {f}", flush=True)
                                 if (d % 400 == 0 or d == t - 1) else None))
print(f"extracted {res['count']} images in {time.time()-t0:.0f}s", flush=True)
print("errors:", len(res["errors"]), flush=True)
man = json.load(open(os.path.join(OUT, "manifest.json"), encoding="utf-8"))
fmts = collections.Counter(m["format"] for m in man)
print("formats:", dict(fmts.most_common(8)), flush=True)
print("candidates:", sum(1 for m in man if m["candidate"]), flush=True)
