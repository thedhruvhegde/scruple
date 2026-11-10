"""Seed lists from the command line: `5001-5012`, `1,4,9`, `1-3,10` or `1 2 3`."""

from __future__ import annotations

MAX_SEEDS = 100_000


def parse_seeds(spec: str) -> tuple[int, ...]:
    """Parse a seed spec into sorted, unique, non-negative seeds.

    Ranges are inclusive at both ends. Raises `ValueError` on anything else."""
    out: set[int] = set()
    parts = [p for chunk in spec.replace(",", " ").split() for p in [chunk.strip()] if p]
    if not parts:
        raise ValueError("no seeds given")
    for part in parts:
        if "-" in part[1:]:
            lo_s, hi_s = part.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ValueError(f"bad seed range {part!r}") from exc
            if lo < 0 or hi < lo:
                raise ValueError(f"bad seed range {part!r}")
            if hi - lo + 1 + len(out) > MAX_SEEDS:
                raise ValueError(f"too many seeds (more than {MAX_SEEDS})")
            out.update(range(lo, hi + 1))
        else:
            try:
                seed = int(part)
            except ValueError as exc:
                raise ValueError(f"bad seed {part!r}") from exc
            if seed < 0:
                raise ValueError(f"bad seed {part!r}")
            out.add(seed)
    if len(out) > MAX_SEEDS:
        raise ValueError(f"too many seeds (more than {MAX_SEEDS})")
    return tuple(sorted(out))


def format_seeds(seeds: tuple[int, ...] | list[int]) -> str:
    """The shortest spec that `parse_seeds` reads back to the same seeds."""
    s = sorted(set(seeds))
    parts: list[str] = []
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[j] + 1:
            j += 1
        parts.append(str(s[i]) if i == j else f"{s[i]}-{s[j]}")
        i = j + 1
    return ",".join(parts)


__all__ = ["MAX_SEEDS", "format_seeds", "parse_seeds"]
