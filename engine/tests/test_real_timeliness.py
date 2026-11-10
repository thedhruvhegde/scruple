"""Complaint-handling timeliness on real CFPB data: intervals, bands, labels, numbers."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.realdata import timeliness as tl
from scruple.realdata.v2 import META_SETS, MetaRow, MetaSet


def _row(i: int, company: str, timely: str = "Yes", days: int = 0) -> MetaRow:
    d = date(2025, 1, 1) + timedelta(days=i % 300)
    return MetaRow(
        complaint_id=str(i),
        date_received=d,
        date_sent_to_company=d + timedelta(days=days),
        product="Credit card",
        sub_product="Store credit card",
        issue="i",
        sub_issue="s",
        company=company,
        state="CA",
        submitted_via="Web",
        company_response="Closed with explanation",
        timely_response=timely,
    )


def test_wilson_known_values() -> None:
    lo, hi = tl.wilson(1404, 135313)
    assert (round(lo, 5), round(hi, 5)) == (0.00985, 0.01093)
    assert tl.wilson(0, 0) == (0.0, 1.0)
    lo, hi = tl.wilson(0, 10)
    assert lo == 0.0 and 0.27 < hi < 0.28  # the classic 0 of 10 upper bound
    lo, hi = tl.wilson(10, 10)
    assert math.isclose(hi, 1.0) and 0.72 < lo < 0.73


@given(st.integers(0, 500), st.integers(1, 500))
def test_wilson_contains_the_estimate(k: int, n: int) -> None:
    k = min(k, n)
    lo, hi = tl.wilson(k, n)
    assert 0.0 <= lo <= k / n + 1e-12 and k / n - 1e-12 <= hi <= 1.0


@given(st.integers(0, 200), st.integers(1, 200), st.integers(0, 200), st.integers(1, 200))
def test_newcombe_brackets_the_difference(k1: int, n1: int, k2: int, n2: int) -> None:
    k1, k2 = min(k1, n1), min(k2, n2)
    d, lo, hi = tl.newcombe(k1, n1, k2, n2)
    assert math.isclose(d, k1 / n1 - k2 / n2)
    assert lo <= d + 1e-12 and d - 1e-12 <= hi
    assert lo >= -1.0 - 1e-9 and hi <= 1.0 + 1e-9


def test_size_bands_rank_by_volume_with_name_ties() -> None:
    rows = [_row(i, f"c{i % 30:02d}") for i in range(300)] + [
        _row(1000 + i, "big") for i in range(50)
    ]
    bands = tl.size_bands(rows)
    assert bands["big"] == "top 5"
    assert sum(1 for b in bands.values() if b == "top 5") == 5
    assert sum(1 for b in bands.values() if b == "rank 6-25") == 20
    assert sum(1 for b in bands.values() if b == "rest") == 6
    assert bands["c00"] == "top 5"  # ties broken by name


def test_analyse_on_small_rows() -> None:
    rows = [_row(i, "big", "No" if i < 2 else "Yes", days=i % 3) for i in range(100)]
    rows += [_row(200 + i, f"small{i}", "No" if i < 10 else "Yes") for i in range(30)]
    out = tl.analyse("cfpb_cc2024", rows)
    assert out["untimely"]["k"] == 12 and out["untimely"]["n"] == 130
    assert out["by_size"]["top 5"]["n"] == 104  # big + the four first small ones by name
    assert out["days_to_company"]["max"] == 2
    assert "NOT Reg E" in out["label"]
    assert out["rest_minus_top5_untimely"]["diff"] > 0


@pytest.mark.parametrize("name", META_SETS)
def test_real_numbers_are_stable(name: MetaSet) -> None:
    a = tl.analyse(name)
    assert a == tl.analyse(name)  # deterministic
    assert sum(v["n"] for v in a["by_size"].values()) == a["untimely"]["n"]
    assert sum(v["n"] for v in a["by_product"].values()) == a["untimely"]["n"]
    assert 0 < a["untimely"]["rate"] < 0.05


def test_pinned_real_results() -> None:
    a = tl.analyse_all()["datasets"]
    assert a["cfpb2015_payments"]["untimely"]["k"] == 1404
    assert a["cfpb2025_checking"]["untimely"]["k"] == 77
    assert a["cfpb_cc2024"]["untimely"]["k"] == 157
    for name in META_SETS:
        diff = a[name]["rest_minus_top5_untimely"]
        assert diff["ci95"][0] > 0, name  # smaller companies answer late more often, every set
    assert a["cfpb2015_payments"]["days_to_company"]["negative"] == 1644
