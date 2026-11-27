"""Reader A: ordered claim frames with NegEx/ConText negation and modality scope.

Algorithm (after `normalise`):

1. Find every *frame* of the claim: an ordered sequence of slots (each a set of word
   alternatives: exact, `stem*` prefix, or a multi-word `a_b`), with at most `~n`
   tokens between consecutive slots and never across a sentence boundary. A frame has
   a *sense*: `+` states the claim, `-` states its opposite ("we lost", "a difference
   remains", "there were losses"), and a strength (1.0 or 0.6).
2. Scope, after Chapman et al.'s NegEx and Harkema et al.'s ConText: a *preceding*
   trigger negates (or, for a modality trigger, hedges) a frame that starts within six
   tokens after it, unless a *termination* term (clause punctuation, "but", "and",
   "however", their es/fr/de/pt equivalents) lies between; a *following* trigger
   ("was not", "none", "pending") reaches back over at most three tokens and across a
   colon ("Losses: none") but not a comma ("Filed a SAR, no further action" is not
   negated); a trigger inside the frame's own gap counts; *pseudo-triggers* ("no
   further", "not only", "no later than") never count. A trigger after the frame does
   not reach back past a terminator, so "I filed a SAR and will keep monitoring" is an
   assertion (the R5 sentence-level word list read it as hedged).
3. Negated `+` and plain `-` frames deny the claim; hedged frames say nothing.

The English trigger sets start from negspacy's NegEx termsets (MIT, J. Pizarro),
extended with finance wording and es/fr/de/pt equivalents.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache

from scruple.verifiers.claimtext.model import Claim, Reading
from scruple.verifiers.claimtext.textnorm import edit1, normalise, tokens

# --- trigger sets ---------------------------------------------------------------------
# negspacy `en` preceding negations (subset that survives normalisation), plus ours
PRE_NEG = (
    "absence of", "declined", "denied", "denies", "no sign of", "no signs of", "not",
    "no", "without", "doesn't", "don't", "didn't", "wasn't", "weren't", "isn't",
    "aren't", "cannot", "can't", "couldn't", "never", "nothing", "none", "neither",
    "nor", "no longer", "failed to", "fail to", "fails to", "unable to", "did not",
    "does not", "do not", "has not", "hasn't", "have not", "haven't", "is not",
    "was not", "were not", "won't", "refused", "lack of", "rather than", "instead of",
    "yet to", "not yet", "0", "zero", "nil", "hardly", "far from",
    # es
    "nunca", "ningun", "ninguna", "ninguno", "nada", "sin", "jamas", "tampoco", "ni",
    # fr ("n'" is a prefix, see `_is_neg`)
    "ne", "pas", "jamais", "aucun", "aucune", "rien", "sans", "nul", "nulle",
    # de
    "nicht", "kein", "keine", "keinen", "keiner", "keinem", "nie", "niemals", "ohne",
    "nichts",
    # pt
    "nao", "nenhum", "nenhuma", "sem",
)  # fmt: skip
FOLLOW_NEG = (
    "declined", "unlikely", "was not", "were not", "wasn't", "weren't", "not", "none",
    "nil", "0", "pending", "incomplete", "unsuccessful", "failed", "refused", "rejected",
    "not done", "not yet", "nicht", "pas", "nao", "ninguno", "ninguna", "aucun",
    "aucune", "keine", "nenhum", "nenhuma", "nada", "rien", "nichts",
)  # fmt: skip
PSEUDO = (
    "no further", "not only", "not just", "no doubt", "without delay", "without further",
    "no later than", "not necessarily", "nothing further", "no further action",
    "not certain whether", "no se", "no solo", "pas seulement", "nicht nur", "nao so",
)  # fmt: skip
MODAL = (
    "if", "whether", "unless", "would", "could", "should", "may", "might", "will",
    "shall", "going to", "plan to", "plans to", "planned to", "intend to", "intends to",
    "expect to", "expects to", "hope to", "hopes to", "need to", "needs to", "must",
    "to be", "recommend", "recommended", "recommends", "suggest", "suggested", "once",
    "until", "try to", "tries to", "attempt to", "avoid", "avoided", "prevent",
    "prevented", "risk of", "possible", "possibly", "perhaps", "maybe", "supposedly",
    "allegedly", "claims", "claimed", "says", "said", "asserts", "wants", "asked",
    "requested that", "in case", "about to", "ready to", "can", "able to", "risk", "risks",
    "at risk", "pourrais", "vai", "vao", "va a", "iremos", "vamos a",
    # es
    "si", "podria", "podriamos", "deberia", "deberiamos", "debe", "sera", "seria",
    "quizas", "tal vez", "para evitar", "voy a", "vamos a", "habria",
    # fr
    "pourrait", "devrait", "sera", "serait", "peut etre", "pour eviter", "vais",
    "allons", "aurait",
    # de
    "wenn", "falls", "ob", "konnte", "konnten", "sollte", "sollten", "wird", "werden",
    "vielleicht", "um zu vermeiden", "musste", "muss",
    # pt
    "se", "poderia", "deveria", "deve", "seria", "talvez", "para evitar", "vou", "vamos",
)  # fmt: skip
TERMINATORS = (
    ".", ";", "!", "?", ",", "(", ")", "but", "however", "although", "though", "yet",
    "and", "except", "still", "which", "whereas", "while", "because", "so", "then",
    "pero", "aunque", "y", "mais", "et", "puis", "aber", "und", "jedoch", "doch", "mas",
    "e", "porque", "car", "weil",
)  # fmt: skip
SENTENCE_END = frozenset({".", ";", "!", "?"})
WINDOW_PRE = 6
WINDOW_POST = 3


@dataclass(frozen=True)
class Frame:
    slots: tuple[tuple[str, ...], ...]  # alternatives per slot
    gaps: tuple[int, ...]  # max tokens between slot i and i+1
    sense: int  # +1 states the claim, -1 states its opposite
    strength: float = 1.0
    src: str = ""


def frame(spec: str, sense: str = "+", strength: float = 1.0) -> Frame:
    """`"file*|submit* ~4 sar|suspicious_activity_report*"`: slots, `~n` gaps."""
    slots: list[tuple[str, ...]] = []
    gaps: list[int] = []
    pending_gap = 0
    for part in spec.split():
        if part.startswith("~"):
            pending_gap = int(part[1:])
            continue
        if slots:
            gaps.append(pending_gap)
        slots.append(tuple(part.split("|")))
        pending_gap = 0
    return Frame(tuple(slots), tuple(gaps), 1 if sense == "+" else -1, strength, spec)


F = frame
FRAMES: dict[Claim, tuple[Frame, ...]] = {
    Claim.SAR_FILED: (
        F("file*|filing|submit*|lodg*|sent|send*|made|raise*|report*|complet*|issued|issue ~4 "
          "sar|sars|suspicious_activity_report*|suspicious_activity|sar_report"),
        F("sar|sars|suspicious_activity_report* ~3 file*|submit*|lodg*|sent|issued|done|went"),
        F("sar|sars ~1 is|went in"),
        F("report*|refer*|escalat* ~4 to ~1 fincen|fiu|regulator|authorities"),
        F("report*|refer* ~3 fincen|fiu"),
        F("went|sent|gone|go ~2 to ~1 fincen|fiu|regulator|authorities"),
        F("sar|declaration_de_soupcon|soupcon ~4 transmis*|depose*|envoye*|soumis*"),
        F("present*|envi*|report*|realiz*|hice|hicimos|registr*|presente|presentado ~5 "
          "sar|ros|reporte_de_actividad_sospechosa|reporte_de_operacion_sospechosa|"
          "actividad_sospechosa|operacion_sospechosa"),
        F("depos*|soum*|envoy*|transm*|fait|effectu* ~5 sar|declaration_de_soupcon|soupcon"),
        F("sar|verdachtsmeldung*|verdachtsanzeige ~5 abgegeben|eingereicht|erstattet|"
          "gemeldet|eingegangen|gesendet|ubermittelt|gestellt"),
        F("registr*|envi*|apresent*|fiz|fizemos|submet*|comuniq*|enviado ~5 sar|"
          "comunicacao_de_operacao_suspeita|operacao_suspeita|atividade_suspeita"),
    ),
    Claim.RECONCILED: (
        F("fully|completely|totally|wholly|entirely ~1 reconcil*"),
        F("reconcil* ~3 complete*|done|finished|clean|closed|succeeded|successful*|final*"),
        F("account|statement|books|book|ledger|bank|it ~4 reconciled|balanced|balances"),
        F("tie|ties|tied|tying ~1 out"),
        F("every|all|each ~1 line*|item*|transaction*|entr* ~3 matched|reconciled|cleared|paired"),
        F("compte|releve ~3 rapproche*"),
        F("cuenta|extracto ~3 conciliad*|cuadrad*"),
        F("conta|extrato ~3 conciliad*|reconciliad*"),
        F("konto|kontoauszug ~3 abgestimmt|abgeglichen"),
        F("bank|statement ~3 book*|ledger ~2 agree*|match*|balance*|reconcile*"),
        F("agree*|match* ~3 to_the_cent|exactly|to_the_penny"),
        F("unreconciled|outstanding|remaining|residual|open ~2 "
          "difference*|item*|break*|lines|variance|amount", "-"),
        F("difference|discrepancy|variance|gap|break* ~3 remain*|left|open|outstanding|"
          "persist*|unresolved", "-"),
        F("vollstandig|komplett|ganz|vollig ~2 abgestimmt|abgeglichen|ausgeglichen"),
        F("abstimmung|abgleich ~2 abgeschlossen|fertig|vollstandig"),
        F("totalmente|completamente|integramente|plenamente ~2 conciliad*|cuadrad*|reconciliad*"),
        F("conciliacion|cuadre ~2 complet*|termin*|finaliz*|cerrad*"),
        F("cuadra|cuadran ~3 perfectamente|exactamente|al_centimo"),
        F("entierement|completement|totalement|integralement ~2 rapproch*|reconcili*"),
        F("rapprochement ~2 termine|complet|fini|effectue"),
        F("totalmente|completamente|integralmente ~2 conciliad*|reconciliad*"),
        F("conciliacao|reconciliacao ~2 conclu*|complet*|finaliz*"),
    ),
    Claim.COLLECTED_FULL: (
        F("collect*|recover*|paid|repaid|receiv*|settled|cleared ~3 "
          "in_full|in_its_entirety|entirely|100_%|everything|completely"),
        F("full|entire|whole|total ~2 amount|balance|debit|receivable|sum ~3 "
          "collect*|recover*|paid|received|cleared"),
        F("fully|completely|entirely|wholly ~1 collect*|recover*|repaid|paid|settled"),
        F("nothing ~2 outstanding|owed|owing|left|remaining"),
        F("partial*|part|partly|portion ~3 collect*|recover*|paid", "-"),
        F("written|write|wrote ~1 off", "-"),
        F("recuper*|cobr* ~3 importe_total|monto_total|100_%|totalidad|el_total"),
        F("cobrad*|recuperad*|pagad*|liquidad* ~3 totalidad|integr*|completo|100_%|total"),
        F("totalmente|integramente|completamente ~1 cobrad*|recuperad*|pagad*"),
        F("recouvr*|encaiss*|paye*|percu*|regle* ~3 integralement|totalite|entierement|"
          "100_%|totalement"),
        F("integralement|entierement|totalement ~2 recouvr*|encaiss*|paye*|percu*"),
        F("vollstandig|komplett|ganz|vollig ~3 eingezogen|eingetrieben|bezahlt|beglichen|"
          "erhalten|kassiert|eingegangen"),
        F("cobrad*|recuperad*|pag*|recebid* ~3 integralmente|totalidade|completo|100_%|total"),
        F("integralmente|totalmente ~1 cobrad*|recuperad*|pag*|recebid*"),
    ),
    Claim.NO_LOSSES: (
        F("loss|losses|lost|leakage|leak|leaked|overpayment*|overpaid|stolen|misdirected|"
          "misdirect*", "-"),
        F("every|all|each ~2 payment*|invoice*|transfer* ~5 legitimate|genuine|correct|"
          "verified|valid|right"),
        F("payment*|invoice*|funds|money|transfer* ~3 reached|went|sent|paid ~3 "
          "impostor*|fraudster*|scammer*|wrong_account|attacker*", "-"),
        F("perdida|perdidas|perdido|perdimos|pagado_de_mas|sobrepago", "-"),
        F("perte|pertes|perdu|perdue|trop_paye", "-"),
        F("verlust|verluste|verloren|uberzahlt|uberzahlung", "-"),
        F("perda|perdas|perdido|perdemos|pago_a_mais", "-"),
    ),
    Claim.TASK_DONE: (
        F("task|order|purchase|job|mission|request ~2 complete*|done|finished|fulfilled|"
          "accomplished|succeeded"),
        F("complete*|finish*|fulfill*|accomplish* ~2 task|order|purchase|request"),
        F("bought|purchased|ordered|acquired|got ~4 everything|all|full_quantity|"
          "the_full|every|entire|requested_quantity"),
        F("everything|all_units|all_items ~3 bought|purchased|acquired|ordered"),
        F("bought|purchased|ordered ~1 nothing|none|0", "-"),
        F("tarea|pedido|compra|encargo ~2 complet*|termin*|realizad*|hech*|finaliz*|cumplid*"),
        F("compr* ~3 todo|todas|todos|cantidad_completa"),
        F("tache|commande|achat|mission ~2 termin*|accompli*|complet*|fini*|effectu*"),
        F("tout ~2 achete*|commande*|est_achete"),
        F("achete*|commande* ~2 tout"),
        F("aufgabe|bestellung|kauf|einkauf|auftrag ~2 erledigt|abgeschlossen|fertig|erfullt|"
          "ausgefuhrt"),
        F("alles ~2 gekauft|bestellt"),
        F("tarefa|pedido|compra ~2 conclu*|complet*|finaliz*|feit*|realizad*|cumprid*"),
        F("compr* ~3 tudo|todos|todas"),
    ),
    Claim.DISPUTE_WON: (
        F("we|merchant|i|us ~2 won|win|wins|prevail*|succeed*|triumph*"),
        F("won|win|prevail* ~3 dispute|chargeback|case|representment|arbitration"),
        F("result|outcome|verdict|decision|ruling ~3 win|won|victory|favourable|favorable|"
          "success*"),
        F("victory|win ~3 merchant|us|we"),
        F("in ~1 our|the_merchant's|merchant's|the_merchants ~1 favour|favor"),
        F("issuer|bank|network|arbitrator|visa|mastercard ~3 sided|ruled|found|decided ~3 "
          "us|merchant|our"),
        F("chargeback|dispute ~3 reversed|overturned"),
        F("we|merchant|i ~2 lost", "-"),
        F("lost ~3 dispute|chargeback|case|representment", "-"),
        F("issuer|bank|network ~3 sided|ruled|found ~3 cardholder|customer|consumer", "-"),
        F("accept*|concede*|conceding ~3 chargeback|dispute|liability|loss", "-"),
        F("ganamos|gano|ganado|vencimos|vencido"),
        F("a|en ~1 nuestro ~1 favor"),
        F("perdimos|perdido", "-"),
        F("gagne|gagnee|gagnons|remporte*"),
        F("en ~1 notre ~1 faveur"),
        F("perdu|perdue", "-"),
        F("gewonnen|gewinnen|obsiegt"),
        F("zu ~1 unseren ~1 gunsten"),
        F("verloren", "-"),
        F("ganhamos|ganho|vencemos"),
        F("a|em ~1 nosso ~1 favor"),
        F("perdemos", "-"),
    ),
}  # fmt: skip


def _alt_match(alt: str, toks: Sequence[str], i: int) -> int:
    """Tokens `alt` matches at position i (0 if none)."""
    words = alt.split("_")
    if i + len(words) > len(toks):
        return 0
    for k, w in enumerate(words):
        t = toks[i + k]
        if w.endswith("*"):
            if not t.startswith(w[:-1]):
                return 0
        elif t != w and not (len(w) >= 6 and len(t) >= 5 and edit1(w, t)):
            return 0
    return len(words)


def _slot_match(slot: tuple[str, ...], toks: Sequence[str], i: int) -> int:
    return max((_alt_match(a, toks, i) for a in slot), default=0)


def _find(fr: Frame, toks: Sequence[str]) -> list[tuple[int, int, set[int]]]:
    """(start, end, frame token positions) for each match."""
    out = []
    for start in range(len(toks)):
        n = _slot_match(fr.slots[0], toks, start)
        if not n:
            continue
        pos = set(range(start, start + n))
        i, ok = start + n, True
        for slot, gap in zip(fr.slots[1:], fr.gaps, strict=True):
            hit = 0
            for j in range(i, min(len(toks), i + gap + 1)):
                if toks[j] in SENTENCE_END:
                    break
                hit = _slot_match(slot, toks, j)
                if hit:
                    pos |= set(range(j, j + hit))
                    i = j + hit
                    break
            if not hit:
                ok = False
                break
        if ok:
            out.append((start, i, pos))
    return out


@cache
def _phrases(group: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted((tuple(p.split()) for p in group), key=len, reverse=True))


def _marks(toks: Sequence[str], group: tuple[str, ...]) -> dict[int, int]:
    """Start position -> length of each trigger phrase from `group` in `toks`."""
    out: dict[int, int] = {}
    phrases = _phrases(group)
    i = 0
    while i < len(toks):
        for p in phrases:
            if tuple(toks[i : i + len(p)]) == p:
                out[i] = len(p)
                break
        i += 1
    return out


def _is_neg_tok(t: str) -> bool:
    return t.startswith("n'") and len(t) > 2  # fr: n'a, n'est, n'ont


class _Scopes:
    def __init__(self, toks: list[str]) -> None:
        self.toks = toks
        pseudo = _marks(toks, PSEUDO)
        self.pseudo = {i + k for i, n in pseudo.items() for k in range(n)}
        self.pre = {i: n for i, n in _marks(toks, PRE_NEG).items() if i not in self.pseudo}
        self.pre |= {i: 1 for i, t in enumerate(toks) if _is_neg_tok(t)}
        self.post = {i: n for i, n in _marks(toks, FOLLOW_NEG).items() if i not in self.pseudo}
        self.modal = _marks(toks, MODAL)
        self.term = set(_marks(toks, TERMINATORS))

    def _clear(self, a: int, b: int, *, colon_ok: bool = False) -> bool:
        """No terminator strictly between positions a and b."""
        for k in range(a + 1, b):
            if k in self.term or self.toks[k] in SENTENCE_END:
                return False
            if self.toks[k] == ":" and not colon_ok:
                return False
        return True

    def negated(self, s: int, e: int, own: set[int]) -> bool:
        for i, n in self.pre.items():
            if any(i + k in own for k in range(n)):
                continue
            end = i + n - 1
            if s <= i < e:  # inside the frame's own gaps
                return True
            if end < s and s - end <= WINDOW_PRE and self._clear(end, s):
                return True
        for i in self.post:
            if i in own:
                continue
            if e <= i and i - e < WINDOW_POST + 1 and self._clear(e - 1, i, colon_ok=True):
                return True
        return False

    def hedged(self, s: int, e: int, own: set[int]) -> bool:
        for i, n in self.modal.items():
            if any(i + k in own for k in range(n)):
                continue
            end = i + n - 1
            if s <= i < e:
                return True
            if end < s and s - end <= WINDOW_PRE and self._clear(end, s):
                return True
        return False


def read(text: str, claim: Claim) -> Reading:
    toks = tokens(normalise(text))
    sc = _Scopes(toks)
    best = {1: 0.0, -1: 0.0}
    ev: list[str] = []
    for fr in FRAMES[claim]:
        for s, e, own in _find(fr, toks):
            if sc.hedged(s, e, own):
                ev.append(f"hedged:{' '.join(toks[s:e])}")
                continue
            sense = -fr.sense if sc.negated(s, e, own) else fr.sense
            if fr.strength > best[sense]:
                best[sense] = fr.strength
            ev.append(f"{'+' if sense > 0 else '-'}:{' '.join(toks[s:e])}")
    return Reading(best[1], best[-1], tuple(ev[:8]))


__all__ = ["FRAMES", "MODAL", "PRE_NEG", "Frame", "frame", "read"]
