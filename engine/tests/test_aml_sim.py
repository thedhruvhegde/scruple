"""AML alert triage environment: generator, transaction graph invariants, engine."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.sim.aml.engine import (
    RFI_DAYS,
    RFI_MAX,
    ActionError,
    AmlEngine,
    CloseAlert,
    Escalate,
    FileSar,
    RequestInfo,
    decision_cost,
)
from scruple.sim.aml.generator import PATTERNS, generate_aml_case, generate_aml_cases
from scruple.sim.aml.models import CASH, FAMILIES, TYPOLOGIES, AmlCase, AmlFamily

GENERATED = tuple(f for f in FAMILIES if f in PATTERNS)


def test_every_family_has_a_generator() -> None:
    assert set(GENERATED) == set(FAMILIES)


@pytest.mark.parametrize("family", GENERATED)
@pytest.mark.parametrize("difficulty", [0.0, 0.5, 1.0])
def test_cases_are_deterministic(family: AmlFamily, difficulty: float) -> None:
    a = generate_aml_case(3, family, difficulty)
    assert a == generate_aml_case(3, family, difficulty)
    assert a != generate_aml_case(4, family, difficulty)
    assert a.truth.family is family
    assert a.truth.suspicious == (family in TYPOLOGIES)
    assert a.truth.expected == ("file_sar" if family in TYPOLOGIES else "close")


def test_bad_difficulty_is_refused() -> None:
    with pytest.raises(ValueError):
        generate_aml_case(1, None, 1.5)


def test_seed_picks_families() -> None:
    fams = {c.truth.family for c in generate_aml_cases(1, 60)}
    assert fams == set(GENERATED)


def assert_graph_invariants(case: AmlCase) -> None:
    """No money invented: every transfer moves a positive amount between known
    accounts, the subject never spends money it does not hold, and its closing
    balance is its opening balance plus what came in less what went out."""
    ids = {a.account_id for a in case.accounts}
    for t in case.transfers:
        assert t.amount_minor > 0
        assert {t.src, t.dst} <= ids | {CASH}
    balances = case.subject_balances()
    assert all(b >= 0 for b in balances)
    sid = case.subject_id
    ins = sum(t.amount_minor for t in case.transfers if t.dst == sid)
    outs = sum(t.amount_minor for t in case.transfers if t.src == sid)
    assert balances[-1] == case.opening_balance_minor + ins - outs
    a = case.alert
    for x in a.linked_txn_ids:
        t = case.transfer(x)
        assert sid in (t.src, t.dst)
    assert {case.transfer(x).at.date() for x in case.truth.typology_txn_ids} <= {
        t.at.date() for t in case.transfers if a.window_start <= t.at.date() <= a.window_end
    }
    assert case.lookback_start <= case.transfers[0].at.date()
    assert case.transfers[-1].at.date() < a.raised_on


@given(
    seed=st.integers(0, 10_000),
    family=st.sampled_from(GENERATED),
    difficulty=st.floats(0, 1),
)
@settings(max_examples=60, deadline=None)
def test_no_money_is_invented_in_any_graph(seed: int, family: AmlFamily, difficulty: float) -> None:
    assert_graph_invariants(generate_aml_case(seed, family, difficulty))


def test_view_hides_the_truth_and_the_rfi_reply() -> None:
    for fam in GENERATED:
        c = generate_aml_case(5, fam)
        text = json.dumps(c.view()).lower()
        assert fam.value not in text and "truth" not in text and "typology" not in text
        assert "rfi" not in text and c.view()["synthetic"] is True


def test_external_accounts_show_no_kyc() -> None:
    c = generate_aml_case(2, GENERATED[0])
    ext = next(a for a in c.accounts if not a.internal)
    assert "occupation" not in ext.view() and "opened_on" not in ext.view()
    assert "occupation" in c.subject.view()


def test_cash_business_files_ctrs_and_keeps_to_its_profile() -> None:
    for seed in range(20):
        c = generate_aml_case(seed, "cash_business", 1.0)
        cash = [c.transfer(x) for x in c.alert.linked_txn_ids]
        assert all(t.channel == "cash" for t in cash)
        assert any(t.amount_minor > 1_000_000 for t in cash)  # a CTR was due
        declared = c.subject.expected_monthly_in_minor
        assert declared is not None and sum(t.amount_minor for t in cash) < declared


# --- engine -----------------------------------------------------------------------------
def test_one_decision_ends_the_alert() -> None:
    c = generate_aml_case(1, "payroll")
    eng = AmlEngine(c)
    assert eng.allowed_actions() == ("request_info", "close", "escalate", "file_sar")
    assert eng.act(CloseAlert(reason="payroll")).ok
    again = eng.act(Escalate())
    assert not again.ok and again.error is ActionError.DECIDED
    o = eng.outcome()
    assert o.decision == "close" and o.correct and o.cost == 0


def test_requests_for_information_take_days_and_are_capped() -> None:
    c = generate_aml_case(2, "marketplace_seller")
    eng = AmlEngine(c)
    start = eng.today
    for _ in range(RFI_MAX):
        assert eng.act(RequestInfo(question="what are these payments?")).ok
    assert (eng.today - start).days == RFI_DAYS * RFI_MAX
    refused = eng.act(RequestInfo(question="again?"))
    assert refused.error is ActionError.RFI_LIMIT
    assert eng.info_replies()[0]["responded"] == c.rfi.responded


def test_unknown_transfers_are_refused() -> None:
    eng = AmlEngine(generate_aml_case(3, "property_purchase"))
    r = eng.act(FileSar(txn_ids=("T-99999",), amount_minor=1))
    assert r.error is ActionError.UNKNOWN_TXN and not eng.decided


def test_replay_rebuilds_the_engine() -> None:
    c = generate_aml_case(4, "cash_business")
    eng = AmlEngine(c)
    eng.act(RequestInfo(question="source of cash?"))
    eng.act(Escalate(indicators=("x",), txn_ids=c.alert.linked_txn_ids[:2]))
    again = AmlEngine.replay(c, AmlEngine.events_from_json(eng.events_json()))
    assert again.state_digest() == eng.state_digest()
    assert again.outcome() == eng.outcome()


@pytest.mark.parametrize(
    ("suspicious", "decision", "cost"),
    [
        (True, "file_sar", 0),
        (True, "escalate", 10),
        (True, "close", 100),
        (True, None, 100),
        (False, "close", 0),
        (False, "escalate", 5),
        (False, "file_sar", 20),
        (False, None, 20),
    ],
)
def test_decision_costs_weigh_against_false_negatives(
    suspicious: bool, decision: str | None, cost: int
) -> None:
    assert decision_cost(suspicious, decision) == cost  # type: ignore[arg-type]
    # a missed typology costs more than any mistake on a false positive
    assert decision_cost(True, "close") > 4 * max(
        decision_cost(False, d) for d in ("escalate", "file_sar")
    )
