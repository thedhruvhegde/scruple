"""Backtest (154): costs, observed vs estimated labels, verdicts, statistics, reports.

Histories here are hand-made test data or SYNTHETIC (scruple.calibration.synthetic)."""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from pypdf import PdfReader

from scruple.backtest import (
    BacktestConfig,
    BacktestReport,
    DecisionView,
    History,
    from_records,
    run_backtest,
    to_json,
    to_markdown,
    write_report,
)
from scruple.backtest.candidate import ModuleCandidate
from scruple.calibration import DEFAULT_PROFILE
from scruple.domain import Action
from scruple.ingest.models import ExternalCase, LedgerMovement
from tests.test_backtest_view import case, history, moves


def always(action: Action | None) -> ModuleCandidate:
    return ModuleCandidate(lambda _v: action, f"always-{action.value if action else 'history'}")


def trio() -> History:
    a = case(1, "won", submitted=True)  # contested and won
    b = case(2, "lost")  # accepted
    c = case(3, "lost", submitted=True)  # contested and lost
    return history([a, b, c], moves(a, credit_days=40) + moves(b) + moves(c))


def run(h: History, cand: ModuleCandidate, **cfg: Any) -> BacktestReport:
    return run_backtest(
        h, cand, profile=DEFAULT_PROFILE, config=BacktestConfig(include_cases=True, **cfg)
    )


def by_id(r: BacktestReport) -> dict[str, Any]:
    return {x.case_id: x for x in r.cases}


def test_baseline_costs_come_from_the_ledger() -> None:
    r = run(trio(), always(None))
    lines = by_id(r)
    assert [lines[f"stripe:dp_{i}"].baseline_cost for i in (1, 2, 3)] == [1_500, 11_500, 11_500]
    assert r.cost_source == {"ledger": 3} and r.ledger_reconciled
    assert r.ledger_merchant_net == -(1_500 + 11_500 + 11_500)
    assert r.verdict == "no change" and r.n_estimated == 0
    assert all(x.basis == "observed" for x in r.cases)


def test_accept_everything() -> None:
    lines = by_id(run(trio(), always(Action.ACCEPT)))
    a, b, c = (lines[f"stripe:dp_{i}"] for i in (1, 2, 3))
    assert (a.basis, a.method, a.candidate_cost) == (
        "estimated",
        "accept_chargeback_stands",
        11_500,
    )
    assert (b.basis, b.candidate_cost) == ("observed", 11_500)
    assert (c.basis, c.candidate_cost) == ("estimated", 11_500)


def test_contest_everything_uses_the_simulated_win_rate() -> None:
    lines = by_id(run(trio(), always(Action.CONTEST), contest_fee_minor=200))
    b = lines["stripe:dp_2"]
    p = DEFAULT_PROFILE.win_probability("fraudulent")
    assert (b.basis, b.method) == ("estimated", "contest_simulated_win_rate")
    assert math.isclose(b.candidate_cost, 1_500 + 200 + (1 - p) * 10_000)
    # the default profile was fitted to nothing: its band is the whole [0, 1]
    assert (b.candidate_cost_low, b.candidate_cost_high) == (1_700, 11_700)
    assert lines["stripe:dp_1"].basis == "observed"


def test_escalate_adds_the_escalation_cost() -> None:
    r = run(trio(), always(Action.ESCALATE), escalation_cost_minor=1_000)
    for x in r.cases:
        assert x.basis == "estimated" and x.method == "escalate_historical_plus_cost"
        assert x.candidate_cost == x.baseline_cost + 1_000


def test_without_a_ledger_costs_come_from_the_state() -> None:
    h = history([case(1, "won", submitted=True), case(2, "lost")])
    r = run(h, always(None))
    assert r.cost_source == {"state": 2}
    assert [x.baseline_cost for x in r.cases] == [0, 10_000]


def test_exclusions_are_counted() -> None:
    eur = case(9).model_copy(update={"currency": "EUR"})
    h = history([case(1, "inquiry"), case(2, "open"), case(3, "lost"), eur])
    r = run(h, always(None))
    assert r.n_included == 1 and r.n_input == 4
    assert sum(r.excluded.values()) == 3
    assert set(r.excluded) == {
        "currency other than USD",
        "inquiry or prevented (no chargeback decision)",
        "open (no outcome yet)",
    }


def test_empty_history_is_insufficient() -> None:
    r = run(history([case(1, "inquiry")]), always(Action.ACCEPT))
    assert r.verdict == "insufficient evidence" and r.stats is None and r.by_reason == ()


def bulk(n_won: int, n_accepted: int, n_lost_contested: int) -> History:
    cases: list[ExternalCase] = []
    ms: list[LedgerMovement] = []
    i = 0
    for status, sub, k in (
        ("won", True, n_won),
        ("lost", False, n_accepted),
        ("lost", True, n_lost_contested),
    ):
        for _ in range(k):
            c = case(i, status, submitted=sub, hours=i + 0.25)
            cases.append(c)
            ms += moves(c, credit_days=30 if status == "won" else None)
            i += 1
    return history(cases, ms)


def test_giving_up_won_cases_is_worse() -> None:
    r = run_backtest(bulk(200, 50, 50), always(Action.ACCEPT))
    assert r.verdict == "worse" and r.robust
    assert r.by_reason[0].verdict == "worse" and r.by_reason[0].worse_fwer
    assert r.calibration is not None  # profile fitted from the history's aggregates


def test_contesting_cases_that_usually_win_is_better() -> None:
    r = run_backtest(bulk(280, 300, 120), always(Action.CONTEST))
    assert r.calibration is not None
    assert math.isclose(r.calibration.profile.reasons["fraudulent"].p_win, 0.7)
    assert r.verdict == "better" and r.robust
    assert r.by_reason[0].better_fdr and r.by_reason[0].verdict == "better"
    assert r.n_estimated == 300 and r.n_observed == 400


def test_small_history_reports_insufficient_evidence_and_the_cases_needed() -> None:
    r = run_backtest(bulk(8, 6, 6), always(Action.CONTEST))
    assert r.verdict == "insufficient evidence"
    p = r.by_reason[0].plan
    assert p.cases_needed is not None and not p.sufficient and p.cases == 20
    assert r.calibration is not None and r.calibration.profile.fallbacks
    assert "too few" in to_markdown(r)


def test_reports_are_deterministic() -> None:
    h = bulk(40, 40, 20)
    a = run_backtest(h, always(Action.CONTEST))
    b = run_backtest(h, always(Action.CONTEST))
    assert to_json(a) == to_json(b) and to_markdown(a) == to_markdown(b)


def test_write_report_writes_json_markdown_and_pdf(tmp_path: Path) -> None:
    r = run_backtest(bulk(30, 30, 10), always(Action.CONTEST))
    files = write_report(r, tmp_path)
    assert [f.name for f in files] == ["report.json", "report.md", "report.pdf"]
    doc = json.loads(files[0].read_text())
    assert doc["n_observed"] + doc["n_estimated"] == doc["n_included"]
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(files[2].read_bytes())).pages)
    assert "Backtest" in text and "estimated" in text
    assert write_report(r, tmp_path / "x", pdf=False)[-1].name == "report.md"


def test_markdown_labels_observed_and_estimated() -> None:
    md = to_markdown(run(trio(), always(Action.CONTEST)))
    for word in ("observed", "estimated", "Assumptions", "selection bias"):
        assert word in md
    assert "SYNTHETIC" not in md


# --- properties over histories --------------------------------------------------------


history_rows = st.lists(
    st.tuples(
        st.sampled_from(("won", "lost", "lost", "open", "inquiry")),
        st.booleans(),
        st.sampled_from(("fraudulent", "general", "product_not_received")),
        st.integers(50, 500_000),
        st.booleans(),  # has a ledger
        st.integers(0, 3_000),  # fee
    ),
    min_size=1,
    max_size=60,
)
candidates = st.sampled_from((None, Action.ACCEPT, Action.CONTEST, Action.ESCALATE, "mixed"))


def build_history(rows: list[tuple[Any, ...]]) -> History:
    cases: list[ExternalCase] = []
    ms: list[LedgerMovement] = []
    for i, (status, sub, reason, amount, ledger, fee) in enumerate(rows):
        c = case(i, status, submitted=sub and status != "inquiry", reason=reason, amount=amount)
        cases.append(c)
        if ledger and status != "inquiry":
            ms += moves(c, fee=fee, credit_days=10 if status == "won" else None)
    return from_records(cases, ms, label="hypothesis history (test data)")


def mixed(view: DecisionView) -> Action | None:
    if view.amount_minor % 3 == 0:
        return Action.CONTEST
    if view.amount_minor % 3 == 1:
        return Action.ACCEPT
    return None


@settings(max_examples=40)
@given(history_rows, candidates)
def test_labels_and_totals_are_consistent(rows: list[tuple[Any, ...]], c: Any) -> None:
    h = build_history(rows)
    cand = ModuleCandidate(mixed, "mixed") if c == "mixed" else always(c)
    r = run(h, cand)
    assert r.n_observed + r.n_estimated == r.n_included == len(r.cases)
    assert r.n_included + sum(r.excluded.values()) == r.n_input
    for x in r.cases:
        assert x.candidate_cost_low <= x.candidate_cost <= x.candidate_cost_high
        assert abs(x.candidate_cost_high - x.baseline_cost) <= x.cap
        assert abs(x.candidate_cost_low - x.baseline_cost) <= x.cap
        if x.basis == "observed":
            assert x.candidate_action is x.historical_action and x.method == "observed"
            assert x.candidate_cost == x.baseline_cost == x.candidate_cost_high
        else:
            assert x.candidate_action is not x.historical_action
    assert math.isclose(r.baseline_cost, sum(x.baseline_cost for x in r.cases), abs_tol=0.05)
    assert math.isclose(
        r.candidate_cost, r.candidate_cost_observed + r.candidate_cost_estimated, abs_tol=0.05
    )
    assert sum(x.n for x in r.by_reason) == r.n_included
    for rr in r.by_reason:
        assert rr.n_observed + rr.n_estimated == rr.n
        if rr.n_estimated == 0:
            assert rr.verdict == "no change"
    assert r.ledger_reconciled
    if r.n_estimated == 0:
        assert r.verdict in ("no change", "insufficient evidence")


@settings(max_examples=15)
@given(history_rows)
def test_same_input_same_report(rows: list[tuple[Any, ...]]) -> None:
    h = build_history(rows)
    assert to_json(run(h, always(Action.CONTEST))) == to_json(run(h, always(Action.CONTEST)))


def test_time_order_is_used_and_shuffle_is_seeded() -> None:
    h = bulk(40, 40, 20)
    a = run_backtest(h, always(Action.CONTEST), config=BacktestConfig(shuffle_seed=3))
    b = run_backtest(h, always(Action.CONTEST), config=BacktestConfig(shuffle_seed=3))
    assert to_json(a) == to_json(b)
    assert all(
        x.opened_at <= y.opened_at
        for x, y in zip(run(h, always(None)).cases, run(h, always(None)).cases[1:], strict=False)
    )
