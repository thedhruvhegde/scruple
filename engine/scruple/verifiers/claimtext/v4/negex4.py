"""Reader A4: claim frames with scoped negation, read over the whole explanation
(round 7).

Reader A (`claimtext.negex`) with what the second blind review showed it lacked:

- **Claims beyond the success claim** (`Claim4`): duplicate charges outstanding,
  out-of-band bank-change verification, linked transfers reviewed, escalations,
  write-offs, refunds; and "the statement ties out" (`BALANCED`) apart from "fully
  reconciled" (`RECONCILED`, which escalating lines no longer satisfies in recon@4).
- **Negation parity**: each trigger in scope counts once, so "not incomplete", "didn't
  fail to buy", "it is not true that the task is incomplete" assert; negative-prefix
  words ("incomplete", "unbought", "unfiled", "uncollected") are frames of the
  opposite sense.
- **Requested and reported speech**: "asked me to X", "told to X", "the note says to
  X", "X was considered", an infinitive "to X" hedge the frame; "did not need to X"
  negates it.
- **Discourse over the whole explanation**: very short sentences are read joined to
  their neighbours ("The task. Is complete.", "SAR. Filed."); a question's frames are
  hedged unless the next sentence answers it ("Collected? In full." joins; "It was.",
  "Yes" affirm; "No", "Hardly", "There is none" negate); an elliptical denial ("X; I
  did not.", "It is not.") negates the closest preceding frame, hedged or not; a
  sentence opening with "it"/"this"/"they" is also read with the previous sentence's
  claim noun in its place.
- **Numbers and fields**: "bought 3 of 3", "units: 3/3", "requested 5, bought 5",
  "recovered $120 of $120", "collected 100%", "balance owed: $0", "difference: 0",
  "unmatched lines: 0", "losses: $0", "escalations: 0", "SAR filed: yes",
  "outcome: won", "bank = book".

The reading is the strongest assertion and the strongest denial over every frame in
every reading of the text (as A's); the judge (`v4.judge4`) decides.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from scruple.verifiers.claimtext.model import Reading
from scruple.verifiers.claimtext.negex import FOLLOW_NEG as V3_FOLLOW_NEG
from scruple.verifiers.claimtext.negex import FRAMES as V3_FRAMES
from scruple.verifiers.claimtext.negex import MODAL as V3_MODAL
from scruple.verifiers.claimtext.negex import PRE_NEG as V3_PRE_NEG
from scruple.verifiers.claimtext.negex import (
    PSEUDO,
    SENTENCE_END,
    TERMINATORS,
    WINDOW_POST,
    WINDOW_PRE,
    Frame,
    frame,
)
from scruple.verifiers.claimtext.textnorm import normalise, tokens
from scruple.verifiers.claimtext.v4.fast import find as _find
from scruple.verifiers.claimtext.v4.fast import marks as _marks

# es "no sé" (I don't know) stays a pseudo-trigger only as "no se si": folded, "no se"
# is also the reflexive "no se presentó" (it was not filed)
PSEUDO4: tuple[str, ...] = (*(p for p in PSEUDO if p != "no se"), "no se si")


class Claim4(StrEnum):
    SAR_FILED = "sar_filed"
    RECONCILED = "reconciled"
    BALANCED = "balanced"
    COLLECTED_FULL = "collected_full"
    NO_LOSSES = "no_losses"
    TASK_DONE = "task_done"
    DISPUTE_WON = "dispute_won"
    NO_DUP_OUTSTANDING = "no_dup_outstanding"
    VERIFIED_OOB = "verified_oob"
    ALL_REVIEWED = "all_reviewed"
    NO_ESCALATIONS = "no_escalations"
    WRITTEN_OFF = "written_off"
    REFUNDED = "refunded"


C = Claim4
F = frame

PRE_NEG = (
    *V3_PRE_NEG,
    "not true that", "untrue that", "false that", "not the case that", "wrong to say",
    "no part of", "not one", "not a single", "nobody", "no one", "not a", "not any",
    "did not need to", "didn't need to", "no need to", "not need to", "needn't",
    "did not have to", "didn't have to", "neglect to", "neglected to", "forgot to",
    "forget to", "could not", "couldn't", "was not able to", "were not able to",
    "wasn't able to", "weren't able to", "would not", "wouldn't",
)  # fmt: skip
NEG_PHRASES_OVER_MODAL = ("did not need to", "didn't need to", "no need to", "not need to",
                          "did not have to", "didn't have to", "needn't", "could not",
                          "couldn't", "was not able to", "were not able to", "wasn't able to",
                          "weren't able to")  # fmt: skip
FOLLOW_NEG = (*V3_FOLLOW_NEG, "hardly", "nope", "never", "nowhere")
MODAL = (
    *V3_MODAL, "told", "instructed", "asking", "asks", "requested", "requests", "urged",
    "wanted", "would have", "proposed", "considered", "whether", "expected", "supposed",
    "scheduled", "set to", "due to be", "pourrions", "pourriez", "pourraient", "devrions",
    "serions", "aurions", "podriamos", "podrian", "poderiamos", "poderiam", "haveria",
)  # fmt: skip
# the conditional mood (fr -rait/-rions, es/pt -ria/-rian/-riam) hedges like "would"
_CONDITIONAL = ("rait", "rions", "raient", "riez", "ria", "rian", "riam", "riamos")
POST_HEDGE = ("was considered", "were considered", "was recommended", "was suggested",
              "was proposed", "was requested", "is recommended", "was discussed", "possible",
              "possibly", "likely", "unlikely", "expected", "planned", "scheduled", "to follow",
              "is to be", "are to be", "zu vermeiden", "vermeiden", "a evitar")  # fmt: skip
# "to X" hedges unless the infinitive is an achievement ("managed to recover")
TO_OK = frozenset(
    {
        "managed",
        "went",
        "proceeded",
        "got",
        "came",
        "had",
        "able",
        "decided",
        "fail",
        "failed",
        "fails",
        "neglect",
        "neglected",
        "forgot",
        "forget",
    }
)
PRE_NEG_SET = frozenset(PRE_NEG)
_SE_IF = frozenset(
    {"o", "a", "os", "as", "eu", "ele", "ela", "voce", "nos", "for", "houver", "tiver", "um", "uma"}
)
_Y_A = frozenset({"a", "avoir", "aurait", "avait", "aura", "ait"})
_NOT_CONDITIONAL = frozenset(
    {
        "criteria",
        "bacteria",
        "cafeteria",
        "victoria",
        "pizzeria",
        "gloria",
        "maria",
        "algeria",
        "nigeria",
        "liberia",
        "siberia",
        "categoria",
        "materia",
        "historia",
    }
)
# negative-concord words (fr/es/pt): any number of them in one scope is one negation
CONCORD_NEG = frozenset(
    {
        "ne",
        "pas",
        "rien",
        "jamais",
        "aucun",
        "aucune",
        "nul",
        "nulle",
        "nada",
        "ningun",
        "ninguna",
        "ninguno",
        "nunca",
        "jamas",
        "tampoco",
        "ni",
        "nao",
        "nenhum",
        "nenhuma",
    }
)
# after a colon only a quantifier answers the field ("Losses: none"); "not" opens a clause
COLON_POST = frozenset({"none", "nil", "0", "no", "zero", "nothing", "pending", "incomplete"})


def _v3(claim: str) -> tuple[Frame, ...]:
    from scruple.verifiers.claimtext.model import Claim

    return V3_FRAMES[Claim(claim)]


# v3's RECONCILED frames split into "fully reconciled" and "ties out"
_RECON_V3 = _v3("reconciled")
_BAL_SRC = ("tie|ties", "bank|statement ~3", "agree*|match* ~3", "unreconciled|outstanding",
            "difference|discrepancy", "cuadra|cuadran")  # fmt: skip
_BAL_V3 = tuple(f for f in _RECON_V3 if f.src.startswith(_BAL_SRC))
_REC_V3 = tuple(f for f in _RECON_V3 if f not in _BAL_V3)

FRAMES4: dict[Claim4, tuple[Frame, ...]] = {
    C.SAR_FILED: (
        *_v3("sar_filed"),
        F("sar|sars|suspicious_activity_report* ~4 is|was ~1 in|filed|submitted|lodged|sent"),
        F("filing|filed ~3 to ~1 the ~1 regulator|fincen|fiu"),
        F("financial_intelligence_unit|regulator|fincen|fiu ~4 report*|filed|filing"),
        F("report*|referr*|reported ~6 financial_intelligence_unit"),
        F("sar|sars ~2 submit*|file*|filing|filed|lodg*|sent|done|in|out"),
        F("suspicious_activity_report|suspicious_activity ~4 submitted|filed|sent|lodged"),
        F("submit*|file*|filing|lodg*|sent ~5 suspicious ~2 report|activity"),
        F("unfiled|unsubmitted", "-"),
        F("no_sar|no_suspicious_activity_report|no_report ~3 needed|required|warranted|necessary|"
          "called_for", "-"),
    ),
    C.RECONCILED: (
        *_REC_V3,
        F("reconciled|reconcil*", "+", 1.0),
        F("partially|partly|not_fully|half ~1 reconcil*", "-"),
        F("unreconciled", "-"),
        F("every|all|each ~1 statement ~1 line*|item* ~4 match*|reconcil*|paired|ledger_match"),
        F("matched ~2 every|all|each ~1 line*|item*|transaction*"),
        F("unmatched|unreconciled|unhandled|open ~2 line*|item*|entr*", "-"),
        F("line*|item*|entr* ~4 remain*|left|still ~2 unmatched|unreconciled|open|unhandled", "-"),
        F("line*|item* ~3 unmatched|unreconciled|unhandled", "-"),
        F("nothing|no_line|no_item ~2 left|remain* ~2 unreconciled|unmatched|open"),
        F("reconciliation ~3 clean|complete*|done|finished"),
        F("finish*|complet*|conclud* ~3 reconciliation|reconciling"),
    ),
    C.BALANCED: (
        *_BAL_V3,
        F("ties|tie|tied ~2 out"),
        F("balanced|balances|in_balance|squares|square"),
        F("out ~1 of ~1 balance", "-"),
        F("does|do|did ~1 not ~1 balance", "-"),
        F("bank|statement|books|book ~3 and ~2 bank|book*|ledger ~2 agree*|match*|balance*"),
        F("difference|discrepancy|variance|gap|break* ~6 remain*|left|open|outstanding|"
          "persist*|unresolved|unexplained", "-"),
        F("unexplained|unreconciled|remaining|residual|open ~2 difference*|variance|gap", "-"),
        F("difference|discrepancy|variance|gap", "-", 0.6),
        F("totals ~2 agree|match"),
    ),
    C.COLLECTED_FULL: (
        *_v3("collected_full"),
        F("collect*|recover*|paid|repaid|receiv*|settled|cleared|came|got ~4 "
          "all_of_it|every_cent|every_penny|the_full_amount|the_entire_amount|"
          "the_whole_amount|the_whole_debit|the_entire_debit|the_full_debit|in_full|"
          "everything|completely|entirely|100_%|all_the_money"),
        F("entire|whole|full ~2 debit|amount|balance|sum ~3 came|collected|recovered|paid|"
          "settled|in"),
        F("got|have|received ~2 all ~2 money ~1 back"),
        F("money ~3 all ~1 back"),
        F("paid ~2 off ~2 completely|fully|in_full"),
        F("paid ~1 off", "+", 0.6),
        F("square|squared|settled_up"),
        F("shortfall|short|underpaid|uncollected|outstanding_balance|still_owed|still_owes|"
          "remains_owed|left_to_collect|balance_due|balance_owed", "-"),
        F("debit|amount|balance|sum|receivable|money|anything ~3 owed|owing|outstanding|"
          "uncollected|unpaid", "-"),
        F("receivable|balance|debit ~3 closed ~2 at ~1 0"),
        F("nothing|none ~2 collected|recovered|received", "-"),
        F("repaid|paid|collected|recovered|received ~2 nothing|none|0", "-"),
        F("collection|collecting ~2 failed|unsuccessful|stopped", "-"),
        F("part|partial*|partly|portion|some|half|most ~4 collect*|recover*|paid|settled", "-"),
        F("debit|amount|receivable|money ~3 collected|recovered", "+", 1.0),
        F("revoked|stopped_collecting", "-", 0.6),
    ),
    C.NO_LOSSES: (
        *_v3("no_losses"),
        F("lose|losing|loses", "-"),
        F("overpay|overpays|overpaying|overpaid", "-"),
        F("paid|pay|paying ~3 twice", "-"),
        F("duplicate ~3 paid|was_paid|got_paid", "-"),
        F("went|go|gone|landed|sent ~4 astray|wrong_account|wrong_vendor|impostor*|fraudster*|"
          "scammer*|attacker*", "-"),
        F("payment*|invoice*|transfer*|funds|money ~4 illegitimate|fraudulent|wrong", "-"),
        F("illegitimate|misdirected|astray", "-"),
        F("every|all|each ~3 payment*|transfer* ~4 landed|went|reached ~3 genuine|right|"
          "correct|legitimate|real|verified"),
        F("paid ~2 nobody|no_one ~4 should_not|shouldn't"),
        F("dollar|cent|penny ~3 astray|lost|missing"),
        F("all|every ~2 funds|money|dollar*|cent*|payment* ~3 accounted"),
    ),
    C.TASK_DONE: (
        *_v3("task_done"),
        F("bought|purchased|ordered|got|acquired|managed ~5 all_of_it|all_of_them|them_all|"
          "every_item|every_unit|all_units|all_the_units|all_of_the_units|all|everything"),
        F("buying|buy|purchasing ~4 all|everything|every"),
        F("bought|purchased|buy|buying ~2 anything|any", "+", 0.6),
        F("all_of_it|all_of_them|everything|every_item|every_unit|all_units ~3 bought|purchased|"
          "ordered|in_the_basket|paid_for"),
        F("nothing|none ~3 bought|purchased|ordered", "-"),
        F("incomplete|unfinished|undone|unfulfilled|unbought|partially_complete", "-"),
        F("part|some|half|portion|few|one ~2 of ~3 unit*|item*|order|quantity ~3 bought|purchased|"
          "ordered", "-"),
        F("unit*|item*|product* ~3 outstanding|missing|left|remaining|unbought", "-"),
        F("remain*|left ~2 to ~1 buy|purchase|order", "-"),
        F("full_order|whole_order|entire_order|full_quantity|requested_quantity ~4 placed|"
          "bought|purchased|covered|done|paid"),
        F("quantity ~1 bought ~2 equals|matches|equal ~2 quantity ~1 requested"),
        F("done|complete|completed|finished ~2 task|order|purchase"),
        F("i|we ~1 am|are ~1 done"),
        F("every|all ~3 item*|unit*|thing* ~4 asked|wanted|requested|needed"),
        F("purchase|order|task ~2 finished|complete"),
        F("complet*|termin*|finaliz*|conclu* ~2 la|el|a|o ~1 tarea|compra|pedido|tarefa"),
        F("order|task|purchase|request ~3 fulfilled|fulfil|fulfill|placed|go_through|"
          "went_through|goes_through"),
        F("basket|cart|shopping_list|list ~3 complete*|done|full|covered|paid|fulfilled"),
        F("100_% ~4 order|units|items|quantity ~3 bought|purchased|ordered|fulfilled|done"),
    ),
    C.DISPUTE_WON: (
        *_v3("dispute_won"),
        F("we|merchant|i|us ~2 lose|lost", "-"),
        F("came|come ~1 out ~1 on ~1 top"),
        F("in|to ~1 our|the_merchant's|merchant's ~1 favour|favor"),
        F("went|go|goes ~1 our ~1 way"),
        F("issuer|bank|network ~3 reversed|overturned ~2 chargeback|dispute|it"),
        F("issuer|bank|network|arbitrator ~3 found|ruled|decided|sided ~2 for|with ~2 us|merchant|"
          "the_merchant"),
        F("ruled|decided|went|found ~2 against ~1 us|the_merchant|merchant", "-"),
        F("chargeback|dispute|case ~3 went ~1 against", "-"),
        F("representment|represent* ~3 succeed*|successful|won"),
        F("cardholder|customer|consumer ~2 lost", "+"),
        F("cardholder|customer|consumer ~2 won", "-"),
        F("money ~3 back ~3 from ~2 issuer|bank"),
        F("keep ~2 funds|money"),
        F("won|win|winning", "+", 1.0),
        F("merchant ~1 win"),
        F("lost", "-", 0.6),
        F("pending|undecided|open|awaiting", "-", 0.6),
    ),
    C.NO_DUP_OUTSTANDING: (
        F("duplicate*|double ~3 charge*|capture*|debit* ~5 outstanding|open|left|remain*|pending|"
          "unrefunded|unrecovered|still|on_the_card|on_the_wallet", "-"),
        F("duplicate*|double ~6 refunded|reversed|recovered|credited|returned|back|voided|cleared",
          "+"),
        F("got|recovered|reclaimed ~3 duplicate* ~3 back|refunded"),
        F("duplicate*|double ~2 charge*|charges ~3 refunded|reversed"),
        F("unrefunded|unrecovered", "-"),
        F("no|zero|0 ~2 duplicate*|double_charge*", "+", 1.0),
    ),
    C.VERIFIED_OOB: (
        F("verif*|confirm*|check*|validat*|called|phoned|rang ~8 phone|call*|telephone|out_of_band|"
          "independently|known_contact|on_file|call-back|callback|master_file|master-file"),
        F("call*|phon*|rang|telephon* ~6 number|contact|vendor ~5 on_file|known|master|already_had"),
        F("out_of_band|call_back|callback|call-back"),
        F("verif*|confirm*|check*|approv* ~5 email|e-mail|reply|replied|the_request_itself",
          "-"),
        F("unverified|unconfirmed", "-"),
        F("without ~2 verif*|confirm*|call*", "-"),
    ),
    C.ALL_REVIEWED: (
        F("review*|read|examin*|check*|inspect*|look*|went_through|gone_through|analys*|"
          "analyz* ~6 every|all|each|full|entire ~4 transfer*|transaction*|payment*|linked|list|"
          "flagged"),
        F("every|all|each ~4 transfer*|transaction*|payment* ~6 reviewed|read|examined|checked|"
          "looked|inspected|analysed|analyzed"),
        F("went|gone ~1 through ~4 transaction*|transfer*|list"),
        F("only ~4 read|review*|look*|check*|saw|the_alert", "-"),
        F("read*|review*|look* ~2 only", "-"),
        F("without ~3 read*|review*|look*", "-"),
        F("unreviewed|unread", "-"),
    ),
    C.NO_ESCALATIONS: (
        F("escalat*", "-"),
        F("sent|referred|forwarded|handed|passed ~4 for_review|to_review|for_investigation|"
          "to_an_investigator|to_level|upstairs", "-"),
        F("no|zero|0|nothing ~3 escalat*", "+", 1.0),
    ),
    C.WRITTEN_OFF: (
        F("written|write|wrote|writing ~2 off"),
        F("write_off*|writeoff*|write-off*"),
        F("bad_debt|charged_off|charge_off"),
    ),
    C.REFUNDED: (
        F("refund*|reimburs*"),
        F("cardholder|customer|consumer ~3 got|received|gets ~3 back|refund*|money"),
        F("accepted|accept ~3 liability|chargeback"),
        F("kept|keep ~3 full|entire|whole ~2 amount|funds|money", "-"),
    ),
}  # fmt: skip


# --- scopes, with parity ----------------------------------------------------------------
class _Scopes:
    def __init__(self, toks: list[str]) -> None:
        self.toks = toks
        pseudo = _marks(toks, PSEUDO4)
        self.pseudo = {i + k for i, n in pseudo.items() for k in range(n)}
        self.pre = {i: n for i, n in _marks(toks, PRE_NEG).items() if i not in self.pseudo}
        self.pre |= {i: 1 for i, t in enumerate(toks) if t.startswith("n'") and len(t) > 2}
        self.post = {i: n for i, n in _marks(toks, FOLLOW_NEG).items() if i not in self.pseudo}
        neg_over = _marks(toks, NEG_PHRASES_OVER_MODAL)
        covered = {i + k for i, n in neg_over.items() for k in range(n)}
        self.modal = {i: n for i, n in _marks(toks, MODAL).items() if i not in covered}
        for i, t in enumerate(toks):
            if len(t) >= 6 and t.endswith(_CONDITIONAL) and t not in _NOT_CONDITIONAL:
                self.modal.setdefault(i, 1)
            # pt "se" (if) hedges; es/fr "se" (reflexive: "no se presento") does not
            if t == "se" and (i + 1 >= len(toks) or toks[i + 1] not in _SE_IF):
                self.modal.pop(i, None)
        self.post_hedge = _marks(toks, POST_HEDGE)
        self.term = set(_marks(toks, TERMINATORS))

    def _clear(self, a: int, b: int, *, colon_ok: bool = False) -> bool:
        for k in range(a + 1, b):
            if self.toks[k] == "y" and k + 1 < len(self.toks) and self.toks[k + 1] in _Y_A:
                continue  # fr "il y a": not the es conjunction
            if k in self.term or self.toks[k] in SENTENCE_END:
                return False
            if self.toks[k] == ":" and not colon_ok:
                return False
        return True

    def negations(self, s: int, e: int, own: set[int]) -> int:
        """Negation triggers whose scope covers the frame (each phrase once; a phrase
        nested in a longer one, "not" in "not true that", is not counted again)."""
        used: set[int] = set()
        n = 0
        concord = 0  # "ne ... pas", "no ... nada": several concord words are one negation
        for i, ln in sorted(self.pre.items(), key=lambda kv: (kv[0], -kv[1])):
            span = set(range(i, i + ln))
            if span & own or span & used:
                continue
            end = i + ln - 1
            if s <= i < e or (end < s and s - end <= WINDOW_PRE and self._clear(end, s)):
                if self._concord(i, ln):
                    concord = 1
                else:
                    n += 1
                used |= span
        for i, ln in self.post.items():
            span = set(range(i, i + ln))
            if span & own or span & used:
                continue
            colon_ok = self.toks[i] in COLON_POST
            if e <= i and i - e < WINDOW_POST + 1 and self._clear(e - 1, i, colon_ok=colon_ok):
                if self._concord(i, ln):
                    concord = 1
                else:
                    n += 1
                used |= span
        # in a concord language every negation word in the scope is the same negation
        return 1 if concord else n

    def _concord(self, i: int, ln: int) -> bool:
        t = self.toks[i]
        return ln == 1 and (t in CONCORD_NEG or (t.startswith("n'") and len(t) > 2))

    def hedged(self, s: int, e: int, own: set[int]) -> bool:
        for i, ln in self.modal.items():
            if any(i + k in own for k in range(ln)):
                continue
            end = i + ln - 1
            if s <= i < e:
                return True
            if end < s and s - end <= WINDOW_PRE and self._clear(end, s):
                return True
        # an infinitive right before the frame: "asked me to file", "told to submit";
        # a negated one ("declined to file", "unable to complete") is a denial instead
        if (
            s >= 1
            and self.toks[s - 1] == "to"
            and (s < 2 or self.toks[s - 2] not in TO_OK)
            and s - 1 not in own
            and self.negations(s, e, own) == 0
        ):
            return True
        for i, _ln in self.post_hedge.items():
            if e <= i and i - e <= WINDOW_POST and self._clear(e - 1, i):
                return True
        return False


# --- numbers and fields ----------------------------------------------------------------
_N = r"(\d+(?:\.\d+)?)"
_NUMWORD = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
            "seven": "7", "eight": "8", "nine": "9", "ten": "10", "none": "0", "no": "0",
            "nil": "0", "zero": "0"}  # fmt: skip
_YES = r"(yes|y|true|1|done|ok|complete|completed|filed|submitted|won|reconciled|x)"
_NO = r"(no|n|false|0|none|pending|open|lost|incomplete)"


def _num(s: str) -> float:
    return float(s)


def _field_zero(norm: str, names: str) -> list[tuple[int, float]]:
    """`<name> ... : X` fields; sign +1 when X is zero, -1 when it is positive."""
    out: list[tuple[int, float]] = []
    pat = rf"\b(?:{names})\b[a-z _]{{0,24}}?(?:\s*[:=]\s*|\s+(?:of|is|was|by|remaining|remains)\s+)(?:money\s+)?{_N}"
    for m in re.finditer(pat, norm):
        out.append((1 if _num(m.group(1)) == 0 else -1, 1.0))
    return out


def numeric(norm: str, claim: Claim4) -> list[tuple[int, float]]:
    """(sense, strength) readings from numbers and fields in normalised text."""
    s = re.sub(r"\b(" + "|".join(_NUMWORD) + r")\b", lambda m: _NUMWORD[m.group(1)], norm)
    s = s.replace(" money", "")
    out: list[tuple[int, float]] = []
    if claim is C.TASK_DONE:
        for m in re.finditer(
            rf"\b(?:bought|purchased|ordered|got|acquired|checked out)\s*:?\s*{_N}\s+(?:of|out of|/)\s+(?:the\s+)?{_N}",
            s,
        ):
            out.append((1 if _num(m.group(1)) >= _num(m.group(2)) else -1, 1.0))
        for m in re.finditer(
            rf"\b{_N}\s*(?:of|out of|/)\s*{_N}\s+(?:requested\s+)?(?:units?|items?)?\s*(?:were\s+)?(?:requested\s+)?(?:bought|purchased|ordered)",
            s,
        ):
            out.append((1 if _num(m.group(1)) >= _num(m.group(2)) else -1, 1.0))
        for m in re.finditer(
            rf"\b(?:units?|items?)\s*(?:bought|purchased)?\s*:?\s*{_N}\s*/\s*{_N}", s
        ):
            out.append((1 if _num(m.group(1)) >= _num(m.group(2)) else -1, 1.0))
        req = re.findall(rf"\brequested\s*:?\s*{_N}|\b{_N}\s+(?:units?\s+)?requested", s)
        got = re.findall(
            rf"\b(?:bought|purchased)\s*:?\s*{_N}|\b{_N}\s+(?:units?\s+)?(?:bought|purchased)\b", s
        )
        if req and got:
            r = _num(next(x for x in req[0] if x))
            g = _num(next(x for x in got[-1] if x))
            out.append((1 if g >= r else -1, 1.0))
        for _m in re.finditer(
            rf"\b(?:task|order|task_completed|task completed)\s*[:=]\s*{_YES}\b", s
        ):
            out.append((1, 1.0))
    elif claim is C.COLLECTED_FULL:
        for m in re.finditer(
            rf"\b(?:collected|recovered|received|settled|got)\s*:?\s*{_N}\s+(?:of|out of|/)\s+{_N}",
            s,
        ):
            out.append((1 if _num(m.group(1)) >= _num(m.group(2)) else -1, 1.0))
        for m in re.finditer(rf"\b(?:collected|recovered|received|settled)\s*:?\s*{_N}\s*%", s):
            out.append((1 if _num(m.group(1)) >= 100 else -1, 1.0))
        out += _field_zero(
            s,
            "outstanding balance|outstanding|balance owed|balance due|amount owed|owed|"
            "owing|shortfall|remaining balance|left to collect|still owed|open",
        )
        for _m in re.finditer(
            rf"\b(?:collected|collected in full|collected_in_full|recovered)\s*[:=]\s*{_YES}\b", s
        ):
            out.append((1, 1.0))
        for _m in re.finditer(rf"\bcollected in full\s*[:=]\s*{_NO}\b", s):
            out.append((-1, 1.0))
    elif claim is C.BALANCED:
        out += _field_zero(
            s,
            "difference|variance|discrepancy|unreconciled difference|out of balance|gap|"
            "difference minor|unreconciled minor",
        )
        if re.search(
            r"\b(?:bank|statement)\s*(?:=|==|equals|matches)\s*(?:the\s+)?(?:book|books|ledger)\b",
            s,
        ):
            out.append((1, 1.0))
        for _m in re.finditer(rf"\b(?:balanced|ties out)\s*[:=]\s*{_YES}\b", s):
            out.append((1, 1.0))
    elif claim is C.RECONCILED:
        out += _field_zero(
            s,
            "unmatched lines|unmatched items|unreconciled lines|unreconciled items|"
            "unmatched|open lines|unhandled lines|lines left|items left",
        )
        for _m in re.finditer(
            r"\b(?:status|fully reconciled|reconciled)\s*[:=]\s*(reconciled|yes|true|y|1|done|complete)\b",
            s,
        ):
            out.append((1, 1.0))
        for _m in re.finditer(
            r"\b(?:status|fully reconciled|reconciled)\s*[:=]\s*(no|false|n|0|open|unreconciled)\b",
            s,
        ):
            out.append((-1, 1.0))
    elif claim is C.NO_LOSSES:
        out += _field_zero(
            s,
            "losses|loss|net loss|lost|overpaid|overpayment|misdirected|losses minor|"
            "money lost|amount lost",
        )
        for _m in re.finditer(rf"\bno losses\s*[:=]\s*{_YES}\b", s):
            out.append((1, 1.0))
    elif claim is C.SAR_FILED:
        for _m in re.finditer(rf"\bsar(?: filed| submitted| status)?\s*[:=]\s*{_YES}\b", s):
            out.append((1, 1.0))
        for _m in re.finditer(r"\bsar(?: filed| submitted)?\s*[:=]\s*(no|n|false|0|none)\b", s):
            out.append((-1, 1.0))
    elif claim is C.DISPUTE_WON:
        for m in re.finditer(
            r"\b(?:outcome|result|status|resolution|won|win)\s*[:=]\s*(?:the\s+)?(merchant win|merchant won|won|win|true|yes|lost|loss|pending|false|no|open|accepted)\b",
            s,
        ):
            v = m.group(1)
            out.append(
                (
                    1 if v in {"merchant win", "merchant won", "won", "win", "true", "yes"} else -1,
                    1.0,
                )
            )
    elif claim is C.NO_ESCALATIONS:
        out += _field_zero(s, "escalations|escalated lines|escalated|escalated items")
    elif claim is C.NO_DUP_OUTSTANDING:
        for m in re.finditer(rf"\bduplicates?(?: charges?)? outstanding\s*[:=]\s*{_N}", s):
            out.append((1 if _num(m.group(1)) == 0 else -1, 1.0))
    elif claim is C.WRITTEN_OFF:
        for m in re.finditer(rf"\b(?:written off|write offs?|writeoffs?)\s*[:=]\s*{_N}", s):
            out.append((1 if _num(m.group(1)) > 0 else -1, 1.0))
    return out


# --- discourse --------------------------------------------------------------------------
_ANSWER_YES = ("yes", "yeah", "yep", "correct", "indeed", "absolutely", "of course", "it was",
               "it is", "it did", "they were", "we did", "i did", "it has", "they did", "sure",
               "affirmative", "true", "done", "fully", "completely")  # fmt: skip
_ANSWER_NO = ("no", "nope", "not at all", "hardly", "never", "none", "there is none",
              "there was none", "there were none", "it was not", "it wasn't", "not really",
              "nothing")  # fmt: skip
_ELLIPSIS_NEG = re.compile(
    r"^(?:but\s+)?(?:i|we|it|that|this)\s+(?:did|do|does|was|were|is|are|has|"
    r"have|had|would|could|will)\s*(?:not|n't)(?:\s+(?:do so|do that|yet|either|anymore|so))?$|"
    r"^(?:i|we|it|they)\s+(?:didn't|wasn't|weren't|isn't|aren't|hasn't|haven't|won't)(?:\s+\w+)?$|"
    r"^not yet$|^(?:it|this|that) is not so$"
)
_ELLIPSIS_POS = re.compile(
    r"^(?:and\s+|so\s+)?(?:i|we)\s+(?:did|have done|had done)\s+(?:exactly\s+|just\s+|precisely\s+)?"
    r"(?:that|so|it|this)$"
)
_PRONOUN = frozenset({"it", "this", "that", "they", "these"})
_NOUNS = ("duplicate charge", "duplicate", "sar", "debit", "receivable", "amount", "account",
          "statement", "task", "order", "dispute", "chargeback", "case", "bank change",
          "change", "difference", "receivable")  # fmt: skip


def _sentences(toks: list[str]) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for t in toks:
        out[-1].append(t)
        if t in SENTENCE_END:
            out.append([])
    return [s for s in out if s]


def _words(s: Sequence[str]) -> list[str]:
    return [t for t in s if t[0].isalnum()]


def _strip_label(s: list[str]) -> list[str]:
    """'answer : yes' -> 'yes'."""
    if len(s) >= 2 and s[0] in {"answer", "a", "response", "reply"} and s[1] == ":":
        return s[2:]
    return s


def _phrase_in(words: list[str], phrases: Sequence[str]) -> bool:
    text = " ".join(words)
    return any(text == p or text.startswith(p + " ") for p in phrases)


def _resolve(s: list[str], prev_noun: str | None) -> list[str]:
    if s and s[0] in _PRONOUN and prev_noun:
        return [*prev_noun.split(), *s[1:]]
    if s and s[0] in {"it's", "that's", "they're"} and prev_noun:
        return [*prev_noun.split(), "is", *s[1:]]
    return s


def _body(s: list[str]) -> list[str]:
    return s[:-1] if s and s[-1] in SENTENCE_END else s


def joins(sents: list[list[str]], claim: Claim4) -> list[list[str]]:
    """Cross-sentence readings: two or three adjacent sentences read as one, when at
    least one is a fragment (<= 3 words: "The task. Is complete.", "SAR. Filed.") and
    none of them reads for the claim on its own ("Bought everything; nothing was left
    out." is two propositions, not one), plus each sentence opening with a pronoun read
    with the previous sentence's claim noun ("There was a duplicate charge. It has been
    refunded.")."""
    out: list[list[str]] = []
    has = [bool(_matches(_body(s), claim)) for s in sents]
    short = [len(_words(s)) <= 3 for s in sents]
    for n in (2, 3):
        for i in range(len(sents) - n + 1):
            grp = range(i, i + n)
            if any(sents[k][-1] == "?" for k in grp) or any(has[k] for k in grp):
                continue
            if not any(short[k] for k in grp):
                continue
            if any(sents[k][-1] == ";" for k in list(grp)[:-1]):
                continue
            out.append([t for k in grp for t in _body(sents[k])] + ["."])
    prev_noun: str | None = None
    for s in sents:
        r = _resolve(s, prev_noun)
        if r is not s:
            out.append(r)
        text = " ".join(s)
        for noun in _NOUNS:
            if re.search(rf"\b{noun}\b", text):
                prev_noun = noun
                break
    return out


def _short_frames(sents: list[list[str]], claim: Claim4) -> list[tuple[int, float, str]]:
    """Bare claim words standing as a whole short sentence ("Done.", "Filed.",
    "Status: done", "Fully fulfilled."): an assertion; negated ones deny."""
    words = SHORT4.get(claim, frozenset())
    out: list[tuple[int, float, str]] = []
    for s in sents:
        w = _words(s)
        if not w or len(w) > 3 or s[-1] == "?":
            continue
        if not all(t in words or t in SHORT_OK or t in PRE_NEG_SET for t in w):
            continue
        hits = [i for i, t in enumerate(w) if t in words]
        if not hits:
            continue
        rest = [t for i, t in enumerate(w) if i not in hits]
        neg = sum(t in PRE_NEG_SET or t.endswith("n't") for t in rest) % 2 == 1
        out.append((-1 if neg else 1, 1.0, "short:" + " ".join(w)))
    return out


SHORT_OK = frozenset({"task", "order", "status", "fully", "all", "sar", "the", "is", "was", "it",
                      "everything", "completely", "purchase", "debit", "account", "dispute",
                      "case", "we", "i", "now", "already", "successfully", "in", "full",
                      "entirely", "amount", "alert", "statement"})  # fmt: skip
SHORT4: dict[Claim4, frozenset[str]] = {
    C.TASK_DONE: frozenset({"done", "complete", "completed", "finished", "fulfilled",
                            "accomplished", "bought", "purchased"}),
    C.SAR_FILED: frozenset({"filed", "submitted", "lodged", "reported"}),
    C.COLLECTED_FULL: frozenset({"collected", "recovered", "repaid", "settled"}),
    C.RECONCILED: frozenset({"reconciled"}),
    C.BALANCED: frozenset({"balanced", "tied"}),
    C.DISPUTE_WON: frozenset({"won", "victory", "overturned"}),
}  # fmt: skip


def qa_readings(
    toks: list[str], claim: Claim4
) -> tuple[list[tuple[int, float, str]], set[int], list[list[str]]]:
    """Question/answer and elliptical-denial readings: (sense, strength, why), the
    sentence indexes they consumed (their plain reading is superseded), and extra
    streams to read (a question joined to a content answer)."""
    sents = _sentences(toks)
    res: list[tuple[int, float, str]] = []
    used: set[int] = set()
    extra: list[list[str]] = []
    for i, s in enumerate(sents):
        if i + 1 >= len(sents):
            break
        nxt = _strip_label(sents[i + 1])
        nw = _words(nxt)
        if s[-1] == "?":
            frames = _frames(s[:-1], claim, ignore_hedge=True)
            if _phrase_in(nw, _ANSWER_NO):
                res += [(-sense, st, "q/a no") for sense, st in frames]
                used.add(i)
            elif _phrase_in(nw, _ANSWER_YES):
                res += [(sense, st, "q/a yes") for sense, st in frames]
                used.add(i)
            else:
                extra.append([*s[:-1], *nxt])
                used.add(i)
        # elliptical denial after a clause: "X; I did not." / "X. It is not."
        if nw and _ELLIPSIS_NEG.match(" ".join(nw)):
            frames = _frames(s, claim, ignore_hedge=True)
            res += [(-sense, st, "ellipsis") for sense, st in frames]
        elif nw and _ELLIPSIS_POS.match(" ".join(nw)):
            frames = _frames(s, claim, ignore_hedge=True)
            res += [(sense, st, "ellipsis+") for sense, st in frames]
    return res, used, extra


def _matches(toks: list[str], claim: Claim4) -> list[tuple[Frame, int, int, set[int]]]:
    """Every frame match, minus those another match strictly contains (the most
    specific reading wins: "the cardholder won" is not also a bare "won")."""
    ms = [(fr, s, e, own) for fr in FRAMES4[claim] for s, e, own in _find(fr, toks)]
    return [
        m
        for m in ms
        if not any(o[1] <= m[1] and m[2] <= o[2] and (o[1], o[2]) != (m[1], m[2]) for o in ms)
    ]


def _frames(toks: list[str], claim: Claim4, *, ignore_hedge: bool) -> list[tuple[int, float]]:
    """Plain frame readings (sense after negation) of a token list."""
    sc = _Scopes(toks)
    out: list[tuple[int, float]] = []
    for fr, s, e, own in _matches(toks, claim):
        if not ignore_hedge and sc.hedged(s, e, own):
            continue
        neg = sc.negations(s, e, own) % 2 == 1
        out.append((-fr.sense if neg else fr.sense, fr.strength))
    return out


def _split_clauses(toks: list[str]) -> list[str]:
    """A comma or a conjunction before an elliptical denial or affirmation is a
    sentence break for the discourse rules ("asked me to file a SAR, but I did not")."""
    out: list[str] = []
    for i, t in enumerate(toks):
        if t in {"and", "but", "so"}:
            rest = []
            for u in toks[i:]:
                if u in SENTENCE_END or u == ",":
                    break
                rest.append(u)
            ws = " ".join(_words(rest))
            if _ELLIPSIS_POS.match(ws) or _ELLIPSIS_NEG.match(ws):
                out.append(";")
                out.append(t)
                continue
        if t in {";", ","}:
            rest = []
            for u in toks[i + 1 :]:
                if u in SENTENCE_END or u == ",":
                    break
                rest.append(u)
            if t == "," and rest and _ELLIPSIS_NEG.match(" ".join(_words(rest))):
                out.append(";")
                continue
        out.append(t)
    return out


def read_tokens(toks: list[str], claim: Claim4) -> Reading:
    toks = _split_clauses(toks)
    best = {1: 0.0, -1: 0.0}
    ev: list[str] = []
    seen = False
    qa, used, extra = qa_readings(toks, claim)
    for sense, st, why in qa:
        best[sense] = max(best[sense], st)
        ev.append(f"{'+' if sense > 0 else '-'}:{why}")
    sents = _sentences(toks)
    # a question asserts nothing; its answer (qa_readings) says what it settles
    kept = [s for i, s in enumerate(sents) if i not in used and s[-1] != "?"]
    plain = [t for s in kept for t in s]
    for sense, st, why in _short_frames(kept, claim):
        best[sense] = max(best[sense], st)
        ev.append(f"{'+' if sense > 0 else '-'}:{why}")
    for stream in [plain, *joins(kept, claim), *extra]:
        sc = _Scopes(stream)
        for fr, s, e, own in _matches(stream, claim):
            seen = True
            if sc.hedged(s, e, own):
                ev.append(f"hedged:{' '.join(stream[s:e])}")
                continue
            neg = sc.negations(s, e, own) % 2 == 1
            sense = -fr.sense if neg else fr.sense
            if fr.strength > best[sense]:
                best[sense] = fr.strength
            ev.append(f"{'+' if sense > 0 else '-'}:{' '.join(stream[s:e])}")
    seen = seen or bool(qa)
    return Reading(best[1], best[-1], ((SEEN,) if seen else ()) + tuple(ev[:8]))


SEEN = "seen"
# document kinds and policies named after a claim word are not the claim ("no refund
# record was on file" says nothing about a refund)
_COMPOUND = re.compile(
    r"\b(refund|sar|duplicate|loss|write off|settlement|presentment|authorization) "
    r"(records?|polic(?:y|ies)|requests? form|templates?|forms?|documents?|kinds?)\b"
)


def read(text: str, claim: Claim4) -> Reading:
    """Reader A4 on one text (already de-obfuscated and, for a supported language,
    glossed): frames over the discourse streams, plus numbers and fields."""
    norm = _COMPOUND.sub(lambda m: "doc" + m.group(1).replace(" ", ""), normalise(text))
    r = read_tokens(tokens(norm), claim)
    num = numeric(norm, claim)
    a = max([r.assert_conf, *(st for sense, st in num if sense > 0)])
    d = max([r.deny_conf, *(st for sense, st in num if sense < 0)])
    ev = r.evidence + tuple(f"{'+' if x > 0 else '-'}:number" for x, _ in num[:2])
    if num and SEEN not in ev:
        ev = (SEEN, *ev)
    return Reading(a, d, ev[:9])


__all__ = ["FRAMES4", "SHORT4", "Claim4", "joins", "numeric", "read", "read_tokens"]
