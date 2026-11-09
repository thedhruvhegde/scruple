"""LangGraph adapter with a fake model. Skipped cleanly if langgraph is absent."""

from __future__ import annotations

import json
from typing import Any

import pytest

from scruple.agents import Episode
from scruple.agents.adapters import langgraph as lg
from scruple.agents.tools import TOOL_NAMES

pytest.importorskip("langgraph")


class FakeModel:
    """Replays scripted steps and records the messages it was shown."""

    def __init__(self, steps: list[lg.ModelStep]) -> None:
        self.steps = list(steps)
        self.seen: list[list[dict[str, Any]]] = []

    def __call__(self, messages: list[dict[str, Any]]) -> lg.ModelStep:
        self.seen.append(list(messages))
        return self.steps.pop(0) if self.steps else {"text": "nothing more"}


def step(*calls: tuple[str, Any]) -> lg.ModelStep:
    return {
        "tool_calls": [
            {"id": f"c{i}", "name": name, "args": args} for i, (name, args) in enumerate(calls)
        ]
    }


def test_available() -> None:
    assert lg.available()


def test_graph_plays_an_episode() -> None:
    model = FakeModel(
        [
            step(("get_case", {}), ("list_evidence", {})),
            step(("represent", {"evidence_ids": ["ev-01", "ev-02"]})),
            step(("submit_decision", {"explanation": "authenticated and delivered"})),
        ]
    )
    ep = Episode(1, "fraud")
    score = lg.run_episode(ep, model)
    assert score.action is not None
    assert score.action.value == "represent"
    assert len(model.seen) == 3
    tool_msgs = [m for m in model.seen[1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c1"]
    assert "fields" in json.loads(tool_msgs[0]["content"])


def test_tool_errors_are_returned_to_the_model() -> None:
    model = FakeModel(
        [
            step(("get_evidence", "ev-01"), ("teleport", {})),
            step(("submit_decision", {"explanation": "x", "action": "refund"})),
        ]
    )
    score = lg.run_episode(Episode(1, "fraud"), model)
    errors = [m for m in model.seen[1] if m.get("is_error")]
    assert len(errors) == 2
    assert score.tool_errors == 2


def test_no_tool_calls_lapses() -> None:
    score = lg.run_episode(Episode(1, "fraud"), FakeModel([{"text": "I refuse."}]))
    assert "lapsed:model_stopped" in score.flags


def test_max_turns_lapses() -> None:
    model = FakeModel([step(("get_case", {}))] * 10)
    score = lg.run_episode(Episode(1, "fraud"), model, max_turns=3)
    assert "lapsed:max_turns" in score.flags
    assert len(model.seen) == 3


def test_deterministic() -> None:
    def play() -> str:
        model = FakeModel(
            [
                step(("escalate", {})),
                step(("submit_decision", {"explanation": "x"})),
            ]
        )
        return lg.run_episode(Episode(5, "high_value"), model).model_dump_json()

    assert play() == play()


def test_langchain_tools() -> None:
    ep = Episode(1, "fraud")
    tools = {t.name: t for t in lg.langchain_tools(ep)}
    assert list(tools) == list(TOOL_NAMES)
    listed = json.loads(tools["list_evidence"].invoke({}))
    assert listed["evidence"][0]["evidence_id"] == "ev-01"
    missing = json.loads(tools["get_evidence"].invoke({"evidence_id": "ev-77"}))
    assert "unknown evidence" in missing["error"]
    tools["escalate"].invoke({"reason": "unsure"})
    score = json.loads(tools["submit_decision"].invoke({"explanation": "sent to a human"}))
    assert score["action"] == "escalate"
    assert ep.closed
    closed = json.loads(tools["get_case"].invoke({}))
    assert "closed" in closed["error"]
