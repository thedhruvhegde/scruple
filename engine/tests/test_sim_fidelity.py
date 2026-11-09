"""Fidelity against real CFPB marginals (040): metrics, provenance, calibration, CLI."""

from __future__ import annotations

import json
import math
import random
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.cli.main import main
from scruple.sim.disputes import FULL_MIX, generate_cases
from scruple.sim.families import FAMILY_WEIGHTS, CaseFamily
from scruple.sim.fidelity import (
    AGGREGATES,
    CALIBRATED,
    FRAUD_FAMS,
    _multinomial,
    build_aggregates,
    calibrated_mix,
    divergence,
    fidelity,
    jensen_shannon,
    load_aggregates,
    real_marginals,
    reference_outcome,
    simulate,
    total_variation,
    weights_for,
)

RAW = Path("/home/user/data-src/real")
ROOT = Path(__file__).resolve().parents[1]


def _dist(k: int) -> st.SearchStrategy[list[float]]:
    return st.lists(st.floats(0.0, 1.0), min_size=k, max_size=k).filter(lambda x: sum(x) > 0)


def _norm(x: list[float]) -> list[float]:
    s = sum(x)
    return [v / s for v in x]


@given(st.integers(2, 5).flatmap(lambda k: st.tuples(_dist(k), _dist(k))))
def test_divergences_are_bounded_symmetric_and_zero_on_equal(
    pq: tuple[list[float], list[float]],
) -> None:
    p, q = _norm(pq[0]), _norm(pq[1])
    for f in (total_variation, jensen_shannon):
        v = f(p, q)
        assert -1e-12 <= v <= 1 + 1e-12
        assert math.isclose(v, f(q, p), abs_tol=1e-12)
        assert abs(f(p, p)) < 1e-12
    # Lin (1991): JS (bits) is at most TV
    assert jensen_shannon(p, q) <= total_variation(p, q) + 1e-12


def test_disjoint_distributions_are_maximally_apart() -> None:
    assert total_variation([1, 0], [0, 1]) == 1.0
    assert math.isclose(jensen_shannon([1, 0], [0, 1]), 1.0)


@given(st.integers(0, 5_000), _dist(4), st.integers(0, 2**32))
def test_multinomial_draws_sum_to_n(n: int, p: list[float], seed: int) -> None:
    draw = _multinomial(random.Random(seed), n, _norm(p))
    assert sum(draw) == n and all(x >= 0 for x in draw)


def test_bootstrap_is_deterministic_and_ordered() -> None:
    a = divergence("x", {"a": 300, "b": 700}, {"a": 500, "b": 500}, boot=300)
    b = divergence("x", {"a": 300, "b": 700}, {"a": 500, "b": 500}, boot=300)
    assert a == b
    assert a.tv_ci[0] <= a.tv <= a.tv_ci[1] and a.js_ci[0] <= a.js_ci[1]
    assert math.isclose(a.tv, 0.2)
    same = divergence("y", {"a": 300, "b": 700}, {"a": 300, "b": 700}, boot=300)
    assert same.tv == 0 and same.tv_noise_floor > 0


def test_committed_aggregates_are_counts_with_provenance() -> None:
    agg = load_aggregates()
    for p in agg["provenance"]:
        assert len(p["mirror_commit"]) == 40 and len(p["file_sha256"]) == 64
        assert "public domain" in p["licence"]
    for table in ("credit_card_issue_response", "checking_subissue_response"):
        for responses in agg[table].values():
            assert all(isinstance(v, int) and v >= 0 for v in responses.values())
    text = AGGREGATES.read_text()
    assert "narrative" not in text.replace("no rows or narratives", "")
    assert AGGREGATES.stat().st_size < 50_000


def test_real_marginals_match_the_data_card() -> None:
    m = real_marginals(load_aggregates())
    assert m["card_category"] == {"fraud": 5_899, "non_fraud": 11_042}  # DATA_CARD.md counts
    assert m["debit_category"] == {"fraud": 953, "processing_error": 170}
    assert m["relief_billing"]["relief"] == 3_473 and m["relief_fraud"]["relief"] == 1_011


@pytest.mark.skipif(not RAW.exists(), reason="raw CFPB data not on this machine")
def test_aggregates_rebuild_from_the_raw_files() -> None:
    assert build_aggregates(RAW) == load_aggregates()


def test_no_raw_cfpb_file_is_committed() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.split()
    assert not [t for t in tracked if "complaints" in t or t.endswith((".csv", ".zip"))]
    data = ROOT / "scruple" / "sim" / "data"
    assert all(p.stat().st_size < 1_000_000 for p in data.iterdir())


def test_reference_policy_outcomes() -> None:
    cases = generate_cases(3, 300, FULL_MIX)
    out = [reference_outcome(c) for c in cases]
    assert out == [reference_outcome(c) for c in cases]  # deterministic
    valid = [o for o, c in zip(out, cases, strict=True) if c.truth.claim_valid]
    invalid = [o for o, c in zip(out, cases, strict=True) if not c.truth.claim_valid]
    assert sum(valid) / len(valid) > sum(invalid) / len(invalid)


def test_simulate_counts_add_up() -> None:
    s = simulate(FULL_MIX, 300, 2)
    assert sum(s.category.values()) == 300
    assert sum(s.relief_fraud.values()) + sum(s.relief_billing.values()) == 300
    assert s.debit_category["fraud"] == s.category["fraud"]


@given(st.floats(0.01, 0.99), st.floats(0, 1), st.floats(0, 1))
def test_weights_for_hits_the_requested_shares(f: float, vf: float, vo: float) -> None:
    w = weights_for(f, vf, vo)
    assert math.isclose(sum(w.values()), 1.0)
    assert math.isclose(sum(w[x] for x in FRAUD_FAMS), f)
    assert math.isclose(w[CaseFamily.TRUE_FRAUD], f * vf)


def test_calibrated_profile_is_opt_in() -> None:
    assert FULL_MIX.family_weights == FAMILY_WEIGHTS  # defaults unchanged
    raw = json.loads(CALIBRATED.read_text())
    assert "Opt-in" in raw["note"]
    mix = calibrated_mix()
    assert mix.family_weights is not None
    fraud = sum(mix.family_weights[f] for f in FRAUD_FAMS)
    real = real_marginals(load_aggregates())["card_category"]
    assert math.isclose(fraud, real["fraud"] / (real["fraud"] + real["non_fraud"]))


def test_calibration_reduces_divergence() -> None:
    before = fidelity(FULL_MIX, "default", n=800, boot=200)
    after = fidelity(calibrated_mix(), "calibrated", n=800, boot=200)
    b = {d.name: d.tv for d in before.comparisons}
    a = {d.name: d.tv for d in after.comparisons}
    assert a["card_category"] < b["card_category"]
    assert a["relief_fraud"] < b["relief_fraud"]
    assert sum(a.values()) < sum(b.values())


def test_cli_reports(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sim", "fidelity", "--n", "300", "--boot", "50"]) == 0
    out = capsys.readouterr().out
    assert "card_category" in out and "public domain" in out and "e75a4b1fafb0" in out
    assert main(["sim", "fidelity", "--n", "300", "--boot", "50", "--profile", "calibrated"]) == 0
    assert "profile calibrated" in capsys.readouterr().out


@pytest.mark.slow
def test_cli_calibrates(capsys: pytest.CaptureFixture[str]) -> None:
    args = ["sim", "fidelity", "--n", "600", "--boot", "100", "--calibrate"]
    assert main([*args, "--calibration-n", "400"]) == 0
    assert "fitted: fraud share 0.3482" in capsys.readouterr().out
