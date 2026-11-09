"""Backtest (154): rule files and agent modules as candidates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scruple.backtest import (
    CandidateDecision,
    PaymentIndex,
    build_view,
    load_candidate,
    rule_candidate,
)
from scruple.domain import Action
from scruple.sim.library import GOOD_RULE
from tests.test_backtest_view import case, history

SPEC: dict[str, Any] = {
    "description": "contest fraud under $200",
    "conditions": [{"field": "amount_usd_minor", "op": "lt", "value": 20_000}],
    "action": "contest",
    "priority": 5,
}


def view(amount: int = 10_000) -> Any:
    h = history([case(1, amount=amount)])
    return build_view(h.cases[0], PaymentIndex(h.cases))


def test_single_rule_keeps_history_where_it_does_not_fire() -> None:
    c = rule_candidate(SPEC, "small")
    assert c.decide(view(10_000)) == CandidateDecision(action=Action.CONTEST, ref="small@candidate")
    assert c.decide(view(50_000)) == CandidateDecision(action=None, ref="historical")


def test_rule_set_with_a_default_and_priorities() -> None:
    doc = {
        "name": "policy",
        "rules": [
            {
                "rule_id": "big-escalate",
                "spec": {
                    **SPEC,
                    "conditions": [{"field": "amount_usd_minor", "op": "ge", "value": 40_000}],
                    "action": "escalate",
                    "priority": 1,
                },
            },
            SPEC,
        ],
        "default_action": "accept",
    }
    c = rule_candidate(doc)
    assert c.name == "policy"
    assert c.decide(view(50_000)).action is Action.ESCALATE
    assert c.decide(view(10_000)).action is Action.CONTEST
    assert c.decide(view(30_000)) == CandidateDecision(action=Action.ACCEPT, ref="default")


def test_proposal_body_is_accepted() -> None:
    c = rule_candidate({"rule_id": "inr", "spec": GOOD_RULE.model_dump(mode="json")})
    assert c.name == "inr"


@pytest.mark.parametrize(
    "doc",
    [
        [],
        {"rules": []},
        {"rules": [SPEC], "default_action": "refund"},
        {**SPEC, "surprise": 1},
        {"spec": SPEC},
        {"rules": [{**SPEC, "conditions": [{"field": "claim_valid", "op": "eq", "value": True}]}]},
    ],
)
def test_bad_rule_files_are_refused(doc: Any) -> None:
    with pytest.raises(ValueError):
        rule_candidate(doc)


def test_rules_cannot_reference_hidden_fields(tmp_path: Path) -> None:
    p = tmp_path / "peek.json"
    p.write_text(
        json.dumps({**SPEC, "conditions": [{"field": "state", "op": "eq", "value": "won"}]})
    )
    with pytest.raises(ValueError, match="not a rule candidate"):
        load_candidate(str(p))


def test_load_json_and_python_files(tmp_path: Path) -> None:
    rp = tmp_path / "rule.json"
    rp.write_text(json.dumps(SPEC))
    assert load_candidate(str(rp)).decide(view()).action is Action.CONTEST
    mp = tmp_path / "agent.py"
    mp.write_text(
        "def decide(view):\n    return 'escalate' if view.amount_minor > 20000 else None\n"
    )
    agent = load_candidate(str(mp))
    assert agent.name == "agent"
    assert agent.decide(view(30_000)) == CandidateDecision(action=Action.ESCALATE, ref="agent")
    assert agent.decide(view(1_000)).action is None


def test_module_attr_and_objects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "my_agent_mod.py").write_text(
        "class Agent:\n"
        "    def decide(self, view):\n"
        "        return ('contest', 'my-rule')\n"
        "AGENT = Agent()\n"
        "NOT_CALLABLE = 3\n"
        "def bad(view):\n"
        "    return 'refund'\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    a = load_candidate("my_agent_mod:AGENT")
    assert a.decide(view()) == CandidateDecision(action=Action.CONTEST, ref="my-rule")
    with pytest.raises(ValueError, match="not callable"):
        load_candidate("my_agent_mod:NOT_CALLABLE")
    with pytest.raises(ValueError, match="no `missing`"):
        load_candidate("my_agent_mod:missing")
    with pytest.raises(ValueError, match="expected an Action"):
        load_candidate("my_agent_mod:bad").decide(view())
    with pytest.raises(FileNotFoundError):
        load_candidate(str(tmp_path / "nope.py"))
