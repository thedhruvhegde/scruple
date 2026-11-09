"""Local OpenAI-compatible runner against a scripted fake server. No live calls."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scruple.agents import Episode
from scruple.agents.runners import LocalRunner, RunnerConfig
from scruple.agents.runners.local import DEFAULT_LOCAL_URL, extract_text_tool_calls
from tests.test_agents_runner_anthropic import Script
from tests.test_agents_runner_openai import call, completion


def runner(script: Script, **kw: Any) -> LocalRunner:
    return LocalRunner(
        RunnerConfig(model="qwen-local"),
        transport=httpx.MockTransport(script),
        sleep=lambda _s: None,
        **kw,
    )


def test_base_url_and_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRUPLE_LOCAL_API_KEY", raising=False)
    monkeypatch.delenv("SCRUPLE_LOCAL_BASE_URL", raising=False)
    script = Script(
        [completion(call("c", "submit_decision", {"explanation": "x", "action": "refund"}))]
    )
    trace = runner(script, base_url="http://gpu-box:8000/v1/").run(Episode(1, "fraud"))
    assert trace.stop == "submitted"
    req = script.requests[0]
    assert str(req.url) == "http://gpu-box:8000/v1/chat/completions"
    assert "authorization" not in req.headers
    body = script.bodies()[0]
    assert body["max_tokens"] == 1024
    assert "max_completion_tokens" not in body


def test_default_and_env_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRUPLE_LOCAL_BASE_URL", raising=False)
    script = Script(
        [completion(call("c", "submit_decision", {"explanation": "x", "action": "refund"}))]
    )
    runner(script).run(Episode(1, "fraud"))
    assert str(script.requests[0].url) == DEFAULT_LOCAL_URL + "/chat/completions"
    monkeypatch.setenv("SCRUPLE_LOCAL_BASE_URL", "http://127.0.0.1:1234/v1")
    script2 = Script(
        [completion(call("c", "submit_decision", {"explanation": "x", "action": "refund"}))]
    )
    runner(script2).run(Episode(1, "fraud"))
    assert script2.requests[0].url.port == 1234


def test_optional_key_is_sent() -> None:
    script = Script(
        [completion(call("c", "submit_decision", {"explanation": "x", "action": "refund"}))]
    )
    runner(script, api_key="local-secret").run(Episode(1, "fraud"))
    assert script.requests[0].headers["authorization"] == "Bearer local-secret"


def test_arguments_as_object_are_accepted() -> None:
    tc = {
        "id": "c1",
        "type": "function",
        "function": {
            "name": "submit_decision",
            "arguments": {"explanation": "x", "action": "escalate"},
        },
    }
    trace = runner(Script([completion(tc)])).run(Episode(1, "fraud"))
    assert trace.stop == "submitted"


@pytest.mark.parametrize(
    "content",
    [
        '{"name": "escalate", "arguments": {"reason": "unsure"}}',
        '<tool_call>\n{"name": "escalate", "arguments": {"reason": "unsure"}}\n</tool_call>',
        'I will escalate.\n```json\n{"name": "escalate", "parameters": {"reason": "unsure"}}\n```',
        '[{"name": "escalate", "arguments": "{\\"reason\\": \\"unsure\\"}"}]',
    ],
)
def test_tool_calls_written_as_text(content: str) -> None:
    script = Script(
        [
            completion(content=content, finish="stop"),
            completion(
                content='{"name": "submit_decision", "arguments": {"explanation": "escalated"}}',
                finish="stop",
            ),
        ]
    )
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "submitted"
    assert trace.score.action is not None
    assert trace.score.action.value == "escalate"
    follow_up = script.bodies()[1]["messages"][-1]
    assert follow_up["role"] == "user"
    assert follow_up["content"].startswith("Tool results:")
    assert json.loads(follow_up["content"].split("\n", 1)[1])[0]["tool"] == "escalate"


def test_text_parsing_can_be_disabled() -> None:
    text = completion(content='{"name": "escalate", "arguments": {}}', finish="stop")
    trace = runner(Script([text, text]), parse_text_calls=False).run(Episode(1, "fraud"))
    assert trace.stop == "model_stopped"  # after one nudge
    assert trace.score.action is None


def test_mixed_turns_switch_back_to_tool_messages() -> None:
    script = Script(
        [
            completion(content='{"name": "get_case", "arguments": {}}', finish="stop"),
            completion(call("c2", "submit_decision", {"explanation": "x", "action": "refund"})),
        ]
    )
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.stop == "submitted"


def test_malformed_text_calls_are_errors_not_crashes() -> None:
    script = Script(
        [
            completion(content='{"name": "get_evidence", "arguments": "{broken"}', finish="stop"),
            completion(call("c2", "submit_decision", {"explanation": "x", "action": "refund"})),
        ]
    )
    trace = runner(script).run(Episode(1, "fraud"))
    assert trace.tool_calls[0].ok is False
    assert trace.stop == "submitted"


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("no tools here", []),
        ("{not json", []),
        ('{"tool": "x"}', []),
        ('{"name": 3}', []),
        ('{"name": "get_case"}', ["get_case"]),
        ('<tool_call>{"name": "a"}</tool_call><tool_call>{"name": "b"}</tool_call>', ["a", "b"]),
        ('[{"name": "a"}, {"oops": 1}, {"name": "b"}]', ["a", "b"]),
    ],
)
def test_extract_text_tool_calls(text: str, names: list[str]) -> None:
    calls = extract_text_tool_calls(text)
    assert [c.name for c in calls] == names
    assert [c.call_id for c in calls] == [f"text_{i}" for i in range(len(names))]
