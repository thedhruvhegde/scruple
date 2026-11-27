"""Text normalisation shared by every claim parser (round 6, layer 1).

`normalise(text)`:

1. Unicode NFKC (full-width letters, ligatures, compatibility digits fold to ASCII);
2. drop format and zero-width characters (category Cf: U+200B..U+200F, U+2060, U+FEFF,
   soft hyphen, bidi controls) and combining marks left after NFKD of Latin letters
   (so "vollständig" and "vollstandig" read the same);
3. map confusables (Cyrillic, Greek and other homoglyphs of Latin letters, from the
   Unicode confusables table's Latin targets) to ASCII;
4. lower-case, unify quotes, dashes and hyphens (a hyphen between letters becomes a
   space: "fully-reconciled" -> "fully reconciled"), collapse whitespace;
5. number words to digits in en/es/fr/de/pt ("zero" -> "0", "cien por ciento" ->
   "100 %") and money/percent forms to canonical tokens ("$1,200.50" -> "1200.50",
   "100%" -> "100 %").

Each step is idempotent: normalise(normalise(x)) == normalise(x).
"""

from __future__ import annotations

import re
import unicodedata

# Latin look-alikes (lower and upper case) from the Unicode confusables data, the ones
# an attacker reaches for: Cyrillic, Greek, Armenian, Cherokee, Latin small capitals,
# mathematical and fullwidth forms are already folded by NFKC.
_CONFUSABLES = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i",
    "ј": "j", "ԁ": "d", "ԛ": "q", "ԝ": "w", "һ": "h", "ӏ": "l", "ɡ": "g", "ո": "n",
    "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h", "О": "o", "Р": "p",
    "С": "c", "Т": "t", "У": "y", "Х": "x", "Ѕ": "s", "І": "i", "Ј": "j", "Ү": "y",
    # Greek
    "α": "a", "ο": "o", "ρ": "p", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "τ": "t",
    "υ": "u", "χ": "x", "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i",
    "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Υ": "y", "Χ": "x",
    # Armenian, Cherokee, Latin extensions that survive NFKC
    "օ": "o", "ս": "u", "ց": "g", "Ꭺ": "a", "Ꮯ": "c", "Ꭼ": "e", "Ꮋ": "h", "Ꮃ": "w",
    "ı": "i", "ȷ": "j", "ł": "l", "ø": "o", "đ": "d", "ħ": "h", "ŀ": "l", "ß": "ss",
    "æ": "ae", "œ": "oe", "ð": "d", "þ": "th",
    # digits used as letters inside words are handled by `_leet` (context-dependent)
}  # fmt: skip
_CONF = str.maketrans(_CONFUSABLES)

_QUOTES = str.maketrans(
    {"‘": "'", "’": "'", "‚": "'", "‛": "'", "´": "'", "`": "'", "“": '"', "”": '"',
     "„": '"', "«": '"', "»": '"', "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
     "―": "-", "−": "-", " ": " ", " ": " ", " ": " "}
)  # fmt: skip

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a"})
_MIXED = re.compile(r"\b(?=[a-z0-9@]*[a-z])(?=[a-z0-9@]*[0-9@])[a-z0-9@]{3,}\b")

# number words that become digits; articles ("un", "une", "ein", "um") stay words so
# the grammars still see them as determiners
_TO_DIGIT = {"zero": "0", "nil": "0", "cero": "0", "zéro": "0", "zero_pt": "0", "null": "0",
             "hundred": "100", "cien": "100", "ciento": "100", "cem": "100", "cent": "100",
             "hundert": "100", "ten": "10", "two": "2", "three": "3", "dos": "2",
             "deux": "2", "zwei": "2", "tres": "3", "trois": "3", "drei": "3"}  # fmt: skip
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_PERCENT = re.compile(r"(\d)\s*(?:%|per ?cent|percent|por ?ciento|pour ?cent|prozent|por ?cento)")
_MONEY = re.compile(r"(?:[$€£]|usd|eur|gbp)\s*(\d[\d,.]*)|(\d[\d,.]*)\s*(?:[$€£]|usd|eur|dollars?)")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_HYPHEN_IN_WORD = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")
_SPACE = re.compile(r"\s+")


def _strip_format(s: str) -> str:
    return "".join(ch for ch in s if unicodedata.category(ch) != "Cf")


def _strip_marks(s: str) -> str:
    """Drop combining marks (accents) after NFKD; keep the base letters."""
    d = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in d if not unicodedata.combining(ch))


def _numbers(s: str) -> str:
    def word(m: re.Match[str]) -> str:
        w = m.group(0)
        return _TO_DIGIT.get(w, w)

    s = _WORD.sub(word, s)
    s = _THOUSANDS.sub("", s)
    s = _MONEY.sub(lambda m: " " + (m.group(1) or m.group(2)) + " money ", s)
    s = _PERCENT.sub(r"\1 %", s)
    return s


def normalise(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    s = _strip_format(s)
    s = s.translate(_QUOTES).translate(_CONF)
    s = _strip_marks(s).translate(_CONF).lower()
    s = _MIXED.sub(lambda m: m.group(0).translate(_LEET), s)  # "f1led" -> "filed"
    s = _HYPHEN_IN_WORD.sub(" ", s)
    s = _numbers(s)
    return _SPACE.sub(" ", s).strip()


_TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?|%|[.;:!?,()\"]")


def tokens(norm: str) -> list[str]:
    """Tokens of normalised text: words (with an inner apostrophe), numbers and
    punctuation (kept: the parsers use it as a clause boundary)."""
    return _TOKEN.findall(norm)


def edit1(a: str, b: str) -> bool:
    """Damerau-Levenshtein distance <= 1 (typos: one insert, delete, substitute or
    adjacent swap)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        i = diff[0] if diff else 0
        return len(diff) == 2 and diff[1] == i + 1 and a[i] == b[i + 1] and a[i + 1] == b[i]
    if la > lb:
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1 :]


__all__ = ["edit1", "normalise", "tokens"]
