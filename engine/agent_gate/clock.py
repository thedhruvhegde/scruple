"""Injectable clocks so that tests and the demo are byte-for-byte reproducible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SteppingClock:
    """Deterministic clock: starts at `start` and advances `step` on every call."""

    def __init__(
        self,
        start: datetime = datetime(2026, 1, 1, tzinfo=UTC),
        step: timedelta = timedelta(seconds=1),
    ) -> None:
        self._t = start
        self._step = step

    def now(self) -> datetime:
        t = self._t
        self._t = t + self._step
        return t
