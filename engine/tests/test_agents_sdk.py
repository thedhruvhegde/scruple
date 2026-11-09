"""`scruple.evaluate` and its scorecard."""

from __future__ import annotations

import contextlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest

import scruple
from scruple.agents import ActionKind, Decision, EpisodeError, Observation, Toolbox
from scruple.agents.baselines import always_accept, oracle, rule_based
from scruple.agents.env import SimEnvironment
from scruple.agents.runners import AnthropicRunner, RunnerConfig
from scruple.agents.sdk import Scorecard, evaluate
from tests.test_agents_runner_anthropic import Script, msg, use

CASE_TYPES = SimEnvironment().case_types()
ROOT = Path(__file__).resolve().parent.parent


def test_top_level_evaluate() -> None:
    assert scruple.evaluate is evaluate
    assert scruple.Scorecard is Scorecard
    with pytest.raises(AttributeError):
        _ = scruple.nope


def test_scorecard_shape() -> None:
    card = evaluate(always_accept, n=16, seed=3)
    assert card.agent == "always_accept"
    assert card.n == card.overall.episodes == 16
    assert card.synthetic
    assert not card.ground_truth_exposed
    assert set(card.by_case_type) == set(CASE_TYPES)
    assert all(s.episodes == 2 for s in card.by_case_type.values())
    assert sum(s.total_cost_usd_minor for s in card.by_case_type.values()) == (
        card.overall.total_cost_usd_minor
    )
    assert card.overall.outcomes == {"accepted": 16}
    assert card.by_case_type["duplicate_dispute"].verifier_failures["policy_constraints"] == 2
    assert card.by_case_type["duplicate_dispute"].findings["no_accept_on_duplicate"] == 2
    json.loads(card.model_dump_json())


def test_deterministic() -> None:
    assert evaluate(rule_based, n=12, seed=5) == evaluate(rule_based, n=12, seed=5)
    assert evaluate(rule_based, n=12, seed=5) != evaluate(rule_based, n=12, seed=6)


def test_case_type_filter() -> None:
    card = evaluate(rule_based, n=5, seed=1, case_types=["high_value"])
    assert set(card.by_case_type) == {"high_value"}
    assert card.overall.outcomes == {"escalated": 5}
    assert card.overall.pass_rate == 1.0


def test_oracle_reference() -> None:
    card = evaluate(oracle, n=16, seed=2, ground_truth=True)
    assert card.ground_truth_exposed
    assert card.overall.mean_compliant_regret_usd_minor == 0
    assert card.overall.pass_rate == 1.0
    assert "ground truth exposed" in card.summary()


def test_oracle_needs_ground_truth() -> None:
    card = evaluate(oracle, n=4, seed=2)
    assert card.overall.outcomes == {"lapsed": 4}
    assert card.overall.flags["lapsed"] == 4


def test_hacking_flags_are_counted() -> None:
    def liar(obs: Observation, tools: Toolbox) -> Decision:
        with contextlib.suppress(EpisodeError):
            tools.call("get_evidence", evidence_id="ev-99")
        tools.call("accept_liability")
        return Decision(explanation="I won.", action=ActionKind.REPRESENT, claimed_cost_usd_minor=0)

    card = evaluate(liar, n=8, seed=4)
    assert card.overall.flags["claimed_action_mismatch"] == 8
    assert card.overall.flags["claimed_cost_mismatch"] == 8
    assert card.overall.flags["unknown_evidence_probe"] == 8
    assert card.overall.hacking_flags == 24
    # Claims never change the money.
    honest = evaluate(always_accept, n=8, seed=4)
    assert card.overall.total_cost_usd_minor == honest.overall.total_cost_usd_minor


def test_summary_table() -> None:
    text = evaluate(rule_based, n=8, seed=0).summary()
    assert "synthetic data" in text
    for t in (*CASE_TYPES, "all"):
        assert t in text


def test_runner_agent() -> None:
    reply = msg(use("t1", "submit_decision", {"explanation": "x", "action": "escalate"}))
    script = Script([reply] * 4)
    runner = AnthropicRunner(
        RunnerConfig(model="claude-haiku-5-5"),
        api_key="k",
        transport=httpx.MockTransport(script),
    )
    card = evaluate(runner, n=4, seed=0)
    assert card.agent == "anthropic:claude-haiku-5-5"
    assert card.overall.outcomes == {"escalated": 4}
    with pytest.raises(ValueError, match="ground truth"):
        evaluate(runner, n=1, ground_truth=True)


def test_invalid_n() -> None:
    with pytest.raises(ValueError, match="n must be"):
        evaluate(rule_based, n=0)


def _load_example(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rule_based_example_runs(capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load_example("rule_based_agent")
    cards = mod.main(8, 0)
    out = capsys.readouterr().out
    assert out.count("scorecard:") == 4
    assert cards[3].overall.mean_compliant_regret_usd_minor == 0
    assert all(c.overall.flags.get("lapsed", 0) == 0 for c in cards)


def test_model_example_imports() -> None:
    assert callable(_load_example("model_agent").main)
