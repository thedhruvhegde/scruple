"""Anthropic Messages API (https://docs.anthropic.com/en/api/messages).

`tools` with `input_schema`; assistant `tool_use` blocks; user `tool_result` blocks;
`stop_reason` (`tool_use`, `end_turn`, `max_tokens`, `refusal`, ...) and `usage`.
The Messages API has no sampling seed, so runs are deterministic only as far as
temperature 0 makes them.
"""

from __future__ import annotations

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

API_VERSION = "2023-06-01"
BASE_URL = "https://api.anthropic.com"

_STOPS: dict[str, StopKind] = {
    "tool_use": "tool_use",
    "end_turn": "end",
    "stop_sequence": "end",
    "pause_turn": "end",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


@dataclass
class AnthropicProvider(Provider):
    name: ClassVar[str] = "anthropic"
    wire: ClassVar[str] = "anthropic-messages"
    key_env: ClassVar[tuple[str, ...]] = ("ANTHROPIC_API_KEY",)
    docs: ClassVar[str] = "https://docs.anthropic.com/en/api/messages"

    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": API_VERSION, **self.extra_headers}

    def path(self, model: str) -> str:
        return "/v1/messages"

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
            "max_tokens": gen.max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.schema}
                for t in tools
            ]
        if gen.temperature is not None:
            body["temperature"] = gen.temperature
        return body

    def parse(self, data: dict[str, Any], turn: int) -> Turn:
        content = data["content"]
        if not isinstance(content, list):
            raise ValueError("response content is not a list")
        calls = [
            ToolCall(call_id=str(b["id"]), name=str(b["name"]), args=b.get("input"))
            for b in content
            if b.get("type") == "tool_use"
        ]
        text = "".join(str(b.get("text", "")) for b in content if b.get("type") == "text")
        usage = data.get("usage") or {}
        stop = _STOPS.get(str(data.get("stop_reason")), "end")
        if calls:
            stop = "tool_use"
        return Turn(
            assistant={"role": "assistant", "content": content},
            calls=calls,
            stop=stop,
            text=text,
            input_tokens=int(usage.get("input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0)),
        )

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def tool_messages(self, results: list[ToolOutcome], turn: Turn) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": json.dumps(r.result, sort_keys=True),
                        "is_error": r.is_error,
                    }
                    for r in results
                ],
            }
        ]


__all__ = ["API_VERSION", "BASE_URL", "AnthropicProvider"]
