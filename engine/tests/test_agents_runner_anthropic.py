"""Anthropic runner against a scripted fake transport. No live calls."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scruple.agents import Episode
from scruple.agents.runners import AnthropicRunner, RunnerConfig
from scruple.agents.runners.anthropic import DEFAULT_MODEL, FAST_MODEL, default_model

Reply = dict[str, Any] | Exception | httpx.Response


class Script:
    """An httpx handler that replays canned Messages API responses in order."""

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("model called more times than scripted")
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, json=r)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def use(call_id: str, name: str, args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def msg(*blocks: dict[str, Any], stop: str = "tool_use", tokens: int = 10) -> dict[str, Any]:
    return {
        "id": "msg",
        "type": "message",
        "role": "assistant",
        "content": list(blocks),
        "stop_reason": stop,
        "usage": {"input_tokens": tokens, "output_tokens": tokens},
    }


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def runner(script: Script, **cfg: Any) -> AnthropicRunner:
    sleeps: list[float] = []
    r = AnthropicRunner(
        RunnerConfig(model=FAST_MODEL, **cfg),
        api_key="test-key",
        transport=httpx.MockTransport(script),
        sleep=sleeps.append,
    )
    r.sleeps = sleeps  # type: ignore[attr-defined]
    return r


HAPPY: list[Reply] = [
    msg(text("Looking."), use("t1", "list_evidence", {})),
    msg(use("t2", "represent", {"evidence_ids": ["ev-01"]})),
    msg(use("t3", "submit_decision", {"explanation": "3DS authenticated, contested."})),
]


def test_happy_path() -> None:
    script = Script(HAPPY)
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "submitted"
    assert trace.turns == 3
    assert trace.score.action is not None
    assert trace.score.action.value == "represent"
    assert trace.score.flags == ()
    assert [c.name for c in trace.tool_calls] == ["list_evidence", "represent", "submit_decision"]
    assert trace.input_tokens == trace.output_tokens == 30
    req = script.requests[0]
    assert req.url.path == "/v1/messages"
    assert req.headers["x-api-key"] == "test-key"
    assert req.headers["anthropic-version"] == "2023-06-01"
    body = script.bodies()[0]
    assert body["model"] == FAST_MODEL
    assert len(body["tools"]) == 10
    assert "temperature" not in body
    assert "Observation" in body["messages"][0]["content"][0]["text"]


def test_tool_results_are_sent_back() -> None:
    script = Script(HAPPY)
    runner(script).run(Episode(1, "fraud"))
    second = script.bodies()[1]["messages"]
    assert second[1]["role"] == "assistant"
    result = second[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "t1"
    assert result["is_error"] is False
    assert "ev-01" in result["content"]


@pytest.mark.parametrize(
    "bad",
    [
        use("b", "get_evidence", "ev-01"),
        use("b", "get_evidence", {}),
        use("b", "get_evidence", {"evidence_id": "ev-01", "extra": 1}),
        use("b", "get_evidence", None),
        use("b", "wire_money", {"to": "me"}),
        use("b", "represent", {"evidence_ids": ["ev-404"]}),
    ],
)
def test_malformed_tool_calls_become_tool_errors(bad: dict[str, Any]) -> None:
    script = Script(
        [
            msg(bad),
            msg(use("t2", "submit_decision", {"explanation": "x", "action": "escalate"})),
        ]
    )
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "submitted"
    assert trace.tool_calls[0].ok is False
    result = script.bodies()[1]["messages"][2]["content"][0]
    assert result["is_error"] is True
    assert "error" in json.loads(result["content"])
    assert trace.score.tool_errors == 1


def test_max_turns_cutoff_lapses() -> None:
    script = Script([msg(use(f"t{i}", "get_case", {})) for i in range(3)])
    trace = runner(script, max_turns=3).run(Episode(1, "fraud"))
    assert trace.stop == "max_turns"
    assert trace.turns == 3
    assert "lapsed:max_turns" in trace.score.flags
    assert trace.score.outcome == "lapsed"


def test_token_budget_cutoff() -> None:
    script = Script([msg(use("t1", "get_case", {}), tokens=400)])
    trace = runner(script, token_budget=500).run(Episode(1, "fraud"))
    assert trace.stop == "token_budget"
    assert "lapsed:token_budget" in trace.score.flags


def test_model_refusal() -> None:
    script = Script([msg(text("I can't help with that."), stop="refusal")])
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "refusal"
    assert trace.detail == "I can't help with that."
    assert "lapsed:refusal" in trace.score.flags


def test_text_only_gets_one_nudge() -> None:
    script = Script(
        [
            msg(text("I think we should accept."), stop="end_turn"),
            msg(use("t1", "submit_decision", {"explanation": "x", "action": "accept_liability"})),
        ]
    )
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "submitted"
    nudge = script.bodies()[1]["messages"][-1]["content"][0]["text"]
    assert "submit_decision" in nudge


def test_text_only_without_nudges_stops() -> None:
    script = Script([msg(text("done"), stop="end_turn")])
    trace = runner(script, nudges=0).run(Episode(1, "fraud"))
    assert trace.stop == "model_stopped"


def test_timeout() -> None:
    script = Script([httpx.ReadTimeout("slow")])
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "timeout"
    assert "lapsed:timeout" in trace.score.flags


def test_timeout_is_configured_on_client() -> None:
    r = runner(Script([]), timeout_s=7.5)
    assert r._client.timeout.read == 7.5


def test_retries_then_succeeds() -> None:
    script = Script([httpx.Response(529, json={"error": "overloaded"}), *HAPPY])
    r = runner(script)
    trace = r.run(Episode(1, "fraud"))
    assert trace.stop == "submitted"
    assert r.sleeps == [1.0]  # type: ignore[attr-defined]


def test_retries_exhausted() -> None:
    script = Script([httpx.Response(500, text="no")] * 3)
    trace = runner(script, max_retries=2).run(Episode(1, "fraud"))
    assert trace.stop == "http_error"
    assert "HTTP 500" in (trace.detail or "")


def test_client_error_is_not_retried() -> None:
    script = Script([httpx.Response(400, json={"error": "bad request"})])
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "http_error"
    assert len(script.requests) == 1


@pytest.mark.parametrize(
    "reply",
    [{"content": "oops"}, {"no_content": True}, httpx.Response(200, text="not json")],
)
def test_bad_response(reply: Reply) -> None:
    trace = runner(Script([reply])).run(Episode(1, "fraud"))
    assert trace.stop == "bad_response"


def test_calls_after_submit_in_same_turn_are_refused() -> None:
    script = Script(
        [
            msg(
                use("t1", "submit_decision", {"explanation": "x", "action": "escalate"}),
                use("t2", "refund", {}),
            )
        ]
    )
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "submitted"
    assert trace.tool_calls[1].ok is False
    assert "closed" in (trace.tool_calls[1].error or "")
    assert trace.score.action is not None
    assert trace.score.action.value == "escalate"


def test_runs_are_deterministic() -> None:
    a = runner(Script(HAPPY)).run(Episode(1, "fraud"))
    b = runner(Script(HAPPY)).run(Episode(1, "fraud"))
    assert a == b


def test_temperature_passed_when_set() -> None:
    script = Script(HAPPY)
    runner(script, temperature=0.0).run(Episode(1, "fraud"))
    assert script.bodies()[0]["temperature"] == 0.0


def test_api_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        AnthropicRunner(RunnerConfig(model=DEFAULT_MODEL))


def test_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRUPLE_ANTHROPIC_MODEL", raising=False)
    assert default_model() == "claude-sonnet-5-5"
    monkeypatch.setenv("SCRUPLE_ANTHROPIC_MODEL", "claude-haiku-5-5")
    assert default_model() == "claude-haiku-5-5"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert AnthropicRunner().config.model == "claude-haiku-5-5"
