"""Fidelity of the synthetic dispute mix against real public complaint data (040).

Real data: the CFPB Consumer Complaint Database (US federal government work, public
domain), in two public GitHub mirrors described in `PROVENANCE`. Only small count
tables derived from it are committed (`data/cfpb_aggregates.json`); `build_aggregates`
rebuilds them from the raw CSVs and records each file's SHA-256.

What is compared, and why it is only a partial check (docs/results/SIM_FIDELITY.md):
CFPB rows are consumer complaints escalated to a regulator, not card-network disputes,
and carry no amounts, reason codes or network outcomes. Three marginals have a mapping
we can defend:

1. `card_category`: among credit card complaints (set 1, 2012-2015), issue
   "Identity theft / Fraud / Embezzlement" vs "Billing disputes", against the share of
   simulated disputes filed under a fraud reason code vs any other category.
2. `debit_category`: among checking-account complaints (set 2, 2025-2026), sub-issue
   "Transaction was not authorized" vs "Money was taken from your account on the wrong
   day or for the wrong amount", against simulated fraud vs processing-error disputes.
3. `relief_fraud`, `relief_billing`: share of closed set-1 complaints "Closed with
   monetary relief" (vs explanation, non-monetary relief, or closed), against the
   share of simulated cases where the cardholder ends with money back
   (`cardholder_net_minor > 0`) under a fixed reference merchant policy
   (`reference_outcome`: represent with every document held whenever representing
   is allowed; never refund, accept or escalate).

Excluded on purpose: the pre-2013 labels "Closed with relief" / "Closed without relief"
(they do not say whether relief was monetary), "In progress" and "Untimely response".

Metrics: total variation distance and Jensen-Shannon divergence (base 2, in [0, 1])
between the real and simulated categorical distributions, with percentile bootstrap
95% intervals (both samples resampled multinomially from their counts), and a noise
floor: the 95th percentile of TV between the real distribution and a same-size
resample of itself.

`calibrate` fits the generator's family weights to the real marginals (fraud share;
share of valid claims within fraud and within other families, by bisection on the
simulated relief rate). The result is an opt-in profile (`calibrated_mix()`); the
defaults are unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    FULL_MIX,
    CardDispute,
    CaseMix,
    DisputeEngine,
    Represent,
    generate_cases,
)
from scruple.sim.families import CLAIM_VALID, FAMILY_WEIGHTS, CaseFamily
from scruple.sim.networks import Category

DATA = Path(__file__).parent / "data"
AGGREGATES = DATA / "cfpb_aggregates.json"
CALIBRATED = DATA / "calibrated_cfpb_2012_2015.json"

PROVENANCE: tuple[dict[str, str], ...] = (
    {
        "id": "cfpb_2012_2015",
        "dataset": "CFPB Consumer Complaint Database, Kaggle snapshot 'Consumer Finance "
        "Complaints' (file dated 2016-04-26), received 2012-01-01..2015-12-31",
        "mirror_repo": "https://github.com/jiegzhan/multi-class-text-classification-cnn",
        "mirror_commit": "e75a4b1fafb011873055c2d39ce7a48f835f0c86",
        "mirror_path": "data/consumer_complaints.csv.zip",
        "mirror_sha256": "38b11cfd7ebd5ec92b56bc86a453251b684c06dcdc7ef2979450458227d9c41d",
        "file": "cfpb_2012_2015/payments_subset.csv",
        "derivation": "rows of the mirror file with product in Credit card, Bank account or "
        "service, Prepaid card, Money transfers",
        "licence": "US federal government work, public domain (17 USC 105)",
    },
    {
        "id": "cfpb_checking_2025_2026",
        "dataset": "CFPB Consumer Complaint Database, checking or savings account "
        "complaints received 2025-05-14..2026-04-24 (random sample of 8,000)",
        "mirror_repo": "https://github.com/mhoehler22/cfpb-agglomerative-clustering",
        "mirror_commit": "5ddb49aeb18218b10d11df15cfe60a5dbe8ece09",
        "mirror_path": "data/outputs/sample_metadata.csv",
        "file": "cfpb_checking_2025_2026/checking_complaints_sample.csv",
        "derivation": "CFPB columns only",
        "licence": "underlying data US federal government work, public domain",
    },
)

FRAUD_ISSUE = "Identity theft / Fraud / Embezzlement"
BILLING_ISSUE = "Billing disputes"
DEBIT_FRAUD = (
    "Problem with a lender or other company charging your account / Transaction was not authorized"
)
DEBIT_ERROR = (
    "Problem with a lender or other company charging your account / "
    "Money was taken from your account on the wrong day or for the wrong amount"
)
RELIEF = "Closed with monetary relief"
NO_RELIEF = ("Closed with explanation", "Closed with non-monetary relief", "Closed")


# --- real data ---------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_aggregates(raw_dir: str | Path) -> dict[str, Any]:
    """Count tables from the raw CSVs (see DATA_CARD.md in the data directory)."""
    root = Path(raw_dir)
    s1 = root / PROVENANCE[0]["file"]
    s2 = root / PROVENANCE[1]["file"]
    card: dict[str, Counter[str]] = {}
    rows1 = 0
    with s1.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows1 += 1
            if row["product"] == "Credit card":
                card.setdefault(row["issue"], Counter())[row["company_response_to_consumer"]] += 1
    debit: dict[str, Counter[str]] = {}
    rows2 = 0
    with s2.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows2 += 1
            key = f"{row['Issue']} / {row['Sub-issue']}"
            debit.setdefault(key, Counter())[row["Company response to consumer"]] += 1
    prov = []
    for p, path, rows in ((PROVENANCE[0], s1, rows1), (PROVENANCE[1], s2, rows2)):
        prov.append({**p, "file_sha256": _sha256(path), "rows_read": str(rows)})
    return {
        "note": "Counts only, derived from public-domain CFPB complaint data; no rows or "
        "narratives. Rebuild: scruple sim fidelity --raw-dir <dir> --write-aggregates",
        "provenance": prov,
        "credit_card_issue_response": {k: dict(sorted(v.items())) for k, v in sorted(card.items())},
        "checking_subissue_response": {
            k: dict(sorted(v.items())) for k, v in sorted(debit.items())
        },
    }


def load_aggregates(path: Path = AGGREGATES) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def real_marginals(agg: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """The four real count tables compared (see the module docstring)."""
    card = agg["credit_card_issue_response"]
    debit = agg["checking_subissue_response"]

    def total(d: Mapping[str, int]) -> int:
        return sum(d.values())

    def relief(d: Mapping[str, int]) -> dict[str, int]:
        return {"relief": d.get(RELIEF, 0), "no_relief": sum(d.get(k, 0) for k in NO_RELIEF)}

    return {
        "card_category": {
            "fraud": total(card[FRAUD_ISSUE]),
            "non_fraud": total(card[BILLING_ISSUE]),
        },
        "debit_category": {
            "fraud": total(debit[DEBIT_FRAUD]),
            "processing_error": total(debit[DEBIT_ERROR]),
        },
        "relief_fraud": relief(card[FRAUD_ISSUE]),
        "relief_billing": relief(card[BILLING_ISSUE]),
    }


# --- simulated side -------------------------------------------------------------------
def reference_outcome(case: CardDispute, max_steps: int = 200) -> bool:
    """Whether the cardholder ends with money back under the reference policy:
    represent with every held document whenever allowed, otherwise let time pass."""
    clock = ManualClock()
    clock.set_date(case.opened_on)
    e = DisputeEngine([case], clock)
    cid = case.case_id
    for _ in range(max_steps):
        if e.is_resolved(cid):
            break
        obs = e.observe(cid)
        if "represent" in obs.allowed_actions:
            e.act(cid, Represent(evidence_ids=tuple(d.doc_id for d in obs.evidence)))
        else:
            clock.set_date(obs.today + timedelta(days=5))
        e.sync()
    out = e.outcome(cid)
    assert out is not None, cid
    return out.cardholder_net_minor > 0


class SimSample(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    category: dict[str, int]  # fraud / non_fraud
    debit_category: dict[str, int]  # fraud / processing_error (subset of cases)
    relief_fraud: dict[str, int]
    relief_billing: dict[str, int]


def simulate(mix: CaseMix, n: int, seed: int) -> SimSample:
    cases = generate_cases(seed, n, mix)
    cat: Counter[str] = Counter()
    deb: Counter[str] = Counter()
    rf: Counter[str] = Counter()
    rb: Counter[str] = Counter()
    for c in cases:
        fraud = c.category is Category.FRAUD
        cat["fraud" if fraud else "non_fraud"] += 1
        if fraud:
            deb["fraud"] += 1
        elif c.category is Category.PROCESSING_ERROR:
            deb["processing_error"] += 1
        key = "relief" if reference_outcome(c) else "no_relief"
        (rf if fraud else rb)[key] += 1

    def full(c: Counter[str], keys: Sequence[str]) -> dict[str, int]:
        return {k: c.get(k, 0) for k in keys}

    return SimSample(
        n=n,
        category=full(cat, ("fraud", "non_fraud")),
        debit_category=full(deb, ("fraud", "processing_error")),
        relief_fraud=full(rf, ("relief", "no_relief")),
        relief_billing=full(rb, ("relief", "no_relief")),
    )


def sim_marginals(s: SimSample) -> dict[str, dict[str, int]]:
    return {
        "card_category": s.category,
        "debit_category": s.debit_category,
        "relief_fraud": s.relief_fraud,
        "relief_billing": s.relief_billing,
    }


# --- divergences ------------------------------------------------------------------------
def _probs(counts: Mapping[str, int], keys: Sequence[str]) -> list[float]:
    n = sum(counts.get(k, 0) for k in keys)
    if n == 0:
        raise ValueError("empty distribution")
    return [counts.get(k, 0) / n for k in keys]


def total_variation(p: Sequence[float], q: Sequence[float]) -> float:
    return 0.5 * sum(abs(a - b) for a, b in zip(p, q, strict=True))


def jensen_shannon(p: Sequence[float], q: Sequence[float]) -> float:
    """JS divergence in bits (0 identical, 1 disjoint)."""
    m = [(a + b) / 2 for a, b in zip(p, q, strict=True)]

    def kl(x: Sequence[float]) -> float:
        # b == 0 with a > 0 only happens when a is subnormal; its term is 0 in the limit.
        return sum(a * math.log2(a / b) for a, b in zip(x, m, strict=True) if a > 0 and b > 0)

    return 0.5 * kl(p) + 0.5 * kl(q)


def _multinomial(rng: random.Random, n: int, p: Sequence[float]) -> list[int]:
    out, left, mass = [], n, 1.0
    for i, pi in enumerate(p):
        if i == len(p) - 1:
            out.append(left)
            break
        k = rng.binomialvariate(left, min(1.0, pi / mass)) if left and mass > 0 else 0
        out.append(k)
        left -= k
        mass -= pi
    return out


class Divergence(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    keys: tuple[str, ...]
    real_counts: dict[str, int]
    sim_counts: dict[str, int]
    real_share: tuple[float, ...]
    sim_share: tuple[float, ...]
    tv: float
    tv_ci: tuple[float, float]
    js: float
    js_ci: tuple[float, float]
    tv_noise_floor: float  # 95th pct TV of real vs a same-size resample of itself


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))
    return xs[i]


def divergence(
    name: str,
    real: Mapping[str, int],
    sim: Mapping[str, int],
    boot: int = 2_000,
    seed: int = 0,
) -> Divergence:
    keys = tuple(real)
    p, q = _probs(real, keys), _probs(sim, keys)
    nr, ns = sum(real[k] for k in keys), sum(sim.get(k, 0) for k in keys)
    rng = random.Random(f"fidelity:{name}:{seed}")
    tvs, jss, floor = [], [], []
    for _ in range(boot):
        rp = [x / nr for x in _multinomial(rng, nr, p)]
        sq = [x / ns for x in _multinomial(rng, ns, q)]
        tvs.append(total_variation(rp, sq))
        jss.append(jensen_shannon(rp, sq))
        same = [x / ns for x in _multinomial(rng, ns, p)]
        floor.append(total_variation(p, same))
    return Divergence(
        name=name,
        keys=keys,
        real_counts={k: real[k] for k in keys},
        sim_counts={k: sim.get(k, 0) for k in keys},
        real_share=tuple(p),
        sim_share=tuple(q),
        tv=total_variation(p, q),
        tv_ci=(_pct(tvs, 0.025), _pct(tvs, 0.975)),
        js=jensen_shannon(p, q),
        js_ci=(_pct(jss, 0.025), _pct(jss, 0.975)),
        tv_noise_floor=_pct(floor, 0.95),
    )


class FidelityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile: str
    n_sim: int
    seed: int
    boot: int
    comparisons: tuple[Divergence, ...]

    def lines(self) -> list[str]:
        out = [
            f"profile {self.profile}: {self.n_sim:,} synthetic cases (seed {self.seed}), "
            f"{self.boot:,} bootstrap resamples; real data CFPB (public domain)",
            f"{'comparison':16} {'real share':>24} {'sim share':>24} {'TV [95% CI]':>24} "
            f"{'JS bits [95% CI]':>26} {'TV floor':>9}",
        ]
        for d in self.comparisons:
            rs = " ".join(f"{k}={v:.3f}" for k, v in zip(d.keys, d.real_share, strict=True))
            ss = " ".join(f"{k}={v:.3f}" for k, v in zip(d.keys, d.sim_share, strict=True))
            out.append(
                f"{d.name:16} {rs:>24} {ss:>24} "
                f"{d.tv:.3f} [{d.tv_ci[0]:.3f}, {d.tv_ci[1]:.3f}]   "
                f"{d.js:.4f} [{d.js_ci[0]:.4f}, {d.js_ci[1]:.4f}]   {d.tv_noise_floor:.3f}"
            )
        return out


def fidelity(
    mix: CaseMix,
    profile: str,
    agg: Mapping[str, Any] | None = None,
    n: int = 4_000,
    seed: int = 1,
    boot: int = 2_000,
) -> FidelityReport:
    real = real_marginals(agg if agg is not None else load_aggregates())
    sim = sim_marginals(simulate(mix, n, seed))
    comps = tuple(divergence(k, real[k], sim[k], boot, seed) for k in real)
    return FidelityReport(profile=profile, n_sim=n, seed=seed, boot=boot, comparisons=comps)


# --- calibration -------------------------------------------------------------------------
FRAUD_FAMS = (CaseFamily.TRUE_FRAUD, CaseFamily.FRIENDLY_FRAUD)
OTHER_FAMS = tuple(f for f in CaseFamily if f not in FRAUD_FAMS)


def weights_for(
    fraud_share: float, valid_fraud: float, valid_other: float
) -> dict[CaseFamily, float]:
    """Family weights with the given fraud share and valid-claim shares, keeping the
    suggested ratios inside each (group, validity) cell (A-22)."""
    out: dict[CaseFamily, float] = {}
    for fams, share, valid in (
        (FRAUD_FAMS, fraud_share, valid_fraud),
        (OTHER_FAMS, 1 - fraud_share, valid_other),
    ):
        for v, cell in ((True, valid), (False, 1 - valid)):
            members = [f for f in fams if CLAIM_VALID[f] is v]
            base = sum(FAMILY_WEIGHTS[f] for f in members)
            for f in members:
                out[f] = share * cell * FAMILY_WEIGHTS[f] / base
    return out


def _relief_rate(
    mix: CaseMix, fams: tuple[CaseFamily, ...], valid: float, n: int, seed: int
) -> float:
    w = weights_for(0.5, valid, valid)
    only = {f: w[f] for f in fams}
    cases = generate_cases(seed, n, mix.model_copy(update={"family_weights": only}))
    return sum(reference_outcome(c) for c in cases) / n


def _bisect(fn: Callable[[float], float], target: float, iters: int = 10) -> float:
    lo, hi = 0.0, 1.0
    if fn(lo) >= target:
        return lo
    if fn(hi) <= target:
        return hi
    for _ in range(iters):
        mid = (lo + hi) / 2
        if fn(mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


class Calibration(BaseModel):
    model_config = ConfigDict(frozen=True)

    fraud_share: float
    valid_fraud: float
    valid_other: float
    family_weights: dict[CaseFamily, float]
    targets: dict[str, float]
    n_per_eval: int
    seed: int


def calibrate(
    agg: Mapping[str, Any] | None = None, n: int = 1_500, seed: int = 11, base: CaseMix = FULL_MIX
) -> Calibration:
    """Fit family weights to the set-1 marginals (fraud share, relief per group).

    Common random numbers (one seed for every evaluation) keep the bisection stable.
    Fitted on a different seed than the report uses, so the report is out-of-sample."""
    real = real_marginals(agg if agg is not None else load_aggregates())
    cat = real["card_category"]
    fraud_share = cat["fraud"] / (cat["fraud"] + cat["non_fraud"])

    def share(d: Mapping[str, int]) -> float:
        return d["relief"] / (d["relief"] + d["no_relief"])

    t_f, t_o = share(real["relief_fraud"]), share(real["relief_billing"])
    v_f = _bisect(lambda v: _relief_rate(base, FRAUD_FAMS, v, n, seed), t_f)
    v_o = _bisect(lambda v: _relief_rate(base, OTHER_FAMS, v, n, seed), t_o)
    return Calibration(
        fraud_share=fraud_share,
        valid_fraud=v_f,
        valid_other=v_o,
        family_weights=weights_for(fraud_share, v_f, v_o),
        targets={"fraud_share": fraud_share, "relief_fraud": t_f, "relief_billing": t_o},
        n_per_eval=n,
        seed=seed,
    )


def save_calibration(cal: Calibration, path: Path = CALIBRATED) -> None:
    body = {
        "note": "Opt-in generator profile fitted to CFPB 2012-2015 credit card complaint "
        "marginals (scruple.sim.fidelity.calibrate). Synthetic cases; not a claim about "
        "real dispute mixes. Defaults (FULL_MIX) are unchanged.",
        **cal.model_dump(mode="json"),
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def calibrated_mix(path: Path = CALIBRATED, base: CaseMix = FULL_MIX) -> CaseMix:
    """FULL_MIX with the calibrated family weights (opt-in)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    weights = {CaseFamily(k): float(v) for k, v in raw["family_weights"].items()}
    return base.model_copy(update={"family_weights": weights})
