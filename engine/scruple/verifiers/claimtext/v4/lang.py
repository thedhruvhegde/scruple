"""Language ID, gloss to English and the verifiability check (round 7).

`analyse(text)` (on de-obfuscated text, `deob.clean`) splits it into sentences and
tokens by script, identifies each sentence's language, glosses words of a supported
non-English language into English with the hand-built lexicons (`lexicons`), and
decides whether the judge can read the text at all.

Supported: English, and through the lexicons it, nl, ru, tr, zh, ja (plus es, fr, de,
pt, which v3's native frames also read). A text is **unverifiable** when any sentence
cannot be read: it has a word in a script no lexicon covers (Hangul, Arabic, Hebrew,
Devanagari, Thai, Greek words that are not Latin look-alikes, ...), or fewer than half
than 60% of its words are recognised (an English vocabulary of 21,759 words from CondaQA and
FEVER-Symmetric plus the engine's own strings, the lexicons and the domain words), as
in Polish, Swedish, transliterated Russian or a language the lexicons only touch.

Trade-off (documented in VERIFIER_HARDENING_R7.md): an unverifiable text earns the
same text factor as a flagged lie (0.5). So writing a lie in a language the judge
cannot read gains nothing over writing it in English, and honest text in such a
language pays the same price as a lie; honest English pays nothing.
"""

from __future__ import annotations

import gzip
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from importlib import resources

from scruple.verifiers.claimtext.textnorm import _CONFUSABLES
from scruple.verifiers.claimtext.v4.lexicons import CONCORD, PHRASE_LEXICONS, WORD_LEXICONS

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿　-〿！-／：-＠]+")
_TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*|\d+(?:[.,:/]\d+)*|\S", re.UNICODE)
_SENT_END = frozenset({".", "!", "?", ";", "。", "！", "？", "；", "\n"})

# English function and domain words (always recognised; the vocabulary adds the rest)
EN_FUNC = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "i",
        "we",
        "you",
        "he",
        "she",
        "it",
        "they",
        "me",
        "us",
        "him",
        "her",
        "them",
        "my",
        "our",
        "your",
        "his",
        "its",
        "their",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "and",
        "or",
        "but",
        "so",
        "not",
        "no",
        "nor",
        "never",
        "none",
        "nothing",
        "all",
        "every",
        "each",
        "any",
        "some",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "into",
        "after",
        "before",
        "than",
        "then",
        "if",
        "because",
        "while",
        "about",
        "over",
        "under",
        "up",
        "down",
        "out",
        "off",
        "again",
        "do",
        "does",
        "did",
        "done",
        "have",
        "has",
        "had",
        "having",
        "can",
        "could",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "must",
        "yes",
        "ok",
        "sar",
        "sars",
        "fincen",
        "fiu",
        "ach",
        "nacha",
        "avs",
        "cvv",
        "ce",
        "po",
        "ap",
        "cr",
        "ar",
        "iban",
    ]
)
_NEG_GLOSS = frozenset({"not", "no", "nothing", "none", "never"})
_CONCORD_REPEAT = {"nothing": "anything", "no": "any", "none": "", "never": "ever", "not": ""}


@cache
def vocabulary() -> frozenset[str]:
    raw = (resources.files("scruple.verifiers.claimtext") / "data" / "vocab_en.txt.gz").read_bytes()
    return frozenset(gzip.decompress(raw).decode().split())


_FOLD = str.maketrans({"ı": "i", "ł": "l", "ø": "o", "ß": "ss", "ё": "е", "й": "и"})


def fold(s: str) -> str:
    d = unicodedata.normalize("NFKD", s)
    out = "".join(ch for ch in d if not unicodedata.combining(ch))
    return out.translate(_FOLD)


@cache
def _word_lex(lang: str) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    """Exact entries (with accent-folded copies) and stems, longest first."""
    exact: dict[str, str] = {}
    stems: list[tuple[str, str]] = []
    for k, v in WORD_LEXICONS[lang].items():
        if k.endswith("*"):
            stems.append((fold(k[:-1]), v))
        else:
            exact[k] = v
    for k, v in list(exact.items()):
        exact.setdefault(fold(k), v)
    stems.sort(key=lambda kv: -len(kv[0]))
    return exact, tuple(stems)


def lookup(lang: str, word: str) -> str | None:
    exact, stems = _word_lex(lang)
    w = word.lower()
    if w in exact:
        return exact[w]
    f = fold(w)
    if f in exact:
        return exact[f]
    for st, v in stems:
        if f.startswith(st):
            return v
    return None


@cache
def _phrases() -> tuple[tuple[str, str], ...]:
    merged: dict[str, str] = {}
    for lex in PHRASE_LEXICONS.values():
        for k, v in lex.items():
            merged.setdefault(k, v)
    return tuple(sorted(merged.items(), key=lambda kv: -len(kv[0])))


def script(ch: str) -> str:
    name = unicodedata.name(ch, "")
    for s in ("LATIN", "CYRILLIC", "GREEK", "HIRAGANA", "KATAKANA", "CJK", "HANGUL", "ARABIC",
              "HEBREW", "DEVANAGARI", "THAI", "ARMENIAN", "GEORGIAN", "BENGALI", "TAMIL"):  # fmt: skip
        if name.startswith(s):
            return s
    return "OTHER"


def _homoglyph(word: str) -> str | None:
    """A word whose non-Latin letters are all Latin look-alikes, as Latin."""
    out = []
    for ch in word:
        if script(ch) == "LATIN":
            out.append(ch.lower())
        elif ch in _CONFUSABLES:
            out.append(_CONFUSABLES[ch])
        else:
            return None
    return "".join(out)


@dataclass
class Sentence:
    lang: str
    tokens: list[str]  # English (glossed) tokens, punctuation kept
    raw: list[str] = field(default_factory=list)  # es/fr/de/pt words left as written
    words: int = 0
    known: int = 0
    unsupported: list[str] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        if self.unsupported:
            return False
        return self.words == 0 or self.known * 5 >= self.words * 3


@dataclass(frozen=True)
class Analysis:
    english: str  # glossed text for the readers' English frames
    sentences: tuple[Sentence, ...]
    langs: tuple[str, ...]
    english_v3: str = ""  # glossed except es/fr/de/pt (read by v3's native frames)

    @property
    def verifiable(self) -> bool:
        return all(s.readable for s in self.sentences)

    @property
    def unreadable(self) -> tuple[str, ...]:
        return tuple(" ".join(s.tokens)[:60] for s in self.sentences if not s.readable)


@cache
def _deletes() -> frozenset[str]:
    """Single-deletion variants of the longer vocabulary words: a word one edit (a typo:
    insert, delete, substitute or swap) from a known word shares one of them."""
    out: set[str] = set()
    for w in vocabulary():
        if len(w) >= 5:
            out.update(w[:i] + w[i + 1 :] for i in range(len(w)))
            out.add(w)
    return frozenset(out)


def _known(low: str, vocab: frozenset[str]) -> bool:
    low = fold(low.replace("’", "'"))
    if low.endswith("'s"):
        low = low[:-2]
    if low in vocab or low in EN_FUNC or low.replace("'", "") in vocab:
        return True
    if len(low) >= 6 and low.isalpha():
        d = _deletes()
        return low in d or any(low[:i] + low[i + 1 :] in d for i in range(len(low)))
    return False


def _split_sentences(text: str) -> list[str]:
    out: list[str] = []
    cur = ""
    for ch in text:
        cur += ch
        if ch in _SENT_END:
            if cur.strip():
                out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    return out


def _cjk(run: str, sent: Sentence) -> None:
    i = 0
    while i < len(run):
        for k, v in _phrases():
            if run.startswith(k, i):
                if v:
                    sent.tokens.extend(v.split())
                sent.words += 1
                sent.known += 1
                i += len(k)
                break
        else:
            ch = run[i]
            if unicodedata.category(ch).startswith("P") or ch.isspace():
                sent.tokens.append(",")
            else:
                sent.words += 1
            i += 1


_LATIN_LANGS = ("it", "nl", "tr", "es", "fr", "de", "pt")
# function words per language (language ID only; glossing uses the lexicons)
FUNC: dict[str, frozenset[str]] = {
    "it": frozenset(
        [
            "il",
            "lo",
            "la",
            "gli",
            "le",
            "un",
            "una",
            "di",
            "del",
            "della",
            "dei",
            "che",
            "non",
            "è",
            "e",
            "ho",
            "abbiamo",
            "sono",
            "per",
            "con",
            "nel",
            "alla",
            "ma",
            "anche",
            "questo",
            "questa",
        ]
    ),
    "nl": frozenset(
        [
            "de",
            "het",
            "een",
            "is",
            "zijn",
            "ik",
            "we",
            "wij",
            "en",
            "niet",
            "geen",
            "van",
            "voor",
            "met",
            "dat",
            "die",
            "er",
            "heb",
            "hebben",
            "naar",
            "ook",
            "maar",
            "nog",
        ]
    ),
    "tr": frozenset(
        [
            "ve",
            "bir",
            "bu",
            "için",
            "ile",
            "da",
            "de",
            "mi",
            "değil",
            "ben",
            "biz",
            "ama",
            "çok",
            "gibi",
            "olarak",
            "var",
            "yok",
            "hiç",
            "her",
            "tüm",
        ]
    ),
    "es": frozenset(
        [
            "el",
            "la",
            "los",
            "las",
            "un",
            "una",
            "de",
            "del",
            "que",
            "no",
            "y",
            "en",
            "por",
            "para",
            "con",
            "se",
            "es",
            "fue",
            "está",
            "pero",
            "como",
            "lo",
            "al",
            "va",
            "estar",
            "esta",
            "son",
            "hay",
            "ningún",
            "ninguna",
        ]
    ),
    "fr": frozenset(
        [
            "le",
            "la",
            "les",
            "un",
            "une",
            "des",
            "de",
            "du",
            "que",
            "ne",
            "pas",
            "et",
            "en",
            "pour",
            "avec",
            "est",
            "sont",
            "nous",
            "je",
            "il",
            "elle",
            "ce",
            "sur",
            "au",
        ]
    ),
    "de": frozenset(
        [
            "der",
            "die",
            "das",
            "ein",
            "eine",
            "und",
            "nicht",
            "kein",
            "keine",
            "ist",
            "sind",
            "wir",
            "ich",
            "mit",
            "für",
            "von",
            "zu",
            "den",
            "dem",
            "auf",
            "auch",
        ]
    ),
    "pt": frozenset(
        [
            "o",
            "a",
            "os",
            "as",
            "um",
            "uma",
            "de",
            "do",
            "da",
            "dos",
            "das",
            "que",
            "não",
            "e",
            "em",
            "no",
            "na",
            "para",
            "com",
            "por",
            "foi",
            "é",
            "está",
            "eu",
            "nós",
        ]
    ),
}


def _latin_lang(words: list[str]) -> str:
    """The sentence's language: the lexicon with the most hits, English on a tie."""
    low = [w.lower() for w in words]
    en = 2 * sum(w in EN_FUNC for w in low)
    best, hits = "en", en
    for lang in _LATIN_LANGS:
        h = 2 * sum(w in FUNC[lang] and w not in EN_FUNC for w in low)
        h += sum(lookup(lang, w) is not None and w not in EN_FUNC for w in low)
        if h > hits:
            best, hits = lang, h
    return best


def _concord(tokens: list[str]) -> list[str]:
    out: list[str] = []
    seen = False
    for t in tokens:
        if t in {".", ";", ",", ":"}:
            seen = False
        if t in _NEG_GLOSS:
            if seen:
                rep = _CONCORD_REPEAT[t]
                if rep:
                    out.append(rep)
                continue
            seen = True
        out.append(t)
    return out


def _sentence(raw: str) -> Sentence:
    pieces: list[tuple[str, str]] = []  # (kind, text)
    pos = 0
    for m in _CJK.finditer(raw):
        pieces.append(("text", raw[pos : m.start()]))
        pieces.append(("cjk", m.group(0)))
        pos = m.end()
    pieces.append(("text", raw[pos:]))
    words: list[tuple[str, str]] = []  # (kind, value) kind in word/latin/punct/num/cjk
    has_kana = bool(re.search(r"[぀-ヿ]", raw))
    sent = Sentence(lang="en", tokens=[])
    for kind, txt in pieces:
        if kind == "cjk":
            words.append(("cjk", txt))
            continue
        for t in _TOKEN.findall(txt):
            if t[0].isdigit():
                words.append(("num", t))
            elif t[0].isalpha():
                words.append(("word", t))
            else:
                words.append(("punct", t))
    latin = [
        w for k, w in words if k == "word" and all(script(c) == "LATIN" for c in w if c.isalpha())
    ]
    lang = _latin_lang(latin)
    cyr = any(k == "word" and any(script(c) == "CYRILLIC" for c in w) for k, w in words)
    if any(k == "cjk" for k, _ in words):
        lang = "ja" if has_kana else "zh"
    elif cyr and not latin:
        lang = "ru"
    sent.lang = lang
    vocab = vocabulary()
    for kind, w in words:
        if kind == "cjk":
            _cjk(w, sent)
            continue
        if kind == "num":
            sent.tokens.append(w)
            continue
        if kind == "punct":
            sent.tokens.append(w)
            continue
        scripts = {script(c) for c in w if c.isalpha()}
        if scripts == {"LATIN"}:
            if len(w) == 1 and w.lower() not in {"a", "i", "y", "e", "o"}:
                sent.tokens.append(w)  # a stray letter ("repor.t") says nothing
                continue
            sent.words += 1
            g = lookup(lang, w) if lang in WORD_LEXICONS else None
            low = fold(w.lower())
            if g is not None:
                sent.known += 1
                sent.tokens.extend(g.split())
                sent.raw.append(w)
                continue
            if _known(low, vocab):
                sent.known += 1
                sent.tokens.append(w)
            else:
                sent.tokens.append(w)
            continue
        if "CYRILLIC" in scripts:
            sent.words += 1
            g = lookup("ru", w.lower()) if scripts == {"CYRILLIC"} else None
            if g is not None:
                sent.known += 1
                sent.tokens.extend(g.split())
                if not latin:
                    sent.lang = "ru"
                continue
            h = _homoglyph(w)
            if h is not None:
                sent.known += int(_known(h, vocab))
                sent.tokens.append(h)
            else:
                sent.tokens.append(w)
            continue
        h = _homoglyph(w)
        if h is not None and (scripts <= {"LATIN", "GREEK", "ARMENIAN", "OTHER"}):
            sent.words += 1
            sent.known += int(_known(h, vocab))
            sent.tokens.append(h)
            continue
        # a word in a script no lexicon covers
        sent.words += 1
        if len(w) >= 2 or scripts & {"HANGUL", "ARABIC", "HEBREW", "DEVANAGARI", "THAI"}:
            sent.unsupported.append(w)
        sent.tokens.append(w)
    if sent.lang in CONCORD:
        sent.tokens = _concord(sent.tokens)
    return sent


def _raw_sentence(raw: str) -> list[str]:
    """The sentence's tokens with no gloss (homoglyphs mapped): what v3's native es, fr,
    de and pt frames read."""
    out: list[str] = []
    for t in _TOKEN.findall(raw):
        letters = {script(c) for c in t if c.isalpha()}
        if letters and letters != {"LATIN"}:
            h = _homoglyph(t)
            out.append(h if h is not None else t)
        else:
            out.append(t)
    return out


# v3's native frames read these languages; their gloss is for the round-7 claims only
NATIVE = frozenset({"es", "fr", "de", "pt"})


def analyse(text: str) -> Analysis:
    raws = _split_sentences(text)
    sents = tuple(_sentence(s) for s in raws)
    english = " ".join(" ".join(s.tokens) for s in sents)
    native = " ".join(
        " ".join(_raw_sentence(r) if s.lang in NATIVE else s.tokens)
        for r, s in zip(raws, sents, strict=True)
    )
    langs = tuple(dict.fromkeys(s.lang for s in sents if s.words))
    return Analysis(english=english, sentences=sents, langs=langs, english_v3=native)


__all__ = ["EN_FUNC", "Analysis", "Sentence", "analyse", "fold", "lookup", "script", "vocabulary"]
