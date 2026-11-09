"""Anthropic Messages API runner (tool use loop over plain HTTP via httpx).

Request and response shapes follow the public Messages API documentation
(https://docs.anthropic.com/en/api/messages): `tools` with `input_schema`,
assistant `tool_use` content blocks, user `tool_result` blocks, `stop_reason` and
`usage`. The model id is configurable; defaults can be overridden with the
`SCRUPLE_ANTHROPIC_MODEL` environment variable.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx

from scruple.agents.runners.base import (
    ModelTurn,
    ParsedCall,
    Runner,
    RunnerConfig,
    ToolResult,
)
from scruple.agents.tools import anthropic_tools

DEFAULT_MODEL = "claude-sonnet-5-5"
FAST_MODEL = "claude-haiku-5-5"
API_VERSION = "2023-06-01"
BASE_URL = "https://api.anthropic.com"


def default_model() -> str:
    return os.environ.get("SCRUPLE_ANTHROPIC_MODEL", DEFAULT_MODEL)


class AnthropicRunner(Runner):
    name = "anthropic"

    def __init__(
        self,
        config: RunnerConfig | None = None,
        *,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError("an Anthropic API key is required (ANTHROPIC_API_KEY)")
        super().__init__(
            config or RunnerConfig(model=default_model()),
            base_url=base_url,
            headers={"x-api-key": key, "anthropic-version": API_VERSION},
            transport=transport,
            sleep=sleep,
        )
        self._tools = anthropic_tools()

    def _initial_messages(self, observation_json: str) -> list[dict[str, Any]]:
        return [self._user_message(f"Observation:\n{observation_json}")]

    def _user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def _complete(self, messages: list[dict[str, Any]]) -> ModelTurn:
        body: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": self.config.system,
            "messages": messages,
            "tools": self._tools,
        }
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        data = self._post("/v1/messages", body)
        content = data["content"]
        if not isinstance(content, list):
            raise ValueError("response content is not a list")
        calls = [
            ParsedCall(call_id=str(b["id"]), name=str(b["name"]), args=b.get("input"))
            for b in content
            if b.get("type") == "tool_use"
        ]
        text = "".join(str(b.get("text", "")) for b in content if b.get("type") == "text")
        usage = data.get("usage") or {}
        return ModelTurn(
            assistant={"role": "assistant", "content": content},
            calls=calls,
            stop_reason=str(data.get("stop_reason")),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            text=text,
        )

    def _tool_messages(self, results: list[ToolResult]) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in results
                ],
            }
        ]
