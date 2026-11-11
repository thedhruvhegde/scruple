"""Regression tests on response shapes recorded from live APIs (bodies only, no headers).

The fixtures in tests/fixtures/providers_live/ were recorded in the E5 smoke test
(docs/experiments/E5_open_models.md): Groq (openai/gpt-oss-120b, qwen/qwen3.8-27b)
and OpenRouter (nvidia/nemotron-3-ultra-550b-a55b:free). The 400 body is Groq's
`tool_use_failed`, which ended a live episode as `http_error` before the fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.providers import ProviderRunner, RunSettings, make_provider
from scruple.agents.strategies import get_strategy

FIX = Path(__file__).parent / "fixtures" / "providers_live"


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


@pytest.mark.parametrize(
    ("name", "calls"),
    [
        ("groq_gpt_oss_120b_tool_call.json", ["get_case"]),
        ("groq_qwen3_8_27b_parallel_calls.json", ["get_case", "list_evidence", "get_ledger_view"]),
        ("openrouter_nemotron_3_ultra_tool_call.json", ["list_evidence"]),
    ],
)
def test_recorded_tool_calls_parse(name: str, calls: list[str]) -> None:
    data = load(name)
    turn = make_provider("groq", api_key="k").parse(data, 1)
    assert [c.name for c in turn.calls] == calls
    assert all(c.args == {} for c in turn.calls)
    assert turn.stop == "tool_use" and not turn.rejected
    assert turn.input_tokens == data["usage"]["prompt_tokens"] > 0
    # The reasoning fields are not echoed back into the history.
    assert set(turn.assistant) == {"role", "content", "tool_calls"}
    ids = [tc["id"] for tc in turn.assistant["tool_calls"]]
    assert ids == [tc["id"] for tc in data["choices"][0]["message"]["tool_calls"]]


def _body() -> str:
    return json.dumps(load("groq_tool_use_failed_400.json")["body"])


def test_tool_use_failed_becomes_a_rejected_turn() -> None:
    turn = make_provider("groq", api_key="k").rejected_turn(400, _body(), 3)
    assert turn is not None and turn.rejected and turn.text_calls
    assert [c.name for c in turn.calls] == ["commentary"]
    assert turn.calls[0].args["explanation"] == "Represented with the receipt."
    assert turn.assistant["role"] == "assistant" and "commentary" in turn.assistant["content"]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, '{"error": {"code": "invalid_api_key"}}'),
        (400, "not json"),
        (400, '["a list"]'),
        (500, None),
    ],
)
def test_other_errors_are_not_rejected_turns(status: int, body: str | None) -> None:
    provider = make_provider("groq", api_key="k")
    assert provider.rejected_turn(status, body or _body(), 1) is None


def test_unparseable_failed_generation_gets_a_nudge() -> None:
    body = json.dumps({"error": {"code": "tool_use_failed", "failed_generation": "I think"}})
    turn = make_provider("groq", api_key="k").rejected_turn(400, body, 1)
    assert turn is not None and turn.calls == [] and turn.stop == "end"


def test_anthropic_never_rejects() -> None:
    assert make_provider("anthropic", api_key="k").rejected_turn(400, _body(), 1) is None


def test_episode_survives_a_rejected_generation() -> None:
    """Before the fix the 400 ended the episode as http_error; now the model is told
    the tool does not exist and goes on to submit."""
    sent: list[dict[str, Any]] = []
    replies: list[httpx.Response] = [
        httpx.Response(400, text=_body()),
        httpx.Response(200, json=load("groq_gpt_oss_120b_tool_call.json")),
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "fc_1",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_decision",
                                        "arguments": '{"explanation": "done"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            },
        ),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return replies.pop(0)

    runner = ProviderRunner(
        make_provider("groq", api_key="k"),
        "openai/gpt-oss-120b",
        get_strategy("zero-shot").api_plan(),
        settings=RunSettings(max_retries=0),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    run = runner.run(DisputeEpisode(5001))
    assert run.stop == "submitted"
    assert run.rejected_turns == 1
    assert [(c.tool, c.ok) for c in run.calls] == [
        ("commentary", False),
        ("get_case", True),
        ("submit_decision", True),
    ]
    feedback = sent[1]["messages"][-1]
    assert feedback["role"] == "user" and "unknown tool" in feedback["content"]
    assert sent[1]["messages"][-2] == {
        "role": "assistant",
        "content": load("groq_tool_use_failed_400.json")["body"]["error"]["failed_generation"],
    }


def _parse_failed() -> str:
    return json.dumps(load("groq_output_parse_failed_400.json")["body"])


def test_output_parse_failed_is_a_rejected_turn_with_nothing_to_keep() -> None:
    turn = make_provider("groq", api_key="k").rejected_turn(400, _parse_failed(), 2)
    assert turn is not None and turn.rejected
    assert turn.calls == [] and turn.stop == "end" and turn.assistant == {}


def test_episode_is_nudged_after_an_unparseable_generation() -> None:
    sent: list[dict[str, Any]] = []
    submit = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "fc_2",
                            "type": "function",
                            "function": {
                                "name": "submit_decision",
                                "arguments": '{"explanation": "done"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }
    replies = [httpx.Response(400, text=_parse_failed()), httpx.Response(200, json=submit)]

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return replies.pop(0)

    runner = ProviderRunner(
        make_provider("groq", api_key="k"),
        "openai/gpt-oss-20b",
        get_strategy("zero-shot").api_plan(),
        settings=RunSettings(max_retries=0),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    run = runner.run(DisputeEpisode(5001))
    assert run.stop == "submitted" and run.rejected_turns == 1
    history = sent[1]["messages"]
    assert [m["role"] for m in history] == ["system", "user", "user"]
    assert history[-1]["content"] == get_strategy("zero-shot").api_plan().loop.nudge_prompt
