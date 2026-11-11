"""Chat Completions style APIs: OpenAI, Mistral and OpenAI-compatible servers.

Shapes follow the OpenAI Chat Completions reference
(https://platform.openai.com/docs/api-reference/chat): `tools` of type `function`,
assistant `tool_calls` with JSON-string `arguments`, `tool` role results,
`finish_reason` and `usage`. Mistral's chat completions
(https://docs.mistral.ai/api/#tag/chat) use the same shapes with `max_tokens`,
`random_seed` and a `name` on tool messages. OpenAI-compatible servers (vLLM,
Ollama, Together, Groq and others) take `max_tokens` and usually `seed`; models
served there sometimes write tool calls into the text, which is parsed as a
fallback (the same parser as scruple.agents.runners.local).

Groq validates tool calls server side. When a model names a tool that was not in
the request (seen live with openai/gpt-oss-120b calling `commentary`, a leaked
channel name), Groq answers HTTP 400 with `error.code` `tool_use_failed` and the
model's text in `error.failed_generation`
(https://console.groq.com/docs/tool-use). That is a model mistake, not a failed
request: `rejected_turn` turns it into a text tool call, the episode answers it
with its usual unknown-tool error, and the model gets another turn.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, ClassVar

from scruple.agents.providers.base import (
    GenSettings,
    Provider,
    StopKind,
    ToolCall,
    ToolDef,
    ToolOutcome,
    Turn,
)
from scruple.agents.runners.local import extract_text_tool_calls
from scruple.agents.runners.openai import parse_arguments

OPENAI_URL = "https://api.openai.com/v1"
MISTRAL_URL = "https://api.mistral.ai/v1"

_STOPS: dict[str, StopKind] = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end",
    "length": "max_tokens",
    "model_length": "max_tokens",
    "content_filter": "refusal",
}


def short_call_id(turn: int, index: int) -> str:
    """A 9-character alphanumeric id (Mistral requires that shape)."""
    return hashlib.sha256(f"{turn}:{index}".encode()).hexdigest()[:9]


@dataclass
class ChatProvider(Provider):
    name: ClassVar[str] = "openai"
    wire: ClassVar[str] = "openai-chat"
    key_env: ClassVar[tuple[str, ...]] = ("OPENAI_API_KEY",)
    seed_field: ClassVar[str | None] = "seed"
    docs: ClassVar[str] = "https://platform.openai.com/docs/api-reference/chat"
    max_tokens_field: ClassVar[str] = "max_completion_tokens"
    tool_message_name: ClassVar[bool] = False
    parse_text_calls: ClassVar[bool] = False

    def headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return {**h, **self.extra_headers}

    def path(self, model: str) -> str:
        return "/chat/completions"

    def body(
        self,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: tuple[ToolDef, ...] | None,
        gen: GenSettings,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            self.max_tokens_field: gen.max_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.schema,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"
        if gen.temperature is not None:
            body["temperature"] = gen.temperature
        if gen.seed is not None and self.seed_field:
            body[self.seed_field] = gen.seed
        return body

    def parse(self, data: dict[str, Any], turn: int) -> Turn:
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
        if isinstance(content, list):  # some servers return content parts
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        calls: list[ToolCall] = []
        normalised: list[dict[str, Any]] = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            call_id = str(tc.get("id") or short_call_id(turn, i))
            raw = fn.get("arguments")
            calls.append(ToolCall(call_id, str(fn.get("name")), parse_arguments(raw)))
            normalised.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(fn.get("name")),
                        "arguments": raw if isinstance(raw, str) else json.dumps(raw or {}),
                    },
                }
            )
        assistant: dict[str, Any] = {"role": "assistant", "content": content or None}
        if normalised:
            assistant["tool_calls"] = normalised
        finish = str(choice.get("finish_reason"))
        stop: StopKind = _STOPS.get(finish, "end")
        if message.get("refusal") and not calls:
            stop = "refusal"
        usage = data.get("usage") or {}
        out = Turn(
            assistant=assistant,
            calls=calls,
            stop="tool_use" if calls else stop,
            text=str(message.get("refusal") or content),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
        )
        if not calls and self.parse_text_calls and stop != "refusal":
            parsed = extract_text_tool_calls(str(content))
            if parsed:
                out.calls = [ToolCall(c.call_id, c.name, c.args) for c in parsed]
                out.stop = "tool_use"
                out.text_calls = True
        return out

    def rejected_turn(self, status: int, body: str, turn: int) -> Turn | None:
        if status != 400:
            return None
        try:
            err = json.loads(body).get("error")
        except (ValueError, AttributeError):
            return None
        if not isinstance(err, dict) or err.get("code") != "tool_use_failed":
            return None
        failed = str(err.get("failed_generation") or "")
        calls: list[ToolCall] = []
        try:
            obj = json.loads(failed)
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            calls = [ToolCall(short_call_id(turn, 0), obj["name"], obj.get("arguments"))]
        else:
            calls = [ToolCall(c.call_id, c.name, c.args) for c in extract_text_tool_calls(failed)]
        return Turn(
            assistant={"role": "assistant", "content": failed or "(no output)"},
            calls=calls,
            stop="tool_use" if calls else "end",
            text=failed,
            text_calls=True,
            rejected=True,
        )

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def tool_messages(self, results: list[ToolOutcome], turn: Turn) -> list[dict[str, Any]]:
        if turn.text_calls:  # no tool call ids to answer: a plain user message
            payload = [
                {"tool": r.name, "is_error": r.is_error, "result": r.result} for r in results
            ]
            return [self.user_message("Tool results:\n" + json.dumps(payload, sort_keys=True))]
        out = []
        for r in results:
            m: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": r.call_id,
                "content": json.dumps(r.result, sort_keys=True),
            }
            if self.tool_message_name:
                m["name"] = r.name
            out.append(m)
        return out


@dataclass
class OpenAIProvider(ChatProvider):
    pass


@dataclass
class MistralProvider(ChatProvider):
    name: ClassVar[str] = "mistral"
    wire: ClassVar[str] = "mistral-chat"
    key_env: ClassVar[tuple[str, ...]] = ("MISTRAL_API_KEY",)
    seed_field: ClassVar[str | None] = "random_seed"
    docs: ClassVar[str] = "https://docs.mistral.ai/api/#tag/chat"
    max_tokens_field: ClassVar[str] = "max_tokens"
    tool_message_name: ClassVar[bool] = True


@dataclass
class CompatProvider(ChatProvider):
    """Any OpenAI-compatible server. The key is optional (local servers need none)."""

    name: ClassVar[str] = "openai-compatible"
    wire: ClassVar[str] = "openai-chat"
    key_env: ClassVar[tuple[str, ...]] = ("SCRUPLE_OPENAI_COMPAT_API_KEY",)
    key_required: ClassVar[bool] = False
    docs: ClassVar[str] = "https://platform.openai.com/docs/api-reference/chat"
    max_tokens_field: ClassVar[str] = "max_tokens"
    parse_text_calls: ClassVar[bool] = True


@dataclass
class VLLMProvider(CompatProvider):
    name: ClassVar[str] = "vllm"
    key_env: ClassVar[tuple[str, ...]] = ("SCRUPLE_VLLM_API_KEY",)
    docs: ClassVar[str] = "https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html"


@dataclass
class OllamaProvider(CompatProvider):
    name: ClassVar[str] = "ollama"
    key_env: ClassVar[tuple[str, ...]] = ()
    docs: ClassVar[str] = "https://github.com/ollama/ollama/blob/main/docs/openai.md"


@dataclass
class TogetherProvider(CompatProvider):
    name: ClassVar[str] = "together"
    key_env: ClassVar[tuple[str, ...]] = ("TOGETHER_API_KEY",)
    key_required: ClassVar[bool] = True
    docs: ClassVar[str] = "https://docs.together.ai/reference/chat-completions-1"


@dataclass
class GroqProvider(CompatProvider):
    name: ClassVar[str] = "groq"
    key_env: ClassVar[tuple[str, ...]] = ("GROQ_API_KEY",)
    key_required: ClassVar[bool] = True
    docs: ClassVar[str] = "https://console.groq.com/docs/openai"


__all__ = [
    "MISTRAL_URL",
    "OPENAI_URL",
    "ChatProvider",
    "CompatProvider",
    "GroqProvider",
    "MistralProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "TogetherProvider",
    "VLLMProvider",
    "short_call_id",
]
