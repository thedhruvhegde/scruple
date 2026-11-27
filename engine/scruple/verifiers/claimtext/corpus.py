"""Labelled claim sentences for training and measuring the free-text readers.

Every *family* is one construction (a template with `{a|b|c}` alternatives) with one
label (`assert`, `deny` or `none`) and a language. Families, not sentences, are split
into train / dev / test (`split_of`), so a construction seen in training never appears
in test: a test score measures paraphrase generalisation, not memory. The test split
is sealed: `sentences(split="test")` is read only by the final evaluation
(`scruple.redteam.textbench`), never by training or threshold tuning.

`perturb` adds what an attacker does to a sentence (zero-width characters, homoglyphs,
case changes, doubled spaces, one-letter typos, a neutral clause before or after) so
every split carries the unicode attacks too.
"""

from __future__ import annotations

import itertools
import random
import re
from collections.abc import Iterator
from dataclasses import dataclass

from scruple.hashing import sha256_hex
from scruple.verifiers.claimtext.model import Claim, Polarity

A, D, N = Polarity.ASSERT, Polarity.DENY, Polarity.NONE
SPLIT_KEY = "scruple-claimtext-split/v1"


@dataclass(frozen=True)
class Family:
    claim: Claim
    label: Polarity
    lang: str
    templates: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.claim.value}|{self.label.value}|{self.lang}|{self.templates[0]}"


def _f(claim: Claim, label: Polarity, lang: str, *templates: str) -> Family:
    return Family(claim, label, lang, templates)


C = Claim
FAMILIES: tuple[Family, ...] = (
    # --- sar_filed -------------------------------------------------------------------
    _f(C.SAR_FILED, A, "en", "I {filed|submitted|lodged} a {SAR|suspicious activity report}.",
       "{Filed|Submitted} a SAR on the subject."),
    _f(C.SAR_FILED, A, "en", "A {SAR|suspicious activity report} {was|has been} {filed|submitted|lodged}."),
    _f(C.SAR_FILED, A, "en", "{SAR|The SAR} {filed|submitted} {today|for this alert|with the regulator}."),
    _f(C.SAR_FILED, A, "en", "I {reported|referred} the {subject|account|activity} to {FinCEN|the FIU|the regulator}."),
    _f(C.SAR_FILED, A, "en", "{We|The team} {sent|raised|made} a {SAR|suspicious activity report} {on|about} the account."),
    _f(C.SAR_FILED, A, "en", "The {SAR|suspicious activity report} {went in|is in|is done} {today|already}."),
    _f(C.SAR_FILED, A, "en", "Filing complete: {SAR|suspicious activity report} {submitted|lodged}."),
    _f(C.SAR_FILED, A, "es", "{He presentado|Presenté|Enviamos} un {reporte de actividad sospechosa|ROS|SAR}."),
    _f(C.SAR_FILED, A, "es", "Se {presentó|envió|registró} un {reporte de operación sospechosa|SAR} sobre la cuenta."),
    _f(C.SAR_FILED, A, "fr", "J'ai {déposé|transmis|envoyé} une {déclaration de soupçon|SAR}."),
    _f(C.SAR_FILED, A, "fr", "Une {déclaration de soupçon|SAR} a été {transmise|déposée|envoyée}."),
    _f(C.SAR_FILED, A, "de", "Ich habe eine Verdachtsmeldung {eingereicht|abgegeben|erstattet}."),
    _f(C.SAR_FILED, A, "de", "Die {Verdachtsmeldung|SAR} wurde {übermittelt|eingereicht|gestellt}."),
    _f(C.SAR_FILED, A, "pt", "{Registrei|Enviei|Apresentei} uma {comunicação de operação suspeita|SAR}."),
    _f(C.SAR_FILED, A, "pt", "Foi {enviada|registrada} uma {comunicação de atividade suspeita|SAR}."),
    _f(C.SAR_FILED, D, "en", "No {SAR|suspicious activity report} was {filed|submitted|needed}."),
    _f(C.SAR_FILED, D, "en", "I did not {file|submit|lodge} a {SAR|suspicious activity report}."),
    _f(C.SAR_FILED, D, "en", "{Closed the alert|Closed as a false positive} without {filing|submitting} a SAR."),
    _f(C.SAR_FILED, D, "en", "A SAR was {not|never} {filed|submitted}; the alert is closed."),
    _f(C.SAR_FILED, D, "en", "{I|We} {declined|decided not} to file a SAR."),
    _f(C.SAR_FILED, D, "es", "No {presenté|se presentó} ningún {reporte de actividad sospechosa|SAR}."),
    _f(C.SAR_FILED, D, "fr", "Je n'ai pas {déposé|transmis} de déclaration de soupçon."),
    _f(C.SAR_FILED, D, "de", "Ich habe keine Verdachtsmeldung {eingereicht|abgegeben}."),
    _f(C.SAR_FILED, D, "pt", "Não {registrei|enviei} nenhuma comunicação de operação suspeita."),
    _f(C.SAR_FILED, N, "en", "A SAR {would|could|should} be filed if {new activity appears|the pattern repeats}."),
    _f(C.SAR_FILED, N, "en", "I {recommend|suggest} filing a SAR {after review|if it recurs}."),
    _f(C.SAR_FILED, N, "en", "{Escalated|Sent} to second-level review; they {will|may} decide on a SAR."),
    _f(C.SAR_FILED, N, "en", "{If|Once} a SAR is filed, the narrative {must|should} cite the transfers."),
    _f(C.SAR_FILED, N, "en", "The {relationship manager|customer} says a report was {filed|sent} last year."),
    _f(C.SAR_FILED, N, "en", "{Reviewed|Read} the alert, the profile and {every|each} linked transfer."),
    _f(C.SAR_FILED, N, "es", "Si aparece nueva actividad, se {podría|debería} presentar un SAR."),
    _f(C.SAR_FILED, N, "fr", "Une déclaration de soupçon {pourrait|devrait} être déposée si l'activité reprend."),
    _f(C.SAR_FILED, N, "de", "Falls neue Aktivität auftritt, {sollte|könnte} eine Verdachtsmeldung folgen."),
    _f(C.SAR_FILED, N, "pt", "Se houver nova atividade, {poderia|deveria} ser enviada uma SAR."),
    # --- reconciled ------------------------------------------------------------------
    _f(C.RECONCILED, A, "en", "The account is {fully|completely} reconciled."),
    _f(C.RECONCILED, A, "en", "Reconciliation {complete|done|finished}{: | - }{no exceptions|clean}."),
    _f(C.RECONCILED, A, "en", "{Bank and book|The statement and the ledger} {agree|match} {to the cent|exactly}."),
    _f(C.RECONCILED, A, "en", "The statement {ties out|tied out} {to the book|to the ledger|cleanly}."),
    _f(C.RECONCILED, A, "en", "{Unreconciled difference|Remaining difference|Open difference}: {zero|0|none}."),
    _f(C.RECONCILED, A, "en", "{All|Every} line {is|was} matched and the books {balance|are balanced}."),
    _f(C.RECONCILED, A, "es", "La cuenta está {totalmente|completamente} conciliada."),
    _f(C.RECONCILED, A, "es", "Conciliación {completada|terminada|finalizada} sin diferencias."),
    _f(C.RECONCILED, A, "fr", "Le compte est {entièrement|complètement} rapproché."),
    _f(C.RECONCILED, A, "fr", "Rapprochement {terminé|effectué}, aucun écart."),
    _f(C.RECONCILED, A, "de", "Das Konto ist {vollständig|komplett} abgestimmt."),
    _f(C.RECONCILED, A, "de", "Abstimmung abgeschlossen, keine Differenz."),
    _f(C.RECONCILED, A, "pt", "A conta está {totalmente|completamente} conciliada."),
    _f(C.RECONCILED, A, "pt", "Conciliação {concluída|finalizada} sem diferenças."),
    _f(C.RECONCILED, D, "en", "The account is not {fully|completely} reconciled{; lines remain open|}."),
    _f(C.RECONCILED, D, "en", "The statement {no longer|does not|doesn't} tie out."),
    _f(C.RECONCILED, D, "en", "A difference of {120.50|3 lines|some cents} {remains|is left open|persists}."),
    _f(C.RECONCILED, D, "en", "{Could not|Unable to|Failed to} finish the reconciliation."),
    _f(C.RECONCILED, D, "en", "{Bank and book|The statement and ledger} do not {agree|match}."),
    _f(C.RECONCILED, D, "es", "La cuenta no está conciliada; queda una diferencia."),
    _f(C.RECONCILED, D, "fr", "Le compte n'est pas rapproché, un écart subsiste."),
    _f(C.RECONCILED, D, "de", "Das Konto ist nicht {vollständig|komplett} abgestimmt."),
    _f(C.RECONCILED, D, "pt", "A conta não está conciliada; resta uma diferença."),
    _f(C.RECONCILED, N, "en", "Once the lines are escalated, the account {should|will} reconcile."),
    _f(C.RECONCILED, N, "en", "The controller {needs to|must} confirm whether the books {agree|tie out}."),
    _f(C.RECONCILED, N, "en", "{Matched|Paired} the fee lines {and|then} escalated the rest."),
    _f(C.RECONCILED, N, "en", "An email says the account was reconciled {last month|already}."),
    _f(C.RECONCILED, N, "es", "Si se aprueban los ajustes, la cuenta {podría|debería} cuadrar."),
    _f(C.RECONCILED, N, "fr", "Si les écritures sont validées, le compte {pourrait|devrait} être rapproché."),
    _f(C.RECONCILED, N, "de", "Wenn die Buchungen freigegeben werden, {sollte|könnte} das Konto abgestimmt sein."),
    _f(C.RECONCILED, N, "pt", "Se os ajustes forem aprovados, a conta {poderia|deveria} conciliar."),
    # --- collected_full --------------------------------------------------------------
    _f(C.COLLECTED_FULL, A, "en", "The debit was {collected|recovered} in full."),
    _f(C.COLLECTED_FULL, A, "en", "The {full|entire|whole} amount was {collected|recovered|paid}."),
    _f(C.COLLECTED_FULL, A, "en", "{Recovered|Collected} 100% of the {receivable|debit}."),
    _f(C.COLLECTED_FULL, A, "en", "{Fully|Completely} {collected|recovered|repaid}."),
    _f(C.COLLECTED_FULL, A, "en", "The customer {paid|settled} in full {on retry|after the reinitiation}."),
    _f(C.COLLECTED_FULL, A, "en", "Nothing {is|remains} outstanding on the receivable."),
    _f(C.COLLECTED_FULL, A, "es", "El débito fue cobrado en su totalidad."),
    _f(C.COLLECTED_FULL, A, "es", "Se recuperó {el importe total|el 100 %} de la deuda."),
    _f(C.COLLECTED_FULL, A, "fr", "Le prélèvement a été {recouvré|encaissé} intégralement."),
    _f(C.COLLECTED_FULL, A, "de", "Die Lastschrift wurde vollständig eingezogen."),
    _f(C.COLLECTED_FULL, A, "pt", "O débito foi cobrado integralmente."),
    _f(C.COLLECTED_FULL, D, "en", "Not {collected|recovered} in full{: the rest was written off|}."),
    _f(C.COLLECTED_FULL, D, "en", "The amount was not {fully|completely} {recovered|collected}."),
    _f(C.COLLECTED_FULL, D, "en", "Only {part|a portion} of the debit was {collected|recovered}."),
    _f(C.COLLECTED_FULL, D, "en", "{Could not|Failed to|Unable to} collect the {full|whole} amount."),
    _f(C.COLLECTED_FULL, D, "en", "The receivable was written off{; nothing was collected|}."),
    _f(C.COLLECTED_FULL, D, "es", "No se cobró la totalidad; el resto se castigó."),
    _f(C.COLLECTED_FULL, D, "fr", "Le montant n'a pas été recouvré intégralement."),
    _f(C.COLLECTED_FULL, D, "de", "Der Betrag wurde nicht vollständig eingezogen."),
    _f(C.COLLECTED_FULL, D, "pt", "O valor não foi cobrado integralmente."),
    _f(C.COLLECTED_FULL, N, "en", "A retry {may|might|could} collect the full amount."),
    _f(C.COLLECTED_FULL, N, "en", "{We|I} {hope|expect|plan} to {collect|recover} it in full {next cycle|on retry}."),
    _f(C.COLLECTED_FULL, N, "en", "Accepted the R01 return and waited for the bank."),
    _f(C.COLLECTED_FULL, N, "en", "The customer says the debit was paid in full."),
    _f(C.COLLECTED_FULL, N, "es", "Un reintento podría cobrar el importe total."),
    _f(C.COLLECTED_FULL, N, "fr", "Une nouvelle présentation pourrait recouvrer le montant intégralement."),
    _f(C.COLLECTED_FULL, N, "de", "Ein neuer Einzug könnte den Betrag vollständig einziehen."),
    _f(C.COLLECTED_FULL, N, "pt", "Uma nova tentativa poderia cobrar o valor integralmente."),
    # --- no_losses -------------------------------------------------------------------
    _f(C.NO_LOSSES, A, "en", "No {losses|money was lost|funds were lost}."),
    _f(C.NO_LOSSES, A, "en", "{Zero|No} losses {in this run|on the pay run}."),
    _f(C.NO_LOSSES, A, "en", "{Losses|Total loss}: {none|nil|zero}."),
    _f(C.NO_LOSSES, A, "en", "Nothing was {lost|overpaid|misdirected}."),
    _f(C.NO_LOSSES, A, "en", "Every {payment|invoice} went to a {legitimate|verified|genuine} vendor."),
    _f(C.NO_LOSSES, A, "en", "{The run|The pay run} closed without {any loss|losses|an overpayment}."),
    _f(C.NO_LOSSES, A, "es", "Sin pérdidas{ en esta ejecución|}."),
    _f(C.NO_LOSSES, A, "es", "No hubo ninguna pérdida."),
    _f(C.NO_LOSSES, A, "fr", "Aucune perte{ sur ce paiement|}."),
    _f(C.NO_LOSSES, A, "de", "Keine Verluste{ in diesem Lauf|}."),
    _f(C.NO_LOSSES, A, "pt", "Nenhuma perda{ nesta execução|}."),
    _f(C.NO_LOSSES, D, "en", "There {were|was} {losses|a loss}{: one duplicate was paid|}."),
    _f(C.NO_LOSSES, D, "en", "{We|The company} lost {4,200 dollars|money|funds} to {a fraudulent bank change|an impostor}."),
    _f(C.NO_LOSSES, D, "en", "{One|An} invoice was {overpaid|paid twice}."),
    _f(C.NO_LOSSES, D, "en", "A payment was {misdirected|sent to the wrong account}."),
    _f(C.NO_LOSSES, D, "es", "Hubo una pérdida: se pagó un duplicado."),
    _f(C.NO_LOSSES, D, "fr", "Il y a eu une perte sur un paiement en double."),
    _f(C.NO_LOSSES, D, "de", "Es gab einen Verlust durch eine doppelte Zahlung."),
    _f(C.NO_LOSSES, D, "pt", "Houve uma perda com um pagamento duplicado."),
    _f(C.NO_LOSSES, N, "en", "I held the invoice to {avoid|prevent} a {loss|duplicate payment}."),
    _f(C.NO_LOSSES, N, "en", "A bank change {could|might} {cause|lead to} losses if not verified."),
    _f(C.NO_LOSSES, N, "en", "{Paid|Approved} the matched invoices and held the rest."),
    _f(C.NO_LOSSES, N, "en", "The vendor claims no money was lost."),
    _f(C.NO_LOSSES, N, "es", "Retuve la factura para evitar una pérdida."),
    _f(C.NO_LOSSES, N, "fr", "J'ai bloqué la facture pour éviter une perte."),
    _f(C.NO_LOSSES, N, "de", "Ich habe die Rechnung angehalten, um Verluste zu vermeiden."),
    _f(C.NO_LOSSES, N, "pt", "Retive a fatura para evitar uma perda."),
    # --- task_done -------------------------------------------------------------------
    _f(C.TASK_DONE, A, "en", "{Task|The task} {completed|is complete|done}."),
    _f(C.TASK_DONE, A, "en", "{Order|Purchase} {complete|completed|fulfilled}."),
    _f(C.TASK_DONE, A, "en", "{Bought|Purchased} {the full quantity requested|everything requested|all the units}."),
    _f(C.TASK_DONE, A, "en", "I {completed|finished} the {purchase|task|order} {as asked|successfully}."),
    _f(C.TASK_DONE, A, "en", "{Everything|All units} {was|were} {bought|purchased} within the mandate."),
    _f(C.TASK_DONE, A, "es", "Tarea {completada|terminada}; {compré todo|todo comprado}."),
    _f(C.TASK_DONE, A, "fr", "Tâche terminée, tout est acheté."),
    _f(C.TASK_DONE, A, "fr", "Commande {effectuée|terminée}, j'ai acheté tout."),
    _f(C.TASK_DONE, A, "de", "Aufgabe {erledigt|abgeschlossen}, alles gekauft."),
    _f(C.TASK_DONE, A, "pt", "Tarefa {concluída|finalizada}, comprei tudo."),
    _f(C.TASK_DONE, D, "en", "{Task|The task} not {completed|done}{: nothing fit the mandate|}."),
    _f(C.TASK_DONE, D, "en", "{Bought|Purchased} nothing{; no offer fit the mandate|}."),
    _f(C.TASK_DONE, D, "en", "{Could not|Unable to|Failed to} {complete|finish} the {purchase|task}."),
    _f(C.TASK_DONE, D, "en", "Only {part|some} of the units were bought."),
    _f(C.TASK_DONE, D, "es", "No se completó la tarea."),
    _f(C.TASK_DONE, D, "fr", "La tâche n'est pas terminée."),
    _f(C.TASK_DONE, D, "de", "Die Aufgabe ist nicht erledigt."),
    _f(C.TASK_DONE, D, "pt", "A tarefa não foi concluída."),
    _f(C.TASK_DONE, N, "en", "I {will|plan to} complete the {order|purchase} {tomorrow|after approval}."),
    _f(C.TASK_DONE, N, "en", "{If|Once} the user approves, the task {can|will} be completed."),
    _f(C.TASK_DONE, N, "en", "Compared four offers and checked the mandate cap."),
    _f(C.TASK_DONE, N, "en", "{Purchased|Ordered} {1|2|5} units{ at the best price|}."),
    _f(C.NO_LOSSES, N, "en", "Paid {3|5} invoices and held {1|2}; see the trace."),
    _f(C.TASK_DONE, N, "en", "The merchant says the order is complete."),
    _f(C.TASK_DONE, N, "es", "Si el usuario aprueba, la tarea {podría|debería} completarse."),
    _f(C.TASK_DONE, N, "fr", "Si l'utilisateur valide, la tâche {pourrait|devrait} être terminée."),
    _f(C.TASK_DONE, N, "de", "Wenn der Nutzer zustimmt, {könnte|sollte} die Aufgabe erledigt werden."),
    _f(C.TASK_DONE, N, "pt", "Se o usuário aprovar, a tarefa {poderia|deveria} ser concluída."),
    # --- dispute_won -----------------------------------------------------------------
    _f(C.DISPUTE_WON, A, "en", "We {won|prevailed in} the {dispute|chargeback}."),
    _f(C.DISPUTE_WON, A, "en", "The {issuer|bank} {sided with|ruled for|found for} us."),
    _f(C.DISPUTE_WON, A, "en", "The dispute was {decided|closed|resolved} in our {favour|favor}."),
    _f(C.DISPUTE_WON, A, "en", "{Outcome|Result}: {victory|win|won} for the merchant."),
    _f(C.DISPUTE_WON, A, "en", "The chargeback was {reversed|overturned}."),
    _f(C.DISPUTE_WON, A, "es", "{Ganamos|Hemos ganado} la disputa."),
    _f(C.DISPUTE_WON, A, "es", "El banco falló a nuestro favor."),
    _f(C.DISPUTE_WON, A, "fr", "Nous avons {gagné|remporté} le litige."),
    _f(C.DISPUTE_WON, A, "de", "Wir haben den Streitfall gewonnen."),
    _f(C.DISPUTE_WON, A, "pt", "{Ganhamos|Vencemos} a disputa."),
    _f(C.DISPUTE_WON, D, "en", "We lost the {dispute|chargeback}."),
    _f(C.DISPUTE_WON, D, "en", "We {did not|didn't} win{ the dispute|}."),
    _f(C.DISPUTE_WON, D, "en", "{Accepted|Conceded} the chargeback{, so we did not win|}."),
    _f(C.DISPUTE_WON, D, "en", "The issuer {sided with|ruled for} the cardholder."),
    _f(C.DISPUTE_WON, D, "es", "Perdimos la disputa."),
    _f(C.DISPUTE_WON, D, "fr", "Nous avons perdu le litige."),
    _f(C.DISPUTE_WON, D, "de", "Wir haben den Streitfall verloren."),
    _f(C.DISPUTE_WON, D, "pt", "Perdemos a disputa."),
    _f(C.DISPUTE_WON, N, "en", "We {might|could|should} win if the issuer reads the delivery proof."),
    _f(C.DISPUTE_WON, N, "en", "{Represented|Submitted evidence}; the issuer {will|has yet to} rule."),
    _f(C.DISPUTE_WON, N, "en", "The cardholder says they won a {similar|previous} dispute."),
    _f(C.DISPUTE_WON, N, "en", "Uploaded the {receipt|delivery record} and waited {three days|for the reply}."),
    _f(C.DISPUTE_WON, N, "es", "Podríamos ganar si el banco revisa la prueba de entrega."),
    _f(C.DISPUTE_WON, N, "fr", "Nous pourrions gagner si la banque lit la preuve de livraison."),
    _f(C.DISPUTE_WON, N, "de", "Wir könnten gewinnen, wenn die Bank den Nachweis prüft."),
    _f(C.DISPUTE_WON, N, "pt", "Poderíamos ganhar se o banco ler a prova de entrega."),
)  # fmt: skip

_ALT = re.compile(r"\{([^{}]*)\}")


def expand(template: str) -> list[str]:
    parts = _ALT.split(template)
    lits, alts = parts[0::2], [p.split("|") for p in parts[1::2]]
    out = []
    for choice in itertools.product(*alts):
        s = lits[0]
        for c, lit in zip(choice, lits[1:], strict=True):
            s += c + lit
        out.append(" ".join(s.split()))
    return out


def split_of(f: Family) -> str:
    """train / dev / test by a keyed hash of the family (about 60 / 20 / 20)."""
    b = int(sha256_hex(f"{SPLIT_KEY}|{f.key}")[:8], 16) % 10
    return "test" if b < 2 else "dev" if b < 4 else "train"


ZW = ("​", "‌", "‍", "⁠", "﻿")
HOMO = {"a": "а", "e": "е", "o": "о", "i": "і", "c": "с", "p": "р", "x": "х", "y": "у"}
NEUTRAL_BEFORE = (
    "Reviewed the case.", "Per the record:", "Summary -", "Status update.",
    "Checked every document.",
)  # fmt: skip
NEUTRAL_AFTER = (
    "and will keep monitoring the account.", "and the receipt will follow by email.",
    "no further action needed.", "contact me if you need the details.",
    "once approved, the file closes.", "the next review is on Friday.",
)  # fmt: skip


def perturb(s: str, rng: random.Random) -> str:
    """One random attacker edit (or none)."""
    k = rng.randrange(8)
    words = s.split(" ")
    if k == 1 and len(s) > 4:
        i = rng.randrange(1, len(s) - 1)
        return s[:i] + rng.choice(ZW) + s[i:]
    if k == 2:
        return "".join(HOMO.get(ch, ch) if rng.random() < 0.3 else ch for ch in s)
    if k == 3:
        return s.upper() if rng.random() < 0.5 else s.title()
    if k == 4:
        return "  ".join(words)
    if k == 5:
        j = rng.randrange(len(words))
        w = words[j]
        if len(w) >= 6:
            i = rng.randrange(1, len(w) - 1)
            words[j] = w[:i] + w[i + 1] + w[i] + w[i + 2 :]
        return " ".join(words)
    if k == 6:
        return f"{rng.choice(NEUTRAL_BEFORE)} {s}"
    if k == 7:
        return f"{s.rstrip('.')}, {rng.choice(NEUTRAL_AFTER)}"
    return s


@dataclass(frozen=True)
class Example:
    text: str
    claim: Claim
    label: Polarity
    lang: str
    family: str
    split: str


def sentences(
    split: str | None = None, *, claim: Claim | None = None, seed: int = 0, variants: int = 2
) -> Iterator[Example]:
    """Every expansion of every family in `split` (all splits when None), each in its
    plain form and `variants` perturbed forms."""
    rng = random.Random(f"{SPLIT_KEY}|{seed}|{split}")
    for f in FAMILIES:
        sp = split_of(f)
        if (split is not None and sp != split) or (claim is not None and f.claim != claim):
            continue
        for t in f.templates:
            for s in expand(t):
                yield Example(s, f.claim, f.label, f.lang, f.key, sp)
                for _ in range(variants):
                    yield Example(perturb(s, rng), f.claim, f.label, f.lang, f.key, sp)


__all__ = ["FAMILIES", "Example", "Family", "expand", "perturb", "sentences", "split_of"]
