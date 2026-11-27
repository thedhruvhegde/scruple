"""Reader C: a lexicon-free statistical classifier (char n-gram logistic regression).

One multinomial logistic regression per claim over hashed features of the normalised
sentence: character 2- to 5-grams (inside word boundaries) and word 1- and 2-grams,
hashed into `DIM` buckets (blake2b, stable across processes and platforms). Classes:
assert / deny / none. Trained by `scruple.verifiers.claimtext.train` on the corpus's
train split (plus public negation corpora as "none" distractors, see
docs/results/VERIFIER_HARDENING_R6.md); the weights ship in `data/ngram.npz`.

It shares no lexicon and no rule with readers A and B, so it fails differently: it
generalises over spelling (typos, inflections it has never seen) where the rule
readers need an exact stem, and it misses constructions far from its training data.

A text is read sentence by sentence; the claim's assert (deny) confidence is the
highest class probability over its sentences.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from functools import cache
from importlib import resources

import numpy as np

from scruple.verifiers.claimtext.model import Claim, Reading
from scruple.verifiers.claimtext.textnorm import normalise

DIM = 1 << 15
CLASSES = ("assert", "deny", "none")
_SENT = re.compile(r"[.;!?\n]+")
_WORD = re.compile(r"[a-z0-9%']+")


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=4).digest(), "little") % DIM


# Scope marking (Das & Chen 2001; Pang et al. 2002): words after a negation or a
# modality cue, up to the next clause boundary, also get a "neg:" / "mod:" feature.
# The cue lists are small and generic (no claim words); they are the classifier's only
# hand-written input.
NEG_CUES = frozenset(
    [
        "no",
        "not",
        "never",
        "nothing",
        "none",
        "without",
        "nor",
        "cannot",
        "sin",
        "nunca",
        "ningun",
        "ninguna",
        "ninguno",
        "nada",
        "ne",
        "pas",
        "aucun",
        "aucune",
        "rien",
        "sans",
        "nicht",
        "kein",
        "keine",
        "keinen",
        "nie",
        "nichts",
        "ohne",
        "nao",
        "nenhum",
        "nenhuma",
        "sem",
        "0",
        "zero",
    ]
)
MOD_CUES = frozenset(
    [
        "if",
        "would",
        "could",
        "should",
        "may",
        "might",
        "will",
        "can",
        "must",
        "plan",
        "plans",
        "expect",
        "hope",
        "recommend",
        "risk",
        "risks",
        "avoid",
        "si",
        "podria",
        "deberia",
        "va",
        "vamos",
        "pourrait",
        "devrait",
        "sera",
        "vais",
        "wenn",
        "falls",
        "konnte",
        "sollte",
        "wird",
        "werden",
        "se",
        "poderia",
        "deveria",
        "vai",
        "vou",
    ]
)
BOUNDARY = frozenset(
    [",", ":", "(", ")", "and", "but", "y", "et", "und", "e", "pero", "mais", "aber", "mas"]
)
_TOK = re.compile(r"[a-z0-9%']+|[,:()]")


def _scoped(norm_sentence: str) -> list[str]:
    out, mode = [], ""
    for t in _TOK.findall(norm_sentence):
        if t in BOUNDARY:
            mode = ""
            continue
        if t in NEG_CUES or t.endswith("n't") or t.startswith("n'"):
            mode = "neg:"
            continue
        if t in MOD_CUES:
            mode = "mod:"
            continue
        if mode:
            out.append(mode + t)
    return out


def features(norm_sentence: str) -> list[int]:
    words = _WORD.findall(norm_sentence)
    feats: list[int] = [_h(f"s:{w}") for w in _scoped(norm_sentence)]
    for w in words:
        padded = f" {w} "
        for n in (2, 3, 4, 5):
            feats.extend(_h(f"c{n}:{padded[i : i + n]}") for i in range(len(padded) - n + 1))
        feats.append(_h(f"w:{w}"))
    feats.extend(_h(f"b:{a} {b}") for a, b in itertools.pairwise(words))
    return feats


def segments(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(normalise(text)) if s.strip()]


@cache
def _weights() -> dict[str, tuple[np.ndarray, np.ndarray]] | None:
    try:
        ref = resources.files("scruple.verifiers.claimtext").joinpath("data/ngram.npz")
        with resources.as_file(ref) as path, np.load(path) as z:
            return {
                c.value: (
                    z[f"{c.value}_W"].astype(np.float32),
                    z[f"{c.value}_b"].astype(np.float32),
                )
                for c in Claim
                if f"{c.value}_W" in z
            }
    except (FileNotFoundError, OSError, KeyError):
        return None


def available() -> bool:
    return _weights() is not None


def proba(sentence_norm: str, claim: Claim) -> np.ndarray:
    w = _weights()
    if w is None or claim.value not in w:
        return np.array([0.0, 0.0, 1.0])
    weights, b = w[claim.value]
    idx = features(sentence_norm)
    z = b + (weights[:, idx].sum(axis=1) if idx else 0.0)
    z = z - z.max()
    p: np.ndarray = np.exp(z) / np.exp(z).sum()
    return p


def read(text: str, claim: Claim) -> Reading:
    best_a = best_d = 0.0
    ev: list[str] = []
    for seg in segments(text):
        p = proba(seg, claim)
        best_a, best_d = max(best_a, float(p[0])), max(best_d, float(p[1]))
        if max(p[0], p[1]) > 0.5:
            ev.append(f"{CLASSES[int(np.argmax(p))]}:{p.max():.2f}:{seg[:60]}")
    return Reading(round(best_a, 4), round(best_d, 4), tuple(ev[:8]))


__all__ = ["CLASSES", "DIM", "available", "features", "proba", "read", "segments"]
