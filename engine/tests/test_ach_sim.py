"""ACH returns model: rules, windows, the seeded generator and the event-sourced engine."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.ingest.nacha_codes import RETURN_REASONS
from scruple.sim.ach.engine import (
    BAD_DEBT,
    CASH,
    CONTRA,
    AcceptReturn,
    AchAction,
    AchEngine,
    ActionError,
    Dishonor,
    Reinitiate,
    WriteOff,
)
from scruple.sim.ach.generator import family_weights, generate_ach_case, generate_ach_cases
from scruple.sim.ach.models import FAMILIES, AchFamily
from scruple.sim.ach.rules import (
    CODES,
    CONTEST_CODES,
    DISHONOR_CODES,
    REINIT_DESCRIPTION,
    UNAUTHORIZED_ENTRY_FEE_MINOR,
    Group,
    Reinit,
    Window,
    dishonor_deadline,
    is_timely,
    reinit_deadline,
    return_deadline,
)
from scruple.sim.deadlines import BusinessCalendar

CAL = BusinessCalendar()
MON = date(2026, 3, 2)  # a Monday
F = AchFamily


# --- rules ------------------------------------------------------------------------------
def test_every_code_has_a_moov_title_and_a_source() -> None:
    for code, rc in CODES.items():
        assert rc.title == RETURN_REASONS[code]
        assert rc.source
    for c in (*DISHONOR_CODES, *CONTEST_CODES):
        assert c in RETURN_REASONS


@pytest.mark.parametrize("code", ["R05", "R07", "R10", "R11"])
def test_unauthorized_consumer_codes_have_the_sixty_day_window(code: str) -> None:
    rc = CODES[code]
    assert rc.window is Window.SIXTY_CALENDAR_DAYS
    assert rc.unauthorized_fee and rc.group is Group.UNAUTHORIZED
    assert return_deadline(code, MON, CAL) == MON + timedelta(days=60)


@pytest.mark.parametrize("code", ["R01", "R02", "R03", "R04", "R08", "R09", "R16", "R20", "R29"])
def test_other_codes_have_the_two_banking_day_window(code: str) -> None:
    assert CODES[code].window is Window.TWO_BANKING_DAYS
    assert return_deadline(code, MON, CAL) == MON + timedelta(days=2)


def test_r29_is_unauthorized_but_two_day_and_charged_the_fee() -> None:
    rc = CODES["R29"]
    assert rc.group is Group.UNAUTHORIZED and rc.unauthorized_fee
    assert rc.window is Window.TWO_BANKING_DAYS


def test_only_r01_and_r09_are_reinitiable() -> None:
    assert {c for c, rc in CODES.items() if rc.reinit is Reinit.NSF} == {"R01", "R09"}


@pytest.mark.parametrize(
    ("settled", "holidays", "due"),
    [
        (date(2026, 3, 5), (), date(2026, 3, 9)),  # Thursday -> Monday
        (date(2026, 3, 6), (), date(2026, 3, 10)),  # Friday -> Tuesday
        (date(2026, 3, 2), (date(2026, 3, 3),), date(2026, 3, 5)),  # holiday skipped
    ],
)
def test_two_banking_days_skip_weekends_and_holidays(
    settled: date, holidays: tuple[date, ...], due: date
) -> None:
    cal = BusinessCalendar(frozenset(holidays))
    assert return_deadline("R01", settled, cal) == due
    assert is_timely("R01", settled, due, cal)
    assert not is_timely("R01", settled, due + timedelta(days=1), cal)


def test_dishonor_and_reinit_windows() -> None:
    assert dishonor_deadline(MON, CAL) == MON + timedelta(days=7)  # five banking days
    assert reinit_deadline(MON) == MON + timedelta(days=180)
    assert REINIT_DESCRIPTION == "RETRY PYMT"
    assert UNAUTHORIZED_ENTRY_FEE_MINOR == 450


# --- generator --------------------------------------------------------------------------
@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("difficulty", [0.0, 0.5, 1.0])
def test_generator_is_deterministic_and_consistent(family: AchFamily, difficulty: float) -> None:
    for seed in range(15):
        a = generate_ach_case(seed, family, difficulty)
        assert a == generate_ach_case(seed, family, difficulty)
        assert a.truth.family is family
        assert a.truth.expected.total == a.amount_minor
        cal = BusinessCalendar(frozenset(a.holidays))
        assert cal.is_business_day(a.settled_on)
        first = a.returns[0]
        timely = is_timely(first.code, a.settled_on, first.settles_on, cal)
        assert timely == (family is not F.UNTIMELY_RETURN)
        if family is F.R05_EXTENDED:
            assert a.sec_code == "CCD" and first.code == "R05"
            assert first.settles_on > cal.add_business_days(a.settled_on, 2)
        if family is F.DUPLICATE_RETURN:
            assert len(a.returns) == 2 and a.returns[1].code == first.code
        if family in (F.UNAUTHORIZED, F.STOP_PAYMENT) and first.code != "R29":
            assert a.consumer


def test_case_ids_and_views_do_not_reveal_the_family() -> None:
    for family in FAMILIES:
        c = generate_ach_case(4, family)
        text = str(c.view()).lower()
        assert family.value not in c.case_id
        for word in ("truth", "funds_on", "insolvent", "duplicate", "untimely"):
            assert word not in text


def test_difficulty_raises_the_share_of_traps() -> None:
    lo, hi = family_weights(0.0), family_weights(1.0)
    assert lo[F.R05_EXTENDED] < lo[F.UNAUTHORIZED]
    assert hi[F.R05_EXTENDED] == hi[F.UNAUTHORIZED]
    holidays = [bool(generate_ach_case(s, None, d).holidays) for d in (0.0, 1.0) for s in range(60)]
    assert sum(holidays[:60]) == 0 < sum(holidays[60:])


def test_generate_many_covers_every_family() -> None:
    cases = generate_ach_cases(1, 300)
    assert {c.truth.family for c in cases} == set(FAMILIES)
    assert len({c.case_id for c in cases}) == 300


def test_difficulty_out_of_range() -> None:
    with pytest.raises(ValueError, match="difficulty"):
        generate_ach_case(1, None, 1.5)


# --- engine -----------------------------------------------------------------------------
def _engine(family: AchFamily, seed: int = 0, difficulty: float = 0.0) -> AchEngine:
    return AchEngine(generate_ach_case(seed, family, difficulty))


def test_opening_ledger_invoice_debit_return() -> None:
    eng = _engine(F.ACCOUNT_CLOSED)
    kinds = [e.kind for e in eng.journal]
    assert kinds == ["invoice", "ach_debit", "ach_return"]
    assert eng.receivable() == eng.case.amount_minor
    assert eng.ledger.balance(CASH, "USD") == 0


def test_unauthorized_return_charges_the_fee() -> None:
    eng = _engine(F.UNAUTHORIZED)
    assert [e.kind for e in eng.journal][-1] == "ach_fee"
    assert eng.outcome().fees_minor == UNAUTHORIZED_ENTRY_FEE_MINOR


def test_accept_voids_and_write_off_books_bad_debt() -> None:
    a = _engine(F.UNAUTHORIZED)
    assert a.act(AcceptReturn()).ok
    assert a.ledger.balance(CONTRA, "USD") == a.case.amount_minor and a.receivable() == 0
    assert a.outcome().correct
    w = _engine(F.ACCOUNT_CLOSED)
    assert w.act(WriteOff()).ok
    assert w.ledger.balance(BAD_DEBT, "USD") == w.case.amount_minor
    assert w.outcome().correct
    r = w.act(AcceptReturn())
    assert not r.ok and r.error is ActionError.NOTHING_OPEN


def test_untimely_return_dishonored_r68_stands_and_collects() -> None:
    eng = _engine(F.UNTIMELY_RETURN)
    assert eng.act(Dishonor(return_id="RET-1", code="R68")).ok
    assert eng.act(WriteOff()).error is ActionError.PENDING
    eng.run_out()
    o = eng.outcome()
    assert o.final_dishonors == 1 and o.contested_dishonors == 0
    assert o.allocation.collected_minor == eng.case.amount_minor and o.correct


@pytest.mark.parametrize(
    ("family", "code", "contest"),
    [
        (F.ACCOUNT_CLOSED, "R68", "R73"),  # a timely return is not untimely
        (F.ACCOUNT_CLOSED, "R67", "R75"),  # nor a duplicate
        (F.R05_EXTENDED, "R68", "R73"),  # the 60-day window applies to R05
    ],
)
def test_improper_dishonor_is_contested(family: AchFamily, code: str, contest: str) -> None:
    eng = _engine(family)
    assert eng.act(Dishonor(return_id="RET-1", code=code)).ok
    eng.run_out()
    o = eng.outcome()
    assert o.contested_dishonors == 1
    assert eng.receivable() == eng.case.amount_minor
    contested = [e for e in eng.events if e.type == "dishonor_contested"]
    assert contested[0].code == contest


def test_late_dishonor_is_contested_r72() -> None:
    eng = _engine(F.UNTIMELY_RETURN)
    eng.advance(10)
    assert eng.act(Dishonor(return_id="RET-1", code="R68")).ok
    eng.run_out()
    assert [e.code for e in eng.events if e.type == "dishonor_contested"] == ["R72"]


def test_duplicate_return_dishonor_r67_then_write_off() -> None:
    eng = _engine(F.DUPLICATE_RETURN)
    eng.advance(6)
    assert eng.receivable() == 2 * eng.case.amount_minor
    assert eng.act(Dishonor(return_id="RET-2", code="R67")).ok
    eng.run_out()
    assert eng.act(WriteOff()).ok
    assert eng.outcome().correct


def test_reinitiation_after_payday_collects() -> None:
    case = generate_ach_case(0, F.NSF_RECOVERABLE, 0.0)
    eng = AchEngine(case)
    assert case.truth.funds_on is not None
    eng.advance((case.truth.funds_on - eng.today).days)
    r = eng.act(Reinitiate(company_entry_description=REINIT_DESCRIPTION))
    assert r.ok and not r.violations
    eng.run_out()
    assert eng.outcome().correct and eng.outcome().retries == 1


def test_reinitiation_before_funds_is_returned_r01() -> None:
    eng = _engine(F.NSF_INSOLVENT)
    for _ in range(2):
        assert eng.act(Reinitiate(company_entry_description=REINIT_DESCRIPTION)).ok
        eng.run_out()
    codes = [r.code for r in eng.settled_returns()]
    assert codes[1:] == ["R01", "R01"]
    third = eng.act(Reinitiate(company_entry_description=REINIT_DESCRIPTION))
    assert third.ok and third.violations == ("reinit_limit",)
    eng.run_out()
    assert eng.act(WriteOff()).ok
    assert eng.outcome().violations == ("reinit_limit",)


@pytest.mark.parametrize("family", [F.UNAUTHORIZED, F.ACCOUNT_CLOSED, F.STOP_PAYMENT])
def test_reinitiating_a_non_nsf_return_is_a_violation_and_comes_back(family: AchFamily) -> None:
    eng = _engine(family)
    r = eng.act(Reinitiate(company_entry_description="INVOICE"))
    assert set(r.violations) == {"reinit_code", "reinit_description"}
    eng.advance(1)
    eng.run_out()
    assert eng.settled_returns()[-1].code == eng.case.returns[0].code
    assert eng.receivable() == eng.case.amount_minor


def test_reinitiation_outside_180_days_is_a_violation() -> None:
    eng = _engine(F.NSF_INSOLVENT)
    eng.advance(200)
    r = eng.act(Reinitiate(company_entry_description=REINIT_DESCRIPTION))
    assert "reinit_window" in r.violations


def test_refusals_change_nothing() -> None:
    eng = _engine(F.ACCOUNT_CLOSED)
    before = eng.state_digest()
    assert eng.act(Dishonor(return_id="RET-9", code="R68")).error is ActionError.UNKNOWN_RETURN
    assert eng.act(Dishonor(return_id="RET-1", code="R70")).error is ActionError.BAD_CODE
    assert eng.state_digest() == before
    eng.act(Dishonor(return_id="RET-1", code="R68"))
    assert eng.act(Dishonor(return_id="RET-1", code="R67")).error is ActionError.ALREADY_DISHONORED


def test_replay_and_json_round_trip() -> None:
    eng = _engine(F.DUPLICATE_RETURN, seed=3, difficulty=0.7)
    eng.advance(6)
    eng.act(Dishonor(return_id="RET-2", code="R67"))
    eng.run_out()
    eng.act(WriteOff())
    back = AchEngine.replay(eng.case, AchEngine.events_from_json(eng.events_json()))
    assert back.state_digest() == eng.state_digest()
    assert back.journal == eng.journal
    assert back.outcome() == eng.outcome()


# --- properties: the ledger is always balanced, nothing is invented ---------------------
_ACTIONS = st.one_of(
    st.just(AcceptReturn()),
    st.just(WriteOff()),
    st.builds(
        Dishonor,
        return_id=st.sampled_from(["RET-1", "RET-2", "RET-R1"]),
        code=st.sampled_from(["R67", "R68"]),
    ),
    st.builds(Reinitiate, company_entry_description=st.sampled_from([REINIT_DESCRIPTION, "X"])),
)
_STEPS = st.lists(st.tuples(_ACTIONS, st.integers(0, 9)), max_size=12)


@given(
    seed=st.integers(0, 10_000),
    family=st.sampled_from(FAMILIES),
    difficulty=st.floats(0, 1),
    steps=_STEPS,
)
def test_property_ledger_balanced_and_allocation_sums(
    seed: int, family: AchFamily, difficulty: float, steps: list[tuple[AchAction, int]]
) -> None:
    eng = AchEngine(generate_ach_case(seed, family, difficulty))
    for action, wait in steps:
        eng.act(action)
        eng.advance(wait)
        assert eng.receivable() >= 0
        assert eng.outcome().allocation.collected_minor <= eng.case.amount_minor
    eng.run_out()
    for e in eng.journal:
        assert sum(p.amount_minor for p in e.postings) == 0
    o = eng.outcome()
    assert o.allocation.total == eng.case.amount_minor
    assert o.cost_minor == eng.case.amount_minor - o.allocation.collected_minor + o.fees_minor
    back = AchEngine.replay(eng.case, eng.events)
    assert back.state_digest() == eng.state_digest()
