"""AES-key sourcing for the (optional) local test games — never committed.

The sample_b pak index is encrypted with the game's own AES key, which is
not the project's to publish. Tests that need it resolve the key in this
order:

  1. SLRUNREAL_SAMPLE_B_AES_KEY  env var (hex)
  2. fixtures/local/sample_b.key untracked file (hex, one line)

Returns None when neither is set; every consumer then skips.
"""
import os


def sample_b_key():
    hexkey = os.environ.get("SLRUNREAL_SAMPLE_B_AES_KEY", "").strip()
    if not hexkey:
        kf = os.path.join(os.path.dirname(__file__), "fixtures", "local",
                          "sample_b.key")
        if os.path.isfile(kf):
            with open(kf, encoding="utf-8") as f:
                hexkey = f.read().strip()
    return bytes.fromhex(hexkey) if hexkey else None
