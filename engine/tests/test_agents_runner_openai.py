"""OpenAI Chat Completions runner against a scripted fake transport. No live calls."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scruple.agents import Episode
from scruple.agents.runners import OpenAIRunner, RunnerConfig
from scruple.agents.runners.openai import parse_arguments
from tests.test_agents_runner_anthropic import Reply, Script

MODEL = "test-model"


def call(call_id: str | None, name: str, args: Any) -> dict[str, Any]:
    tc: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args),
        },
    }
    if call_id is not None:
        tc["id"] = call_id
    return tc


def completion(
    *calls: dict[str, Any],
    content: str | None = None,
    finish: str = "tool_calls",
    refusal: str | None = None,
    tokens: int = 10,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    if refusal is not None:
        message["refusal"] = refusal
    return {
        "id": "chatcmpl",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": tokens, "completion_tokens": tokens},
    }


def runner(script: Script, **cfg: Any) -> OpenAIRunner:
    return OpenAIRunner(
        RunnerConfig(model=MODEL, **cfg),
        api_key="sk-test",
        transport=httpx.MockTransport(script),
        sleep=lambda _s: None,
    )


HAPPY: list[Reply] = [
    completion(call("c1", "get_case", {}), call("c2", "list_evidence", {})),
    completion(call("c3", "escalate", {"reason": "high value"})),
    completion(call("c4", "submit_decision", {"explanation": "over threshold, escalated"})),
]


def test_happy_path() -> None:
    script = Script(HAPPY)
    trace = runner(script).run(Episode(2, "high_value"))
    assert trace.stop == "submitted"
    assert trace.runner == "openai"
    assert trace.score.outcome == "escalated"
    assert trace.score.passed
    req = script.requests[0]
    assert req.url.path == "/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = script.bodies()[0]
    assert body["model"] == MODEL
    assert body["max_completion_tokens"] == 1024
    assert body["messages"][0]["role"] == "system"
    assert body["tools"][0]["type"] == "function"


def test_tool_messages_follow_assistant_tool_calls() -> None:
    script = Script(HAPPY)
    runner(script).run(Episode(2, "high_value"))
    msgs = script.bodies()[1]["messages"]
    assert msgs[2]["role"] == "assistant"
    assert [tc["id"] for tc in msgs[2]["tool_calls"]] == ["c1", "c2"]
    assert [m["tool_call_id"] for m in msgs[3:5]] == ["c1", "c2"]
    assert all(m["role"] == "tool" for m in msgs[3:5])


@pytest.mark.parametrize(
    "args",
    ['{"evidence_id": ', "[1, 2]", '"ev-01"', '{"evidence_id": 3}', '{"bogus": 1}'],
)
def test_malformed_arguments(args: str) -> None:
    script = Script(
        [
            completion(call("c1", "get_evidence", args)),
            completion(call("c2", "submit_decision", {"explanation": "x", "action": "refund"})),
        ]
    )
    trace = runner(script).run(Episode(2, "fraud"))
    assert trace.stop == "submitted"
    assert trace.tool_calls[0].ok is False
    tool_msg = script.bodies()[1]["messages"][-1]
    assert "error" in json.loads(tool_msg["content"])
    assert trace.score.tool_errors == 1


def test_missing_call_ids_are_synthesised() -> None:
    script = Script(
        [completion(call(None, "submit_decision", {"explanation": "x", "action": "refund"}))]
    )
    trace = runner(script).run(Episode(2, "fraud"))
    assert trace.stop == "submitted"


def test_max_turns() -> None:
    script = Script([completion(call(f"c{i}", "get_ledger_view", {})) for i in range(4)])
    trace = runner(script, max_turns=4).run(Episode(2, "fraud"))
    assert trace.stop == "max_turns"
    assert trace.score.outcome == "lapsed"


def test_token_budget() -> None:
    script = Script([completion(call("c1", "get_case", {}), tokens=5_000)])
    trace = runner(script, token_budget=1_000).run(Episode(2, "fraud"))
    assert trace.stop == "token_budget"


@pytest.mark.parametrize(
    "reply",
    [
        completion(refusal="I can't assist with that.", finish="stop"),
        completion(content="", finish="content_filter"),
    ],
)
def test_refusal(reply: dict[str, Any]) -> None:
    trace = runner(Script([reply])).run(Episode(2, "fraud"))
    assert trace.stop == "refusal"
    assert "lapsed:refusal" in trace.score.flags


def test_stop_without_tools_nudges_then_stops() -> None:
    script = Script(
        [completion(content="Accept it.", finish="stop"), completion(content="ok", finish="stop")]
    )
    trace = runner(script).run(Episode(2, "fraud"))
    assert trace.stop == "model_stopped"
    assert script.bodies()[1]["messages"][-1]["role"] == "user"


def test_timeout_and_http_errors() -> None:
    assert runner(Script([httpx.ConnectTimeout("x")])).run(Episode(2, "fraud")).stop == "timeout"
    assert (
        runner(Script([httpx.Response(401, json={"error": "bad key"})]))
        .run(Episode(2, "fraud"))
        .stop
        == "http_error"
    )
    assert (
        runner(Script([httpx.ConnectError("refused")])).run(Episode(2, "fraud")).stop
        == "http_error"
    )


def test_bad_response() -> None:
    assert runner(Script([{"choices": []}])).run(Episode(2, "fraud")).stop == "bad_response"


def test_deterministic() -> None:
    assert runner(Script(HAPPY)).run(Episode(2, "high_value")) == runner(Script(HAPPY)).run(
        Episode(2, "high_value")
    )


def test_api_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        OpenAIRunner(RunnerConfig(model=MODEL))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ({"a": 1}, {"a": 1}),
        ("", {}),
        (None, {}),
        ("{oops", "{oops"),
        ("[1]", [1]),
        (5, 5),
    ],
)
def test_parse_arguments(raw: Any, expected: Any) -> None:
    assert parse_arguments(raw) == expected
