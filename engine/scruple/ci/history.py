"""Gate a candidate on the partner's own history, next to the synthetic gate.

`scruple ci run --history <case store or export>` runs the usual synthetic shadow and
canary gate, then backtests the same candidate rule on the partner's history
(`scruple.backtest.run_backtest`, no look-ahead, observed and estimated costs kept
apart) and puts both on the scorecard as separate sources. The overall verdict is the
worse of the two; each source keeps its own.

History verdict, from the backtest's anytime-valid statistics (betting e-processes,
weighted e-Bonferroni to block, e-BH per reason; `scruple.stats.evaluate_pairs`):

- `worse` -> BLOCK, whether or not it holds at both ends of the estimate band (a
  regression that appears under any reading of the estimates is not waved through);
- `better` and robust (same verdict with the optimistic and the pessimistic end of
  every estimate) -> PASS; `better` but not robust -> HOLD;
- `insufficient evidence` -> HOLD (the scorecard says how many cases each reason
  would need);
- `no change` (the rule never fires on the history, or always agrees with it) -> PASS,
  with the reason stated: there is nothing on the history to regress.

The candidate is the rule alone, judged where it fires; elsewhere history keeps what
was actually done. Deterministic: the backtest is pure and seeded, the history is
identified by a content hash (not by file bytes or a path), so the same candidate and
the same history give the same scorecard bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.backtest.candidate import RuleCandidate, RuleEntry
from scruple.backtest.engine import BacktestConfig, BacktestReport, run_backtest
from scruple.backtest.history import History, load_adyen, load_store, load_stripe, load_tabular
from scruple.calibration.aggregates import InsufficientHistoryError
from scruple.calibration.profile import DEFAULT_PROFILE
from scruple.domain import RuleSpec
from scruple.ingest.keys import adyen_keys_from_env
from scruple.ingest.tabular import ImportConfig

HistoryFormat = Literal["store", "stripe", "adyen", "csv", "jsonl"]
# Same values as `scruple.ci.scorecard.Verdict` (kept as strings: the scorecard imports
# this module).
GateVerdict = Literal["pass", "hold", "block"]
METHOD = (
    "backtest on history: betting e-processes per reason, weighted e-Bonferroni to "
    "block (FWER), e-BH for improvement (FDR); anytime-valid (scruple.stats)"
)
SQLITE_MAGIC = b"SQLite format 3\x00"


class HistorySection(BaseModel):
    """`[history]` in scruple.toml: the partner history to gate on (optional)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    format: HistoryFormat | None = None  # None: a case store, or inferred from the suffix
    import_config: str | None = None  # ImportConfig JSON for csv / jsonl
    label: str | None = Field(default=None, max_length=200)
    allow_live: bool = False
    k: int = Field(default=10, ge=2)
    currency: str | None = None
    escalation_cost_minor: int | None = Field(default=None, ge=0)
    contest_fee_minor: int = Field(default=0, ge=0)


class HistoryReason(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: str
    n: int
    n_observed: int
    n_estimated: int
    verdict: str
    mean_diff: float
    ci_low: float
    ci_high: float
    cases_needed: int | None
    sufficient: bool


class HistoryGate(BaseModel):
    """The partner-history source on a scorecard. Costs in minor units of `currency`."""

    model_config = ConfigDict(frozen=True)

    source: Literal["partner_history"] = "partner_history"
    dataset: str
    synthetic: bool
    format: str
    history_sha256: str
    verdict: GateVerdict
    backtest_verdict: str
    robust: bool
    why: str
    profile: str  # where estimated costs come from
    method: str = METHOD
    currency: str
    n_input: int
    n_included: int
    n_observed: int
    n_estimated: int
    excluded: dict[str, int]
    baseline_cost: float
    candidate_cost: float
    candidate_cost_observed: float
    candidate_cost_estimated: float
    candidate_cost_low: float
    candidate_cost_high: float
    mean_diff: float | None
    ci_low: float | None
    ci_high: float | None
    rule_fields_defaulted: dict[str, int]
    reasons: tuple[str, ...]
    by_reason: tuple[HistoryReason, ...]
    planning_note: str


def history_sha256(history: History) -> str:
    """A content hash of the cases and movements (independent of file layout)."""
    body = {
        "cases": [hc.case.model_dump(mode="json") for hc in history.cases],
        "movements": [
            m.model_dump(mode="json")
            for m in sorted(history.movements, key=lambda m: (m.source, m.external_id))
        ],
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _infer(path: Path, fmt: HistoryFormat | None) -> HistoryFormat:
    if fmt is not None:
        return fmt
    with path.open("rb") as f:
        if f.read(16) == SQLITE_MAGIC:
            return "store"
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "stripe"
    raise ValueError(f"cannot tell the format of {path.name}; pass --history-format")


def load_history(section: HistorySection, root: Path | None = None) -> History:
    path = Path(section.file)
    if root is not None and not path.is_absolute():
        path = root / path
    fmt = _infer(path, section.format)
    label = section.label or f"{path.name} ({fmt})"
    if fmt == "store":
        # The file name, not the path: the scorecard must not depend on where it ran.
        return load_store(str(path), label=section.label or f"case store {path.name}")
    data = path.read_bytes()
    if fmt == "stripe":
        return load_stripe(data, label=label, allow_live=section.allow_live)
    if fmt == "adyen":
        return load_adyen(
            data, label=label, hmac_keys=adyen_keys_from_env(), allow_live=section.allow_live
        )
    if not section.import_config:
        raise ValueError(f"--history-format {fmt} needs --history-config (ImportConfig JSON)")
    cfg_path = Path(section.import_config)
    if root is not None and not cfg_path.is_absolute():
        cfg_path = root / cfg_path
    cfg = ImportConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
    return load_tabular(data, cfg, label=label, jsonl=fmt == "jsonl")


def verdict_for(report: BacktestReport) -> tuple[GateVerdict, str]:
    v = report.verdict
    if v == "worse":
        return "block", "the candidate is worse on the partner history"
    if v == "insufficient evidence":
        return "hold", "not enough resolved cases to decide either way"
    if v == "no change":
        return "pass", "the candidate never changes a decision on this history"
    if report.robust:
        return "pass", "better, at both ends of every estimate"
    return "hold", "better on the central estimate only; not robust to the estimate band"


def gate_on_history(
    history: History, rule_id: str, spec: RuleSpec, section: HistorySection
) -> tuple[HistoryGate, BacktestReport]:
    rule = RuleCandidate((RuleEntry(rule_id=rule_id, spec=spec),), default=None)
    overrides: dict[str, object] = {
        "k": section.k,
        "currency": section.currency,
        "contest_fee_minor": section.contest_fee_minor,
    }
    if section.escalation_cost_minor is not None:
        overrides["escalation_cost_minor"] = section.escalation_cost_minor
    bcfg = BacktestConfig.model_validate(overrides)
    try:
        report = run_backtest(history, rule, config=bcfg)
        verdict, why = verdict_for(report)
        profile = "calibrated from this history's suppressed aggregates"
    except InsufficientHistoryError:
        # Fewer than k cases: no aggregate may leave the history, so nothing can be
        # calibrated. Costs are still shown, estimated from the synthetic default.
        report = run_backtest(history, rule, config=bcfg, profile=DEFAULT_PROFILE)
        verdict = "hold"
        why = f"fewer than k={section.k} cases: too few to calibrate or to decide"
        report = report.model_copy(update={"verdict": "insufficient evidence"})
        profile = "DEFAULT_PROFILE (synthetic, uncalibrated): history below k"
    st = report.stats
    gate = HistoryGate(
        dataset=report.dataset,
        synthetic=report.synthetic,
        format=report.format,
        history_sha256=history_sha256(history),
        verdict=verdict,
        backtest_verdict=report.verdict,
        robust=report.robust,
        why=why,
        profile=profile,
        currency=report.currency,
        n_input=report.n_input,
        n_included=report.n_included,
        n_observed=report.n_observed,
        n_estimated=report.n_estimated,
        excluded=report.excluded,
        baseline_cost=report.baseline_cost,
        candidate_cost=report.candidate_cost,
        candidate_cost_observed=report.candidate_cost_observed,
        candidate_cost_estimated=report.candidate_cost_estimated,
        candidate_cost_low=report.candidate_cost_low,
        candidate_cost_high=report.candidate_cost_high,
        mean_diff=st.mean_diff if st else None,
        ci_low=st.ci_low if st else None,
        ci_high=st.ci_high if st else None,
        rule_fields_defaulted=report.rule_fields_defaulted,
        reasons=report.reasons_text,
        by_reason=tuple(
            HistoryReason(
                reason=r.reason,
                n=r.n,
                n_observed=r.n_observed,
                n_estimated=r.n_estimated,
                verdict=r.verdict,
                mean_diff=r.mean_diff,
                ci_low=r.ci_low,
                ci_high=r.ci_high,
                cases_needed=r.plan.cases_needed,
                sufficient=r.plan.sufficient,
            )
            for r in report.by_reason
        ),
        planning_note=report.planning_note,
    )
    return gate, report


_RANK = {"pass": 0, "hold": 1, "block": 2}


def combine(*verdicts: str) -> str:
    """The worst of the sources' verdicts ("pass" < "hold" < "block")."""
    return max(verdicts, key=lambda v: _RANK[v])


__all__ = [
    "HistoryGate",
    "HistoryReason",
    "HistorySection",
    "combine",
    "gate_on_history",
    "history_sha256",
    "load_history",
    "verdict_for",
]
