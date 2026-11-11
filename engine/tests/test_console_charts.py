"""Chart geometry for the console's server-rendered SVG charts (scruple/console/charts.py)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from scruple.console import charts


@given(
    st.floats(-1e9, 1e9, allow_nan=False),
    st.floats(-1e9, 1e9, allow_nan=False),
)
def test_ticks_cover_the_range_and_zero(a: float, b: float) -> None:
    lo, hi = min(a, b, 0.0), max(a, b, 0.0)
    t = charts.nice_ticks(lo, hi)
    assert t[0] <= lo + 1e-6 * max(1.0, abs(lo)) and t[-1] >= hi - 1e-6 * max(1.0, abs(hi))
    assert t == sorted(t) and len(t) <= 13
    if lo < 0 < hi:
        assert any(math.isclose(x, 0.0, abs_tol=1e-9 * max(1.0, hi - lo)) for x in t)


@given(st.lists(st.floats(-1e7, 1e7, allow_nan=False), min_size=1, max_size=40))
def test_bars_stay_inside_the_plot(values: list[float]) -> None:
    c = charts.bars("t", [charts.Point(f"p{i}", v) for i, v in enumerate(values)], "usd")
    for p in (c.wide, c.narrow):
        for bar in p.bars:
            assert p.left - 0.01 <= bar.x and bar.x + bar.w <= p.right + 0.01
            assert p.top - 0.5 <= bar.y and bar.y + bar.h <= p.bottom + 1.5
        assert len(p.xticks) <= 13


def test_steps_and_empty_charts() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    c = charts.steps("s", [charts.TimePoint(t0 + timedelta(days=i), i * 100.0) for i in range(5)])
    assert c.wide.path.startswith("M") and "H" in c.wide.path and not c.empty
    assert charts.steps("s", []).empty and charts.bars("b", []).empty
    assert charts.compact_usd(-123_456) == "-$1.2k" and charts.compact_usd(5_000) == "$50"
