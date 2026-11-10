"""Number and text formatting for the console templates (Jinja filters).

Every filter returns plain text; escaping stays with Jinja's autoescape.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import date, datetime
from typing import Any


def usd(minor: float | int | None) -> str:
    """US cents as dollars: -1234.5 -> -$12.35."""
    if minor is None:
        return "n/a"
    x = float(minor)
    if not math.isfinite(x):
        return str(x)
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x) / 100:,.2f}"


def signed_usd(minor: float | int | None) -> str:
    if minor is None:
        return "n/a"
    return ("+" if float(minor) > 0 else "") + usd(minor)


def num(x: float | int | None, digits: int = 0) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float) and not math.isfinite(x):
        return str(x)
    return f"{x:,.{digits}f}"


def evalue(x: float | None) -> str:
    """E-values span many orders of magnitude: 3 significant digits, or scientific."""
    if x is None:
        return "n/a"
    if math.isinf(x):
        return "inf"
    if math.isnan(x):
        return "nan"
    if x != 0 and (abs(x) >= 1e4 or abs(x) < 1e-2):
        return f"{x:.2e}"
    return f"{x:.3g}"


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def ratio(a: int, b: int) -> str:
    return f"{a}/{b}" if b else "0/0"


def short(h: str | None, n: int = 12) -> str:
    return "" if not h else h[:n]


def when(x: datetime | date | str | None) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, datetime):
        return x.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    if isinstance(x, date):
        return x.isoformat()
    return str(x)


def pretty(x: Any) -> str:
    return json.dumps(x, indent=2, sort_keys=True, default=str, ensure_ascii=False)


def minor(amount: int, currency: str) -> str:
    """A minor-unit amount in its currency: 1819 USD -> 18.19 USD."""
    from scruple.sim.fx import EXPONENT

    exp = EXPONENT.get(currency, 2)
    sign = "-" if amount < 0 else ""
    whole = abs(amount) / (10**exp)
    return f"{sign}{whole:,.{exp}f} {currency}"


FILTERS: dict[str, Callable[..., str]] = {
    "usd": usd,
    "signed_usd": signed_usd,
    "num": num,
    "evalue": evalue,
    "pct": pct,
    "ratio": ratio,
    "short": short,
    "when": when,
    "pretty": pretty,
    "minor": minor,
}

__all__ = ["FILTERS"]
