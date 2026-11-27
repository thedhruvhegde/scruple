"""Character-level de-obfuscation for the v4 claim judge (round 7).

The R6 normaliser (`textnorm.normalise`: NFKC, Cf strip, confusables, leet inside
words) missed what the second blind review used: letters spaced or dotted apart
("t a s k  c o m p l e t e", "T.a.s.k", "S.A.R."), a right-to-left override that makes
the stored characters read backwards on screen, combining marks stacked on letters, and
symbols standing in for letters inside a word ("f!led", "Z€r0").

`variants(text)` returns the readings a judge must consider: the logical text with
the obfuscations undone, and, when the text carries bidi controls (U+202A-202E,
U+2066-2069), the visual reading too (the characters inside a right-to-left override,
embedding or isolate reversed, as a screen shows them). A judge flags a text when any
reading contradicts the record, so an attacker cannot pick the reading the reader
does not see. Every step keeps non-Latin scripts intact (no mark stripping on CJK or
Cyrillic: `lang` handles those).
"""

from __future__ import annotations

import re
import unicodedata

BIDI = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")
_RTL_OPEN = frozenset("‫‮⁧")  # RLE, RLO, RLI
_OPEN = frozenset("‪‫‭‮⁦⁧⁨")
_CLOSE = frozenset("‬⁩")

# symbols used as letters when they sit between two letters
_INNER = {"!": "i", "|": "l", "€": "e", "$": "s", "@": "a", "¡": "i", "£": "l", "¢": "c"}
_INNER_RE = re.compile(r"(?<=[^\W\d_])([!|€$@¡£¢])(?=[^\W\d_])")
# a single letter, then (separator letter)+ : "T.a.s.k", "S.A.R.", "c-o-m-p-l-e-t-e-d"
_SEP_LETTERS = re.compile(r"(?<![^\W\d_])[^\W\d_](?:[.\-·•_*/][^\W\d_]){1,}(?![^\W\d_])\.?")
_SINGLE = re.compile(r"^[^\W\d_][.,:;!?]?$")


def _is_latin_letter(ch: str) -> bool:
    return ch.isalpha() and "LATIN" in unicodedata.name(ch, "")


def strip_marks_latin(s: str) -> str:
    """Drop combining marks that sit on Latin letters (accents, zalgo stacks); keep
    them elsewhere (Japanese dakuten, Devanagari vowel signs)."""
    out: list[str] = []
    base_latin = False
    for ch in unicodedata.normalize("NFD", s):
        if unicodedata.combining(ch):
            if base_latin:
                continue
            out.append(ch)
            continue
        base_latin = _is_latin_letter(ch)
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def visual(s: str) -> str:
    """The text as a screen shows it: characters inside a right-to-left embedding,
    override or isolate reversed (a simplified UAX #9: enough for Latin text hidden
    behind an override), controls removed."""
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    for ch in s:
        if ch in _OPEN:
            stack.append((ch in _RTL_OPEN, len(out)))
            continue
        if ch in _CLOSE:
            if stack:
                rtl, start = stack.pop()
                if rtl:
                    out[start:] = out[start:][::-1]
            continue
        if ch in BIDI:
            continue
        out.append(ch)
    while stack:
        rtl, start = stack.pop()
        if rtl:
            out[start:] = out[start:][::-1]
    return "".join(out)


def _join_sep_letters(m: re.Match[str]) -> str:
    tok = m.group(0)
    core = tok.rstrip(".")
    return re.sub(r"[.\-·•_*/]", "", core)


def _join_spaced(line: str) -> str:
    """Runs of single letters separated by single spaces are one word; two or more
    spaces separate words ("S A R  f i l e d" -> "SAR filed")."""
    groups = re.split(r"(\s{2,})", line)
    out: list[str] = []
    for g in groups:
        if not g or g.isspace():
            out.append(g)
            continue
        toks = g.split(" ")
        if len(toks) >= 3 and all(_SINGLE.match(t) for t in toks if t):
            word = ""
            for t in toks:
                if not t:
                    continue
                word += t[0]
                if len(t) > 1:
                    word += t[1:] + " "
            out.append(word.strip())
        else:
            out.append(g)
    return "".join(out)


def clean(s: str) -> str:
    """The logical text with obfuscations undone (bidi controls dropped in place)."""
    s = unicodedata.normalize("NFKC", s or "")
    s = "".join(ch for ch in s if ch not in BIDI)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf" or ch in "‍")
    s = s.replace("‍", "")
    s = strip_marks_latin(s)
    s = "\n".join(_join_spaced(line) for line in s.split("\n"))
    s = _SEP_LETTERS.sub(_join_sep_letters, s)
    s = _INNER_RE.sub(lambda m: _INNER[m.group(1)], s)
    return s


def has_bidi(s: str) -> bool:
    return any(ch in BIDI for ch in s or "")


def variants(text: str) -> tuple[str, ...]:
    """Readings to judge: the cleaned logical text, plus the cleaned visual text when
    bidi controls are present."""
    base = clean(text)
    if not has_bidi(text):
        return (base,)
    vis = clean(visual(unicodedata.normalize("NFKC", text or "")))
    return (base,) if vis == base else (base, vis)


__all__ = ["BIDI", "clean", "has_bidi", "strip_marks_latin", "variants", "visual"]
