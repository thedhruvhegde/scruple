"""Runner for local models behind an OpenAI-compatible server (Ollama, vLLM,
llama.cpp server, LM Studio and similar).

Differences from `OpenAIRunner`:

- `base_url` is configurable (default `SCRUPLE_LOCAL_BASE_URL`, else Ollama's
  OpenAI-compatible endpoint at http://localhost:11434/v1); no API key is required.
- sends `max_tokens` (the older field most local servers accept).
- tolerates common local-model quirks: missing tool call ids, arguments already
  parsed into objects, and tool calls written into the message text instead of
  `tool_calls` (a bare JSON object, a ```json fence, or `<tool_call>` tags). Calls
  parsed from text get their results back as a plain user message, since there is
  no tool call id to answer.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

import httpx

from scruple.agents.runners.base import ModelTurn, ParsedCall, RunnerConfig, ToolResult
from scruple.agents.runners.openai import OpenAIRunner, parse_arguments

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"

_TAGGED = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_FENCED = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _as_call(obj: Any, i: int) -> ParsedCall | None:
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        return None
    if "arguments" in obj:
        args = obj["arguments"]
    elif "parameters" in obj:
        args = obj["parameters"]
    else:
        args = {}
    return ParsedCall(call_id=f"text_{i}", name=obj["name"], args=parse_arguments(args))


def extract_text_tool_calls(text: str) -> list[ParsedCall]:
    """Tool calls a model wrote as text. Returns [] when there are none."""
    chunks = _TAGGED.findall(text) or _FENCED.findall(text)
    stripped = text.strip()
    if not chunks and stripped[:1] in ("{", "["):
        chunks = [stripped]
    calls: list[ParsedCall] = []
    for chunk in chunks:
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        for obj in parsed if isinstance(parsed, list) else [parsed]:
            call = _as_call(obj, len(calls))
            if call is not None:
                calls.append(call)
    return calls


class LocalRunner(OpenAIRunner):
    name = "local"

    def __init__(
        self,
        config: RunnerConfig,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        parse_text_calls: bool = True,
    ) -> None:
        super().__init__(
            config,
            api_key=api_key if api_key is not None else os.environ.get("SCRUPLE_LOCAL_API_KEY", ""),
            base_url=base_url or os.environ.get("SCRUPLE_LOCAL_BASE_URL", DEFAULT_LOCAL_URL),
            transport=transport,
            sleep=sleep,
            require_key=False,
            max_tokens_field="max_tokens",
        )
        self._parse_text_calls = parse_text_calls
        self._text_mode = False

    def _complete(self, messages: list[dict[str, Any]]) -> ModelTurn:
        turn = super()._complete(messages)
        self._text_mode = False
        if turn.calls or not self._parse_text_calls or turn.stop_reason == "refusal":
            return turn
        calls = extract_text_tool_calls(turn.text)
        if calls:
            turn.calls = calls
            self._text_mode = True
        return turn

    def _tool_messages(self, results: list[ToolResult]) -> list[dict[str, Any]]:
        if not self._text_mode:
            return super()._tool_messages(results)
        payload = [
            {"tool": r.name, "is_error": r.is_error, "result": json.loads(r.content)}
            for r in results
        ]
        return [self._user_message("Tool results:\n" + json.dumps(payload, sort_keys=True))]
