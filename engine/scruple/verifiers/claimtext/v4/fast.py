"""Compiled frame matching for reader A4: the same semantics as `negex._find` and
`negex._marks`, precompiled per slot and memoised per token (the v4 judge reads every
text in several streams for several claims, and runs inside every episode's scorecard).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache

from scruple.verifiers.claimtext.negex import SENTENCE_END, Frame
from scruple.verifiers.claimtext.textnorm import edit1


class _Slot:
    __slots__ = ("cache", "exact", "long", "multi", "prefixes")

    def __init__(self, alts: tuple[str, ...]) -> None:
        self.exact: set[str] = set()
        self.prefixes: list[str] = []
        self.long: list[str] = []  # exact words of >= 6 letters (one typo allowed)
        self.multi: list[tuple[str, ...]] = []
        self.cache: dict[str, int] = {}
        for a in alts:
            words = a.split("_")
            if len(words) > 1:
                self.multi.append(tuple(words))
            elif a.endswith("*"):
                self.prefixes.append(a[:-1])
            else:
                self.exact.add(a)
                if len(a) >= 6:
                    self.long.append(a)
        self.multi.sort(key=len, reverse=True)

    def _single(self, t: str) -> bool:
        hit = self.cache.get(t)
        if hit is None:
            hit = int(
                t in self.exact
                or any(t.startswith(p) for p in self.prefixes)
                or (
                    len(t) >= 5
                    and any(abs(len(w) - len(t)) <= 1 and edit1(w, t) for w in self.long)
                )
            )
            if len(self.cache) > 20_000:
                self.cache.clear()
            self.cache[t] = hit
        return bool(hit)

    def match(self, toks: Sequence[str], i: int) -> int:
        """Tokens matched at position i (0 if none): the longest alternative."""
        best = 0
        for words in self.multi:
            if len(words) <= best or i + len(words) > len(toks):
                continue
            if all(_word(w, toks[i + k]) for k, w in enumerate(words)):
                best = len(words)
        if best == 0 and self._single(toks[i]):
            best = 1
        return best


def _word(w: str, t: str) -> bool:
    if w.endswith("*"):
        return t.startswith(w[:-1])
    return t == w or (len(w) >= 6 and len(t) >= 5 and edit1(w, t))


@cache
def compiled(fr: Frame) -> tuple[_Slot, ...]:
    return tuple(_Slot(s) for s in fr.slots)


def find(fr: Frame, toks: Sequence[str]) -> list[tuple[int, int, set[int]]]:
    """`negex._find`, compiled."""
    slots = compiled(fr)
    out = []
    n_t = len(toks)
    first = slots[0]
    for start in range(n_t):
        n = first.match(toks, start)
        if not n:
            continue
        pos = set(range(start, start + n))
        i, ok = start + n, True
        for slot, gap in zip(slots[1:], fr.gaps, strict=True):
            hit = 0
            for j in range(i, min(n_t, i + gap + 1)):
                if toks[j] in SENTENCE_END:
                    break
                hit = slot.match(toks, j)
                if hit:
                    pos |= set(range(j, j + hit))
                    i = j + hit
                    break
            if not hit:
                ok = False
                break
        if ok:
            out.append((start, i, pos))
    return out


@cache
def _index(group: tuple[str, ...]) -> dict[str, tuple[tuple[str, ...], ...]]:
    by: dict[str, list[tuple[str, ...]]] = {}
    for p in group:
        words = tuple(p.split())
        by.setdefault(words[0], []).append(words)
    return {k: tuple(sorted(v, key=len, reverse=True)) for k, v in by.items()}


def marks(toks: Sequence[str], group: tuple[str, ...]) -> dict[int, int]:
    """`negex._marks`, indexed by first word."""
    idx = _index(group)
    out: dict[int, int] = {}
    for i, t in enumerate(toks):
        for p in idx.get(t, ()):
            if tuple(toks[i : i + len(p)]) == p:
                out[i] = len(p)
                break
    return out


__all__ = ["compiled", "find", "marks"]
