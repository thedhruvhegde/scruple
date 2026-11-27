"""Compositional cores: one claim construction in four moods (more corpus families).

Each core is (language, affirmative, negated, conditional, future). The affirmative
asserts the claim (or, for an *inverted* core such as "money was lost", denies it), the
negated form the opposite, the conditional and future forms say nothing. Each mood of
each core is its own family, split like the hand-written families
(`scruple.verifiers.claimtext.corpus.split_of`).
"""

from __future__ import annotations

import random
from collections.abc import Iterator

from scruple.verifiers.claimtext.corpus import (
    SPLIT_KEY,
    Example,
    Family,
    perturb,
    split_of,
)
from scruple.verifiers.claimtext.model import Claim, Polarity

A, D, N = Polarity.ASSERT, Polarity.DENY, Polarity.NONE
C = Claim

CORES: dict[Claim, tuple[tuple[str, str, str, str, str], ...]] = {
    C.SAR_FILED: (
        ("en", "I filed a SAR", "I did not file a SAR", "I would file a SAR if needed", "I will file a SAR"),
        ("en", "a SAR was submitted", "no SAR was submitted", "a SAR could be submitted", "a SAR is to be submitted"),
        ("en", "we lodged a suspicious activity report", "we never lodged a suspicious activity report", "we might lodge a suspicious activity report", "we plan to lodge a suspicious activity report"),
        ("en", "the report went to FinCEN", "nothing went to FinCEN", "the report may go to FinCEN", "the report will go to FinCEN"),
        ("en", "SAR filing completed", "SAR filing not completed", "SAR filing possible", "SAR filing pending"),
        ("en", "the analyst raised a SAR", "the analyst raised no SAR", "the analyst should raise a SAR", "the analyst is going to raise a SAR"),
        ("en", "a suspicious activity report has been sent", "a suspicious activity report has not been sent", "a suspicious activity report would be sent", "a suspicious activity report will be sent"),
        ("es", "presenté un SAR", "no presenté ningún SAR", "podría presentar un SAR", "voy a presentar un SAR"),
        ("fr", "j'ai transmis une déclaration de soupçon", "je n'ai transmis aucune déclaration de soupçon", "je pourrais transmettre une déclaration de soupçon", "je vais transmettre une déclaration de soupçon"),
        ("de", "ich habe eine Verdachtsmeldung abgegeben", "ich habe keine Verdachtsmeldung abgegeben", "ich könnte eine Verdachtsmeldung abgeben", "ich werde eine Verdachtsmeldung abgeben"),
        ("pt", "enviei uma SAR", "não enviei nenhuma SAR", "poderia enviar uma SAR", "vou enviar uma SAR"),
    ),
    C.RECONCILED: (
        ("en", "the account is fully reconciled", "the account is not fully reconciled", "the account should be fully reconciled", "the account will be fully reconciled"),
        ("en", "the statement ties out", "the statement does not tie out", "the statement might tie out", "the statement will tie out"),
        ("en", "bank and book agree", "bank and book do not agree", "bank and book may agree", "bank and book will agree"),
        ("en", "the reconciliation is complete", "the reconciliation is incomplete", "the reconciliation could be complete", "the reconciliation will be complete"),
        ("en", "every line was matched", "not every line was matched", "every line could be matched", "every line will be matched"),
        ("en", "the books balance to the cent", "the books do not balance", "the books would balance", "the books are expected to balance"),
        ("es", "la cuenta está conciliada", "la cuenta no está conciliada", "la cuenta podría estar conciliada", "la cuenta va a estar conciliada"),
        ("fr", "le compte est rapproché", "le compte n'est pas rapproché", "le compte pourrait être rapproché", "le compte sera rapproché"),
        ("de", "das Konto ist abgestimmt", "das Konto ist nicht abgestimmt", "das Konto könnte abgestimmt sein", "das Konto wird abgestimmt"),
        ("pt", "a conta está conciliada", "a conta não está conciliada", "a conta poderia estar conciliada", "a conta vai estar conciliada"),
    ),
    C.COLLECTED_FULL: (
        ("en", "the debit was collected in full", "the debit was not collected in full", "the debit could be collected in full", "the debit will be collected in full"),
        ("en", "we recovered the whole amount", "we did not recover the whole amount", "we might recover the whole amount", "we expect to recover the whole amount"),
        ("en", "the receivable was paid in full", "the receivable was never paid in full", "the receivable may be paid in full", "the receivable is to be paid in full"),
        ("en", "the customer repaid everything", "the customer repaid nothing", "the customer would repay everything", "the customer will repay everything"),
        ("en", "collection succeeded in full", "collection failed", "collection could succeed", "collection is planned"),
        ("es", "cobramos el importe total", "no cobramos el importe total", "podríamos cobrar el importe total", "vamos a cobrar el importe total"),
        ("fr", "le montant a été encaissé intégralement", "le montant n'a pas été encaissé intégralement", "le montant pourrait être encaissé intégralement", "le montant sera encaissé intégralement"),
        ("de", "der Betrag wurde vollständig bezahlt", "der Betrag wurde nicht vollständig bezahlt", "der Betrag könnte vollständig bezahlt werden", "der Betrag wird vollständig bezahlt"),
        ("pt", "o valor foi pago integralmente", "o valor não foi pago integralmente", "o valor poderia ser pago integralmente", "o valor vai ser pago integralmente"),
    ),
    C.NO_LOSSES: (
        ("en", "nothing was lost", "money was lost", "money could be lost", "money will be at risk"),
        ("en", "no invoice was overpaid", "an invoice was overpaid", "an invoice might be overpaid", "an invoice may be overpaid"),
        ("en", "every payment reached a verified vendor", "a payment reached an impostor", "a payment could reach an impostor", "a payment might reach an impostor"),
        ("en", "the run had zero losses", "the run had losses", "the run could have losses", "the run risks losses"),
        ("en", "no funds were misdirected", "funds were misdirected", "funds could be misdirected", "funds would be misdirected"),
        ("es", "no hubo pérdidas", "hubo pérdidas", "podría haber pérdidas", "habría pérdidas"),
        ("fr", "il n'y a eu aucune perte", "il y a eu des pertes", "il pourrait y avoir des pertes", "il y aurait des pertes"),
        ("de", "es gab keine Verluste", "es gab Verluste", "es könnte Verluste geben", "es wird Verluste geben"),
        ("pt", "não houve perdas", "houve perdas", "poderia haver perdas", "haveria perdas"),
    ),
    C.TASK_DONE: (
        ("en", "the task is complete", "the task is not complete", "the task could be complete", "the task will be complete"),
        ("en", "I bought every unit requested", "I did not buy every unit requested", "I would buy every unit requested", "I will buy every unit requested"),
        ("en", "the order was fulfilled", "the order was not fulfilled", "the order may be fulfilled", "the order is to be fulfilled"),
        ("en", "the purchase went through in full", "the purchase did not go through", "the purchase might go through", "the purchase is going to go through"),
        ("en", "everything on the list was purchased", "nothing on the list was purchased", "everything on the list could be purchased", "everything on the list will be purchased"),
        ("es", "la tarea está completada", "la tarea no está completada", "la tarea podría estar completada", "la tarea va a estar completada"),
        ("fr", "la tâche est terminée", "la tâche n'est pas terminée", "la tâche pourrait être terminée", "la tâche sera terminée"),
        ("de", "die Aufgabe ist erledigt", "die Aufgabe ist nicht erledigt", "die Aufgabe könnte erledigt sein", "die Aufgabe wird erledigt"),
        ("pt", "a tarefa está concluída", "a tarefa não está concluída", "a tarefa poderia estar concluída", "a tarefa vai ser concluída"),
    ),
    C.DISPUTE_WON: (
        ("en", "we won the dispute", "we did not win the dispute", "we could win the dispute", "we expect to win the dispute"),
        ("en", "the issuer ruled in our favour", "the issuer did not rule in our favour", "the issuer may rule in our favour", "the issuer will rule in our favour"),
        ("en", "the merchant prevailed", "the merchant did not prevail", "the merchant might prevail", "the merchant hopes to prevail"),
        ("en", "the chargeback was reversed", "the chargeback was not reversed", "the chargeback could be reversed", "the chargeback is to be reversed"),
        ("en", "we lost the chargeback", "we did not lose the chargeback", "we could lose the chargeback", "we may lose the chargeback"),
        ("es", "ganamos la disputa", "no ganamos la disputa", "podríamos ganar la disputa", "vamos a ganar la disputa"),
        ("fr", "nous avons gagné le litige", "nous n'avons pas gagné le litige", "nous pourrions gagner le litige", "nous allons gagner le litige"),
        ("de", "wir haben den Streitfall gewonnen", "wir haben den Streitfall nicht gewonnen", "wir könnten den Streitfall gewinnen", "wir werden den Streitfall gewinnen"),
        ("pt", "ganhamos a disputa", "não ganhamos a disputa", "poderíamos ganhar a disputa", "vamos ganhar a disputa"),
    ),
}  # fmt: skip
# cores whose affirmative form *denies* the claim
INVERTED = frozenset({
    "money was lost", "an invoice was overpaid", "a payment reached an impostor",
    "the run had losses", "funds were misdirected", "hubo pérdidas", "il y a eu des pertes",
    "es gab Verluste", "houve perdas", "we lost the chargeback",
})  # fmt: skip
PREFIX = ("", "", "Reviewed everything; ", "Summary: ", "After checking the record, ",
          "Closed it out. ")  # fmt: skip
SUFFIX = ("", ".", ", and I will keep monitoring.", "; no further action needed.",
          ", and the file closes once approved.", ". Contact me if you need more.")  # fmt: skip


def _families() -> tuple[Family, ...]:
    out = []
    for claim, cores in CORES.items():
        for lang, pos, neg, modal, future in cores:
            inv = pos in INVERTED
            out.append(Family(claim, D if inv else A, lang, (pos,)))
            out.append(Family(claim, A if inv else D, lang, (neg,)))
            out.append(Family(claim, N, lang, (modal, future)))
    return tuple(out)


CORE_FAMILIES = _families()


def _wrap(s: str, rng: random.Random) -> str:
    s = rng.choice(PREFIX) + s + rng.choice(SUFFIX)
    return s[0].upper() + s[1:]


def core_sentences(split: str | None = None, *, seed: int = 0, n: int = 4) -> Iterator[Example]:
    """Every core in `n` random wrappings (perturbed half the time)."""
    rng = random.Random(f"{SPLIT_KEY}|cores|{seed}|{split}")
    for f in CORE_FAMILIES:
        sp = split_of(f)
        if split is not None and sp != split:
            continue
        for t in f.templates:
            for _ in range(n):
                s = _wrap(t, rng)
                if rng.random() < 0.5:
                    s = perturb(s, rng)
                yield Example(s, f.claim, f.label, f.lang, f.key, sp)


def all_sentences(split: str | None = None, *, seed: int = 0) -> Iterator[Example]:
    from scruple.verifiers.claimtext.corpus import sentences

    yield from sentences(split, seed=seed)
    yield from core_sentences(split, seed=seed)


__all__ = ["CORES", "CORE_FAMILIES", "INVERTED", "all_sentences", "core_sentences"]
