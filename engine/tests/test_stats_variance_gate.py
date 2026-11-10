"""The variance-reduced gate option (GateConfig.stats = "evalue_vr"): stratified
betting in the shadow gate, its plan, the centred canary, engine wiring. Synthetic."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from scruple.clock import SteppingClock
from scruple.domain import Action, RuleSpec
from scruple.gate.config import GateConfig
from scruple.gate.engine import Verdict, evaluate_canary, evaluate_shadow
from scruple.gate.evalue import EValueEvidence, plan_sample, stratified_test
from scruple.gate.models import Outcome
from scruple.gate.provider import EValueStatsProvider, provider_for
from scruple.gate.sequential import Observation, ipw_term, sequential_canary
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID, C
from scruple.stats import LossPair
from scruple.stats.simulate import binomial_se
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

ALPHA = 0.05
VR = GateConfig(stats="evalue_vr", n_cases=3_000, canary_n_cases=4_000)
HARMFUL = RuleSpec(
    description="Contest fraud disputes without 3-D Secure.",
    conditions=(C("reason_code", "eq", "fraud"), C("three_ds", "eq", False)),
    action=Action.CONTEST,
    priority=35,
)


def _gate(cfg: GateConfig) -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), cfg)
    g.bootstrap_baseline()
    return g


def _shadow(cfg: GateConfig, rule_id: str) -> Verdict:
    g = _gate(cfg)
    spec = GOOD_RULE if rule_id == GOOD_RULE_ID else BAD_RULE
    rv = g.propose(rule_id, spec, owner="ops", provenance=PROV)
    return evaluate_shadow(rv, g.registry.active_rules(), cfg)


# --- config -------------------------------------------------------------------------


def test_default_stays_bootstrap_and_vr_is_accepted() -> None:
    assert GateConfig().stats == "bootstrap"
    assert VR.evalue_vr_stratified_share == 0.8
    assert isinstance(provider_for(VR), EValueStatsProvider)
    for bad in (0.0, 1.0):
        with pytest.raises(ValidationError):
            GateConfig(stats="evalue_vr", evalue_vr_stratified_share=bad)


# --- the core -------------------------------------------------------------------------


def _pairs(rng: random.Random, means: dict[str, float], n: int, spread: float) -> list[LossPair]:
    out = [
        LossPair(
            case_id=f"{t}-{i}",
            case_type=t,
            baseline_loss=0.0,
            candidate_loss=mu + rng.uniform(-spread, spread),
        )
        for t, mu in means.items()
        for i in range(n)
    ]
    rng.shuffle(out)
    return out


def _run(pairs: list[LossPair], types: list[str], share: float | None) -> EValueEvidence:
    return stratified_test(
        pairs,
        dict.fromkeys(types, 100.0),
        dict.fromkeys(types, 0.0),
        types,
        alpha=ALPHA,
        alpha_certify=ALPHA,
        confidence=0.95,
        cost_cap=100.0,
        stratified_share=share,
    )


TYPES = [f"t{i}" for i in range(8)]


def test_spread_regression_blocked_by_stratified_not_by_ebh() -> None:
    """Three of eight types regress a little: no single type reaches e-BH's bar, the
    stratified e-process adds the evidence up and blocks."""
    rng = random.Random(1)
    means = dict.fromkeys(TYPES, 0.0) | {"t0": 1.6, "t1": 1.6, "t2": 1.6}
    pairs = _pairs(rng, means, 400, 20.0)
    plain = _run(pairs, TYPES, None)
    vr = _run(pairs, TYPES, 0.8)
    assert plain.regressed == () and not plain.blocks
    assert vr.stratified is not None and vr.stratified.rejected and vr.blocks
    assert vr.stratified.alpha == pytest.approx(0.04)
    assert vr.stratified.threshold == pytest.approx(25.0)
    assert max(vr.stratified.contributions, key=vr.stratified.contributions.__getitem__) in {
        "t0",
        "t1",
        "t2",
    }
    assert all(t.alpha_share == pytest.approx(0.01 / 8) for t in vr.types)


def test_vr_keeps_the_false_block_rate_at_the_null_boundary() -> None:
    rng = random.Random(2)
    reps, blocks = 60, 0
    for _ in range(reps):
        pairs = _pairs(rng, dict.fromkeys(TYPES[:4], 0.0), 80, 90.0)
        blocks += _run(pairs, TYPES[:4], 0.8).blocks
    assert blocks / reps <= ALPHA + 3 * binomial_se(ALPHA, reps)


def test_plain_evalue_has_no_stratified_key() -> None:
    rng = random.Random(3)
    ev = _run(_pairs(rng, {"a": 0.0}, 50, 5.0), ["a"], None)
    assert ev.stratified is None and "stratified" not in ev.model_dump()
    vr = _run(_pairs(rng, {"a": 0.0}, 50, 5.0), ["a"], 0.8)
    assert "stratified" in vr.model_dump()
    assert type(vr).model_validate_json(vr.model_dump_json()) == vr
    with pytest.raises(ValueError):
        _run(_pairs(rng, {"a": 0.0}, 5, 5.0), ["a"], 1.0)


def test_vr_plan_never_needs_more_than_the_plain_plan() -> None:
    counts = {"a": 2_000, "b": 300, "c": 100}
    caps = dict.fromkeys(counts, 10_000.0)
    tol = dict.fromkeys(counts, 0.0)
    plain = plan_sample(counts, caps, tol, list(counts), GateConfig(stats="evalue"))
    vr = plan_sample(counts, caps, tol, list(counts), VR)
    for p, v in zip(plain.types, vr.types, strict=True):
        assert v.required_n <= p.required_n
    assert vr.required_total_cases <= plain.required_total_cases
    assert "stratified" in vr.method


# --- canary ---------------------------------------------------------------------------


@pytest.mark.parametrize("p", [0.1, 0.3])
@pytest.mark.parametrize("f", [0.0, 250.0, 900.0])
def test_centred_ipw_term_is_unbiased_for_any_prediction(p: float, f: float) -> None:
    y1, y0 = 700.0, 400.0
    mean = p * ipw_term(y1, True, p, f) + (1 - p) * ipw_term(y0, False, p, f)
    assert mean == pytest.approx(y1 - y0)


def _canary(effect: float, centred: bool, reps: int, seed: int, n: int = 1500) -> float:
    rng = random.Random(seed)
    hits = 0
    for _ in range(reps):
        obs = []
        for _ in range(n):
            base = 600.0 + rng.uniform(-100, 100)  # costs far from zero
            treated = rng.random() < 0.3
            cost = min(1000.0, max(0.0, base + (effect if treated else 0.0)))
            obs.append(Observation("t", 1000.0, treated, cost))
        hits += sequential_canary(obs, 0.3, ALPHA, centred=centred).decision == "harm"
    return hits / reps


@pytest.mark.parametrize("centred", [False, True])
def test_canary_holds_alpha_with_no_effect(centred: bool) -> None:
    reps = 100
    assert _canary(0.0, centred, reps, seed=20, n=600) <= ALPHA + 3 * binomial_se(ALPHA, reps)


def test_centred_canary_is_more_powerful() -> None:
    plain = _canary(60.0, False, 40, seed=21)
    centred = _canary(60.0, True, 40, seed=21)
    assert centred >= plain + 0.3, (plain, centred)


def test_engine_canary_uses_the_centred_test() -> None:
    cfg = VR.model_copy(update={"n_cases": 1_000, "canary_fraction": 0.5})
    g = _gate(cfg)
    rv = g.propose("fraud-no-3ds-contest", HARMFUL, owner="ops", provenance=PROV)
    v = evaluate_canary(rv, g.registry.active_rules(), cfg)
    assert v.canary is not None and v.canary.sequential is not None
    assert "centred" in v.canary.sequential.method
    assert v.outcome is Outcome.ROLLBACK


# --- engine ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shadow() -> dict[str, Verdict]:
    return {r: _shadow(VR, r) for r in (GOOD_RULE_ID, BAD_RULE_ID)}


def test_engine_reports_the_stratified_test(shadow: dict[str, Verdict]) -> None:
    for v in shadow.values():
        assert v.shadow is not None and v.shadow.evalue is not None
        ev = v.shadow.evalue
        assert ev.stratified is not None
        assert "stratified" in ev.method
        check = next(c for c in v.checks if c.name == "evalue_no_type_regression")
        assert check.kind == "hard" and "stratified betting" in check.detail
        assert check.passed == (not ev.blocks)
        s = v.shadow
        assert type(s).model_validate_json(s.model_dump_json()) == s


def test_bad_rule_still_blocked_by_its_policy_check(shadow: dict[str, Verdict]) -> None:
    bad = shadow[BAD_RULE_ID]
    assert bad.outcome is Outcome.BLOCK
    assert "no_new_policy_violations" in {c.name for c in bad.checks if not c.passed}


def test_vr_share_at_default_leaves_the_config_hash_alone() -> None:
    assert "evalue_vr_stratified_share" not in GateConfig().model_dump()
    custom = GateConfig(stats="evalue_vr", evalue_vr_stratified_share=0.5)
    assert custom.model_dump()["evalue_vr_stratified_share"] == 0.5
    assert GateConfig.model_validate_json(custom.model_dump_json()) == custom
    assert GateConfig.model_validate_json(VR.model_dump_json()) == VR
