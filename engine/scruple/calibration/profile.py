"""A generator profile fitted from aggregates only, and how far two histories differ.

`CalibrationProfile` holds the parameters of scruple's history simulator
(`scruple.calibration.synthetic`): reason mix, contest and win rates per reason, the
evidence field that moves the win rate most, inquiry and open shares, late submissions,
a log-normal amount model and the dispute fee. `fit_profile` reads a
`HistoryAggregates` and nothing else; it cannot see a row.

`DEFAULT_PROFILE` is the uncalibrated starting point. Every value in it is synthetic
(ASSUMPTION, docs/BACKTEST.md): the amount model and network weights repeat the
simulator's own defaults (`scruple.sim.disputes.generate_cases`, `CaseMix`), the fee
repeats `scruple.sim.models.REPRESENTMENT_LOSS_FEE_USD`, and the rates are round
placeholders. None of it is a real network or processor figure.

Win model of one reason: contested cases carry the reason's key evidence field with
probability `p_key_evidence`; the merchant wins with `p_win_with_key` when it is
present and `p_win_without_key` when not. Without a key field both are equal.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scruple.calibration.aggregates import OTHER, CountTable, EvidenceRates, HistoryAggregates, Rate

if TYPE_CHECKING:  # pragma: no cover
    from scruple.sim.disputes import CaseMix

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ReasonProfile(BaseModel):
    model_config = _FROZEN

    weight: float = Field(gt=0, le=1)
    p_contest: float = Field(ge=0, le=1)
    p_win_with_key: float = Field(ge=0, le=1)
    p_win_without_key: float = Field(ge=0, le=1)
    key_evidence: str | None = None
    p_key_evidence: float = Field(default=0.0, ge=0, le=1)
    # The aggregate behind the win rate (0 trials: not fitted, a fallback).
    win_successes: int = Field(default=0, ge=0)
    win_trials: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _key(self) -> ReasonProfile:
        if self.key_evidence is None and (
            self.p_key_evidence or self.p_win_with_key != self.p_win_without_key
        ):
            raise ValueError("without a key evidence field the two win rates must match")
        if self.win_successes > self.win_trials:
            raise ValueError("win_successes cannot exceed win_trials")
        return self

    @property
    def p_win(self) -> float:
        """Marginal win rate of a contested case."""
        q = self.p_key_evidence
        return q * self.p_win_with_key + (1 - q) * self.p_win_without_key

    def win_interval(self) -> tuple[float, float]:
        """95% Wilson interval of the fitted win rate; [0, 1] when nothing was fitted."""
        if self.win_trials == 0:
            return 0.0, 1.0
        return Rate(successes=self.win_successes, trials=self.win_trials).wilson()


class CalibrationProfile(BaseModel):
    model_config = _FROZEN

    kind: Literal["default", "fitted"]
    k: int | None = None  # suppression threshold of the aggregates it came from
    n_cases: int | None = None
    currency: str = "USD"
    network_weights: dict[str, float]
    reasons: dict[str, ReasonProfile] = Field(min_length=1)
    p_inquiry: float = Field(ge=0, le=1)
    p_open: float = Field(ge=0, le=1)  # still open, among chargebacks
    p_late: float = Field(ge=0, le=1)  # submitted after the due date, among submissions
    amount_log_mean: float
    amount_log_sd: float = Field(gt=0)
    dispute_fee_minor: int = Field(ge=0)
    fallbacks: tuple[str, ...] = ()  # parameters not fitted (default or pooled used)

    @model_validator(mode="after")
    def _weights(self) -> CalibrationProfile:
        totals = (
            ("reason", math.fsum(r.weight for r in self.reasons.values())),
            ("network", math.fsum(self.network_weights.values())),
        )
        for name, total in totals:
            if not math.isclose(total, 1.0, abs_tol=1e-6):
                raise ValueError(f"{name} weights must sum to 1, not {total}")
        if any(w < 0 for w in self.network_weights.values()):
            raise ValueError("network weights must be non-negative")
        return self

    def reason(self, key: str) -> ReasonProfile:
        """The profile of a reason; unknown reasons use OTHER, else the pooled rates."""
        if key in self.reasons:
            return self.reasons[key]
        if OTHER in self.reasons:
            return self.reasons[OTHER]
        return _pooled(self.reasons)

    def win_probability(self, key: str, evidence_fields: tuple[str, ...] | None = None) -> float:
        """Chance a contested case of this reason is won. `None` fields: unknown."""
        r = self.reason(key)
        if evidence_fields is None or r.key_evidence is None:
            return r.p_win
        return r.p_win_with_key if r.key_evidence in evidence_fields else r.p_win_without_key


def _pooled(reasons: Mapping[str, ReasonProfile]) -> ReasonProfile:
    total = math.fsum(r.weight for r in reasons.values())
    p_contest = math.fsum(r.weight * r.p_contest for r in reasons.values()) / total
    p_win = math.fsum(r.weight * r.p_win for r in reasons.values()) / total
    return ReasonProfile(
        weight=1.0,
        p_contest=p_contest,
        p_win_with_key=p_win,
        p_win_without_key=p_win,
        win_successes=sum(r.win_successes for r in reasons.values()),
        win_trials=sum(r.win_trials for r in reasons.values()),
    )


def _placeholder(weight: float) -> ReasonProfile:
    return ReasonProfile(weight=weight, p_contest=0.5, p_win_with_key=0.3, p_win_without_key=0.3)


# Synthetic starting point (ASSUMPTION; see the module docstring).
DEFAULT_PROFILE = CalibrationProfile(
    kind="default",
    currency="USD",
    network_weights={"mastercard": 0.4, "visa": 0.6},
    reasons={
        r: _placeholder(0.25)
        for r in ("fraudulent", "general", "product_not_received", "product_unacceptable")
    },
    p_inquiry=0.2,
    p_open=0.05,
    p_late=0.05,
    amount_log_mean=round(4.4 + math.log(100), 6),
    amount_log_sd=0.9,
    dispute_fee_minor=1_500,
)


def _shares(t: CountTable) -> dict[str, float]:
    total = t.total
    out = {key: v / total for key, v in t.cells.items()} if total else {}
    if t.suppressed:
        out[OTHER] = t.suppressed / total
    return out


def _normalised(w: Mapping[str, float]) -> dict[str, float]:
    total = math.fsum(w.values())
    keys = sorted(w)
    out = {key: w[key] / total for key in keys}
    # absorb rounding into the largest weight so the sum is exactly 1 within 1e-12
    big = max(keys, key=lambda x: (out[x], x))
    out[big] += 1.0 - math.fsum(out.values())
    return out


KEY_EVIDENCE_Z = 3.0


def _clear_lift(er: EvidenceRates) -> bool:
    """A field becomes the key only if its lift is clear of noise: a two-proportion
    z statistic of at least `KEY_EVIDENCE_Z` (pooled standard error)."""
    a, b = er.present, er.absent
    p = (a.successes + b.successes) / (a.trials + b.trials)
    se = math.sqrt(p * (1 - p) * (1 / a.trials + 1 / b.trials))
    return se > 0 and abs(er.lift) / se >= KEY_EVIDENCE_Z


def fit_profile(
    aggs: HistoryAggregates, prior: CalibrationProfile = DEFAULT_PROFILE
) -> CalibrationProfile:
    """Fit the generator to aggregates. Missing (suppressed) parameters fall back to
    pooled aggregates, then to `prior`; every fallback is listed in `fallbacks`."""
    fallbacks: list[str] = []
    pooled = prior.reason("__none__")

    def rate(r: Rate | None, default: float, name: str) -> float:
        if r is None:
            fallbacks.append(name)
            return default
        return r.value

    p_inquiry = rate(aggs.inquiry, prior.p_inquiry, "p_inquiry")
    p_open = rate(aggs.open_chargebacks, prior.p_open, "p_open")
    p_late = rate(aggs.late_submission, prior.p_late, "p_late")
    p_contest_pooled = rate(aggs.contest_overall, pooled.p_contest, "p_contest (pooled)")
    win_pooled = aggs.win_overall
    p_win_pooled = rate(win_pooled, pooled.p_win, "p_win (pooled)")

    weights = _shares(aggs.reasons)
    if not weights:  # pragma: no cover - aggregates always hold n_cases >= k
        raise ValueError("the reason table is withheld; nothing to fit")
    weights = _normalised(weights)
    reasons: dict[str, ReasonProfile] = {}
    for key, w in weights.items():
        c = aggs.contest_by_reason.get(key)
        if c is None:
            fallbacks.append(f"p_contest[{key}]")
        p_contest = c.value if c is not None else p_contest_pooled
        win = aggs.win_by_reason.get(key)
        if win is None:
            fallbacks.append(f"p_win[{key}]")
            win = win_pooled
        p_win = win.value if win is not None else p_win_pooled
        ev = {f: er for f, er in aggs.win_by_evidence.get(key, {}).items() if _clear_lift(er)}
        if ev:
            field = max(sorted(ev), key=lambda f: abs(ev[f].lift))
            er = ev[field]
            n_with, n_without = er.present.trials, er.absent.trials
            reasons[key] = ReasonProfile(
                weight=w,
                p_contest=p_contest,
                key_evidence=field,
                p_key_evidence=n_with / (n_with + n_without),
                p_win_with_key=er.present.value,
                p_win_without_key=er.absent.value,
                win_successes=win.successes if win is not None else 0,
                win_trials=win.trials if win is not None else 0,
            )
        else:
            reasons[key] = ReasonProfile(
                weight=w,
                p_contest=p_contest,
                p_win_with_key=p_win,
                p_win_without_key=p_win,
                win_successes=win.successes if win is not None else 0,
                win_trials=win.trials if win is not None else 0,
            )

    networks = _shares(aggs.networks)
    if not networks:
        fallbacks.append("network_weights")
        networks = dict(prior.network_weights)
    if aggs.log_amount_mean is None or aggs.log_amount_sd is None or aggs.log_amount_sd <= 0:
        fallbacks.append("amounts")
        mu, sd = prior.amount_log_mean, prior.amount_log_sd
    else:
        mu, sd = aggs.log_amount_mean, aggs.log_amount_sd
    if aggs.debit_fee_mean is None:
        fallbacks.append("dispute_fee_minor")
        fee = prior.dispute_fee_minor
    else:
        fee = round(aggs.debit_fee_mean)
    return CalibrationProfile(
        kind="fitted",
        k=aggs.k,
        n_cases=aggs.n_cases,
        currency=aggs.currency,
        network_weights=_normalised(networks),
        reasons=reasons,
        p_inquiry=p_inquiry,
        p_open=p_open,
        p_late=p_late,
        amount_log_mean=mu,
        amount_log_sd=sd,
        dispute_fee_minor=fee,
        fallbacks=tuple(fallbacks),
    )


# --- divergence ---------------------------------------------------------------------


class Divergence(BaseModel):
    """How far `other` is from `target`, component by component (0 = identical).

    Total variation distances for the reason mix and the amount histogram; absolute
    differences for rates and log-amount moments. `overall` is the mean of the
    components that both sides could compute."""

    model_config = _FROZEN

    components: dict[str, float]
    overall: float


def _tv(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    keys = set(a) | set(b)
    return 0.5 * math.fsum(abs(a.get(x, 0.0) - b.get(x, 0.0)) for x in keys)


def _mae(a: Mapping[str, Rate], b: Mapping[str, Rate]) -> float | None:
    keys = sorted(set(a) & set(b))
    if not keys:
        return None
    return math.fsum(abs(a[x].value - b[x].value) for x in keys) / len(keys)


def divergence(target: HistoryAggregates, other: HistoryAggregates) -> Divergence:
    comp: dict[str, float] = {
        "reason_mix_tv": _tv(_shares(target.reasons), _shares(other.reasons)),
    }
    if target.amount_bins.total and other.amount_bins.total:
        comp["amount_bins_tv"] = _tv(_shares(target.amount_bins), _shares(other.amount_bins))
    for name, a, b in (
        ("contest_rate_mae", target.contest_by_reason, other.contest_by_reason),
        ("win_rate_mae", target.win_by_reason, other.win_by_reason),
    ):
        v = _mae(a, b)
        if v is not None:
            comp[name] = v
    for name in ("inquiry", "late_submission", "contest_overall", "win_overall"):
        ra: Rate | None = getattr(target, name)
        rb: Rate | None = getattr(other, name)
        if ra is not None and rb is not None:
            comp[f"{name}_abs"] = abs(ra.value - rb.value)
    if target.log_amount_mean is not None and other.log_amount_mean is not None:
        comp["log_amount_mean_abs"] = abs(target.log_amount_mean - other.log_amount_mean)
    if target.log_amount_sd is not None and other.log_amount_sd is not None:
        comp["log_amount_sd_abs"] = abs(target.log_amount_sd - other.log_amount_sd)
    rounded = {key: round(v, 6) for key, v in sorted(comp.items())}
    return Divergence(
        components=rounded, overall=round(math.fsum(rounded.values()) / len(rounded), 6)
    )


def to_case_mix(profile: CalibrationProfile, base: CaseMix | None = None) -> CaseMix:
    """Opt-in: carry the fitted inquiry share and network weights into `scruple.sim`'s
    generator. Only those two map onto `CaseMix`; the simulator's amount model, reason
    catalogue and issuer odds are its own and are left unchanged."""
    from scruple.sim.disputes import DEFAULT_MIX
    from scruple.sim.networks import Network

    mix = base or DEFAULT_MIX
    known = {n.value: n for n in Network}
    nets = {known[x]: w for x, w in profile.network_weights.items() if x in known and w > 0}
    update: dict[str, object] = {"p_inquiry": profile.p_inquiry}
    if nets:
        total = math.fsum(nets.values())
        update["network_weights"] = {n: w / total for n, w in nets.items()}
    return type(mix).model_validate({**mix.model_dump(), **update})
