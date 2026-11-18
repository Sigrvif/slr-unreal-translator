# SLRUnreal Phase 1 E2E checklist

Automated (pytest, real games): utoc header/TOC parse + byte round-trip on both
utocs; ContainerHeader extraction (LE IoCn magic); extraction walk over stored
+ Oodle blocks; pak footer + plaintext index read (1669 entries, sha1-verified
first entry); encrypted-index detection (SampleGameB); manifest name coverage;
damage fingerprint (patched-e1 magic on both paks).

Manual (user): none yet — Phase 1 is read-only; no game files are modified.

Spike answers recorded: Q1 blocks_by_method=sample_a-Windows.utoc
{"0": 3543 stored, "1": 15879 Oodle} of 19422 blocks (81.8% Oodle; global.utoc
adds 41 stored blocks; pak side: 382/1669 entries Oodle-compressed) — reading
is heavily Oodle-dependent and a local dev DLL (runtime/oodle/, fetched by
build/fetch_oodle.py) covers all of it (extraction sample 25/25 ok, 0 fail).
(The former Q2/Q3 reverse-engineering notes were removed from the repo;
release builds do not bundle Oodle — it is discovered at scan time.)
