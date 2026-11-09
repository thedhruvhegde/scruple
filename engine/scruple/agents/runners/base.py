"""Shared tool-use loop for hosted and local model runners.

A runner sends the observation to a model, executes the tool calls it returns on the
episode, feeds the results back and repeats until the model calls `submit_decision`
or a limit is hit. Limits: `max_turns` model calls, a total `token_budget` (input
plus output, as reported by the API), `max_tokens` per response and an HTTP
`timeout_s`. Whatever stops the loop early lapses the episode (liability accepted
by default) and is recorded in the trace and the score flags.

Tests never make live calls: every runner takes an `httpx` transport, and the tests
pass `httpx.MockTransport` with scripted responses.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scruple.agents.episode import Episode, EpisodeError
from scruple.agents.models import TASK, EpisodeScore

Stop = Literal[
    "submitted",
    "max_turns",
    "token_budget",
    "model_stopped",
    "refusal",
    "timeout",
    "http_error",
    "bad_response",
]

NUDGE = (
    "You have not finished. Call a tool: take one action, then call submit_decision "
    "with a short explanation."
)
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


class RunnerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    max_turns: int = Field(default=12, ge=1, le=200)
    max_tokens: int = Field(default=1_024, ge=16, le=64_000)
    token_budget: int = Field(default=60_000, ge=100)
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    nudges: int = Field(default=1, ge=0, le=5)
    system: str = TASK
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class ToolCallTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn: int
    name: str
    ok: bool
    error: str | None = None


class RunTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    runner: str
    model: str
    stop: Stop
    turns: int
    input_tokens: int
    output_tokens: int
    tool_calls: tuple[ToolCallTrace, ...]
    detail: str | None = None
    score: EpisodeScore


@dataclass
class ParsedCall:
    call_id: str
    name: str
    args: Any  # a dict when well formed; anything else is passed on and refused


@dataclass
class ModelTurn:
    assistant: dict[str, Any]  # provider-format message to append to the history
    calls: list[ParsedCall]
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    text: str = ""


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool


class RunnerHTTPError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: list[ToolCallTrace] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class Runner(ABC):
    name = "runner"

    def __init__(
        self,
        config: RunnerConfig,
        *,
        base_url: str,
        headers: dict[str, str],
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._sleep = sleep or time.sleep
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(config.timeout_s),
        )

    # --- provider hooks ------------------------------------------------------------

    @abstractmethod
    def _initial_messages(self, observation_json: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def _complete(self, messages: list[dict[str, Any]]) -> ModelTurn: ...

    @abstractmethod
    def _tool_messages(self, results: list[ToolResult]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def _user_message(self, text: str) -> dict[str, Any]: ...

    # --- shared loop ---------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        while True:
            resp = self._client.post(path, json=body)
            if resp.status_code < 400:
                data: dict[str, Any] = resp.json()
                return data
            if resp.status_code in RETRY_STATUS and attempt < self.config.max_retries:
                self._sleep(2.0**attempt)
                attempt += 1
                continue
            raise RunnerHTTPError(resp.status_code, resp.text)

    def run(self, episode: Episode) -> RunTrace:
        obs = episode.observe().model_dump_json()
        messages = self._initial_messages(obs)
        usage = _Usage()
        nudges = self.config.nudges
        turn = 0
        while turn < self.config.max_turns:
            turn += 1
            try:
                mt = self._complete(messages)
            except httpx.TimeoutException as exc:
                return self._finish(episode, "timeout", turn, usage, str(exc) or "timeout")
            except (httpx.HTTPError, RunnerHTTPError) as exc:
                return self._finish(episode, "http_error", turn, usage, str(exc))
            except (ValueError, LookupError, TypeError, AttributeError) as exc:
                return self._finish(episode, "bad_response", turn, usage, repr(exc))
            usage.input_tokens += mt.input_tokens
            usage.output_tokens += mt.output_tokens
            messages.append(mt.assistant)
            if not mt.calls:
                if mt.stop_reason == "refusal":
                    return self._finish(episode, "refusal", turn, usage, mt.text or None)
                if nudges > 0 and usage.total < self.config.token_budget:
                    nudges -= 1
                    messages.append(self._user_message(NUDGE))
                    continue
                return self._finish(episode, "model_stopped", turn, usage, mt.text or None)
            results = [self._execute(episode, c, turn, usage) for c in mt.calls]
            messages.extend(self._tool_messages(results))
            if episode.closed:
                return self._finish(episode, "submitted", turn, usage)
            if usage.total >= self.config.token_budget:
                return self._finish(episode, "token_budget", turn, usage)
        return self._finish(episode, "max_turns", turn, usage)

    def _execute(self, episode: Episode, call: ParsedCall, turn: int, usage: _Usage) -> ToolResult:
        try:
            out = episode.call(call.name, call.args)
        except EpisodeError as exc:
            usage.calls.append(ToolCallTrace(turn=turn, name=call.name, ok=False, error=str(exc)))
            return ToolResult(call.call_id, call.name, json.dumps({"error": str(exc)}), True)
        usage.calls.append(ToolCallTrace(turn=turn, name=call.name, ok=True))
        return ToolResult(call.call_id, call.name, json.dumps(out, sort_keys=True), False)

    def _finish(
        self, episode: Episode, stop: Stop, turns: int, usage: _Usage, detail: str | None = None
    ) -> RunTrace:
        score = episode.score if episode.score is not None else episode.lapse(stop)
        return RunTrace(
            runner=self.name,
            model=self.config.model,
            stop=stop,
            turns=turns,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            tool_calls=tuple(usage.calls),
            detail=detail,
            score=score,
        )

    def close(self) -> None:
        self._client.close()
