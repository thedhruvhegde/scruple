"""Chart geometry for the console's server-rendered SVG charts. No chart library.

Every builder here turns numbers into plain data (rectangles, path strings, tick
positions, label text); the Jinja macros in `templates/_charts.html` print it as
inline SVG. Labels stay text, so autoescape covers them like everything else on a
page, and nothing here ever builds markup. Colour comes from CSS classes (the CSP
allows no inline style), so a chart has no colour of its own.

Each chart is built twice, wide (desktop) and narrow (phones), and CSS shows one:
an SVG scales its text with its width, so one drawing cannot be legible at both
1440 and 390 pixels.

Kinds: `bars` (one series, signed or not, optional interval whiskers), `hbars`
(labelled horizontal bars, for a mix), `steps` (a step line over time or index),
and `ramp` (canary stages as a staircase with the current stage marked).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from scruple.console.fmt import num, usd

Units = str  # "usd" (minor units), "count", "pct"

WIDE = 720
NARROW = 360


def fmt_value(x: float, units: Units) -> str:
    if units == "usd":
        return compact_usd(x)
    if units == "pct":
        return f"{x:.0f}%"
    return compact_count(x)


def compact_usd(minor: float) -> str:
    """Axis labels: -$1.2k, $35, $2.4M (cents in, dollars out)."""
    d = minor / 100
    sign = "-" if d < 0 else ""
    a = abs(d)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            v = a / div
            return f"{sign}${v:.1f}{suf}".replace(".0" + suf, suf)
    return f"{sign}${a:,.0f}"


def compact_count(x: float) -> str:
    a = abs(x)
    sign = "-" if x < 0 else ""
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            v = a / div
            return f"{sign}{v:.1f}{suf}".replace(".0" + suf, suf)
    return f"{sign}{a:,.0f}" if float(a).is_integer() else f"{sign}{a:,.1f}"


def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    """Round tick values covering [lo, hi] (always including 0 when in range)."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return [0.0]
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    out: list[float] = []
    t = start
    while t <= hi + step * 1e-9 and len(out) < 12:
        out.append(round(t, 10))
        t += step
    if out[-1] < hi:
        out.append(round(out[-1] + step, 10))
    return out


@dataclass(frozen=True)
class Tick:
    pos: float  # y for value axes, x for category / time axes
    label: str


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    w: float
    h: float
    cls: str  # pos | neg | dim | hi
    label: str
    value: str  # printed value (for the title and the narrow table)
    lo: float | None = None  # whisker ends, in y pixels
    hi: float | None = None


@dataclass(frozen=True)
class Plot:
    """One drawing at one width. `yticks` are value gridlines, `xticks` category or
    time labels along the baseline."""

    width: int
    height: int
    left: float
    right: float
    top: float
    bottom: float
    zero: float
    yticks: tuple[Tick, ...]
    xticks: tuple[Tick, ...]
    bars: tuple[Bar, ...] = ()
    path: str = ""  # a step line
    area: str = ""  # the region under the step line (filled flat, no gradient)
    marks: tuple[tuple[float, float, str], ...] = ()  # (x, y, class) dots
    labels: tuple[tuple[float, float, str, str], ...] = ()  # (x, y, text, class)


@dataclass(frozen=True)
class Chart:
    """A chart: a title for screen readers, both drawings, and the data as rows (the
    accessible fallback and the narrow-screen reading)."""

    kind: str
    title: str
    units: Units
    wide: Plot
    narrow: Plot
    rows: tuple[tuple[str, str], ...]
    empty: bool = False
    note: str = ""
    basis: str = ""  # observed | estimated | synthetic | mixed (printed with the chart)


@dataclass(frozen=True)
class Point:
    label: str
    value: float
    lo: float | None = None
    hi: float | None = None
    cls: str = ""


def _scale(lo: float, hi: float) -> tuple[list[float], float, float]:
    ticks = nice_ticks(min(lo, 0.0), max(hi, 0.0))
    return ticks, ticks[0], ticks[-1]


def _y(v: float, vmin: float, vmax: float, top: float, bottom: float) -> float:
    span = vmax - vmin or 1.0
    return round(bottom - (v - vmin) / span * (bottom - top), 2)


def _bars_plot(points: Sequence[Point], units: Units, width: int, height: int) -> Plot:
    left, right, top, bottom = (58.0 if width >= WIDE else 46.0), width - 8.0, 10.0, height - 24.0
    vals = [p.value for p in points] + [x for p in points for x in (p.lo, p.hi) if x is not None]
    ticks, vmin, vmax = _scale(min(vals, default=0.0), max(vals, default=1.0))
    zero = _y(0.0, vmin, vmax, top, bottom)
    n = max(len(points), 1)
    slot = (right - left) / n
    w = max(1.0, min(slot * 0.62, 48.0))
    bars = []
    for i, p in enumerate(points):
        x = left + slot * i + (slot - w) / 2
        y = _y(p.value, vmin, vmax, top, bottom)
        top_y, h = (y, zero - y) if p.value >= 0 else (zero, y - zero)
        cls = p.cls or ("neg" if p.value < 0 else "pos")
        bars.append(
            Bar(
                x=round(x, 2),
                y=round(top_y, 2),
                w=round(w, 2),
                h=round(max(h, 0.0 if p.value == 0 else 1.0), 2),
                cls=cls,
                label=p.label,
                value=fmt_value(p.value, units),
                lo=None if p.lo is None else _y(p.lo, vmin, vmax, top, bottom),
                hi=None if p.hi is None else _y(p.hi, vmin, vmax, top, bottom),
            )
        )
    every = max(1, math.ceil(n / (12 if width >= WIDE else 5)))
    xticks = tuple(
        Tick(round(left + slot * i + slot / 2, 2), _clip(p.label, 12 if width >= WIDE else 8))
        for i, p in enumerate(points)
        if i % every == 0
    )
    yticks = tuple(Tick(_y(t, vmin, vmax, top, bottom), fmt_value(t, units)) for t in ticks)
    return Plot(width, height, left, right, top, bottom, zero, yticks, xticks, tuple(bars))


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "~"


def bars(
    title: str,
    points: Sequence[Point],
    units: Units = "usd",
    *,
    height: int = 200,
    note: str = "",
    basis: str = "",
) -> Chart:
    """Vertical bars, one per point; negative values hang below the zero line."""
    pts = list(points)
    return Chart(
        kind="bars",
        title=title,
        units=units,
        wide=_bars_plot(pts, units, WIDE, height),
        narrow=_bars_plot(pts, units, NARROW, height),
        rows=tuple((p.label, _row_value(p, units)) for p in pts),
        empty=not pts,
        note=note,
        basis=basis,
    )


def _row_value(p: Point, units: Units) -> str:
    v = usd(p.value) if units == "usd" else (f"{p.value:.1f}%" if units == "pct" else num(p.value))
    if p.lo is not None and p.hi is not None:
        a = usd(p.lo) if units == "usd" else num(p.lo, 1)
        b = usd(p.hi) if units == "usd" else num(p.hi, 1)
        return f"{v} ({a} to {b})"
    return v


def _hbars_plot(points: Sequence[Point], units: Units, width: int) -> Plot:
    row = 22.0
    label_w = 150.0 if width >= WIDE else 104.0
    value_w = 64.0 if width >= WIDE else 52.0
    left, right, top = label_w, width - value_w, 4.0
    height = int(top + row * max(len(points), 1) + 4)
    vmax = max([p.value for p in points] + [1e-9])
    bars_ = []
    labels = []
    for i, p in enumerate(points):
        y = top + row * i + 4
        w = (right - left) * (max(p.value, 0.0) / vmax)
        bars_.append(
            Bar(
                round(left, 2),
                round(y, 2),
                round(max(w, 1.0), 2),
                row - 9,
                p.cls or "pos",
                p.label,
                fmt_value(p.value, units),
            )
        )
        labels.append((left - 8, y + row / 2 - 1, _clip(p.label, 18 if width >= WIDE else 13), "l"))
        labels.append((right + 6, y + row / 2 - 1, fmt_value(p.value, units), "v"))
    return Plot(
        width,
        height,
        left,
        right,
        top,
        float(height - 4),
        left,
        (),
        (),
        tuple(bars_),
        labels=tuple(labels),
    )


def hbars(
    title: str, points: Sequence[Point], units: Units = "count", *, note: str = "", basis: str = ""
) -> Chart:
    """Horizontal bars with a label column: a mix (reason codes, outcomes)."""
    pts = list(points)
    return Chart(
        kind="hbars",
        title=title,
        units=units,
        wide=_hbars_plot(pts, units, WIDE),
        narrow=_hbars_plot(pts, units, NARROW),
        rows=tuple((p.label, _row_value(p, units)) for p in pts),
        empty=not pts,
        note=note,
        basis=basis,
    )


@dataclass(frozen=True)
class TimePoint:
    at: datetime
    value: float


def _steps_plot(
    points: Sequence[TimePoint], units: Units, width: int, height: int, cls: str
) -> Plot:
    left, right, top, bottom = (58.0 if width >= WIDE else 46.0), width - 10.0, 10.0, height - 24.0
    vals = [p.value for p in points]
    ticks, vmin, vmax = _scale(min(vals, default=0.0), max(vals, default=1.0))
    zero = _y(0.0, vmin, vmax, top, bottom)
    if not points:
        return Plot(width, height, left, right, top, bottom, zero, (), ())
    t0 = points[0].at.timestamp()
    t1 = points[-1].at.timestamp()
    span = (t1 - t0) or 1.0

    def x(t: datetime) -> float:
        return round(left + (t.timestamp() - t0) / span * (right - left), 2)

    xs = [x(p.at) for p in points] if t1 > t0 else [left + (right - left) / 2] * len(points)
    ys = [_y(p.value, vmin, vmax, top, bottom) for p in points]
    d = [f"M{xs[0]},{zero}", f"V{ys[0]}"]
    for i in range(1, len(points)):
        d.append(f"H{xs[i]}")
        d.append(f"V{ys[i]}")
    d.append(f"H{right}")
    line = " ".join(d)
    area = line + f" V{zero} H{xs[0]} Z"
    n_ticks = 6 if width >= WIDE else 3
    xticks: list[Tick] = []
    if t1 > t0:
        for k in range(n_ticks + 1):
            t = t0 + span * k / n_ticks
            xticks.append(
                Tick(
                    round(left + (right - left) * k / n_ticks, 2),
                    datetime.fromtimestamp(t, points[0].at.tzinfo).strftime("%m-%d"),
                )
            )
    else:
        xticks.append(Tick(xs[0], points[0].at.strftime("%m-%d")))
    marks = ((xs[-1], ys[-1], cls),)
    return Plot(
        width,
        height,
        left,
        right,
        top,
        bottom,
        zero,
        tuple(Tick(_y(t, vmin, vmax, top, bottom), fmt_value(t, units)) for t in ticks),
        tuple(xticks),
        path=line,
        area=area,
        marks=marks,
    )


def steps(
    title: str,
    points: Sequence[TimePoint],
    units: Units = "usd",
    *,
    height: int = 180,
    note: str = "",
    basis: str = "",
    cls: str = "pos",
    max_rows: int = 12,
) -> Chart:
    """A step line: the value holds until the next point (a running total)."""
    pts = sorted(points, key=lambda p: p.at)
    shown = pts[-max_rows:]
    return Chart(
        kind="steps",
        title=title,
        units=units,
        wide=_steps_plot(pts, units, WIDE, height, cls),
        narrow=_steps_plot(pts, units, NARROW, height, cls),
        rows=tuple(
            (p.at.strftime("%Y-%m-%d"), usd(p.value) if units == "usd" else num(p.value))
            for p in shown
        ),
        empty=not pts,
        note=note,
        basis=basis,
    )


def ramp(stages: Sequence[float], current: int, status: str) -> Chart:
    pts = [Point(f"{s:g}%", s, cls=_ramp_cls(i, current, status)) for i, s in enumerate(stages)]
    return Chart(
        kind="bars",
        title=f"canary ramp, stage {current + 1} of {len(stages)} ({status})",
        units="pct",
        wide=_bars_plot(pts, "pct", WIDE, 140),
        narrow=_bars_plot(pts, "pct", NARROW, 140),
        rows=tuple((p.label, p.cls or "") for p in pts),
        empty=not pts,
    )


def _ramp_cls(i: int, current: int, status: str) -> str:
    if status == "completed" or i < current:
        return "pos"
    if i == current:
        return "neg" if status == "breached" else "hi"
    return "dim"


__all__ = [
    "NARROW",
    "WIDE",
    "Bar",
    "Chart",
    "Plot",
    "Point",
    "Tick",
    "TimePoint",
    "bars",
    "compact_count",
    "compact_usd",
    "hbars",
    "nice_ticks",
    "ramp",
    "steps",
]
