"""Sequential two-sample canary (inverse-propensity betting): validity by seeded Monte
Carlo, early stopping, and wiring into the one-shot canary and the staged ramp.
All data synthetic."""

from __future__ import annotations

import random

import pytest

from scruple.clock import SteppingClock
from scruple.domain import Action, RuleSpec
from scruple.gate.canary import RampSchedule, evaluate_stage
from scruple.gate.config import GateConfig
from scruple.gate.engine import evaluate_canary
from scruple.gate.models import Outcome
from scruple.gate.sequential import (
    Observation,
    ipw_term,
    scale,
    sequential_canary,
    sequential_checks,
)
from scruple.service import AgentGate
from scruple.sim.generator import generate_cases
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID, C
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

CAP = 1_000.0

# Contests fraud claims without 3-D Secure: most are valid, so contesting loses the
# amount plus the arbitration fee. Clearly harmful (synthetic).
HARMFUL_ID = "fraud-no-3ds-contest"
HARMFUL = RuleSpec(
    description="Contest fraud disputes without 3-D Secure.",
    conditions=(C("reason_code", "eq", "fraud"), C("three_ds", "eq", False)),
    action=Action.CONTEST,
    priority=35,
)


# --- the estimator --------------------------------------------------------------------


@pytest.mark.parametrize("p", [0.05, 0.1, 0.25, 0.5])
def test_ipw_term_is_unbiased_for_the_effect(p: float) -> None:
    y1, y0 = 700.0, 300.0
    expected = p * ipw_term(y1, True, p) + (1 - p) * ipw_term(y0, False, p)
    assert expected == pytest.approx(y1 - y0)
    assert -CAP <= ipw_term(y0, False, p) / scale(CAP, p) * CAP <= CAP
    assert abs(ipw_term(CAP, True, p) / scale(CAP, p)) <= 1.0
    assert abs(ipw_term(CAP, False, p) / scale(CAP, p)) <= 1.0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"p": 0.0}, "probability"),
        ({"p": 1.0}, "probability"),
        ({"alpha": 0.0}, "alpha"),
        ({"tolerance": -1.0}, "tolerance"),
        ({"tolerance": 1e9}, "scale"),
    ],
)
def test_rejects_bad_parameters(kwargs: dict[str, float], match: str) -> None:
    args = {"p": 0.1, "alpha": 0.05, "tolerance": 0.0, **kwargs}
    obs = [Observation("a", CAP, True, 1.0)]
    with pytest.raises(ValueError, match=match):
        sequential_canary(obs, args["p"], args["alpha"], args["tolerance"])


def test_cost_outside_cap_and_inconsistent_caps_are_errors() -> None:
    with pytest.raises(ValueError, match="outside"):
        sequential_canary([Observation("a", CAP, True, CAP + 1)], 0.1, 0.05)
    with pytest.raises(ValueError, match="outside"):
        sequential_canary([Observation("a", CAP, False, -1.0)], 0.1, 0.05)
    with pytest.raises(ValueError, match="two different caps"):
        sequential_canary(
            [Observation("a", CAP, True, 1.0), Observation("a", 2 * CAP, True, 1.0)], 0.1, 0.05
        )
    checks, res = sequential_checks([Observation("a", CAP, True, CAP + 1)], 0.1, 0.05, 0.0)
    assert res is None and checks[0].name == "canary_declared_caps" and not checks[0].passed


def test_empty_stream_decides_nothing() -> None:
    res = sequential_canary([], 0.1, 0.05)
    assert res.decision == "no_decision" and res.stopped_at == 0 and res.types == ()


# --- Monte Carlo ------------------------------------------------------------------------


def _stream(
    rng: random.Random, n: int, p: float, effect: float, types: int = 2, hetero: bool = False
) -> list[Observation]:
    """Potential costs Y(0) ~ U[0, 600] and Y(1) = Y(0) + effect, clipped to the cap
    (the tests keep effects small enough that clipping never moves the mean).
    With ``hetero``, Y(0) ~ U[300, 600] and each case's effect is ``effect +/- 300``
    (mean ``effect``, no clipping for |effect| <= 100). T ~ Bernoulli(p)."""
    out = []
    for _ in range(n):
        y0 = rng.uniform(300.0, 600.0) if hetero else rng.uniform(0.0, 600.0)
        delta = effect + (rng.choice((-300.0, 300.0)) if hetero else 0.0)
        y1 = min(CAP, max(0.0, y0 + delta))
        treated = rng.random() < p
        out.append(Observation(f"t{rng.randrange(types)}", CAP, treated, y1 if treated else y0))
    return out


def _rates(
    reps: int, n: int, p: float, effect: float, tol: float, seed: int, hetero: bool = False
) -> dict[str, float]:
    rng = random.Random(seed)
    harm = benefit = naive = 0
    for _ in range(reps):
        obs = _stream(rng, n, p, effect, hetero=hetero)
        res = sequential_canary(obs, p, 0.05, tol)
        harm += res.decision == "harm"
        benefit += res.decision == "benefit"
        t = [o.cost for o in obs if o.treated]
        c = [o.cost for o in obs if not o.treated]
        naive += bool(t and c) and sum(t) / len(t) > sum(c) / len(c) + tol
    return {"harm": harm / reps, "benefit": benefit / reps, "naive": naive / reps}


@pytest.mark.parametrize("p", [0.1, 0.5])
def test_false_rollback_rate_at_the_null_boundary(p: float) -> None:
    """Effect exactly at the tolerance, checked after every case (continuous
    monitoring): false rollbacks <= alpha; the point rule rolls back about half."""
    r = _rates(reps=150, n=300, p=p, effect=50.0, tol=50.0, seed=21)
    assert r["harm"] <= 0.05 + 0.04
    assert r["naive"] >= 0.3


def test_false_early_pass_rate_when_no_benefit() -> None:
    r = _rates(reps=150, n=300, p=0.25, effect=0.0, tol=0.0, seed=22)
    assert r["benefit"] <= 0.05 + 0.04


def test_heterogeneous_effects_with_zero_mean_keep_validity() -> None:
    r = _rates(reps=150, n=300, p=0.5, effect=0.0, tol=0.0, seed=23, hetero=True)
    assert r["harm"] <= 0.05 + 0.04 and r["benefit"] <= 0.05 + 0.04


def test_clear_harm_stops_early() -> None:
    rng = random.Random(24)
    obs = _stream(rng, 2_000, 0.5, effect=350.0)
    res = sequential_canary(obs, 0.5, 0.05)
    assert res.decision == "harm"
    assert res.stopped_at < 600
    assert res.benefit == ()
    assert all(t.harm_e_value >= 1.0 for t in res.types)
    assert all(t.effect_estimate > 0 for t in res.types)


def test_clear_benefit_stops_early() -> None:
    rng = random.Random(25)
    res = sequential_canary(_stream(rng, 2_000, 0.5, effect=-350.0), 0.5, 0.05)
    assert res.decision == "benefit" and res.stopped_at < 600 and res.harm == ()


def test_without_early_stop_all_cases_are_used() -> None:
    rng = random.Random(26)
    obs = _stream(rng, 500, 0.5, effect=350.0)
    res = sequential_canary(obs, 0.5, 0.05, stop_early=False)
    assert res.decision == "harm" and res.stopped_at == 500
    assert sum(t.n for t in res.types) == 500


def test_deterministic() -> None:
    obs = _stream(random.Random(27), 400, 0.1, effect=0.0)
    assert sequential_canary(obs, 0.1, 0.05) == sequential_canary(obs, 0.1, 0.05)


@pytest.mark.slow
@pytest.mark.parametrize("p", [0.05, 0.1, 0.25, 0.5])
def test_type_one_error_monte_carlo_long(p: float) -> None:
    for seed, hetero in ((31, False), (32, True)):
        r = _rates(reps=1_000, n=1_000, p=p, effect=0.0, tol=0.0, seed=seed, hetero=hetero)
        assert r["harm"] <= 0.05 + 0.015
        assert r["benefit"] <= 0.05 + 0.015


# --- wiring ----------------------------------------------------------------------------


def _gate(cfg: GateConfig) -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), cfg)
    g.bootstrap_baseline()
    return g


EV = GateConfig(stats="evalue", n_cases=1_000, canary_n_cases=4_000)


def test_one_shot_canary_good_rule_passes_with_sequential_evidence() -> None:
    g = _gate(EV)
    rv = g.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops", provenance=PROV)
    v = evaluate_canary(rv, g.registry.active_rules(), EV)
    assert v.outcome is Outcome.CANARY_PASSED
    assert v.canary is not None and v.canary.sequential is not None
    seq = v.canary.sequential
    assert seq.decision != "harm"
    assert seq.assignment_probability == pytest.approx(0.1)
    assert seq.n_matched == v.canary.matched_treatment + v.canary.matched_control
    names = [c.name for c in v.checks]
    assert "canary_sequential_no_harm" in names and "canary_cost_within_tolerance" not in names
    assert "sequential" in v.canary.model_dump()


def test_one_shot_canary_rolls_back_clear_harm_early() -> None:
    cfg = EV.model_copy(update={"canary_fraction": 0.5})
    g = _gate(cfg)
    rv = g.propose(HARMFUL_ID, HARMFUL, owner="ops", provenance=PROV)
    v = evaluate_canary(rv, g.registry.active_rules(), cfg)
    assert v.outcome is Outcome.ROLLBACK
    assert v.canary is not None and v.canary.sequential is not None
    seq = v.canary.sequential
    assert seq.decision == "harm" and seq.stopped_at < seq.n_matched
    assert any("harm detected" in r for r in v.reasons)


def test_bootstrap_canary_has_no_sequential_key() -> None:
    cfg = GateConfig(n_cases=1_000, bootstrap_resamples=200)
    g = _gate(cfg)
    rv = g.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops", provenance=PROV)
    v = evaluate_canary(rv, g.registry.active_rules(), cfg)
    assert v.canary is not None and v.canary.sequential is None
    assert "sequential" not in v.canary.model_dump()
    assert "canary_cost_within_tolerance" in [c.name for c in v.checks]


def test_stage_spends_alpha_across_the_schedule() -> None:
    g = _gate(EV)
    rv = g.propose(HARMFUL_ID, HARMFUL, owner="ops", provenance=PROV)
    cases = generate_cases(77, 3_000, prefix="s")
    sched = RampSchedule(stages=(10.0, 25.0, 50.0, 75.0))
    res = evaluate_stage(rv, g.registry.active_rules(), cases, sched, 2, config=EV)
    assert res.sequential is not None
    assert res.sequential.alpha == pytest.approx(EV.evalue_alpha / 4)
    assert res.sequential.assignment_probability == pytest.approx(0.5)
    assert res.outcome == "breach"
    assert res.metrics("default").sequential == res.sequential
    plain = evaluate_stage(rv, g.registry.active_rules(), cases, sched, 2)
    assert plain.sequential is None


def test_service_ramp_uses_sequential_test_when_configured() -> None:
    """Promote with the bootstrap gate, then run the ramp with the e-value config on
    the same storage (an operator switching statistics between stages)."""
    storage = SQLiteStorage(":memory:")
    boot = AgentGate(storage, SteppingClock(), GateConfig(n_cases=2_000, bootstrap_resamples=300))
    boot.bootstrap_baseline()
    boot.propose(GOOD_RULE_ID, GOOD_RULE, owner="ops", provenance=PROV)
    assert boot.evaluate(GOOD_RULE_ID, 1)[-1].decision.outcome is Outcome.CANARY_PASSED
    ev = AgentGate(storage, SteppingClock(), EV)
    sched = RampSchedule(stages=(10.0, 50.0))
    ev.start_canary_ramp(GOOD_RULE_ID, 1, sched)
    rec, _ = ev.advance_canary(GOOD_RULE_ID, 1)
    canary = rec.decision.canary
    assert canary is not None and canary.sequential is not None
    assert canary.sequential.alpha == pytest.approx(EV.evalue_alpha / 2)
    assert rec.decision.outcome in (Outcome.PROMOTE, Outcome.HOLD)
    assert ev.verify_integrity().ok
