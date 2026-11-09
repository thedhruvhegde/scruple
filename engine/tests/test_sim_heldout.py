"""Held-out size planning: rare case types decide how big a held-out set must be."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.cli.main import main
from scruple.sim.difficulty import difficulty_mix
from scruple.sim.disputes import DEFAULT_MIX, FULL_MIX, generate_cases
from scruple.sim.heldout import check_heldout, heldout_size, measured_shares, mix_shares
from scruple.stats import StratumSpec, stratum_cases_needed


def test_mix_shares_normalise() -> None:
    s = mix_shares(FULL_MIX)
    assert abs(sum(s.values()) - 1) < 1e-12 and len(s) == 6
    with pytest.raises(ValueError, match="family weights"):
        mix_shares(DEFAULT_MIX)


def test_measured_shares() -> None:
    cases = generate_cases(1, 500, FULL_MIX)
    s = measured_shares(cases)
    assert abs(sum(s.values()) - 1) < 1e-12
    with pytest.raises(ValueError, match="no cases"):
        measured_shares([])


def test_rarest_type_binds_and_matches_stats() -> None:
    res = heldout_size(mix_shares(FULL_MIX))
    rare = min(res.needs, key=lambda t: t.share)
    assert res.binding_type == rare.case_type
    assert res.total_cases == max(t.total_cases for t in res.needs)
    # every type gets alpha / K under the uniform allocation
    spec = StratumSpec(
        name="x", frequency=rare.share, cost_cap=10_000, min_detectable_regression=500, sd=2_000
    )
    n, _ = stratum_cases_needed(spec, 0.05 / 6, 0.2)
    assert rare.type_cases == n
    assert rare.total_cases >= n / rare.share  # needs at least n / share on average


def test_demo_sized_sets_are_far_too_small() -> None:
    res = check_heldout(generate_cases(3, 3_000, FULL_MIX))
    assert res.heldout_cases == 3_000
    assert res.total_cases > 3 * 3_000
    short = {t.case_type for t in res.shortfall}
    assert {"merchant_error", "cardholder_mistake"} <= short
    assert any("SHORT" in line for line in res.lines())


def test_a_big_enough_set_passes() -> None:
    shares = {"a": 0.5, "b": 0.5}
    need = heldout_size(shares)
    have = {t.case_type: t.type_cases for t in need.needs}
    res = heldout_size(shares, have=have)
    assert res.shortfall == () and all(t.enough for t in res.needs)


def test_harder_mixes_need_bigger_heldout_sets() -> None:
    sizes = [heldout_size(mix_shares(difficulty_mix(d))).total_cases for d in (0, 0.5, 1)]
    assert sizes[0] < sizes[1] < sizes[2]


@settings(max_examples=15)
@given(st.floats(min_value=0.005, max_value=0.5))
def test_rarer_types_need_more_traffic(p: float) -> None:
    a = heldout_size({"rare": p, "common": 1 - p})
    b = heldout_size({"rare": p / 2, "common": 1 - p / 2})
    assert b.total_cases > a.total_cases


def test_optimised_allocation_is_no_bigger() -> None:
    s = mix_shares(difficulty_mix(0.9))
    assert heldout_size(s, allocation="optimised").total_cases <= heldout_size(s).total_cases


def test_empty_shares_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        heldout_size({})


def test_cli_prints_a_plan(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sim", "heldout-size", "--difficulty", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "held-out cases needed" in out and "merchant_error" in out
    assert main(["sim", "heldout-size", "--check", "600", "--seed", "2"]) == 0
    assert "SHORT" in capsys.readouterr().out
