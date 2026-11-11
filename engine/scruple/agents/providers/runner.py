"""One episode loop for every provider: `ProviderRunner.run(DisputeEpisode)`.

The model sees the strategy's system prompt, the episode's observation (the
whitelisted case view and tool names, never ground truth) and the tool results the
episode returns. The loop:

1. Optional planning turn (`LoopConfig.planning_turn`): tools disabled, the model
   writes a plan; then it is told to execute it.
2. Each turn: one request, the tool calls in it run in order on the episode.
3. Optional self-review (`LoopConfig.critique_holds`): the first `submit_decision`
   is answered with the critique prompt instead of being executed; the episode
   never sees the held call.
4. A generation the API refused (`Provider.rejected_turn`, e.g. Groq's
   `tool_use_failed`) is a model turn: its calls are answered like any other
   (an unknown tool gets the episode's error), and it is counted.
5. A turn with no tool calls gets a nudge (`nudges`), then the run stops.
6. Stops: `submitted`, `max_turns`, `token_budget`, `model_stopped`, `refusal`,
   `max_tokens`, `http_error`, `timeout`, `bad_response`. Anything but `submitted`
   lapses the episode (the scorecard flags it).

The result of `submit_decision` (the score, which names the case family) is never
written into the conversation: the model gets `{"submitted": true}`, and nothing is
sent after it anyway.

Token and cost accounting: tokens as each API reports them; cost only when the
caller supplies a `Price` (no prices are built in).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.errors import EpisodeError
from scruple.agents.providers.base import (
    GenSettings,
    Provider,
    ProviderError,
    ProviderHTTPError,
    ToolDef,
    ToolOutcome,
    Turn,
    dispute_tool_defs,
)
from scruple.agents.providers.http import ProviderClient, RateLimiter
from scruple.agents.strategies.base import ApiPlan

RunStop = Literal[
    "submitted",
    "max_turns",
    "token_budget",
    "model_stopped",
    "refusal",
    "max_tokens",
    "http_error",
    "timeout",
    "bad_response",
]
TRANSIENT_STOPS: frozenset[str] = frozenset({"http_error", "timeout"})


class Price(BaseModel):
    """USD per million tokens. Supplied by the caller; scruple ships no price list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6, 6
        )


class RunSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_s: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=4, ge=0, le=20)
    backoff_s: float = Field(default=1.0, ge=0)
    seed_from_episode: bool = True  # send the episode seed where the API takes one


class CallLog(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn: int
    tool: str
    args: Any
    ok: bool
    error: str | None = None


class EpisodeRun(BaseModel):
    """Everything about one model run on one episode, ground truth excluded until
    `score` (computed after the episode closed)."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    strategy: str
    seed: int
    episode_id: str
    stop: RunStop
    turns: int
    held_submits: int
    rejected_turns: int = 0  # generations the API refused (counted, then answered)
    input_tokens: int
    output_tokens: int
    requests: int
    retries: int
    cost_usd: float | None
    calls: tuple[CallLog, ...]
    detail: str | None = None
    score: dict[str, Any]
    synthetic: bool = True

    def replay_calls(self) -> list[tuple[str, Any]]:
        """The calls the episode saw, in order (held submits excluded)."""
        return [(c.tool, c.args) for c in self.calls]


def observation_message(episode: DisputeEpisode) -> str:
    return "Observation:\n" + episode.observe().model_dump_json()


class ProviderRunner:
    def __init__(
        self,
        provider: Provider,
        model: str,
        plan: ApiPlan,
        *,
        settings: RunSettings | None = None,
        transport: httpx.BaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        price: Price | None = None,
        tools: tuple[ToolDef, ...] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.plan = plan
        self.settings = settings or RunSettings()
        self.price = price
        self.tools = tools if tools is not None else dispute_tool_defs()
        self.client = ProviderClient(
            base_url=provider.base_url,
            headers=provider.headers(),
            transport=transport,
            timeout_s=self.settings.timeout_s,
            max_retries=self.settings.max_retries,
            backoff_s=self.settings.backoff_s,
            limiter=limiter,
            sleep=sleep,
        )

    # --- one request ------------------------------------------------------------------

    def _complete(
        self, messages: list[dict[str, Any]], tools: bool, seed: int | None, turn: int
    ) -> Turn:
        loop = self.plan.loop
        gen = GenSettings(max_tokens=loop.max_tokens, temperature=loop.temperature, seed=seed)
        body = self.provider.body(
            self.model, self.plan.system, messages, self.tools if tools else None, gen
        )
        try:
            data = self.client.post(self.provider.path(self.model), body)
        except ProviderHTTPError as exc:
            rejected = self.provider.rejected_turn(exc.status, exc.body, turn)
            if rejected is None:
                raise
            return rejected
        return self.provider.parse(data, turn)

    # --- the loop ---------------------------------------------------------------------

    def run(self, episode: DisputeEpisode) -> EpisodeRun:
        loop = self.plan.loop
        seed = episode.seed if self.settings.seed_from_episode else None
        p = self.provider
        state = _State(holds_left=loop.critique_holds, nudges_left=loop.nudges)
        first = observation_message(episode)
        if loop.planning_turn:
            first += "\n\n" + loop.plan_prompt
        messages = [p.user_message(first)]

        def finish(stop: RunStop, detail: str | None = None) -> EpisodeRun:
            return self._finish(episode, state, stop, detail)

        planning = loop.planning_turn
        while state.turns < loop.max_turns:
            state.turns += 1
            try:
                mt = self._complete(messages, not planning, seed, state.turns)
            except httpx.TimeoutException as exc:
                return finish("timeout", str(exc) or "timeout")
            except (httpx.HTTPError, ProviderError) as exc:
                return finish("http_error", str(exc))
            except (ValueError, LookupError, TypeError, AttributeError) as exc:
                return finish("bad_response", repr(exc))
            state.rejected += mt.rejected
            state.input_tokens += mt.input_tokens
            state.output_tokens += mt.output_tokens
            if mt.assistant:  # empty when the API refused a generation it could not parse
                messages.append(mt.assistant)
            if planning:
                planning = False
                if mt.stop == "refusal":
                    return finish("refusal", mt.text or None)
                messages.append(p.user_message(loop.execute_prompt))
                continue
            if not mt.calls:
                if mt.stop == "refusal":
                    return finish("refusal", mt.text or None)
                if state.nudges_left > 0 and state.tokens < loop.token_budget:
                    state.nudges_left -= 1
                    messages.append(p.user_message(loop.nudge_prompt))
                    continue
                return finish("max_tokens" if mt.stop == "max_tokens" else "model_stopped")
            results = [self._execute(episode, c.call_id, c.name, c.args, state) for c in mt.calls]
            messages.extend(p.tool_messages(results, mt))
            if episode.closed:
                return finish("submitted")
            if state.tokens >= loop.token_budget:
                return finish("token_budget")
        return finish("max_turns")

    def _execute(
        self, episode: DisputeEpisode, call_id: str, name: str, args: Any, state: _State
    ) -> ToolOutcome:
        if name == "submit_decision" and state.holds_left > 0 and not episode.closed:
            state.holds_left -= 1
            state.held += 1
            msg = {"held": True, "message": self.plan.loop.critique_prompt}
            return ToolOutcome(call_id, name, msg, False)
        logged = args if isinstance(args, dict | str | int | float | bool | None) else repr(args)
        try:
            out = episode.call(name, args)
        except EpisodeError as exc:
            state.calls.append(
                CallLog(turn=state.turns, tool=name, args=logged, ok=False, error=str(exc))
            )
            return ToolOutcome(call_id, name, {"error": str(exc)}, True)
        state.calls.append(CallLog(turn=state.turns, tool=name, args=logged, ok=True))
        if name == "submit_decision":
            out = {"submitted": True}  # the score names the family: never shown
        return ToolOutcome(call_id, name, json.loads(json.dumps(out, default=str)), False)

    def _finish(
        self, episode: DisputeEpisode, state: _State, stop: RunStop, detail: str | None
    ) -> EpisodeRun:
        score = episode.score if episode.score is not None else episode.lapse(stop)
        stats = self.client.stats
        return EpisodeRun(
            provider=self.provider.name,
            model=self.model,
            strategy=self.plan.strategy,
            seed=episode.seed,
            episode_id=episode.episode_id,
            stop=stop,
            turns=state.turns,
            held_submits=state.held,
            rejected_turns=state.rejected,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            requests=stats.requests,
            retries=stats.retries,
            cost_usd=None
            if self.price is None
            else self.price.cost(state.input_tokens, state.output_tokens),
            calls=tuple(state.calls),
            detail=detail,
            score=json.loads(score.model_dump_json()),
        )

    def close(self) -> None:
        self.client.close()


class _State:
    def __init__(self, *, holds_left: int, nudges_left: int) -> None:
        self.turns = 0
        self.holds_left = holds_left
        self.nudges_left = nudges_left
        self.held = 0
        self.rejected = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls: list[CallLog] = []

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


__all__ = [
    "TRANSIENT_STOPS",
    "CallLog",
    "EpisodeRun",
    "Price",
    "ProviderRunner",
    "RunSettings",
    "RunStop",
    "observation_message",
]
