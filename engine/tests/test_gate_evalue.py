"""Per-case-type e-value gate (GateConfig.stats = "evalue"): case types, caps,
stratified e-BH regression flags, improvement certificate, engine wiring.

Also the evidence for closing verifier gap G-7 (zero-tolerance slice check
false-blocks at the null boundary) when the e-value gate is selected. Synthetic.
"""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from scruple.clock import SteppingClock
from scruple.domain import Action
from scruple.gate.config import EVALUE_FEES_USD_MINOR, GateConfig
from scruple.gate.engine import Verdict, evaluate_shadow
from scruple.gate.evalue import (
    EValueEvidence,
    case_type,
    evaluate_evalue,
    stratified_test,
    tolerance_for,
)
from scruple.gate.models import Outcome
from scruple.registry.models import RuleVersion
from scruple.service import AgentGate
from scruple.sim.generator import generate_cases
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.sim.models import Scenario
from scruple.stats import LossPair
from scruple.storage import SQLiteStorage
from scruple.verifiers.outcome import OutcomeScores
from tests.ci_support import PROV

EV = GateConfig(stats="evalue", n_cases=3_000)


def _gate(cfg: GateConfig) -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), cfg)
    g.bootstrap_baseline()
    return g


def _shadow(cfg: GateConfig, rule_id: str) -> Verdict:
    g = _gate(cfg)
    spec = GOOD_RULE if rule_id == GOOD_RULE_ID else BAD_RULE
    rv: RuleVersion = g.propose(rule_id, spec, owner="ops", provenance=PROV)
    return evaluate_shadow(rv, g.registry.active_rules(), cfg)


# --- case types and caps -------------------------------------------------------------


def test_case_types_band_by_amount_with_declared_caps() -> None:
    cases = generate_cases(5, 400)
    for c in cases:
        t, cap = case_type(c, EV)
        scen, band = t.split("/")
        assert scen == c.scenario.value
        if c.amount_minor < 8_000:
            assert band == "<$80" and cap == 8_000 + EVALUE_FEES_USD_MINOR
        elif c.amount_minor < 1_000_000:
            assert band == "<$10,000" and cap == 1_000_000 + EVALUE_FEES_USD_MINOR
        else:
            assert band == ">=$10,000" and cap == EV.evalue_cost_cap_usd_minor
    assert EVALUE_FEES_USD_MINOR == 30_000  # $250 arbitration + $15 + $35 (sim fees)


def test_band_labels_with_cents() -> None:
    cfg = GateConfig(evalue_band_edges_usd_minor=(1_050,))
    c = generate_cases(1, 5)[0].model_copy(update={"amount_minor": 1_000})
    assert case_type(c, cfg)[0].endswith("/<$10.50")


@pytest.mark.parametrize("edges", [(), (0,), (100, 100), (200, 100)])
def test_band_edges_must_increase(edges: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        GateConfig(evalue_band_edges_usd_minor=edges)


def test_type_tolerances_precedence_and_validation() -> None:
    cfg = GateConfig(
        evalue_tolerance_usd_minor=5.0,
        evalue_type_tolerances_usd_minor={"late_evidence": 50.0, "late_evidence/<$80": 7.0},
    )
    assert tolerance_for("late_evidence/<$80", cfg) == 7.0
    assert tolerance_for("late_evidence/<$10,000", cfg) == 50.0
    assert tolerance_for("fraud/<$80", cfg) == 5.0
    with pytest.raises(ValidationError):
        GateConfig(evalue_type_tolerances_usd_minor={"fraud": -1.0})
    with pytest.raises(ValidationError):
        GateConfig(evalue_tolerance_usd_minor=-1.0)


# --- the statistical core on synthetic pairs ----------------------------------------


def _pairs(
    rng: random.Random, means: dict[str, float], n: int, cap: float, spread: float
) -> list[LossPair]:
    out: list[LossPair] = []
    for t, mu in means.items():
        for i in range(n):
            d = mu + rng.uniform(-spread, spread)
            out.append(
                LossPair(case_id=f"{t}-{i}", case_type=t, baseline_loss=0.0, candidate_loss=d)
            )
    rng.shuffle(out)
    return out


def _run(pairs: list[LossPair], types: list[str], cap: float, tol: float = 0.0) -> EValueEvidence:
    return stratified_test(
        pairs,
        {t: cap for t in types},
        {t: tol for t in types},
        types,
        alpha=0.05,
        alpha_certify=0.05,
        confidence=0.95,
        cost_cap=cap,
    )


TYPES = ["a", "b", "c", "d"]


def _null_boundary_rates(reps: int, n: int, seed: int) -> tuple[float, float]:
    """(zero-tolerance slice check false-block rate, e-BH false-block rate) when
    every type's true mean change is exactly zero (the null boundary)."""
    rng = random.Random(seed)
    naive = ebh = 0
    for _ in range(reps):
        pairs = _pairs(rng, dict.fromkeys(TYPES, 0.0), n, 100.0, 90.0)
        slice_means = {t: sum(p.diff for p in pairs if p.case_type == t) / n for t in TYPES}
        naive += any(m > 0.0 for m in slice_means.values())
        ebh += bool(_run(pairs, TYPES, 100.0).regressed)
    return naive / reps, ebh / reps


def test_g7_zero_tolerance_false_blocks_but_ebh_does_not() -> None:
    """Gap G-7: the zero-tolerance slice check blocks most null-boundary candidates
    (about 1 - 0.5^4 = 94% with 4 types); e-BH keeps the false-block rate <= alpha."""
    naive, ebh = _null_boundary_rates(reps=60, n=60, seed=7)
    assert naive >= 0.8
    assert ebh <= 0.05 + 0.05  # alpha plus Monte Carlo slack at 60 reps


@pytest.mark.slow
def test_g7_null_boundary_monte_carlo_long() -> None:
    naive, ebh = _null_boundary_rates(reps=600, n=150, seed=8)
    assert naive >= 0.9
    assert ebh <= 0.05 + 0.02


def test_unchanged_types_never_flag() -> None:
    pairs = [
        LossPair(case_id=str(i), case_type=TYPES[i % 4], baseline_loss=5.0, candidate_loss=5.0)
        for i in range(400)
    ]
    ev = _run(pairs, TYPES, 100.0)
    assert ev.regressed == () and ev.improved == ()
    assert all(t.regression_e_value == 1.0 for t in ev.types)


def test_rare_type_regression_is_flagged_while_average_improves() -> None:
    rng = random.Random(3)
    pairs = _pairs(rng, {"a": -20.0, "b": -20.0, "c": -20.0}, 400, 100.0, 10.0)
    pairs += _pairs(rng, {"rare": 30.0}, 120, 100.0, 5.0)
    ev = _run(pairs, [*"abc", "rare"], 100.0)
    assert ev.pooled_mean_diff < 0
    assert ev.regressed == ("rare",)
    assert set(ev.improved) == {"a", "b", "c"}
    rare = next(t for t in ev.types if t.case_type == "rare")
    assert rare.regressed and rare.ci_low > 0
    assert rare.alpha_share == pytest.approx(0.05 / 4)
    assert rare.e_threshold == pytest.approx(4 / 0.05)


def test_regression_within_tolerance_is_not_flagged() -> None:
    rng = random.Random(4)
    pairs = _pairs(rng, {"a": 3.0, "b": 0.0}, 600, 100.0, 2.0)
    assert _run(pairs, ["a", "b"], 100.0, tol=0.0).regressed == ("a",)
    assert _run(pairs, ["a", "b"], 100.0, tol=5.0).regressed == ()


def test_cap_violation_raises_in_the_core() -> None:
    pairs = [LossPair(case_id="x", case_type="a", baseline_loss=0.0, candidate_loss=101.0)]
    with pytest.raises(ValueError, match="cap"):
        _run(pairs, ["a"], 100.0)


def test_empty_family_tests_nothing() -> None:
    pairs = [LossPair(case_id="x", case_type="a", baseline_loss=1.0, candidate_loss=1.0)]
    ev = stratified_test(
        pairs, {"a": 100.0}, {}, [], alpha=0.05, alpha_certify=0.05, confidence=0.95, cost_cap=100.0
    )
    assert ev.family == () and ev.types == () and ev.untouched_types == 1


# --- evaluate_evalue on real (synthetic) shadow scores ------------------------------


def _scores(regret: dict[str, int], actions: dict[str, Action]) -> OutcomeScores:
    return OutcomeScores(
        regret=regret,
        action=actions,
        scenario=dict.fromkeys(regret, Scenario.FRAUD),
        mean_regret=0.0,
        accuracy=0.0,
        escalation_rate=0.0,
        slice_mean_regret={},
    )


def test_loss_change_without_decision_change_fails_closed() -> None:
    cases = generate_cases(9, 20)
    acts = {c.case_id: Action.ACCEPT for c in cases}
    b = _scores({c.case_id: 100 for c in cases}, acts)
    c_reg = {c.case_id: 100 for c in cases}
    c_reg[cases[0].case_id] = 150
    ev = evaluate_evalue(cases, b, _scores(c_reg, acts), EV)
    assert ev.violations and "decision did not" in ev.violations[0]
    assert ev.types == ()


def test_cap_violation_fails_closed_in_the_engine() -> None:
    cfg = EV.model_copy(
        update={
            "n_cases": 400,
            "evalue_fees_usd_minor": 0,
            "evalue_band_edges_usd_minor": (1,),
            "evalue_cost_cap_usd_minor": 1.0,
        }
    )
    v = _shadow(cfg, GOOD_RULE_ID)
    assert v.outcome is Outcome.BLOCK
    check = next(c for c in v.checks if c.name == "evalue_declared_caps")
    assert not check.passed and "exceeds declared cap" in check.detail


def test_tolerance_at_or_above_cap_is_an_error() -> None:
    cfg = EV.model_copy(update={"n_cases": 300, "evalue_tolerance_usd_minor": 40_000.0})
    with pytest.raises(ValueError, match="tolerance"):
        _shadow(cfg, GOOD_RULE_ID)


# --- engine wiring -------------------------------------------------------------------


@pytest.fixture(scope="module")
def small() -> dict[str, Verdict]:
    return {r: _shadow(EV, r) for r in (GOOD_RULE_ID, BAD_RULE_ID)}


@pytest.fixture(scope="module")
def big() -> dict[str, Verdict]:
    """25,000 held-out cases: enough for the e-value gate on both demo rules."""
    cfg = GateConfig(stats="evalue", n_cases=25_000)
    return {r: _shadow(cfg, r) for r in (GOOD_RULE_ID, BAD_RULE_ID)}


def test_evalue_checks_replace_slice_and_bootstrap_checks(small: dict[str, Verdict]) -> None:
    names = [c.name for c in small[GOOD_RULE_ID].checks]
    for gone in ("no_slice_regression", "not_significantly_worse", "significant_improvement"):
        assert gone not in names
    for new in ("evalue_declared_caps", "evalue_no_type_regression"):
        assert new in names
        assert next(c for c in small[GOOD_RULE_ID].checks if c.name == new).kind == "hard"
    cert = next(c for c in small[GOOD_RULE_ID].checks if c.name == "evalue_improvement_certified")
    assert cert.kind == "evidence"


@pytest.mark.slow  # the demo test runs the same story at 30,000 cases by default
def test_good_rule_certified_and_promoted(big: dict[str, Verdict]) -> None:
    v = big[GOOD_RULE_ID]
    assert v.outcome is Outcome.PROMOTE
    assert v.shadow is not None and v.shadow.evalue is not None
    ev = v.shadow.evalue
    assert ev.regressed == ()
    assert "not_received/<$10,000" in ev.improved
    assert all(t.case_type.startswith("not_received/") for t in ev.types)
    assert any("certified better" in r for r in v.reasons)


@pytest.mark.slow
def test_bad_rule_blocked_by_the_stratified_check(big: dict[str, Verdict]) -> None:
    v = big[BAD_RULE_ID]
    assert v.outcome is Outcome.BLOCK
    assert v.shadow is not None and v.shadow.evalue is not None
    assert "late_evidence/<$80" in v.shadow.evalue.regressed
    failed = {c.name for c in v.checks if not c.passed}
    assert "evalue_no_type_regression" in failed


def test_small_held_out_set_holds_both_rules() -> None:
    """The finding from docs/math/06: at 3,000 cases the stratified test certifies
    nothing and flags nothing (the bad rule is still blocked by its policy check)."""
    good = _shadow(EV, GOOD_RULE_ID)
    assert good.outcome is Outcome.HOLD
    bad = _shadow(EV, BAD_RULE_ID)
    assert bad.shadow is not None and bad.shadow.evalue is not None
    assert bad.shadow.evalue.regressed == ()
    assert "no_new_policy_violations" in {c.name for c in bad.checks if not c.passed}


def test_bootstrap_path_is_unchanged_and_has_no_evalue_key() -> None:
    v = _shadow(GateConfig(n_cases=1_000, bootstrap_resamples=200), GOOD_RULE_ID)
    assert v.shadow is not None and v.shadow.evalue is None
    assert "evalue" not in v.shadow.model_dump()
    assert "no_slice_regression" in [c.name for c in v.checks]


def test_evalue_evidence_survives_a_round_trip(small: dict[str, Verdict]) -> None:
    s = small[BAD_RULE_ID].shadow
    assert s is not None
    again = type(s).model_validate_json(s.model_dump_json())
    assert again == s
