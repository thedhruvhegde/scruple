"""OpenAI-compatible Chat Completions runner (function calling over httpx).

Shapes follow the public Chat Completions documentation
(https://platform.openai.com/docs/api-reference/chat): `tools` of type `function`,
assistant `tool_calls` with JSON-string `arguments`, `tool` role results,
`finish_reason` and `usage`. The Responses API is not implemented.

There is no default model: pass the model id you mean to evaluate.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import httpx

from scruple.agents.runners.base import ModelTurn, ParsedCall, Runner, RunnerConfig, ToolResult
from scruple.agents.tools import openai_tools

BASE_URL = "https://api.openai.com/v1"


def parse_arguments(raw: Any) -> Any:
    """JSON-string arguments to a dict. Anything unparsable is passed through as-is,
    so the episode refuses it with a tool error the model can see."""
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


class OpenAIRunner(Runner):
    name = "openai"

    def __init__(
        self,
        config: RunnerConfig,
        *,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        require_key: bool = True,
        max_tokens_field: str = "max_completion_tokens",
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if require_key and not key:
            raise ValueError("an OpenAI API key is required (OPENAI_API_KEY)")
        super().__init__(
            config,
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"} if key else {},
            transport=transport,
            sleep=sleep,
        )
        self._tools = openai_tools()
        self._max_tokens_field = max_tokens_field
        self._turn = 0

    def _initial_messages(self, observation_json: str) -> list[dict[str, Any]]:
        self._turn = 0
        return [
            {"role": "system", "content": self.config.system},
            self._user_message(f"Observation:\n{observation_json}"),
        ]

    def _user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def _body(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "tools": self._tools,
            self._max_tokens_field: self.config.max_tokens,
        }
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        return body

    def _complete(self, messages: list[dict[str, Any]]) -> ModelTurn:
        self._turn += 1
        data = self._post("/chat/completions", self._body(messages))
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
        calls: list[ParsedCall] = []
        normalised: list[dict[str, Any]] = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            call_id = str(tc.get("id") or f"call_{self._turn}_{i}")
            raw_args = fn.get("arguments")
            calls.append(
                ParsedCall(
                    call_id=call_id, name=str(fn.get("name")), args=parse_arguments(raw_args)
                )
            )
            normalised.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(fn.get("name")),
                        "arguments": raw_args
                        if isinstance(raw_args, str)
                        else json.dumps(raw_args or {}),
                    },
                }
            )
        assistant: dict[str, Any] = {"role": "assistant", "content": content or None}
        if normalised:
            assistant["tool_calls"] = normalised
        finish = str(choice.get("finish_reason"))
        refused = bool(message.get("refusal")) or finish == "content_filter"
        usage = data.get("usage") or {}
        return ModelTurn(
            assistant=assistant,
            calls=calls,
            stop_reason="refusal" if refused and not calls else finish,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            text=str(message.get("refusal") or content),
        )

    def _tool_messages(self, results: list[ToolResult]) -> list[dict[str, Any]]:
        return [{"role": "tool", "tool_call_id": r.call_id, "content": r.content} for r in results]
