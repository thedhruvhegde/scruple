"""scripts/pilot_plan.py: the pilot sizing wrapper over scruple.stats.planning."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.stats.planning import StratumSpec, plan

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pilot_plan.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pilot_plan", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pilot_plan"] = mod  # pydantic resolves annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


pp: Any = _load()

MIX = {"fraud": 0.5, "not_received": 0.3, "not_as_described": 0.2}
BASE: dict[str, Any] = {
    "monthly_volume": 4000,
    "mix": MIX,
    "min_improvement_usd": 5.0,
    "cost_cap_usd": 100.0,
    "sd_usd": 20.0,
}


def inputs(**kw: Any) -> Any:
    return pp.PilotInputs(**{**BASE, **kw})


@pytest.fixture(scope="module")
def example() -> Any:
    return pp.pilot_plan(inputs())


# --- agreement with the underlying planner ----------------------------------------------


def test_totals_match_the_stats_planner(example: Any) -> None:
    direct = plan(
        [
            StratumSpec(
                name=n, frequency=MIX[n], cost_cap=10_000, min_detectable_regression=500, sd=2000
            )
            for n in sorted(MIX)
        ],
        allocation="optimised",
    )
    assert example.total_cases == direct.total_cases
    assert example.binding_reason == direct.binding_stratum
    assert [r.cases_needed for r in example.reasons] == [s.stratum_cases for s in direct.strata]


def test_pinned_example_output(example: Any) -> None:
    """The numbers pasted into docs/pilot/SUCCESS_CRITERIA.md."""
    assert example.total_cases == 3044
    assert example.binding_reason == "fraud"
    assert example.months_of_history == 0.8
    assert example.shadow_weeks_traffic == 3.3
    assert {r.reason: r.cases_needed for r in example.reasons} == {
        "fraud": 1477,
        "not_as_described": 573,
        "not_received": 872,
    }


def test_alpha_budget_is_spent_exactly(example: Any) -> None:
    assert math.isclose(math.fsum(r.alpha for r in example.reasons), 0.05, rel_tol=1e-9)


def test_rigorous_plan_sits_above_the_lower_bound(example: Any) -> None:
    for r in example.reasons:
        assert r.cases_needed >= r.lower_bound_cases


def test_each_reason_total_covers_its_count(example: Any) -> None:
    for r in example.reasons:
        assert r.total_cases >= r.cases_needed
        assert r.total_cases <= example.total_cases


# --- the time conversions ---------------------------------------------------------------


def test_history_and_shadow_weeks_follow_volume(example: Any) -> None:
    n = example.total_cases
    assert example.months_of_history >= n / 4000
    assert example.months_of_history - n / 4000 < 0.1 + 1e-9
    weekly = 4000 * 12 / 52
    assert example.shadow_weeks_traffic >= n / weekly
    assert example.shadow_weeks_traffic - n / weekly < 0.1 + 1e-9


def test_outcome_lag_adds_to_shadow_weeks_only() -> None:
    a = pp.pilot_plan(inputs())
    b = pp.pilot_plan(inputs(outcome_lag_weeks=6))
    assert b.total_cases == a.total_cases
    assert b.months_of_history == a.months_of_history
    assert b.shadow_weeks_traffic == a.shadow_weeks_traffic
    assert math.isclose(b.shadow_weeks_total, a.shadow_weeks_traffic + 6, abs_tol=0.1 + 1e-9)
    assert not b.shadow_fits_window


def test_shadowing_half_the_traffic_doubles_the_weeks() -> None:
    full = pp.pilot_plan(inputs())
    half = pp.pilot_plan(inputs(shadow_fraction=0.5))
    assert half.shadow_weeks_traffic == pytest.approx(2 * full.shadow_weeks_traffic, abs=0.11)


def test_window_flag() -> None:
    assert pp.pilot_plan(inputs(monthly_volume=40_000)).shadow_fits_window
    assert not pp.pilot_plan(inputs(monthly_volume=400)).shadow_fits_window


# --- monotonicity -----------------------------------------------------------------------


@pytest.mark.parametrize(("small", "large"), [(10.0, 5.0), (5.0, 2.5)])
def test_smaller_improvement_needs_more_cases(small: float, large: float) -> None:
    a = pp.pilot_plan(inputs(min_improvement_usd=small))
    b = pp.pilot_plan(inputs(min_improvement_usd=large))
    assert b.total_cases > a.total_cases


def test_looser_cap_needs_more_cases() -> None:
    a = pp.pilot_plan(inputs(cost_cap_usd=100.0))
    b = pp.pilot_plan(inputs(cost_cap_usd=400.0))
    assert b.total_cases > a.total_cases


def test_worst_case_sd_needs_more_cases() -> None:
    a = pp.pilot_plan(inputs())
    b = pp.pilot_plan(inputs(sd_usd=None))
    assert b.total_cases > a.total_cases


def test_rare_reason_code_drives_the_total() -> None:
    even = pp.pilot_plan(inputs(mix={"fraud": 0.5, "not_received": 0.5}))
    skewed = pp.pilot_plan(inputs(mix={"fraud": 0.95, "not_received": 0.05}))
    assert skewed.total_cases > even.total_cases


def test_uniform_allocation_is_never_cheaper_than_optimised() -> None:
    opt = pp.pilot_plan(inputs())
    uni = pp.pilot_plan(inputs(allocation="uniform"))
    assert uni.total_cases >= opt.total_cases


@settings(max_examples=25, deadline=None)
@given(
    volume=st.integers(min_value=50, max_value=1_000_000),
    lag=st.floats(min_value=0, max_value=20, allow_nan=False),
)
def test_volume_changes_time_not_cases(volume: int, lag: float) -> None:
    a = pp.pilot_plan(inputs(monthly_volume=volume, outcome_lag_weeks=lag))
    assert a.total_cases == 3044
    assert a.months_of_history >= 3044 / volume
    assert a.shadow_weeks_total >= a.shadow_weeks_traffic


def test_mix_order_does_not_matter() -> None:
    a = pp.pilot_plan(inputs(mix=dict(reversed(list(MIX.items())))))
    b = pp.pilot_plan(inputs())
    assert a.model_dump() == b.model_dump()


# --- input validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"mix": {"fraud": 0.5, "not_received": 0.4}},  # does not sum to 1
        {"mix": {"fraud": 1.2}},
        {"mix": {"fraud": 0.0, "other": 1.0}},
        {"mix": {"Fraud": 1.0}},  # name pattern
        {"mix": {}},
        {"monthly_volume": 0},
        {"min_improvement_usd": 0.0},
        {"min_improvement_usd": 100.0},  # not below the cap
        {"cost_cap_usd": -1.0},
        {"sd_usd": 0.0},
        {"shadow_fraction": 0.0},
        {"shadow_fraction": 1.5},
        {"alpha": 1.0},
        {"outcome_lag_weeks": -1.0},
        {"method": "magic"},
        {"unknown_field": 1},
    ],
)
def test_bad_inputs_are_refused(kw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        inputs(**kw)


def test_inputs_are_frozen() -> None:
    inp = inputs()
    with pytest.raises(ValidationError):
        inp.monthly_volume = 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fraud=1", {"fraud": 1.0}),
        ("fraud=0.6, not_received=0.4", {"fraud": 0.6, "not_received": 0.4}),
    ],
)
def test_parse_mix(text: str, expected: dict[str, float]) -> None:
    assert pp.parse_mix(text) == expected


@pytest.mark.parametrize("text", ["fraud", "=0.5", "fraud=x", "fraud=0.5,fraud=0.5", "a=nan"])
def test_parse_mix_refuses(text: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        pp.parse_mix(text)


# --- command line -----------------------------------------------------------------------

ARGV = [
    "--monthly-volume",
    "4000",
    "--mix",
    "fraud=0.5,not_received=0.3,not_as_described=0.2",
    "--min-improvement",
    "5",
    "--cost-cap",
    "100",
    "--sd",
    "20",
]


def test_cli_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert pp.main(ARGV) == 0
    out = capsys.readouterr().out
    assert "total resolved cases needed: 3,044 (binding: fraud)" in out
    assert "months of resolved history:  0.8" in out


def test_cli_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert pp.main([*ARGV, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["total_cases"] == 3044
    assert doc["inputs"]["mix"] == MIX


def test_cli_is_deterministic(capsys: pytest.CaptureFixture[str]) -> None:
    pp.main(ARGV)
    first = capsys.readouterr().out
    pp.main(ARGV)
    assert capsys.readouterr().out == first


@pytest.mark.parametrize(
    "bad",
    [
        ["--mix", "fraud=0.5"],
        ["--min-improvement", "500"],
        ["--monthly-volume", "-3"],
    ],
)
def test_cli_bad_input_exits_2(bad: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    argv = list(ARGV)
    flag = bad[0]
    argv[argv.index(flag) + 1] = bad[1]
    assert pp.main(argv) == 2
    assert "pilot_plan:" in capsys.readouterr().err
