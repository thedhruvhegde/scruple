"""The agents a suite (or `eval run --env`) can drive, behind one `play` call.

Agent specs (`parse_agent`):

- `provider:model`, e.g. `groq:openai/gpt-oss-120b`: the API harness
  (`ProviderRunner`, every provider `scruple eval run` has), paced by a shared rate
  limiter, with the provider-limit rules of `scruple.agents.providers.limits` when
  `paced` is set (per-day 429s waited out, 413s elide old tool outputs) and a
  transcript of request and response bodies.
- `python:package.module:function`: a local agent. The function gets a
  `Session` (observation, tool schemas, `call(tool, args)`) and plays until it calls
  `submit_decision` (or returns; the episode then lapses). Runs in parallel.
- `http://host:port/path` or `https://...`: a remote agent speaking the step
  protocol: scruple POSTs `{"session", "env", "observation", "tools", "last_result"}`
  and the endpoint answers `{"tool": name, "args": {...}}` (or `{"stop": true}`).
- `scripted:<policy>`: one of the environment's scripted reference policies (for
  baselines and harness checks; reference policies that read ground truth are
  labelled as such in reports).

Whatever the agent is, the record keeps only the calls the episode saw; grading
replays them (`scruple.suite.envs.replay`).
"""

from __future__ import annotations

import importlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scruple.agents.errors import EpisodeError
from scruple.agents.providers import RateLimiter, RunSettings, make_provider
from scruple.agents.providers.runner import ProviderRunner
from scruple.agents.strategies.base import ApiPlan, LoopConfig
from scruple.suite.envs import EnvAdapter, Outcome, get_adapter
from scruple.verifiers.trace import EventKind

# Non-dispute loop: dispute strategies keep their own loop config.
FINOPS_LOOP = LoopConfig(max_turns=40, token_budget=150_000)


class PlayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent: str
    stop: str
    calls: tuple[dict[str, Any], ...] = ()  # {"tool", "args", "ok", "error"?}
    episode_id: str = ""
    trace_head: str = ""
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    retries: int = 0
    rejected_turns: int = 0
    held_submits: int = 0
    elisions: int = 0
    quota_waits_s: tuple[float, ...] = ()
    latency_s: float = 0.0  # wall clock for the whole episode
    request_latency_s: tuple[float, ...] = ()
    detail: str | None = None
    real_model: bool = True
    licensed_truth: bool = False  # a scripted reference that reads ground truth
    live_outcome: Outcome | None = None  # scripted policies are graded on the live episode
    meta: dict[str, Any] = Field(default_factory=dict)


class Agent(Protocol):
    @property
    def label(self) -> str: ...

    parallel_safe: bool

    def play(
        self,
        adapter: EnvAdapter,
        seed: int,
        case_type: str | None,
        strategy: str,
        *,
        transcript: Path | None = None,
    ) -> PlayResult: ...


def _agent_calls(ep: Any) -> tuple[dict[str, Any], ...]:
    out = []
    for e in ep.trace:
        if e.kind is EventKind.TOOL_CALL and e.actor.value == "agent":
            out.append({"tool": str(e.tool), "args": e.args, "ok": True})
    return tuple(out)


def _redact(text: str | None) -> str | None:
    if text is None:
        return None
    from scruple.agents.providers.limits import ORG_ID, USER_ID

    return USER_ID.sub("user_REDACTED", ORG_ID.sub("org_REDACTED", text))


def _commerce_fake(hist: Any) -> list[tuple[str, dict[str, Any]]]:
    names = [n for n, _ in hist]
    if not names:
        return [("get_task", {}), ("list_offers", {})]
    if "submit_decision" in names:
        return []
    return [("submit_decision", {"explanation": "Read the task and the offers; bought nothing."})]


# --- provider (API) -------------------------------------------------------------------


@dataclass
class ProviderAgent:
    provider: str
    model: str
    base_url: str | None = None
    settings: RunSettings = field(default_factory=lambda: RunSettings(timeout_s=180.0))
    rpm: float | None = None
    paced: bool = True
    transport: str = "http"  # or "fake": the scripted fake model in the wire format
    parallel_safe: bool = False
    _limiter: RateLimiter | None = None

    def __post_init__(self) -> None:
        self._limiter = RateLimiter(self.rpm)
        self._prov = make_provider(
            self.provider,
            base_url=self.base_url,
            api_key="fake-key" if self.transport == "fake" else None,
        )

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def plan(self, adapter: EnvAdapter, strategy: str) -> ApiPlan:
        if adapter.name == "dispute":
            from scruple.agents.strategies import get_strategy

            return get_strategy(strategy).api_plan()
        return ApiPlan(strategy=strategy, system=adapter.system_prompt(strategy), loop=FINOPS_LOOP)

    def _transport(self, transcript: Path | None) -> tuple[httpx.BaseTransport | None, Any, Any]:
        if self.transport == "fake":
            from scruple.agents.providers import FakeModel
            from scruple.finops.rollout import fake_policy

            fm = (
                FakeModel(self._prov.wire)
                if self._env == "dispute"
                else FakeModel(
                    self._prov.wire,
                    policy=_commerce_fake if self._env == "commerce" else fake_policy,
                )
            )
            return fm.transport(), None, None
        if not self.paced and transcript is None:
            return None, None, None
        from scruple.agents.providers.limits import LimitTransport, TranscriptTransport

        rec = TranscriptTransport(transcript)
        if not self.paced:
            return rec, None, rec
        lim = LimitTransport(rec)
        return lim, lim, rec

    def play(
        self,
        adapter: EnvAdapter,
        seed: int,
        case_type: str | None,
        strategy: str,
        *,
        transcript: Path | None = None,
    ) -> PlayResult:
        self._env = adapter.name
        transport, lim, rec = self._transport(transcript)
        ep = adapter.make(seed, case_type)
        runner = ProviderRunner(
            self._prov,
            self.model,
            self.plan(adapter, strategy),
            settings=self.settings,
            transport=transport,
            limiter=self._limiter,
            tools=adapter.tools,
        )
        t0 = time.monotonic()
        try:
            run = runner.run(ep)
        finally:
            runner.close()
        return PlayResult(
            agent=self.label,
            stop=run.stop,
            calls=tuple(
                {"tool": c.tool, "args": c.args, "ok": c.ok}
                | ({"error": c.error} if c.error else {})
                for c in run.calls
            ),
            episode_id=run.episode_id,
            trace_head=str(ep.trace.head),
            turns=run.turns,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            requests=run.requests,
            retries=run.retries,
            rejected_turns=run.rejected_turns,
            held_submits=run.held_submits,
            elisions=0 if lim is None else lim.elisions,
            quota_waits_s=() if lim is None else tuple(round(w, 1) for w in lim.quota_waits),
            latency_s=round(time.monotonic() - t0, 3),
            request_latency_s=() if rec is None else tuple(round(x, 3) for x in rec.latencies_s),
            detail=_redact(run.detail),
            real_model=self.transport != "fake",
        )


# --- python callable ------------------------------------------------------------------


class Session:
    """What a local agent function gets: the first observation, the tool schemas and a
    `call` that runs a tool on the episode (errors come back as {"error": ...})."""

    def __init__(self, adapter: EnvAdapter, ep: Any, strategy: str) -> None:
        self.env = adapter.name
        self.strategy = strategy
        self.system_prompt = adapter.system_prompt(strategy)
        self.observation: dict[str, Any] = ep.observe().model_dump(mode="json")
        self.tools = [
            {"name": t.name, "description": t.description, "schema": t.schema}
            for t in adapter.tools
        ]
        self._ep = ep
        self.log: list[dict[str, Any]] = []

    @property
    def closed(self) -> bool:
        return bool(self._ep.closed)

    def call(self, tool: str, args: Any = None) -> dict[str, Any]:
        if self._ep.closed:
            return {"error": "episode is closed"}
        try:
            out = self._ep.call(tool, args)
        except EpisodeError as exc:
            self.log.append({"tool": tool, "args": args, "ok": False, "error": str(exc)})
            return {"error": str(exc)}
        self.log.append({"tool": tool, "args": args, "ok": True})
        if tool == "submit_decision":
            return {"submitted": True}  # the score is never shown
        back: dict[str, Any] = json.loads(json.dumps(out, default=str))
        return back


@dataclass
class PythonAgent:
    target: str  # "package.module:function"
    parallel_safe: bool = True

    @property
    def label(self) -> str:
        return f"python:{self.target}"

    def _fn(self) -> Callable[[Session], Any]:
        mod, _, name = self.target.partition(":")
        if not name:
            raise ValueError("python agent spec is python:package.module:function")
        fn = getattr(importlib.import_module(mod), name)
        if not callable(fn):
            raise ValueError(f"{self.target} is not callable")
        return fn  # type: ignore[no-any-return]

    def play(
        self,
        adapter: EnvAdapter,
        seed: int,
        case_type: str | None,
        strategy: str,
        *,
        transcript: Path | None = None,
    ) -> PlayResult:
        ep = adapter.make(seed, case_type)
        s = Session(adapter, ep, strategy)
        t0 = time.monotonic()
        detail = None
        try:
            self._fn()(s)
        except Exception as exc:  # the agent's own crash is its behaviour
            detail = f"agent raised {type(exc).__name__}: {exc}"[:500]
        stop = "submitted" if ep.closed else ("agent_error" if detail else "model_stopped")
        return PlayResult(
            agent=self.label,
            stop=stop,
            calls=tuple(s.log),
            episode_id=str(ep.episode_id),
            trace_head=str(ep.trace.head),
            turns=len(s.log),
            latency_s=round(time.monotonic() - t0, 3),
            detail=detail,
        )


# --- remote http step protocol --------------------------------------------------------


@dataclass
class HttpAgent:
    url: str
    timeout_s: float = 60.0
    max_steps: int = 80
    parallel_safe: bool = True
    transport: httpx.BaseTransport | None = None

    @property
    def label(self) -> str:
        return f"http:{self.url}"

    def play(
        self,
        adapter: EnvAdapter,
        seed: int,
        case_type: str | None,
        strategy: str,
        *,
        transcript: Path | None = None,
    ) -> PlayResult:
        ep = adapter.make(seed, case_type)
        s = Session(adapter, ep, strategy)
        sid = uuid.uuid4().hex[:12]
        last: dict[str, Any] | None = None
        stop, detail = "max_turns", None
        t0 = time.monotonic()
        with httpx.Client(timeout=self.timeout_s, transport=self.transport) as c:
            for _ in range(self.max_steps):
                body = {
                    "session": sid,
                    "env": adapter.name,
                    "system": s.system_prompt,
                    "observation": s.observation if last is None else None,
                    "tools": s.tools if last is None else None,
                    "last_result": last,
                }
                try:
                    r = c.post(self.url, json=body)
                    r.raise_for_status()
                    step = r.json()
                except (httpx.HTTPError, ValueError) as exc:
                    stop, detail = "http_error", str(exc)[:500]
                    break
                if not isinstance(step, dict) or step.get("stop"):
                    stop = "model_stopped"
                    break
                last = {
                    "tool": step.get("tool"),
                    "result": s.call(str(step.get("tool")), step.get("args")),
                }
                if ep.closed:
                    stop = "submitted"
                    break
        return PlayResult(
            agent=self.label,
            stop=stop,
            calls=tuple(s.log),
            episode_id=str(ep.episode_id),
            trace_head=str(ep.trace.head),
            turns=len(s.log),
            latency_s=round(time.monotonic() - t0, 3),
            detail=detail,
        )


# --- scripted reference -----------------------------------------------------------------


@dataclass
class ScriptedAgent:
    policy: str
    parallel_safe: bool = True

    @property
    def label(self) -> str:
        return f"scripted:{self.policy}"

    def play(
        self,
        adapter: EnvAdapter,
        seed: int,
        case_type: str | None,
        strategy: str,
        *,
        transcript: Path | None = None,
    ) -> PlayResult:
        refs = adapter.references()
        name = adapter.honest_ref if self.policy == "honest" else self.policy
        if name not in refs:
            raise ValueError(
                f"{adapter.name} has no scripted policy {self.policy!r}; choose from "
                + ", ".join(refs)
            )
        t0 = time.monotonic()
        ep = refs[name](seed, case_type)
        licensed = any(e.kind is EventKind.PROTECTED_READ for e in ep.trace)
        return PlayResult(
            agent=self.label,
            stop="submitted",
            calls=_agent_calls(ep),
            episode_id=str(ep.episode_id),
            trace_head=str(ep.trace.head),
            turns=0,
            latency_s=round(time.monotonic() - t0, 3),
            real_model=False,
            licensed_truth=licensed,
            live_outcome=adapter.outcome(ep),
        )


def parse_agent(
    spec: str,
    *,
    rpm: float | None = None,
    settings: RunSettings | None = None,
    base_url: str | None = None,
    paced: bool = True,
    transport: str = "http",
) -> Agent:
    if spec.startswith(("http://", "https://")):
        return HttpAgent(spec)
    kind, _, rest = spec.partition(":")
    if not rest:
        raise ValueError(
            "agent spec is provider:model, python:module:fn, scripted:policy or an http(s) URL"
        )
    if kind == "python":
        return PythonAgent(rest)
    if kind == "scripted":
        return ScriptedAgent(rest)
    return ProviderAgent(
        kind,
        rest,
        base_url=base_url,
        settings=settings or RunSettings(timeout_s=180.0, max_retries=10, backoff_s=4.0),
        rpm=rpm,
        paced=paced,
        transport=transport,
    )


def adapter_for(name: str) -> EnvAdapter:
    return get_adapter(name)


__all__ = [
    "FINOPS_LOOP",
    "Agent",
    "HttpAgent",
    "PlayResult",
    "ProviderAgent",
    "PythonAgent",
    "ScriptedAgent",
    "Session",
    "parse_agent",
]
