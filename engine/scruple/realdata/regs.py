"""Paragraph ids of the committed eCFR sections, for checking citations.

The text files (`data/real/regs/sec-<section>.txt`, eCFR, public domain) put each
paragraph on its own line, starting with its marker, and chain sub-paragraphs after
a heading, either with an em dash ("(b) Liability of cardholder for unauthorized
use —(1)(i) Definition of unauthorized use.") or after the heading's full stop
("(c) Time for resolution; general procedures. (1) The creditor shall ...").
Markers follow the CFR levels: (a) lower letter, (1) digit, (i) lower roman, (A)
upper letter, then (1) and (i) again (italic in print, plain in this text).

Ambiguous markers are resolved by sequence. A lower-case marker such as (i) is a
roman numeral when it is the next numeral under the current digit paragraph, and a
letter otherwise (1026.13(g)(4)(i) versus 1026.13(i)). A digit under an upper-case
paragraph is the fifth level only when it is the next number there, so
"(C) ... (4) Investigation" in 1005.11(c) returns to 1005.11(c)(4). Cross-references
inside a sentence ("paragraph (a)(1)(i) of this section") are not definitions and
are ignored.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

SECTIONS: tuple[str, ...] = ("1005.6", "1005.11", "1026.12", "1026.13")
_CHAIN = re.compile(r"(?:^|—|\.\s)\s*((?:\([0-9A-Za-z]{1,5}\))+)")
_MARK = re.compile(r"\(([0-9A-Za-z]{1,5})\)")
CITATION = re.compile(r"^12 CFR (\d{4}\.\d{1,3})((?:\([0-9A-Za-z]{1,5}\))*)$")


def _roman_value(s: str) -> int | None:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50}
    if not s or any(ch not in vals for ch in s):
        return None
    total = 0
    for i, ch in enumerate(s):
        v = vals[ch]
        if i + 1 < len(s) and vals[s[i + 1]] > v:
            total -= v
        else:
            total += v
    return total if _to_roman(total) == s else None


def _to_roman(n: int) -> str:
    out = ""
    for v, sym in ((50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out += sym
            n -= v
    return out


def _level(tok: str, stack: list[tuple[int, str]]) -> int:
    levels = [lv for lv, _ in stack]
    if tok.isdigit():
        if 4 in levels:
            cur = next((t for lv, t in stack if lv == 5), None)
            if int(tok) == (1 if cur is None else int(cur) + 1):
                return 5
        return 2
    if tok.isupper():
        return 4
    roman_parent = 2 in levels or 5 in levels
    rv = _roman_value(tok)
    if roman_parent and rv is not None:
        roman_level = 6 if 5 in levels else 3
        cur = next((t for lv, t in stack if lv == roman_level), None)
        expected = 1 if cur is None else (_roman_value(cur) or 0) + 1
        if rv == expected:
            return roman_level
    if len(tok) == 1:
        return 1
    return 6 if 5 in levels else 3


def parse_paragraph_ids(text: str, section: str) -> frozenset[str]:
    """All paragraph ids defined in a section's text, plus the bare section id."""
    ids = {section}
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("("):
            continue
        for chain in _CHAIN.finditer(line):
            for tok in _MARK.findall(chain.group(1)):
                lv = _level(tok, stack)
                stack = [(level, t) for level, t in stack if level < lv]
                stack.append((lv, tok))
                ids.add(section + "".join(f"({t})" for _, t in stack))
    return frozenset(ids)


def regs_dir() -> Path:
    from scruple.realdata.splits import data_dir

    return data_dir() / "regs"


@cache
def paragraph_ids(base: Path | None = None) -> frozenset[str]:
    root = base or regs_dir()
    out: set[str] = set()
    for sec in SECTIONS:
        out |= parse_paragraph_ids((root / f"sec-{sec}.txt").read_text(encoding="utf-8"), sec)
    return frozenset(out)


def parse_citation(text: str) -> tuple[str, str] | None:
    """'12 CFR 1005.11(b)(1)' -> ('1005.11', '1005.11(b)(1)'); None if malformed."""
    m = CITATION.fullmatch(text)
    if m is None:
        return None
    return m.group(1), m.group(1) + m.group(2)


def citation_exists(text: str, base: Path | None = None) -> bool:
    parsed = parse_citation(text)
    return parsed is not None and parsed[1] in paragraph_ids(base)
