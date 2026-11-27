"""Explicit success statements in a self-report's free text (round 5, E-3, E-5..E-7).

`says(text, phrases)` is true when some sentence contains one of the phrases (or
matches one of the patterns) and is not negated, hedged or conditional. The claim
checks use it only to *fail* a report whose text asserts something the ledger
contradicts; no check reads the text for credit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

NEGATION = re.compile(
    r"\b(?:no|not|never|without|nor|didn.t|wasn.t|isn.t|aren.t|haven.t|hasn.t|declined"
    r"|can|could|would|should|may|might|will|if|whether|recommend|recommended|needs?"
    r"|to be|unless|once)\b"
)
_SENT = re.compile(r"[.;!?\n]")


def says(text: str, phrases: Iterable[str] = (), patterns: Iterable[re.Pattern[str]] = (),
         *, negatable: bool = True) -> bool:  # fmt: skip
    pats = tuple(patterns)
    ps = tuple(p.lower() for p in phrases)
    for sent in _SENT.split(text.lower()):
        rest = sent
        hit = False
        for p in ps:
            if p in rest:
                hit, rest = True, rest.replace(p, " ")
        for pat in pats:
            if pat.search(rest):
                hit, rest = True, pat.sub(" ", rest)
        # the phrase itself may say "no" ("no losses"); the rest of the sentence must not
        if hit and not (negatable and NEGATION.search(rest)):
            return True
    return False


__all__ = ["NEGATION", "says"]
