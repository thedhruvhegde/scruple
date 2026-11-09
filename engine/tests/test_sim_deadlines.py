from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.clock import SteppingClock
from scruple.sim.deadlines import (
    STAGE_WINDOW,
    BusinessCalendar,
    ManualClock,
    filing_deadline,
    stage_deadline,
    today,
    window_end,
)
from scruple.sim.models import Stage
from scruple.sim.networks import DayKind, Network, TimeWindow, WindowId, rules_for

CAL = BusinessCalendar()
dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
windows = st.sampled_from([rules_for(n).window(w) for n in Network for w in WindowId])


def test_business_days_skip_weekends() -> None:
    fri = date(2026, 3, 6)
    assert CAL.add_business_days(fri, 1) == date(2026, 3, 9)  # Monday
    assert CAL.add_business_days(fri, 5) == date(2026, 3, 13)
    assert CAL.add_business_days(fri, 0) == fri


def test_holidays_are_skipped() -> None:
    cal = BusinessCalendar(frozenset({date(2026, 3, 9)}))
    assert cal.add_business_days(date(2026, 3, 6), 1) == date(2026, 3, 10)


def test_negative_business_days_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CAL.add_business_days(date(2026, 3, 6), -1)


@pytest.mark.parametrize("network", list(Network))
@pytest.mark.parametrize("stage", list(STAGE_WINDOW))
def test_stage_deadlines_use_the_network_window(network: Network, stage: Stage) -> None:
    rules = rules_for(network)
    start = date(2026, 4, 1)
    dl = stage_deadline(rules, stage, start)
    assert dl is not None
    w = rules.window(STAGE_WINDOW[stage])
    assert dl.window is w.window and dl.day_kind is w.day_kind and dl.actor == w.actor
    if w.day_kind is DayKind.CALENDAR:
        assert dl.due == start + timedelta(days=w.days)
    else:
        assert CAL.business_days_between(start, dl.due) == w.days
    assert not dl.is_missed(dl.due)
    assert dl.is_missed(dl.due + timedelta(days=1))
    assert dl.days_left(start) == (dl.due - start).days


def test_resolved_has_no_deadline() -> None:
    assert stage_deadline(rules_for(Network.VISA), Stage.RESOLVED, date(2026, 1, 1)) is None


def test_visa_filing_deadline_is_120_calendar_days() -> None:
    assert filing_deadline(rules_for(Network.VISA), date(2026, 1, 1)) == date(2026, 5, 1)


@given(a=dates, b=dates, w=windows)
def test_deadlines_are_monotone_in_start(a: date, b: date, w: TimeWindow) -> None:
    lo, hi = min(a, b), max(a, b)
    assert window_end(lo, w, CAL) <= window_end(hi, w, CAL)


@given(start=dates, w=windows)
def test_deadline_is_at_least_the_calendar_span(start: date, w: TimeWindow) -> None:
    end = window_end(start, w, CAL)
    assert end >= start + timedelta(days=w.days)
    if w.day_kind is DayKind.BUSINESS:
        assert CAL.is_business_day(end)
        assert CAL.business_days_between(start, end) == w.days


@given(start=dates, n=st.integers(0, 60), m=st.integers(0, 60))
def test_business_days_compose(start: date, n: int, m: int) -> None:
    assert CAL.add_business_days(CAL.add_business_days(start, n), m) == CAL.add_business_days(
        start, n + m
    )


def test_manual_clock_moves_only_forward() -> None:
    c = ManualClock(datetime(2026, 2, 1, 12, tzinfo=UTC))
    assert today(c) == date(2026, 2, 1)
    c.advance(days=3)
    assert today(c) == date(2026, 2, 4)
    c.set_date(date(2026, 2, 10))
    assert today(c) == date(2026, 2, 10)
    with pytest.raises(ValueError, match="backwards"):
        c.advance(days=-1)
    with pytest.raises(ValueError, match="backwards"):
        c.set_date(date(2026, 2, 9))


def test_today_works_with_any_clock() -> None:
    assert today(SteppingClock()) == date(2026, 1, 1)
