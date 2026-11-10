"""Complaint-handling timeliness on real CFPB data (`data/real/v2/` meta tables).

What is measured: the CFPB's own "Timely response?" flag (did the company respond to
the CFPB within 15 days of receiving the complaint) and the days between the CFPB
receiving a complaint and sending it to the company. This is COMPLAINT HANDLING
timeliness. It is not Reg E (12 CFR 1005.11(c): 10 business days, 45 days) or Reg Z
(12 CFR 1026.13(c): 30 days, two billing cycles) error-investigation timeliness: the
data has no notice, provisional-credit or resolution dates, so those cannot be
measured here.

Company size is the company's complaint count *in the same dataset* (a volume proxy,
not assets or accounts): rank 1-5, rank 6-25, and the rest. Intervals are Wilson 95%
for a rate and Newcombe's hybrid score interval for a difference of two rates.
Everything is deterministic.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from scruple.realdata.v2 import META_SETS, MetaRow, MetaSet, load_meta

Z95 = 1.959963984540054
SIZE_BANDS: tuple[str, ...] = ("top 5", "rank 6-25", "rest")
LABEL = (
    "complaint handling timeliness: the CFPB 'Timely response?' flag (company answered "
    "the CFPB within 15 days) and CFPB routing days; NOT Reg E 1005.11(c) or Reg Z "
    "1026.13(c) investigation timeliness"
)
DATASET_NAMES: dict[MetaSet, str] = {
    "cfpb2015_payments": "CFPB complaints 2011-12 to 2016-04, four payments products",
    "cfpb2025_checking": "CFPB checking/savings complaints 2025-05 to 2026-04 (8,000-row "
    "third-party random sample)",
    "cfpb_cc2024": "CFPB credit card dispute complaints with narrative, 2024-08 to 2026-07",
}


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials ((0, 1) when n == 0)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def newcombe(k1: int, n1: int, k2: int, n2: int, z: float = Z95) -> tuple[float, float, float]:
    """Difference p1 - p2 and Newcombe's hybrid score 95% interval (method 10)."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    lo = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (d, lo, hi)


def _rate(rows: Sequence[MetaRow], hit: Callable[[MetaRow], bool]) -> dict[str, Any]:
    n = len(rows)
    k = sum(1 for r in rows if hit(r))
    lo, hi = wilson(k, n)
    return {
        "n": n,
        "k": k,
        "rate": round(k / n, 5) if n else None,
        "ci95": [round(lo, 5), round(hi, 5)],
    }


def untimely(r: MetaRow) -> bool:
    return r.timely_response == "No"


def size_bands(rows: Iterable[MetaRow]) -> dict[str, str]:
    """company -> band, by complaint count in these rows (ties broken by name)."""
    counts = Counter(r.company for r in rows)
    ranked = sorted(counts, key=lambda c: (-counts[c], c))
    out = {}
    for i, c in enumerate(ranked):
        out[c] = SIZE_BANDS[0] if i < 5 else SIZE_BANDS[1] if i < 25 else SIZE_BANDS[2]
    return out


def _days(rows: Sequence[MetaRow]) -> dict[str, Any]:
    d = sorted(r.days_to_company for r in rows)
    if not d:
        return {"n": 0}
    q = statistics.quantiles(d, n=10, method="inclusive") if len(d) > 1 else [d[0]] * 9
    return {
        "n": len(d),
        "median": statistics.median(d),
        "p90": q[8],
        "max": d[-1],
        "min": d[0],
        "share_within_1_day": round(sum(1 for x in d if 0 <= x <= 1) / len(d), 5),
        "negative": sum(1 for x in d if x < 0),
    }


def _group(rows: Sequence[MetaRow], key: Callable[[MetaRow], str]) -> dict[str, list[MetaRow]]:
    g: dict[str, list[MetaRow]] = {}
    for r in rows:
        g.setdefault(key(r), []).append(r)
    return dict(sorted(g.items()))


def analyse(name: MetaSet, rows: Sequence[MetaRow] | None = None) -> dict[str, Any]:
    data = list(load_meta(name) if rows is None else rows)
    bands = size_bands(data)
    product_key: Callable[[MetaRow], str] = (
        (lambda r: r.product) if name == "cfpb2015_payments" else (lambda r: r.sub_product)
    )
    by_band = _group(data, lambda r: bands[r.company])
    out: dict[str, Any] = {
        "dataset": name,
        "description": DATASET_NAMES[name],
        "label": LABEL,
        "date_received": [
            str(min(r.date_received for r in data)),
            str(max(r.date_received for r in data)),
        ],
        "companies": len(bands),
        "untimely": _rate(data, untimely),
        "response_untimely": _rate(data, lambda r: r.company_response == "Untimely response"),
        "days_to_company": _days(data),
        "by_product": {k: _rate(v, untimely) for k, v in _group(data, product_key).items()},
        "by_size": {b: _rate(by_band.get(b, []), untimely) for b in SIZE_BANDS},
        "by_size_companies": {b: sum(1 for c in bands.values() if c == b) for b in SIZE_BANDS},
        "by_year": {
            k: _rate(v, untimely)
            for k, v in _group(data, lambda r: str(r.date_received.year)).items()
        },
        "top_companies": [
            {"company": c, **_rate([r for r in data if r.company == c], untimely)}
            for c, _ in sorted(
                Counter(r.company for r in data).items(), key=lambda x: (-x[1], x[0])
            )[:5]
        ],
    }
    top, rest = by_band.get("top 5", []), by_band.get("rest", [])
    if top and rest:
        d, lo, hi = newcombe(sum(map(untimely, rest)), len(rest), sum(map(untimely, top)), len(top))
        out["rest_minus_top5_untimely"] = {
            "diff": round(d, 5),
            "ci95": [round(lo, 5), round(hi, 5)],
        }
    return out


def analyse_all() -> dict[str, Any]:
    return {
        "label": LABEL,
        "real_data": True,
        "reproduce": "uv run scruple realdata timeliness --out results/real/timeliness.json",
        "datasets": {n: analyse(n) for n in META_SETS},
    }


__all__ = ["LABEL", "SIZE_BANDS", "analyse", "analyse_all", "newcombe", "size_bands", "wilson"]
