"""Manifest_UFSFiles cross-reference. The UFS manifest lists staged files in pack
order; when the scanned pak entry count matches exactly, order-based name
assignment is safe. Anything else is reported, not guessed."""
from __future__ import annotations


def load_ufs(manifest_path: str) -> list[str]:
    paths: list[str] = []
    with open(manifest_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            path = line.split("\t", 1)[0].strip()
            if path:
                paths.append(path)
    return paths


def _strip_ups(name: str, limit: "int | None" = None) -> str:
    """Strip up to ``limit`` leading ``../`` components (all of them when
    ``limit`` is None)."""
    rel = name
    while rel.startswith("../") and (limit is None or limit > 0):
        rel = rel[3:]
        if limit is not None:
            limit -= 1
    return rel


def normalize_pak_name(mount_point: str, name: str) -> str:
    """Strip the mount prefix's relative ``../`` run (the ``../../../`` in
    ``../../../Game/``) from an index name, aligning it with the
    content-root-relative Manifest_UFSFiles paths (``Engine/...``)."""
    ups = 0
    for part in mount_point.split("/"):
        if part != "..":
            break
        ups += 1
    return _strip_ups(name, ups)


def intersect_names(entries, ufs: list[str]) -> float:
    """Fraction of index entries whose normalized name is present in the UFS
    list. Entry names may carry the pak's relative mount prefix; strip it."""
    if not entries:
        return 0.0
    have = set(ufs)
    hit = sum(1 for e in entries if _strip_ups(e.name) in have)
    return hit / len(entries)


def assign_by_order(entries, ufs: list[str]) -> tuple[dict[int, str], float]:
    if len(entries) == len(ufs) and entries:
        return {i: p for i, p in enumerate(ufs)}, 1.0
    return {}, 0.0
