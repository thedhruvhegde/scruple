"""Shadow mode on imported cases: real decisions, observable scoring, zero side effects.

Inputs: the vendored real Stripe dispute (edited copies) and SYNTHETIC Stripe-shaped
histories from `scruple.calibration.stripe_disputes`."""

from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.backtest import rule_candidate
from scruple.backtest.candidate import CandidateDecision
from scruple.backtest.view import DecisionView
from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.calibration.facts import contested
from scruple.domain import Action
from scruple.gate import shadow_imported as mod
from scruple.gate.shadow_imported import (
    ImportedDecision,
    ImportedOutcome,
    ImportedShadowRecorder,
    ImportedShadowSummary,
)
from scruple.ingest.models import CaseState, ExternalCase, LedgerMovement, MovementKind
from scruple.ingest.stripe.mapping import ingest_dispute
from scruple.sim.library import GOOD_RULE_ID
from scruple.sim.models import Stage
from tests.test_gate_shadow import _gate

# These tests exercise the gate through the API, so they use the development-only
# X-Actor trust; tests/test_auth_*.py cover real authentication.
DEV_AUTH = Authenticator(dev_actor_header=True)
READER = {"X-Actor": "auditor"}  # default caller for reads; writes set their own

FIX = Path(__file__).parent / "fixtures" / "stripe"
REAL: dict[str, Any] = json.loads((FIX / "fixtures3.subset.json").read_text())["resources"]
GOOD = json.loads(Path("docs/pilot/demo/good.json").read_text())


class Always:
    def __init__(self, action: Action | None) -> None:
        self.action = action
        self.name = f"always-{action}"

    def decide(self, view: DecisionView) -> CandidateDecision:
        return CandidateDecision(action=self.action, ref=self.name)


def synthetic(n: int = 60, seed: int = 4) -> list[tuple[ExternalCase, tuple[LedgerMovement, ...]]]:
    out = []
    for o in stripe_disputes(DEFAULT_PROFILE, n, seed):
        b = ingest_dispute(o)
        out.append((b.cases[0], b.movements))
    return out


def decisions(
    rows: list[tuple[ExternalCase, tuple[LedgerMovement, ...]]],
    hours: float = 1.0,
    *,
    as_opened: bool = False,
) -> list[ImportedDecision]:
    """Production's action as the history shows it, decided an hour after opening.
    With `as_opened`, the case is sent as it looked then (the outcome comes later)."""
    return [
        ImportedDecision(
            case=opened_version(c) if as_opened else c,
            action=Action.CONTEST if contested(c) else Action.ACCEPT,
            decided_at=c.opened_at + timedelta(hours=hours),
            movements=tuple(m for m in mv if m.occurred_at <= c.opened_at),
        )
        for c, mv in rows
    ]


def opened_version(c: ExternalCase) -> ExternalCase:
    """The case as an export taken when it opened would show it."""
    return c.model_copy(
        update={"stage": Stage.CHARGEBACK, "state": CaseState.OPEN, "evidence_submitted": False}
    )


def real_case(**over: Any) -> ExternalCase:
    d = copy.deepcopy(REAL["dispute"])
    d.update(over)
    return ingest_dispute(d).cases[0]


# --- side effects -------------------------------------------------------------------


def test_module_imports_nothing_that_can_write() -> None:
    tree = ast.parse(Path(mod.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    forbidden = (
        "scruple.storage",
        "scruple.service",
        "scruple.audit",
        "scruple.registry",
        "scruple.api",
        "scruple.ingest.store",
        "scruple.backtest.report",
        "sqlite3",
    )
    assert not {m for m in mods if m and m.startswith(forbidden)}


def test_its_dependencies_load_no_writer_transitively() -> None:
    """Everything the module imports (the `scruple.gate` package itself aside, whose
    __init__ is the gate) loads no storage, service, store or audit log."""
    tree = ast.parse(Path(mod.__file__).read_text())
    deps = sorted(
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("scruple.")
    )
    code = (
        "import importlib, sys\n"
        f"for m in {deps!r}: importlib.import_module(m)\n"
        "bad = [m for m in ('scruple.storage', 'scruple.service', 'scruple.ingest.store', "
        "'scruple.audit.log', 'sqlite3') if m in sys.modules]\n"
        "print(bad)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=os.environ
    )
    assert deps and out.stdout.strip() == "[]"


def test_gate_recorder_on_imported_cases_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    g, st_ = _gate()
    before_writes, before_audit = list(st_.writes), len(g.audit.entries())
    before_decisions = st_.count_decisions()
    rows = synthetic()
    batch = decisions(rows, as_opened=True)
    frozen = [d.model_dump_json() for d in batch]
    rec = g.shadow_recorder(GOOD_RULE_ID, 1)
    st_.writes.clear()
    rec.record_imported(batch)
    rec.observe_imported([ImportedOutcome(case=c, movements=mv) for c, mv in rows])
    summary = rec.summary_imported()
    assert st_.writes == [] and len(g.audit.entries()) == before_audit
    assert st_.count_decisions() == before_decisions and before_writes
    assert list(tmp_path.iterdir()) == []
    assert [d.model_dump_json() for d in batch] == frozen
    assert summary.decisions == len(rows)
    assert not any(hasattr(r, "execute") for r in (summary, rec.imported()))


# --- scoring -------------------------------------------------------------------------


def test_every_metric_is_labelled_and_changed_actions_are_estimated() -> None:
    rows = synthetic(80)
    rec = ImportedShadowRecorder(Always(Action.ACCEPT), synthetic=True)
    rec.record(decisions(rows, as_opened=True))
    rec.observe([ImportedOutcome(case=c, movements=mv) for c, mv in rows])
    s = rec.summary()
    assert s.data == "synthetic" and "uncalibrated" in s.profile
    assert {m.basis for m in s.metrics} <= {"observed", "estimated"}
    assert s.metric("agreement").basis == "observed"
    changed = s.metric("candidate_cost_changed_action")
    assert changed.basis == "estimated" and changed.n > 0
    assert changed.low is not None and changed.high is not None
    assert changed.low <= (changed.value or 0) <= changed.high
    assert s.metric("candidate_minus_production").basis == "estimated"
    same = s.metric("candidate_cost_same_action")
    assert same.basis == "observed" and same.n + changed.n == s.metric("production_cost").n


def test_before_outcomes_only_observed_metrics_have_values() -> None:
    rows = synthetic(30)
    rec = ImportedShadowRecorder(Always(Action.CONTEST))
    rec.record(decisions(rows, as_opened=True))
    s = rec.summary()
    assert s.resolved == 0 and s.open == 30 and s.data == "real (imported)"
    assert s.metric("production_cost").value is None
    assert s.metric("candidate_minus_production").value is None
    assert s.metric("agreement").value is not None


def test_candidate_that_never_differs_is_all_observed_and_zero() -> None:
    rows = synthetic(50)
    rec = ImportedShadowRecorder(Always(None))
    rec.record(decisions(rows))
    s = rec.summary()
    assert s.metric("agreement").value == 1.0
    d = s.metric("candidate_minus_production")
    assert d.basis == "observed" and d.value == 0.0
    assert s.metric("candidate_cost_changed_action").value is None


def test_rule_candidate_from_the_demo_and_real_fixture() -> None:
    case = real_case(status="lost", reason="product_not_received")
    rec = ImportedShadowRecorder(rule_candidate(GOOD))
    rec.record([ImportedDecision(case=case, action=Action.ACCEPT, decided_at=case.opened_at)])
    s = rec.summary()
    # Delivery is not in the export, so the rule (delivered INR) does not fire.
    assert s.metric("agreement").value == 1.0 and s.resolved == 1


def test_deadline_check_on_both_actions() -> None:
    case = real_case(status="lost")
    assert case.evidence_due_at is not None
    late = case.evidence_due_at + timedelta(days=1)
    rec = ImportedShadowRecorder(Always(Action.CONTEST))
    rec.record([ImportedDecision(case=case, action=Action.ACCEPT, decided_at=late)])
    [f] = [f for f in rec.summary().findings if f.check == "contest_after_deadline"]
    assert (f.kind, f.production, f.candidate) == ("deadline", 0, 1)


def test_process_and_evidence_findings() -> None:
    case = real_case(status="lost")
    rec = ImportedShadowRecorder(Always(None))
    rec.record(
        [
            ImportedDecision(
                case=case, action=Action.CONTEST, decided_at=case.opened_at - timedelta(hours=1)
            )
        ]
    )
    found = {f.check: f for f in rec.summary().findings}
    assert found["decided_before_open"].production == 1
    assert found["contest_without_evidence"].production == 1
    assert found["contest_without_evidence"].candidate is None


def _move(case: ExternalCase, mid: str, amount: int, **over: Any) -> LedgerMovement:
    base: dict[str, Any] = {
        "source": case.source,
        "external_id": mid,
        "case_external_id": case.external_id,
        "payment_ref": case.payment_ref,
        "kind": MovementKind.DISPUTE_DEBIT,
        "amount_minor": amount,
        "fee_minor": 0,
        "net_minor": amount,
        "currency": case.currency,
        "occurred_at": case.opened_at,
        "category": "dispute",
    }
    base.update(over)
    return LedgerMovement(**base)


def test_ledger_findings() -> None:
    case = real_case(status="lost")
    moves = (
        _move(case, "t1", -case.amount_minor - 1),
        _move(case, "t2", -1, case_external_id="dp_other"),
        _move(case, "t3", -1, currency="EUR"),
    )
    rec = ImportedShadowRecorder(Always(None))
    rec.record(
        [
            ImportedDecision(
                case=case, action=Action.ACCEPT, decided_at=case.opened_at, movements=moves
            )
        ]
    )
    rec.observe([ImportedOutcome(case=case, movements=(_move(case, "t1", -5),))])
    found = {f.check: f.production for f in rec.summary().findings}
    assert found == {
        "ledger_exceeds_dispute": 1,
        "ledger_wrong_case": 1,
        "ledger_currency_mismatch": 1,
    }


def test_partner_checks() -> None:
    def big_contest(view: DecisionView, action: Action) -> bool:
        return action is Action.CONTEST and view.amount_minor > 500

    case = real_case()
    rec = ImportedShadowRecorder(
        Always(Action.CONTEST), extra_checks=[("no_big_contest", big_contest)]
    )
    rec.record([ImportedDecision(case=case, action=Action.ACCEPT, decided_at=case.opened_at)])
    [f] = [f for f in rec.summary().findings if f.check == "no_big_contest"]
    assert (f.kind, f.production, f.candidate) == ("partner", 0, 1)
    with pytest.raises(ValueError):
        ImportedShadowRecorder(Always(None), extra_checks=[("ledger_conflict", big_contest)])


def test_duplicates_and_mismatched_outcomes_are_refused() -> None:
    case = real_case()
    d = ImportedDecision(case=case, action=Action.ACCEPT, decided_at=case.opened_at)
    rec = ImportedShadowRecorder(Always(None))
    with pytest.raises(ValueError, match="already recorded"):
        rec.record([d, d])
    rec.record([d])
    with pytest.raises(ValueError, match="already recorded"):
        rec.record([d])
    with pytest.raises(ValueError):
        rec.record([])
    with pytest.raises(ValueError, match="does not match"):
        rec.observe([ImportedOutcome(case=case.model_copy(update={"amount_minor": 1}))])
    other = real_case(id="dp_unknown")
    assert rec.observe([ImportedOutcome(case=other)]).unknown == 1


def test_look_ahead_is_impossible_for_the_candidate() -> None:
    class Peeker:
        name = "peeker"

        def decide(self, view: DecisionView) -> CandidateDecision:
            return CandidateDecision(action=Action(view.state), ref="peek")

    rec = ImportedShadowRecorder(Peeker())
    case = real_case()
    from scruple.backtest.view import LookAheadError

    with pytest.raises(LookAheadError):
        rec.record([ImportedDecision(case=case, action=Action.ACCEPT, decided_at=case.opened_at)])


def test_summary_is_deterministic_and_order_free() -> None:
    rows = synthetic(40, seed=9)
    a, b = (
        ImportedShadowRecorder(Always(Action.ACCEPT)),
        ImportedShadowRecorder(Always(Action.ACCEPT)),
    )
    ds = decisions(rows)
    a.record(ds)
    b.record(ds[20:])
    b.record(ds[:20])
    assert a.summary().model_dump_json() == b.summary().model_dump_json()


@given(
    actions=st.lists(st.sampled_from(list(Action)), min_size=1, max_size=30),
    cand=st.sampled_from([*Action, None]),
)
def test_property_accounting(actions: list[Action], cand: Action | None) -> None:
    rows = synthetic(len(actions), seed=len(actions))
    ds = [
        ImportedDecision(case=c, action=a, decided_at=c.opened_at, movements=mv)
        for (c, mv), a in zip(rows, actions, strict=True)
    ]
    rec = ImportedShadowRecorder(Always(cand))
    res = rec.record(ds)
    s = rec.summary()
    assert s.decisions == res.recorded == len(ds) == s.resolved + s.open
    agree = s.metric("agreement").value
    assert agree is not None and 0 <= agree <= 1
    assert sum(r["n"] for r in s.by_reason.values()) == len(ds)
    same, changed = (
        s.metric("candidate_cost_same_action"),
        s.metric("candidate_cost_changed_action"),
    )
    assert same.n + changed.n == s.metric("production_cost").n
    if changed.n == 0:
        assert s.metric("candidate_minus_production").basis == "observed"


# --- API -------------------------------------------------------------------------------


def test_api_imported_routes(tmp_path: Path) -> None:
    g, st_ = _gate()
    client = TestClient(create_app(g, auth=DEV_AUTH), headers=READER)
    url = f"/shadow/rules/{GOOD_RULE_ID}/versions/1/imported"
    rows = synthetic(20)
    body = {"decisions": [json.loads(d.model_dump_json()) for d in decisions(rows, as_opened=True)]}
    st_.writes.clear()
    hdr = {"X-Actor": "production"}
    r = client.post(f"{url}/decisions", json=body, headers=hdr)
    assert r.status_code == 200 and r.json()["recorded"] == 20
    assert client.post(f"{url}/decisions", json=body, headers=hdr).status_code == 409
    assert client.post(f"{url}/decisions", json=body, headers={"X-Actor": "x"}).status_code == 403
    out = {"outcomes": [{"case": json.loads(c.model_dump_json())} for c, _ in rows]}
    r = client.post(f"{url}/outcomes", json=out, headers=hdr)
    assert r.status_code == 200 and r.json()["updated"] == 20
    s = ImportedShadowSummary.model_validate(client.get(url).json())
    assert s.decisions == 20 and s.resolved > 0
    assert st_.writes == []
