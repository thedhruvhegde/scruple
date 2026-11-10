"""Provider wire formats against fake transports: requests, responses, translation.

No network: every request goes to an `httpx.MockTransport`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.providers import (
    PROVIDERS,
    FakeModel,
    GenSettings,
    ProviderRunner,
    ToolOutcome,
    Turn,
    dispute_tool_defs,
    make_provider,
    provider_names,
)
from scruple.agents.providers.anthropic import API_VERSION, AnthropicProvider
from scruple.agents.providers.chat import (
    CompatProvider,
    MistralProvider,
    OpenAIProvider,
    short_call_id,
)
from scruple.agents.providers.fake import history
from scruple.agents.providers.gemini import (
    GeminiProvider,
    function_declarations,
    to_gemini_schema,
)
from scruple.agents.providers.registry import COMPAT_URL_ENV
from scruple.agents.strategies import get_strategy

TOOLS = dispute_tool_defs()
GEN = GenSettings(max_tokens=100, temperature=0.0, seed=7)
ALL = list(provider_names())
KEY_ENVS = sorted({v for cls, _ in PROVIDERS.values() for v in cls.key_env} | {COMPAT_URL_ENV})


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in KEY_ENVS:
        monkeypatch.delenv(var, raising=False)


def provider(name: str) -> Any:
    url = "http://compat.test/v1" if name == "openai-compatible" else None
    return make_provider(name, api_key="test-key", base_url=url)


def play(name: str, seed: int = 5001, strategy: str = "zero-shot", **fake: Any) -> Any:
    p = provider(name)
    fm = FakeModel(p.wire, **fake)
    r = ProviderRunner(
        p, "m-1", get_strategy(strategy).api_plan(), transport=fm.transport(), sleep=lambda _s: None
    )
    return r.run(DisputeEpisode(seed)), fm


# --- registry and keys -----------------------------------------------------------------


def test_registry_covers_the_required_providers() -> None:
    assert set(ALL) >= {"anthropic", "openai", "gemini", "mistral", "openai-compatible"}
    assert {"vllm", "ollama", "together", "groq"} <= set(ALL)
    for name in ALL:
        info = provider(name).info()
        assert info.name == name
        assert info.docs.startswith("https://")


@pytest.mark.parametrize(
    ("name", "env"),
    [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
        ("mistral", "MISTRAL_API_KEY"),
        ("together", "TOGETHER_API_KEY"),
        ("groq", "GROQ_API_KEY"),
    ],
)
def test_keys_come_from_env(name: str, env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=env.split("_")[0]):
        make_provider(name)
    monkeypatch.setenv(env, "from-env")
    assert make_provider(name).api_key == "from-env"


@pytest.mark.parametrize("name", ["openai-compatible", "vllm", "ollama"])
def test_local_servers_need_no_key(name: str) -> None:
    p = make_provider(name, base_url="http://localhost:1/v1")
    assert p.api_key == ""
    assert "Authorization" not in p.headers()


def test_compat_needs_a_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match=COMPAT_URL_ENV):
        make_provider("openai-compatible")
    monkeypatch.setenv(COMPAT_URL_ENV, "http://env.test/v1")
    assert make_provider("openai-compatible").base_url == "http://env.test/v1"


def test_unknown_provider() -> None:
    with pytest.raises(ValueError, match="choose from"):
        make_provider("nope")


# --- end to end per provider, through the fake model ----------------------------------


@pytest.mark.parametrize("name", ALL)
def test_each_provider_plays_an_episode_end_to_end(name: str) -> None:
    run, fm = play(name)
    assert run.stop == "submitted"
    assert run.provider == name
    assert run.score["scorecard"]["gate"] is True
    assert run.input_tokens > 0
    assert run.output_tokens == 20 * run.turns
    assert run.requests == run.turns == len(fm.requests)
    names = [c.tool for c in run.calls]
    assert names[:2] == ["get_case", "list_evidence"]
    assert names[-1] == "submit_decision"
    assert all(c.ok for c in run.calls)


@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize("strategy", ["plan-then-act", "self-critique"])
def test_loop_features_work_on_every_provider(name: str, strategy: str) -> None:
    run, fm = play(name, strategy=strategy)
    assert run.stop == "submitted"
    if strategy == "plan-then-act":
        assert "tools" not in fm.requests[0]  # the planning turn has tools disabled
        assert all("tools" in b for b in fm.requests[1:])
    else:
        assert run.held_submits == 1
        assert [c.tool for c in run.calls].count("submit_decision") == 1


def test_anthropic_request_shape() -> None:
    _, fm = play("anthropic")
    body = fm.requests[0]
    assert fm.paths[0] == "/v1/messages"
    assert body["model"] == "m-1"
    assert body["system"].startswith("You are an AI agent")
    assert body["temperature"] == 0.0
    assert "seed" not in body  # the Messages API has no seed
    assert {t["name"] for t in body["tools"]} == {t.name for t in TOOLS}
    assert all("input_schema" in t for t in body["tools"])
    last = fm.requests[-1]["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"


def test_anthropic_headers() -> None:
    h = AnthropicProvider(base_url="u", api_key="k").headers()
    assert h == {"x-api-key": "k", "anthropic-version": API_VERSION}


@pytest.mark.parametrize(
    ("name", "max_field", "seed_field"),
    [
        ("openai", "max_completion_tokens", "seed"),
        ("mistral", "max_tokens", "random_seed"),
        ("openai-compatible", "max_tokens", "seed"),
        ("groq", "max_tokens", "seed"),
    ],
)
def test_chat_request_shape(name: str, max_field: str, seed_field: str) -> None:
    _run, fm = play(name, seed=5003)
    body = fm.requests[0]
    assert fm.paths[0].endswith("/chat/completions")
    assert body["messages"][0]["role"] == "system"
    assert body[max_field] == 2048
    assert body[seed_field] == 5003  # deterministic seed = episode seed
    assert body["tool_choice"] == "auto"
    tool_msgs = [m for m in fm.requests[-1]["messages"] if m["role"] == "tool"]
    assert tool_msgs
    assert all(("name" in m) == (name == "mistral") for m in tool_msgs)


def test_gemini_request_shape() -> None:
    _run, fm = play("gemini", seed=5002)
    body = fm.requests[0]
    assert fm.paths[0] == "/v1beta/models/m-1:generateContent"
    assert body["systemInstruction"]["parts"][0]["text"].startswith("You are an AI agent")
    assert body["generationConfig"] == {"maxOutputTokens": 2048, "temperature": 0.0, "seed": 5002}
    decls = body["tools"][0]["functionDeclarations"]
    assert {d["name"] for d in decls} == {t.name for t in TOOLS}
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    # model turns are echoed verbatim (thought signatures kept)
    model_turns = [c for c in fm.requests[-1]["contents"] if c["role"] == "model"]
    assert all("thoughtSignature" in p for c in model_turns for p in c["parts"])
    responses = [
        p["functionResponse"]
        for c in fm.requests[-1]["contents"]
        for p in c.get("parts", [])
        if "functionResponse" in p
    ]
    assert responses
    assert all("result" in r["response"] or "error" in r["response"] for r in responses)


def test_gemini_path_accepts_full_model_names() -> None:
    p = GeminiProvider(base_url="u", api_key="k")
    assert p.path("models/x") == "/v1beta/models/x:generateContent"
    assert p.path("tunedModels/y") == "/v1beta/tunedModels/y:generateContent"
    assert p.headers() == {"x-goog-api-key": "k"}


def test_text_tool_calls_fallback_on_compat() -> None:
    run, fm = play("ollama", text_calls=True)
    assert run.stop == "submitted"
    users = [m for m in fm.requests[-1]["messages"] if m["role"] == "user"]
    assert users[-1]["content"].startswith("Tool results:")


def test_openai_does_not_parse_text_calls() -> None:
    run, _ = play("openai", text_calls=True)
    assert run.stop == "model_stopped"  # the strict API path does not guess calls from text
    assert run.calls == ()


# --- Gemini schema translation ---------------------------------------------------------


_ALLOWED = {
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "properties",
    "required",
    "items",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "maxProperties",
    "minProperties",
}


def _walk(schema: dict[str, Any]) -> None:
    assert set(schema) <= _ALLOWED, set(schema) - _ALLOWED
    assert schema.get("type", "STRING").isupper()
    if "format" in schema:
        assert schema["format"] in ("enum", "date-time")
    for sub in schema.get("properties", {}).values():
        _walk(sub)
    if "items" in schema:
        _walk(schema["items"])


def test_every_dispute_tool_translates_to_the_gemini_subset() -> None:
    decls = function_declarations(TOOLS)
    for d in decls:
        if "parameters" in d:
            _walk(d["parameters"])
    no_args = {d["name"] for d in decls if "parameters" not in d}
    assert no_args == {"get_case", "list_evidence", "get_ledger_view"}


def test_gemini_schema_details() -> None:
    by = {d["name"]: d for d in function_declarations(TOOLS)}
    claims = by["submit_decision"]["parameters"]["properties"]["claims"]["properties"]
    assert claims["refunded_minor"]["type"] == "INTEGER"
    assert claims["refunded_minor"]["nullable"] is True
    assert "Total money returned" in claims["refunded_minor"]["description"]
    assert by["refund"]["parameters"]["properties"]["amount_minor"]["minimum"] == 1
    issued = by["upload_evidence"]["parameters"]["properties"]["issued_on"]
    assert "format" not in issued
    assert "(date)" in issued["description"]
    conf = by["submit_decision"]["parameters"]["properties"]["confidence"]
    assert conf == {"type": "NUMBER", "minimum": 0, "maximum": 1, "nullable": True}


def test_gemini_schema_keeps_supported_formats() -> None:
    out = to_gemini_schema({"type": "string", "format": "date-time"})
    assert out == {"type": "STRING", "format": "date-time"}
    assert to_gemini_schema({"type": "string", "format": "uri"}) == {
        "type": "STRING",
        "description": "(uri)",
    }


# --- response parsing edge cases --------------------------------------------------------


def test_anthropic_parse_stops_and_cache_tokens() -> None:
    p = AnthropicProvider(base_url="u", api_key="k")
    t = p.parse(
        {
            "content": [{"type": "text", "text": "no"}],
            "stop_reason": "refusal",
            "usage": {"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 5},
        },
        1,
    )
    assert (t.stop, t.text, t.input_tokens, t.output_tokens) == ("refusal", "no", 8, 2)
    assert p.parse({"content": [], "stop_reason": "max_tokens"}, 1).stop == "max_tokens"
    assert p.parse({"content": [], "stop_reason": "weird"}, 1).stop == "end"
    with pytest.raises(ValueError, match="not a list"):
        p.parse({"content": "x"}, 1)


def test_chat_parse_edge_cases() -> None:
    p = OpenAIProvider(base_url="u", api_key="k")
    t = p.parse(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"function": {"name": "get_case", "arguments": ""}},
                            {"id": "x", "function": {"name": "wait", "arguments": {"days": 2}}},
                            {"id": "y", "function": {"name": "wait", "arguments": "{bad"}},
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        3,
    )
    assert [c.args for c in t.calls] == [{}, {"days": 2}, "{bad"]
    assert t.calls[0].call_id == short_call_id(3, 0)
    assert len(t.calls[0].call_id) == 9
    assert t.assistant["tool_calls"][1]["function"]["arguments"] == '{"days": 2}'
    refusal = p.parse({"choices": [{"message": {"refusal": "no"}, "finish_reason": "stop"}]}, 1)
    assert (refusal.stop, refusal.text) == ("refusal", "no")
    filtered = p.parse(
        {"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]}, 1
    )
    assert filtered.stop == "refusal"
    length = p.parse({"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}, 1)
    assert length.stop == "max_tokens"
    parts = p.parse(
        {
            "choices": [
                {"message": {"content": [{"type": "text", "text": "hi"}]}, "finish_reason": "stop"}
            ]
        },
        1,
    )
    assert parts.text == "hi"


def test_compat_parses_tagged_text_calls() -> None:
    p = CompatProvider(base_url="u")
    text = '<tool_call>{"name": "get_case", "arguments": {}}</tool_call>'
    t = p.parse({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}, 1)
    assert t.text_calls and t.stop == "tool_use" and t.calls[0].name == "get_case"


def test_gemini_parse_edge_cases() -> None:
    p = GeminiProvider(base_url="u", api_key="k")
    blocked = p.parse({"promptFeedback": {"blockReason": "SAFETY"}}, 1)
    assert blocked.stop == "refusal"
    assert "SAFETY" in blocked.text
    with pytest.raises(ValueError, match="no candidates"):
        p.parse({"candidates": []}, 1)
    safety = p.parse(
        {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "SAFETY"}]}, 1
    )
    assert safety.stop == "refusal"
    thought = p.parse(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "secret", "thought": True}, {"text": "visible"}]
                    },
                    "finishReason": "MAX_TOKENS",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 4,
                "candidatesTokenCount": 2,
                "thoughtsTokenCount": 3,
            },
        },
        1,
    )
    assert (thought.text, thought.stop, thought.output_tokens) == ("visible", "max_tokens", 5)
    with_id = p.parse(
        {
            "candidates": [
                {"content": {"parts": [{"functionCall": {"id": "fc1", "name": "get_case"}}]}}
            ]
        },
        2,
    )
    assert with_id.calls[0].call_id == "fc1" and with_id.calls[0].args == {}
    msgs = p.tool_messages([ToolOutcome("fc1", "get_case", {"error": "no"}, True)], with_id)
    fr = msgs[0]["parts"][0]["functionResponse"]
    assert fr == {"name": "get_case", "response": {"error": "no"}, "id": "fc1"}
    generated = p.parse(
        {"candidates": [{"content": {"parts": [{"functionCall": {"name": "a"}}]}}]}, 4
    )
    out = p.tool_messages(
        [ToolOutcome(generated.calls[0].call_id, "a", {"x": 1}, False)], generated
    )
    assert "id" not in out[0]["parts"][0]["functionResponse"]


def test_mistral_tool_messages_carry_names() -> None:
    p = MistralProvider(base_url="u", api_key="k")
    turn = Turn(assistant={}, calls=[], stop="tool_use")
    msgs = p.tool_messages([ToolOutcome("abc123def", "wait", {"ok": True}, False)], turn)
    assert msgs == [
        {"role": "tool", "tool_call_id": "abc123def", "content": '{"ok": true}', "name": "wait"}
    ]


@given(st.integers(min_value=0, max_value=10_000), st.integers(min_value=0, max_value=50))
def test_short_call_ids_are_mistral_shaped(turn: int, index: int) -> None:
    cid = short_call_id(turn, index)
    assert len(cid) == 9 and cid.isalnum()


@pytest.mark.parametrize("wire", ["anthropic-messages", "openai-chat", "gemini-generate-content"])
def test_fake_history_reads_back_what_providers_write(wire: str) -> None:
    p = {
        "anthropic-messages": AnthropicProvider(base_url="u", api_key="k"),
        "openai-chat": OpenAIProvider(base_url="u", api_key="k"),
        "gemini-generate-content": GeminiProvider(base_url="u", api_key="k"),
    }[wire]
    fm = FakeModel(wire)
    body = p.body("m", "s", [p.user_message("hi")], TOOLS, GEN)
    reply = fm(httpx.Request("POST", "http://x/y", content=json.dumps(body).encode()))
    turn = p.parse(reply.json(), 1)
    assert [c.name for c in turn.calls] == ["get_case"]
    results = [ToolOutcome(turn.calls[0].call_id, "get_case", {"case": {"a": 1}}, False)]
    msgs = [p.user_message("hi"), turn.assistant, *p.tool_messages(results, turn)]
    assert history(wire, p.body("m", "s", msgs, TOOLS, GEN)) == [("get_case", {"case": {"a": 1}})]


def test_fake_rejects_unknown_wire() -> None:
    with pytest.raises(ValueError, match="unknown wire"):
        FakeModel("smoke-signals")


def test_fake_replies_do_not_depend_on_other_conversations() -> None:
    """Parallel batch runs share one FakeModel; its replies must be per conversation."""
    p = AnthropicProvider(base_url="u", api_key="k")
    body = json.dumps(p.body("m", "s", [p.user_message("hi")], TOOLS, GEN)).encode()
    fresh = FakeModel("anthropic-messages")
    busy = FakeModel("anthropic-messages")
    for _ in range(5):  # other conversations went first
        busy(httpx.Request("POST", "http://x/y", content=body))
    a = fresh(httpx.Request("POST", "http://x/y", content=body)).json()
    b = busy(httpx.Request("POST", "http://x/y", content=body)).json()
    assert a == b
