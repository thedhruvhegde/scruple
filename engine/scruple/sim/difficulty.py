"""One difficulty knob for the dispute generator: ambiguity and rarity (roadmap 032).

`difficulty_mix(d)` maps `d` in [0, 1] to a `CaseMix`. Two things move together:

- ambiguity: how much the evidence a merchant holds says about who is right. At
  `d = 0` a merchant in the right holds every required document and one facing a
  valid claim holds none, so the documents decide the case. As `d` rises the merchant
  in the right loses more documents (`p_required_held_invalid` falls after 0.5,
  `p_missing_evidence` rises), the merchant facing a valid claim holds more
  (`p_required_held_valid`, `p_supporting_held_valid` rise) and forges more
  (`p_forged_evidence`). The two coverage distributions converge but never cross:
  the effective required-holding gap is `1.0, 0.65, 0.06` at `d = 0, 0.5, 1`.
- rarity: how thin the tail of case families is. Family weights are tempered,
  `w_f ** (2 d)`: uniform at `d = 0`, the suggested `FAMILY_WEIGHTS` at `d = 0.5`,
  their squares at `d = 1` (rare families get rarer, so a held-out set needs more
  cases to certify them; see `scruple.sim.splits.heldout_size`).

`difficulty_mix(0.5)` is exactly `FULL_MIX`. Every other feature rate stays at its
`FULL_MIX` value. The schedule is ours and synthetic (ASSUMPTION A-30).

`measure(cases)` reports the metrics the tests use to show the knob is monotone:

- `coverage_overlap`: overlap coefficient `sum_b min(P(b | valid), P(b | invalid))`
  of required-evidence coverage `b` (share of the reason code's required kinds held
  as genuine-looking documents that do not contradict the cardholder). 0: evidence
  separates the classes; 1: it says nothing.
- `bayes_error`: the error of the best classifier of `claim_valid` that sees only
  (category, coverage), on this sample: `sum_s min(n_valid(s), n_invalid(s)) / n`.
  Both ambiguity metrics use only informative cases: reason codes whose required
  evidence includes a kind not every merchant holds (for 11.x, 12.5 or 4808 the
  receipt and authorization record are always held, so coverage says nothing at any
  difficulty).
- `rarest_share`, `family_entropy` (normalised to [0, 1]), `rare_families` (families
  under 5% of cases): the rarity side.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from scruple.sim.disputes import ALWAYS_HELD, FULL_MIX, CardDispute, CaseMix
from scruple.sim.evidence import document_problems
from scruple.sim.families import FAMILY_WEIGHTS, CaseFamily

RARE_SHARE = 0.05


def _check(d: float) -> float:
    if not (0.0 <= d <= 1.0) or math.isnan(d):
        raise ValueError(f"difficulty must be in [0, 1], got {d}")
    return float(d)


def family_weights(d: float) -> dict[CaseFamily, float]:
    """Tempered family weights `w ** (2 d)` (unnormalised; `random.choices` accepts that)."""
    e = 2.0 * _check(d)
    return {f: (w if e == 1.0 else w**e) for f, w in FAMILY_WEIGHTS.items()}


def family_shares(d: float) -> dict[CaseFamily, float]:
    """Expected share of each family at difficulty `d` (normalised weights)."""
    w = family_weights(d)
    total = sum(w.values())
    return {f: v / total for f, v in w.items()}


def difficulty_mix(d: float, base: CaseMix = FULL_MIX) -> CaseMix:
    """The generator mix for difficulty `d`. `difficulty_mix(0.5) == FULL_MIX`."""
    d = _check(d)
    late = max(0.0, 2.0 * d - 1.0)  # 0 up to d = 0.5, then rises to 1
    return base.model_copy(
        update={
            "family_weights": family_weights(d),
            "p_required_held_invalid": 1.0 - 0.3 * late,
            "p_missing_evidence": 0.2 * d,
            "p_required_held_valid": 0.5 * d,
            "p_supporting_held_invalid": 0.6 - 0.2 * late,
            "p_supporting_held_valid": 0.4 * d,
            "p_forged_evidence": 0.4 * d,
        }
    )


def required_holding_gap(d: float) -> float:
    """Expected P(required kind held | merchant right) - P(held | claim valid), where a
    lost document counts as not held. Strictly decreasing in `d`, positive at 1."""
    m = difficulty_mix(d)
    return m.p_required_held_invalid * (1 - m.p_missing_evidence) - m.p_required_held_valid


class DifficultyMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    n_informative: int  # cases whose reason code needs a document not always held
    coverage_overlap: float = Field(ge=0, le=1)
    bayes_error: float = Field(ge=0, le=0.5)
    rarest_share: float
    family_entropy: float
    rare_families: int
    family_counts: dict[CaseFamily, int]


def coverage(case: CardDispute) -> float:
    """Share of the reason code's required kinds held as clean, consistent documents."""
    held = {
        d.kind
        for d in case.documents
        if not document_problems(d, case.captured_minor, case.currency, case.processed_on)
        and d.fields.get("matches_cardholder") != "N"
    }
    req = case.spec.required_evidence
    return sum(1 for k in req if k in held) / len(req)


def informative(case: CardDispute) -> bool:
    """Whether evidence can say anything here: some required kind is not always held."""
    return any(k not in ALWAYS_HELD for k in case.spec.required_evidence)


def measure(cases: Sequence[CardDispute]) -> DifficultyMetrics:
    if not cases:
        raise ValueError("no cases to measure")
    n = len(cases)
    by_class: dict[bool, Counter[float]] = {True: Counter(), False: Counter()}
    by_sig: dict[tuple[str, float], Counter[bool]] = {}
    fams: Counter[CaseFamily] = Counter()
    for c in cases:
        fams[c.truth.family] += 1
        if not informative(c):
            continue
        cov = round(coverage(c), 6)
        v = c.truth.claim_valid
        by_class[v][cov] += 1
        by_sig.setdefault((c.category.value, cov), Counter())[v] += 1
    nv, ni = sum(by_class[True].values()), sum(by_class[False].values())
    if nv and ni:
        buckets = set(by_class[True]) | set(by_class[False])
        overlap = sum(min(by_class[True][b] / nv, by_class[False][b] / ni) for b in buckets)
    else:
        overlap = 0.0
    m = nv + ni
    bayes = sum(min(cnt[True], cnt[False]) for cnt in by_sig.values()) / m if m else 0.0
    counts = {f: fams.get(f, 0) for f in CaseFamily}
    shares = [k / n for k in counts.values()]
    entropy = -sum(p * math.log(p) for p in shares if p > 0) / math.log(len(shares))
    return DifficultyMetrics(
        n=n,
        n_informative=m,
        coverage_overlap=min(1.0, overlap),
        bayes_error=bayes,
        rarest_share=min(shares),
        family_entropy=entropy,
        rare_families=sum(1 for p in shares if p < RARE_SHARE),
        family_counts=counts,
    )
