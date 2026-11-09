"""Aggregate statistics of an imported dispute history, with small-cell suppression.

This is the only place in calibration that reads rows. Everything it returns is a
count of at least `k` cases, a rate whose successes and failures are each at least
`k`, or a mean over at least `k` cases. `fit_profile` (in `profile.py`) takes these
aggregates and nothing else, so no row-level value can reach the generator.

Suppression (k-anonymity style, default k = 10):

- Primary: a count below `k` is not emitted; it joins a pooled `suppressed` count.
- Secondary (complementary): if the pooled count would itself be between 1 and k - 1,
  the smallest emitted cells are moved into it until it reaches `k`, so no small cell
  can be recovered as "total minus the published cells". If the whole table is
  below `k`, nothing is emitted (`withheld`).
- A rate is emitted only when its successes and its failures are both at least `k`
  (otherwise the numerator would be a small cell).

Limits: suppression is per table. It does not audit differences across tables (for
example "cases of reason r" minus "contested cases of reason r"). Treat the output as
reduced-risk aggregates, not as a formal privacy guarantee; see docs/BACKTEST.md.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scruple.calibration.facts import (
    contested,
    is_inquiry_level,
    is_open_chargeback,
    is_resolved_chargeback,
    won,
)
from scruple.ingest.models import ExternalCase, LedgerMovement, MovementKind

OTHER = "__other__"
UNKNOWN = "unknown"
# Fixed in advance (minor units), never derived from the data: data-driven edges
# would leak individual amounts.
DEFAULT_AMOUNT_EDGES: tuple[int, ...] = (
    1_000,
    2_500,
    5_000,
    10_000,
    25_000,
    50_000,
    100_000,
    250_000,
    1_000_000,
)
DEADLINE_CELLS = ("submitted_on_time", "submitted_late", "not_submitted")

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class InsufficientHistoryError(ValueError):
    """The history has fewer than `k` cases, so no aggregate may be emitted."""


class CalibrationConfig(BaseModel):
    model_config = _FROZEN

    k: int = Field(default=10, ge=2, le=10_000)
    amount_edges: tuple[int, ...] = DEFAULT_AMOUNT_EDGES

    @model_validator(mode="after")
    def _edges(self) -> CalibrationConfig:
        e = self.amount_edges
        if not e or e[0] <= 0 or any(b <= a for a, b in pairwise(e)):
            raise ValueError("amount_edges must be positive and strictly increasing")
        return self


class CountTable(BaseModel):
    """Counts per key. Every emitted number is 0 or at least `k`."""

    model_config = _FROZEN

    cells: dict[str, int] = Field(default_factory=dict)
    suppressed: int = 0  # pooled small cells: 0 or >= k
    withheld: bool = False  # the whole table had fewer than k cases

    @property
    def total(self) -> int:
        return sum(self.cells.values()) + self.suppressed

    def share(self, key: str) -> float:
        t = self.total
        return self.cells.get(key, 0) / t if t else 0.0


class Rate(BaseModel):
    """`successes` out of `trials`; both successes and failures are at least `k`."""

    model_config = _FROZEN

    successes: int = Field(ge=0)
    trials: int = Field(gt=0)

    @model_validator(mode="after")
    def _order(self) -> Rate:
        if self.successes > self.trials:
            raise ValueError("successes cannot exceed trials")
        return self

    @property
    def value(self) -> float:
        return self.successes / self.trials

    def wilson(self, z: float = 1.959963984540054) -> tuple[float, float]:
        """Wilson score interval (default 95%)."""
        n, p = self.trials, self.value
        den = 1 + z * z / n
        mid = (p + z * z / (2 * n)) / den
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
        return max(0.0, mid - half), min(1.0, mid + half)


class EvidenceRates(BaseModel):
    """Win rates of contested cases with and without one evidence field."""

    model_config = _FROZEN

    present: Rate
    absent: Rate

    @property
    def lift(self) -> float:
        return self.present.value - self.absent.value


class HistoryAggregates(BaseModel):
    """Everything calibration may know about a history. No row-level data."""

    model_config = _FROZEN

    k: int
    n_cases: int  # >= k
    currency: str  # the most common currency; amounts below are in it
    currencies: CountTable
    reasons: CountTable  # by source reason, verbatim; small reasons pooled as OTHER
    categories: CountTable  # scruple category, or "unknown"
    networks: CountTable
    inquiry: Rate | None  # inquiry-level cases among all cases
    open_chargebacks: Rate | None  # still open, among chargebacks
    contest_overall: Rate | None  # contested, among resolved chargebacks
    win_overall: Rate | None  # won, among contested resolved chargebacks
    contest_by_reason: dict[str, Rate] = Field(default_factory=dict)
    win_by_reason: dict[str, Rate] = Field(default_factory=dict)
    win_by_evidence: dict[str, dict[str, EvidenceRates]] = Field(default_factory=dict)
    deadlines: CountTable
    late_submission: Rate | None  # submitted late, among submitted
    amount_bins: CountTable  # chargeback amounts in `currency`, fixed edges
    amount_edges: tuple[int, ...]
    log_amount_mean: float | None  # mean of ln(amount_minor), `currency` chargebacks
    log_amount_sd: float | None
    amount_n: int  # cases behind the two moments: 0 or >= k
    debit_fee_mean: float | None  # mean fee on dispute debits in `currency`
    debit_fee_n: int  # 0 or >= k

    def cells(self) -> Iterator[tuple[str, int]]:
        """Every count this object emits, with a path. Used by the suppression tests."""
        yield "n_cases", self.n_cases
        for name in ("currencies", "reasons", "categories", "networks", "deadlines"):
            t: CountTable = getattr(self, name)
            yield from _table_cells(name, t)
        yield from _table_cells("amount_bins", self.amount_bins)
        for name in ("inquiry", "open_chargebacks", "contest_overall", "win_overall"):
            r: Rate | None = getattr(self, name)
            if r is not None:
                yield from _rate_cells(name, r)
        if self.late_submission is not None:
            yield from _rate_cells("late_submission", self.late_submission)
        for table_name, table in (
            ("contest_by_reason", self.contest_by_reason),
            ("win_by_reason", self.win_by_reason),
        ):
            for key, r in table.items():
                yield from _rate_cells(f"{table_name}.{key}", r)
        for reason, fields in self.win_by_evidence.items():
            for f, er in fields.items():
                yield from _rate_cells(f"win_by_evidence.{reason}.{f}.present", er.present)
                yield from _rate_cells(f"win_by_evidence.{reason}.{f}.absent", er.absent)
        yield "amount_n", self.amount_n
        yield "debit_fee_n", self.debit_fee_n


def _table_cells(name: str, t: CountTable) -> Iterator[tuple[str, int]]:
    for key, v in t.cells.items():
        yield f"{name}.{key}", v
    yield f"{name}.suppressed", t.suppressed
    yield f"{name}.total", t.total


def _rate_cells(name: str, r: Rate) -> Iterator[tuple[str, int]]:
    yield f"{name}.successes", r.successes
    yield f"{name}.failures", r.trials - r.successes
    yield f"{name}.trials", r.trials


# --- suppression ------------------------------------------------------------------


def suppress_counts(counts: Mapping[str, int], k: int) -> CountTable:
    """Primary plus complementary suppression of a one-way table."""
    if k < 2:
        raise ValueError("k must be at least 2")
    if any(v < 0 for v in counts.values()):
        raise ValueError("counts must be non-negative")
    positive = {key: v for key, v in counts.items() if v > 0}
    total = sum(positive.values())
    if total < k:
        return CountTable(withheld=total > 0)
    kept = {key: v for key, v in positive.items() if v >= k}
    small = total - sum(kept.values())
    # Complementary suppression: a pooled count in (0, k) would expose the small cells.
    for key in sorted(kept, key=lambda x: (kept[x], x)):
        if small == 0 or small >= k:
            break
        small += kept.pop(key)
    return CountTable(cells=dict(sorted(kept.items())), suppressed=small)


def suppress_rate(successes: int, trials: int, k: int) -> Rate | None:
    if trials <= 0 or successes < k or trials - successes < k:
        return None
    return Rate(successes=successes, trials=trials)


# --- aggregation ------------------------------------------------------------------


def amount_bin(amount: int, edges: Sequence[int]) -> str:
    lo = 0
    for e in edges:
        if amount < e:
            return f"{lo}-{e}"
        lo = e
    return f"{lo}+"


def _round(x: float) -> float:
    return round(x, 6)


def compute_aggregates(
    cases: Iterable[ExternalCase],
    movements: Iterable[LedgerMovement] = (),
    config: CalibrationConfig | None = None,
) -> HistoryAggregates:
    """Aggregate a history. Raises `InsufficientHistoryError` below `k` cases."""
    cfg = config or CalibrationConfig()
    k = cfg.k
    rows = list(cases)
    if len(rows) < k:
        raise InsufficientHistoryError(f"{len(rows)} cases is below k = {k}; nothing emitted")
    currencies = Counter(c.currency for c in rows)
    currency = min(currencies, key=lambda x: (-currencies[x], x))
    reasons = suppress_counts(Counter(c.source_reason for c in rows), k)

    def reason_key(c: ExternalCase) -> str:
        return c.source_reason if c.source_reason in reasons.cells else OTHER

    chargebacks = [c for c in rows if not is_inquiry_level(c)]
    resolved = [c for c in chargebacks if is_resolved_chargeback(c)]
    disputed = [c for c in resolved if contested(c)]

    contest_by: dict[str, list[bool]] = defaultdict(list)
    for c in resolved:
        contest_by[reason_key(c)].append(contested(c))
    win_by: dict[str, list[bool]] = defaultdict(list)
    for c in disputed:
        win_by[reason_key(c)].append(won(c))

    evidence: dict[str, dict[str, EvidenceRates]] = {}
    for key in sorted(win_by):
        group = [c for c in disputed if reason_key(c) == key]
        fields = sorted({f for c in group for f in c.evidence_fields})
        out: dict[str, EvidenceRates] = {}
        for f in fields:
            with_f = [won(c) for c in group if f in c.evidence_fields]
            without = [won(c) for c in group if f not in c.evidence_fields]
            present = suppress_rate(sum(with_f), len(with_f), k)
            absent = suppress_rate(sum(without), len(without), k)
            if present is not None and absent is not None:
                out[f] = EvidenceRates(present=present, absent=absent)
        if out:
            evidence[key] = out

    deadline_counts: Counter[str] = Counter()
    for c in resolved:
        if c.evidence_due_at is None:
            continue
        if c.evidence_submitted:
            deadline_counts["submitted_late" if c.evidence_past_due else "submitted_on_time"] += 1
        else:
            deadline_counts["not_submitted"] += 1
    submitted = deadline_counts["submitted_on_time"] + deadline_counts["submitted_late"]

    amounts = [c.amount_minor for c in chargebacks if c.currency == currency]
    bins = suppress_counts(Counter(amount_bin(a, cfg.amount_edges) for a in amounts), k)
    mean = sd = None
    amount_n = 0
    if len(amounts) >= k:
        logs = [math.log(max(1, a)) for a in amounts]
        m = math.fsum(logs) / len(logs)
        var = math.fsum((x - m) ** 2 for x in logs) / max(1, len(logs) - 1)
        mean, sd, amount_n = _round(m), _round(math.sqrt(var)), len(logs)

    fees = [
        m.fee_minor
        for m in movements
        if m.kind is MovementKind.DISPUTE_DEBIT and m.currency == currency
    ]
    fee_mean = _round(math.fsum(fees) / len(fees)) if len(fees) >= k else None

    def rates(groups: Mapping[str, list[bool]]) -> dict[str, Rate]:
        out: dict[str, Rate] = {}
        for key in sorted(groups):
            r = suppress_rate(sum(groups[key]), len(groups[key]), k)
            if r is not None:
                out[key] = r
        return out

    n_inquiry = sum(1 for c in rows if is_inquiry_level(c))
    n_open = sum(1 for c in chargebacks if is_open_chargeback(c))
    return HistoryAggregates(
        k=k,
        n_cases=len(rows),
        currency=currency,
        currencies=suppress_counts(currencies, k),
        reasons=reasons,
        categories=suppress_counts(
            Counter(c.reason_code.value if c.reason_code else UNKNOWN for c in rows), k
        ),
        networks=suppress_counts(Counter((c.network or UNKNOWN).lower() for c in rows), k),
        inquiry=suppress_rate(n_inquiry, len(rows), k),
        open_chargebacks=suppress_rate(n_open, len(chargebacks), k),
        contest_overall=suppress_rate(len(disputed), len(resolved), k),
        win_overall=suppress_rate(sum(won(c) for c in disputed), len(disputed), k),
        contest_by_reason=rates(contest_by),
        win_by_reason=rates(win_by),
        win_by_evidence=evidence,
        deadlines=suppress_counts(deadline_counts, k),
        late_submission=suppress_rate(deadline_counts["submitted_late"], submitted, k),
        amount_bins=bins,
        amount_edges=cfg.amount_edges,
        log_amount_mean=mean,
        log_amount_sd=sd,
        amount_n=amount_n,
        debit_fee_mean=fee_mean,
        debit_fee_n=len(fees) if fee_mean is not None else 0,
    )
