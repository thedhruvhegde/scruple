"""Stage deadlines from network time windows, with calendar and business days.

Each lifecycle stage has one window (`STAGE_WINDOW`) taken from the network rule set
(`scruple.sim.networks`, sourced in docs/SOURCES.md). Deadlines count from the date
the stage was entered. "Today" always comes from an injected `Clock`, never from the
system clock, so runs are reproducible.

Business days are Monday to Friday minus an optional holiday set. The default has
no holidays (docs/ASSUMPTIONS.md A-24): real networks and processors use their own
calendars.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from scruple.clock import Clock
from scruple.sim.models import Stage
from scruple.sim.networks import DayKind, NetworkRules, TimeWindow, WindowId

# The window that governs each live stage (who must act, and by when).
STAGE_WINDOW: dict[Stage, WindowId] = {
    Stage.INQUIRY: WindowId.INQUIRY_RESPONSE,
    Stage.CHARGEBACK: WindowId.CHARGEBACK_RESPONSE,
    Stage.REPRESENTMENT: WindowId.PRE_ARBITRATION_FILING,
    Stage.PRE_ARBITRATION: WindowId.PRE_ARBITRATION_RESPONSE,
    Stage.ARBITRATION: WindowId.ARBITRATION_FILING,
}


class BusinessCalendar:
    def __init__(self, holidays: frozenset[date] = frozenset()) -> None:
        self.holidays = holidays

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def add_business_days(self, start: date, n: int) -> date:
        """The n-th business day strictly after `start` (n >= 0; n == 0 is `start`)."""
        if n < 0:
            raise ValueError("n must be non-negative")
        d = start
        while n > 0:
            d += timedelta(days=1)
            if self.is_business_day(d):
                n -= 1
        return d

    def business_days_between(self, start: date, end: date) -> int:
        """Business days in (start, end]."""
        n, d = 0, start
        while d < end:
            d += timedelta(days=1)
            if self.is_business_day(d):
                n += 1
        return n


DEFAULT_CALENDAR = BusinessCalendar()


def window_end(start: date, window: TimeWindow, calendar: BusinessCalendar) -> date:
    if window.day_kind is DayKind.BUSINESS:
        return calendar.add_business_days(start, window.days)
    return start + timedelta(days=window.days)


class Deadline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Stage
    window: WindowId
    actor: str
    starts: date
    due: date  # last day on which the actor may still act
    day_kind: DayKind

    def is_missed(self, today: date) -> bool:
        return today > self.due

    def days_left(self, today: date) -> int:
        return (self.due - today).days


def stage_deadline(
    rules: NetworkRules,
    stage: Stage,
    entered_on: date,
    calendar: BusinessCalendar = DEFAULT_CALENDAR,
) -> Deadline | None:
    wid = STAGE_WINDOW.get(stage)
    if wid is None:
        return None
    w = rules.window(wid)
    return Deadline(
        stage=stage,
        window=wid,
        actor=w.actor,
        starts=entered_on,
        due=window_end(entered_on, w, calendar),
        day_kind=w.day_kind,
    )


def filing_deadline(
    rules: NetworkRules, processed_on: date, calendar: BusinessCalendar = DEFAULT_CALENDAR
) -> date:
    """Last day the issuer may file a chargeback for a transaction processed on a date."""
    return window_end(processed_on, rules.window(WindowId.CHARGEBACK_FILING), calendar)


class ManualClock:
    """A `Clock` that only moves when told to. Use it to drive simulated days."""

    def __init__(self, start: datetime = datetime(2026, 1, 5, 12, tzinfo=UTC)) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, days: int = 0, hours: int = 0) -> None:
        if days < 0 or hours < 0:
            raise ValueError("a clock cannot run backwards")
        self._t += timedelta(days=days, hours=hours)

    def set_date(self, d: date) -> None:
        target = datetime(d.year, d.month, d.day, 12, tzinfo=UTC)
        if target < self._t.replace(hour=12, minute=0, second=0, microsecond=0):
            raise ValueError("a clock cannot run backwards")
        self._t = target


def today(clock: Clock) -> date:
    return clock.now().astimezone(UTC).date()
