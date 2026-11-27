"""Reader B: a hand-written chunk grammar over clauses (concept co-occurrence).

Independent of reader A by construction: no ordered frames and no trigger windows.

1. Split the normalised text into clauses (sentence punctuation, commas, colons
   followed by a new predicate, and coordinating conjunctions).
2. Tag each token with *concept classes* from a stem lexicon (en/es/fr/de/pt;
   typo-tolerant for long stems). A claim fires in a clause when one of its *rules*
   holds: a set of classes that must all occur in the clause, in any order ("SAR" +
   "FILE" in "the suspicious activity report has been submitted", "AGREE" + "BANK" in
   "bank and book agree").
3. Polarity is the clause's own: the parity of negation tokens before the last token of
   the rule (double negation cancels), plus a post-negation tag anywhere after it;
   modality (if, would, plan to, recommend, ...) before the first rule token, or a
   reporting verb with a third-party subject ("the cardholder says"), makes the
   clause say nothing. A rule may be *inverted* (it states the opposite: "LOSS" alone,
   "LOST" + "WE").

Clause polarity, not sentence polarity, is what fixes the R5 failure: in "I filed a
SAR and will keep monitoring" the modal sits in a second clause.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from scruple.verifiers.claimtext.model import Claim, Reading
from scruple.verifiers.claimtext.textnorm import edit1, normalise, tokens

NEG = frozenset(
    [
        "no",
        "not",
        "never",
        "nothing",
        "none",
        "neither",
        "nor",
        "without",
        "cannot",
        "nunca",
        "ningun",
        "ninguna",
        "ninguno",
        "nada",
        "sin",
        "jamas",
        "tampoco",
        "ne",
        "pas",
        "jamais",
        "aucun",
        "aucune",
        "rien",
        "sans",
        "nicht",
        "kein",
        "keine",
        "keinen",
        "keiner",
        "keinem",
        "nie",
        "niemals",
        "ohne",
        "nichts",
        "nao",
        "nenhum",
        "nenhuma",
        "sem",
        "0",
        "zero",
        "nil",
    ]
)
NEG_SUFFIX = ("n't",)
POST_NEG = frozenset(
    [
        "none",
        "nil",
        "0",
        "zero",
        "pending",
        "incomplete",
        "unsuccessful",
        "failed",
        "refused",
        "rejected",
    ]
)
PSEUDO_AFTER = {"no": {"further", "later", "doubt"}, "not": {"only", "just", "necessarily"}}
MODAL = frozenset(
    [
        "if",
        "whether",
        "unless",
        "would",
        "could",
        "should",
        "may",
        "might",
        "will",
        "shall",
        "must",
        "once",
        "until",
        "recommend",
        "recommended",
        "recommends",
        "suggest",
        "suggested",
        "plan",
        "plans",
        "planned",
        "intend",
        "intends",
        "expect",
        "expects",
        "hope",
        "hopes",
        "need",
        "needs",
        "avoid",
        "avoided",
        "prevent",
        "prevented",
        "possibly",
        "perhaps",
        "maybe",
        "supposedly",
        "allegedly",
        "can",
        "try",
        "tries",
        "attempt",
        "risk",
        "risks",
        "aurait",
        "pourrais",
        "va",
        "vai",
        "vao",
        "si",
        "podria",
        "deberia",
        "debe",
        "sera",
        "seria",
        "quizas",
        "pourrait",
        "devrait",
        "serait",
        "wenn",
        "falls",
        "ob",
        "konnte",
        "sollte",
        "wird",
        "werden",
        "vielleicht",
        "poderia",
        "deveria",
        "seria",
        "talvez",
    ]
)
REPORTING = frozenset(
    ["says", "said", "claims", "claimed", "asserts", "alleges", "states", "stated", "told"]
)
THIRD_PARTY = frozenset(
    [
        "cardholder",
        "customer",
        "consumer",
        "vendor",
        "supplier",
        "merchant",
        "email",
        "caller",
        "sender",
        "relationship",
        "manager",
        "he",
        "she",
        "they",
    ]
)
CLAUSE_BREAK = frozenset(
    [
        ".",
        ";",
        "!",
        "?",
        ",",
        "(",
        ")",
        "but",
        "however",
        "although",
        "though",
        "yet",
        "and",
        "while",
        "whereas",
        "because",
        "so",
        "then",
        "pero",
        "aunque",
        "y",
        "mais",
        "et",
        "puis",
        "aber",
        "und",
        "jedoch",
        "doch",
        "mas",
        "e",
        "porque",
        "car",
        "weil",
    ]
)

# concept classes: stem* = prefix, plain = exact (edit distance 1 for >= 6 letters)
LEX: dict[str, tuple[str, ...]] = {
    "FILE": (
        "file*", "filing", "submit*", "lodg*", "sent", "send*", "issued", "raise*",
        "made", "present*", "envi*", "envoy*", "realiz*", "registr*", "depos*", "soumi*",
        "transmi*", "abgegeben", "eingereicht", "erstattet", "gestellt", "ubermittelt",
        "apresent*", "submet*", "comuniq*", "hice", "fiz", "fizemos", "effectu*",
    ),
    "SAR": (
        "sar", "sars", "str", "suspicious", "sospechosa", "soupcon", "verdachtsmeldung*",
        "verdachtsanzeige", "suspeita", "ros",
    ),
    "REGULATOR": ("fincen", "fiu", "regulator", "authorities", "uif", "tracfin", "coaf"),
    "REPORT_V": ("report*", "refer*", "notif*", "escalat*", "denunci*", "signal*", "meld*"),
    "FULL": (
        "fully", "full", "completely", "entirely", "entire", "wholly", "whole", "total",
        "totally", "100", "everything", "integr*", "totalidad*", "totalite", "totalmente",
        "entierement", "integralement", "vollstandig", "komplett", "vollig", "ganz",
        "integralmente", "completo", "completa", "plenamente",
    ),
    "RECONCILE": (
        "reconcil*", "conciliad*", "conciliacion", "conciliacao", "rapproch*",
        "abgestimmt", "abgeglichen", "abstimmung", "abgleich", "reconciliad*",
        "reconciliacao", "cuadra*",
    ),
    "DONE": (
        "complete*", "completed", "done", "finished", "finish*", "closed", "clean",
        "succeeded", "successful*", "final*", "termin*", "finaliz*", "abgeschlossen",
        "fertig", "erledigt", "conclu*", "accompli*", "fulfill*", "fulfilled", "hecho",
        "hecha", "realizad*", "feito", "feita", "cumplid*", "cumprid*", "fini", "finie",
        "erfullt", "ausgefuhrt", "effectue*",
    ),
    "TIE": ("tie", "ties", "tied", "tying"),
    "OUT": ("out",),
    "BANK": ("bank", "statement", "extracto", "releve", "kontoauszug", "extrato"),
    "BOOK": ("book", "books", "ledger", "libro", "livre", "grand", "hauptbuch", "razao"),
    "AGREE": ("agree*", "match*", "coincid*", "concord*", "ubereinstimm*", "batem", "cuadran"),
    "DIFF": (
        "difference*", "discrepanc*", "variance*", "gap", "break*", "residual",
        "diferencia*", "ecart*", "differenz*", "diferenca*",
    ),
    "OPEN": (
        "remain*", "left", "open", "outstanding", "persist*", "unresolved", "unreconciled",
        "pendiente*", "restant*", "offen", "pendente*",
    ),
    "COLLECT": (
        "collect*", "recover*", "repaid", "paid", "receiv*", "settled", "cobr*",
        "recuper*", "pagad*", "recouvr*", "encaiss*", "paye*", "percu*", "eingezogen",
        "eingetrieben", "bezahlt", "beglichen", "kassiert", "recebid*", "pago", "paga",
    ),
    "AMOUNT": ("amount", "balance", "debit", "receivable", "sum", "monto", "montant",
               "betrag", "valor", "importe"),
    "PARTIAL": ("partial*", "partly", "part", "portion", "parcial*", "partiel*", "teilweise"),
    "WRITEOFF": ("written", "write", "wrote", "castigad*", "passe", "abgeschrieben", "baixad*"),
    "OFF": ("off", "perte*", "pertes"),
    "LOSS": (
        "loss", "losses", "lost", "leakage", "leak", "leaked", "overpayment*", "overpaid",
        "stolen", "misdirect*", "perdida*", "perdido", "perdimos", "perte", "pertes",
        "perdu", "perdue", "verlust*", "verloren", "uberzahl*", "perda*", "perdemos",
    ),
    "EVERY": ("every", "all", "each", "todos", "todas", "tous", "toutes", "alle", "cada"),
    "PAYMENT": ("payment*", "invoice*", "transfer*", "pago*", "paiement*", "zahlung*",
                "pagamento*", "factura*", "facture*", "rechnung*", "fatura*"),
    "LEGIT": ("legitimate", "genuine", "correct", "verified", "valid", "legitim*", "correct*",
              "korrekt*", "verificad*", "verifie*"),
    "TASK": ("task", "order", "purchase", "job", "mission", "request", "tarea", "pedido",
             "compra", "encargo", "tache", "commande", "achat", "aufgabe", "bestellung",
             "kauf", "einkauf", "auftrag", "tarefa"),
    "BOUGHT": ("bought", "purchased", "ordered", "acquired", "compr*", "achete*",
               "commande*", "gekauft", "bestellt", "adquiri*"),
    "ALL": ("everything", "all", "every", "entire", "todo", "todos", "todas", "tout",
            "alles", "tudo", "quantity", "cantidad", "quantite", "menge", "quantidade"),
    "NOTHING": ("nothing",),
    "WIN": ("won", "win", "wins", "prevail*", "succeeded", "triumph*", "victory",
            "ganamos", "gano", "ganado", "vencimos", "gagne*", "remporte*", "gewonnen",
            "gewinnen", "obsiegt", "ganhamos", "ganho", "vencemos", "reversed", "overturned"),
    "LOSE": ("lost", "lose", "loses", "perdimos", "perdu", "perdue", "verloren", "perdemos"),
    "RULE_FOR": ("ruled", "found", "decided", "sided", "fallo", "decidio"),
    "FOR": ("for", "with", "por", "pour", "fur", "para"),
    "GO": ("went", "sent", "gone", "forwarded", "submitted"),
    "CONCEDE": ("accept*", "conced*", "conceding", "acept*", "accepte*", "akzeptiert"),
    "FAVOUR": ("favour", "favor", "favorable", "favourable", "faveur", "gunsten", "sided"),
    "WE": ("we", "us", "our", "merchant", "merchant's", "i", "nuestro", "nuestra",
           "nosotros", "notre", "nous", "unseren", "unser", "wir", "nosso", "nossa", "nos"),
    "DISPUTE": ("dispute", "chargeback", "case", "representment", "arbitration", "disputa",
                "litige", "streitfall", "contestacion", "contestacao"),
    "CARDHOLDER": ("cardholder", "customer", "consumer", "titular", "porteur", "karteninhaber"),
}  # fmt: skip


@dataclass(frozen=True)
class Rule:
    classes: tuple[str, ...]
    inverted: bool = False
    strength: float = 1.0
    forbid: tuple[str, ...] = ()  # classes that must not occur in the clause


R = Rule
RULES: dict[Claim, tuple[Rule, ...]] = {
    Claim.SAR_FILED: (
        R(("FILE", "SAR")),
        R(("REPORT_V", "REGULATOR")),
        R(("GO", "REGULATOR")),
        R(("SAR", "DONE")),
    ),
    Claim.RECONCILED: (
        R(("RECONCILE",), forbid=("OPEN",)),
        R(("TIE", "OUT")),
        R(("BANK", "BOOK", "AGREE")),
        R(("DIFF", "OPEN"), inverted=True),
        R(("DIFF",), inverted=True, strength=0.6, forbid=("OPEN",)),
    ),
    Claim.COLLECTED_FULL: (
        R(("COLLECT", "FULL")),
        R(("AMOUNT", "FULL", "COLLECT")),
        R(("PARTIAL", "COLLECT"), inverted=True),
        R(("WRITEOFF", "OFF"), inverted=True),
    ),
    Claim.NO_LOSSES: (
        R(("LOSS",), inverted=True),
        R(("EVERY", "PAYMENT", "LEGIT")),
    ),
    Claim.TASK_DONE: (
        R(("TASK", "DONE")),
        R(("BOUGHT", "ALL"), forbid=("NOTHING",)),
        R(("BOUGHT", "NOTHING"), inverted=True),
    ),
    Claim.DISPUTE_WON: (
        R(("WIN",), forbid=("CARDHOLDER",)),
        R(("FAVOUR", "WE")),
        R(("RULE_FOR", "FOR", "WE")),
        R(("CONCEDE", "DISPUTE"), inverted=True),
        R(("LOSE", "WE"), inverted=True),
        R(("LOSE", "DISPUTE"), inverted=True),
        R(("FAVOUR", "CARDHOLDER"), inverted=True),
    ),
}


def _tag(tok: str) -> set[str]:
    out = set()
    for cls, stems in LEX.items():
        for st in stems:
            if st.endswith("*"):
                if tok.startswith(st[:-1]) and len(tok) >= len(st) - 1:
                    out.add(cls)
                    break
            elif tok == st or (len(st) >= 6 and len(tok) >= 5 and edit1(st, tok)):
                out.add(cls)
                break
    return out


CONJ = frozenset(["and", "y", "et", "und", "e"])
DET = frozenset(
    ["the", "a", "an", "la", "le", "el", "los", "las", "les", "der", "die", "das", "den", "o", "os"]
)


def _joins_nouns(toks: Sequence[str], i: int) -> bool:
    """A conjunction between noun phrases ("bank and book agree", "il y a") does not
    start a new clause: the next content word is itself a concept, or it is fr "y a"."""
    if toks[i] == "y" and i + 1 < len(toks) and toks[i + 1] in {"a", "avoir", "aurait", "avait"}:
        return True
    j = i + 1
    if j < len(toks) and toks[j] in DET:
        j += 1
    return j < len(toks) and bool(_tag(toks[j]) & {"BANK", "BOOK", "TASK", "PAYMENT", "AMOUNT"})


def clauses(toks: Sequence[str]) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for i, t in enumerate(toks):
        # a colon starts a new clause unless what follows is a bare quantifier
        # ("Losses: none", "Difference: 0")
        colon_break = t == ":" and not (
            i + 1 < len(toks)
            and toks[i + 1] in POST_NEG
            and (i + 2 >= len(toks) or toks[i + 2] in CLAUSE_BREAK)
        )
        if colon_break or (t in CLAUSE_BREAK and not (t in CONJ and _joins_nouns(toks, i))):
            if out[-1]:
                out.append([])
            continue
        out[-1].append(t)
    return [c for c in out if c]


def _neg(tok: str, nxt: str | None) -> bool:
    if tok in NEG:
        return nxt not in PSEUDO_AFTER.get(tok, set())
    return tok.endswith(NEG_SUFFIX) or (tok.startswith("n'") and len(tok) > 2)


def _read_clause(cl: list[str], claim: Claim) -> list[tuple[int, float, str]]:
    tags = [_tag(t) for t in cl]
    present = set().union(*tags) if tags else set()
    out: list[tuple[int, float, str]] = []
    for rule in RULES[claim]:
        if not set(rule.classes) <= present or set(rule.forbid) & present:
            continue
        pos = [i for i, tg in enumerate(tags) if tg & set(rule.classes)]
        first, last = pos[0], pos[-1]
        rule_pos = set(pos)
        if any(cl[i] in MODAL for i in range(last) if i not in rule_pos):
            out.append((0, 0.0, "modal"))
            continue
        if any(cl[i] in REPORTING for i in range(len(cl))) and any(
            cl[i] in THIRD_PARTY for i in range(first)
        ):
            out.append((0, 0.0, "reported speech"))
            continue
        # a token tagged as part of the rule (e.g. "nothing" in BOUGHT+NOTHING) is not
        # also counted as a negation
        negs = sum(
            1
            for i in range(last + 1)
            if i not in rule_pos and _neg(cl[i], cl[i + 1] if i + 1 < len(cl) else None)
        )
        negs += sum(1 for i in range(last + 1, len(cl)) if cl[i] in POST_NEG)
        # negative concord (es/fr/pt "no ... ningun", "ne ... pas", "nao ... nenhuma"):
        # several negation words in one clause are one negation
        sign = -1 if negs else 1
        if rule.inverted:
            sign = -sign
        out.append((sign, rule.strength, " ".join(cl)))
    return out


def read(text: str, claim: Claim) -> Reading:
    toks = tokens(normalise(text))
    best = {1: 0.0, -1: 0.0}
    ev: list[str] = []
    for cl in clauses(toks):
        for sign, strength, why in _read_clause(cl, claim):
            if sign and strength > best[sign]:
                best[sign] = strength
            ev.append(f"{'+' if sign > 0 else '-' if sign < 0 else '0'}:{why}")
    return Reading(best[1], best[-1], tuple(ev[:8]))


__all__ = ["LEX", "RULES", "clauses", "read"]
