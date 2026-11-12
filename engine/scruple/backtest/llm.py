"""Backtest a partner's LLM agent on imported history: an HTTP endpoint or a model.

Two forms of the same candidate (`LLMCandidate`):

- `HttpAgent`: the partner hosts an endpoint. Each case is POSTed as an
  `AgentRequest` (JSON) and the reply must be an `AgentReply`. Timeouts, retries with
  backoff (408, 409, 429, 5xx and transport errors; `Retry-After` honoured) and a
  concurrency limit come from `scruple.agents.providers.http.ProviderClient`.
- `ModelAgent`: scruple calls a model through the provider registry of
  `scruple.agents.providers` (the one `scruple eval` uses) with the partner's prompt
  file as the system prompt and the same `AgentRequest` as the user message. The model
  must answer with an `AgentReply` JSON object.

What is sent: exactly the facts of the `DecisionView` that a `decide(view)` policy
gets at decision time (`scruple.backtest.view`; no outcome facts), plus a hashed case
reference and the decision time. Privacy filter on top: every string is sent only if
it is a short code (one token of letters, digits and `_.:/-`, at most 64 characters,
at most 6 digits, nothing `scruple.security.redact` would change). Any other string is
free text: it is sent as null and named in `withheld_text`, unless the partner opts in
(`allow_free_text`, CLI `--agent-send-free-text`), in which case it is sent after
`redact_text` (card numbers, emails, phones and the rest become placeholders). Card
numbers are never sent: the case store refuses them and redaction replaces them.

Every reply is one of four outcomes: `ok`, `malformed` (an answer arrived but is not a
valid `AgentReply`, or the model refused), `failed` (HTTP error or transport failure
after the retries) and `timeout`. Nothing is defaulted here: the backtest counts each
outcome and applies `BacktestConfig.on_agent_error` to the cases without a decision.

Cache: one JSON file per (agent id, case key hash, view hash) in `cache_dir`. A rerun
with the same agent id on the same history makes no calls and gives the same
decisions. `ok` and `malformed` replies are cached; `failed` and `timeout` are not, so
a rerun asks again. Change the agent id whenever the agent changes (the model runner's
id includes the provider, model, prompt hash and sampling settings, so it changes by
itself).

Each call records the model id, latency (milliseconds, including retries) and token
usage (as the API or the partner reports it). Cost is computed only from a `Price`
the caller supplies; scruple ships no prices.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.agents.providers.base import GenSettings, Provider, ProviderHTTPError
from scruple.agents.providers.http import ProviderClient, RateLimiter
from scruple.agents.providers.runner import Price
from scruple.backtest.candidate import CandidateDecision, DecisionOutcome
from scruple.backtest.view import DecisionView, Posting
from scruple.domain import Action
from scruple.hashing import canonical_json, content_hash, sha256_hex
from scruple.security.redact import redact_text

REQUEST_VERSION: Literal["scruple.backtest.agent_request.v1"] = "scruple.backtest.agent_request.v1"
ACTIONS: tuple[str, ...] = tuple(a.value for a in Action)
ReplyOutcome = DecisionOutcome
OUTCOMES: tuple[ReplyOutcome, ...] = ("ok", "malformed", "failed", "timeout")
CACHED_OUTCOMES = frozenset({"ok", "malformed"})
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,63}")
MAX_CODE_DIGITS = 6
MAX_DETAIL = 500

INSTRUCTION = (
    "Decide this card dispute. The JSON below is everything known about the case at "
    "decision time. Reply with only a JSON object, no other text: "
    '{"action": "accept" | "contest" | "escalate", "reason": "<one short sentence>"}.'
)

Json = str | int | float | bool | None


# --- the wire format -----------------------------------------------------------------


class AgentRequest(BaseModel):
    """What the agent receives for one case (POST body; the model's user message)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["scruple.backtest.agent_request.v1"] = REQUEST_VERSION
    agent_id: str
    case_ref: str = Field(description="sha256 of the case id: stable, not reversible")
    decided_at: datetime
    actions: tuple[str, ...] = ACTIONS
    case: dict[str, Json | list[dict[str, Json]]]
    withheld_text: tuple[str, ...] = Field(
        default=(), description="fields holding free text, sent as null (no opt-in)"
    )


class ReplyUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class AgentReply(BaseModel):
    """What the agent must answer. Unknown keys are ignored; known ones are checked."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    action: Action
    reason: str | None = Field(default=None, max_length=4_000)
    model: str | None = Field(default=None, max_length=200)
    usage: ReplyUsage | None = None


def is_code(value: str) -> bool:
    """A short code (reason value, network code, card brand, check result), not free text."""
    return (
        _CODE.fullmatch(value) is not None
        and sum(ch.isdigit() for ch in value) <= MAX_CODE_DIGITS
        and redact_text(value).text == value
    )


def _posting(p: Posting) -> dict[str, Json]:
    return {
        "occurred_at": p.occurred_at.isoformat(),
        "merchant_net_minor": p.merchant_net_minor,
        "fee_minor": p.fee_minor,
    }


def case_payload(
    view: DecisionView, *, allow_free_text: bool = False
) -> tuple[dict[str, Json | list[dict[str, Json]]], tuple[str, ...]]:
    """The view's known facts as JSON, with free text withheld (or redacted on opt-in)."""
    out: dict[str, Json | list[dict[str, Json]]] = {}
    withheld: list[str] = []
    for name in view.known():
        v = view[name]
        if isinstance(v, datetime):
            out[name] = v.isoformat()
        elif isinstance(v, tuple):
            out[name] = [_posting(p) for p in v]
        elif isinstance(v, str) and not is_code(v):
            if allow_free_text:
                out[name] = redact_text(v).text
            else:
                out[name] = None
                withheld.append(name)
        elif v is None or isinstance(v, str | int | float | bool):
            out[name] = v
        else:  # a new fact type must be classified here before it can leave
            raise TypeError(f"view fact {name!r} has unsupported type {type(v).__name__}")
    return out, tuple(withheld)


def case_key_hash(case_key: str) -> str:
    return sha256_hex("scruple-backtest-case:" + case_key)


def build_request(
    view: DecisionView, case_key: str, agent_id: str, *, allow_free_text: bool = False
) -> AgentRequest:
    payload, withheld = case_payload(view, allow_free_text=allow_free_text)
    return AgentRequest(
        agent_id=agent_id,
        case_ref=case_key_hash(case_key),
        decided_at=view.decided_at,
        case=payload,
        withheld_text=withheld,
    )


def view_hash(req: AgentRequest) -> str:
    """Hash of what the agent was shown (decision time, facts, withheld names)."""
    return content_hash(
        {"decided_at": req.decided_at.isoformat(), "case": req.case, "withheld": req.withheld_text}
    )


# --- one call ----------------------------------------------------------------------


class ReplyRecord(BaseModel):
    """One agent call (or its cached copy)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_key: str
    agent_id: str
    case_key_hash: str
    view_hash: str
    outcome: ReplyOutcome
    action: Action | None = None
    reason: str | None = None
    detail: str | None = None
    model: str | None = None
    latency_ms: float = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = None


class Answer(BaseModel):
    """A backend's raw result before validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reply: dict[str, Any] | None  # None: no JSON object could be read
    detail: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class AgentBackend(Protocol):
    kind: str
    agent_id: str

    def ask(self, req: AgentRequest) -> Answer: ...

    def close(self) -> None: ...


def _client(
    url: str,
    headers: dict[str, str],
    *,
    transport: httpx.BaseTransport | None,
    timeout_s: float,
    max_retries: int,
    backoff_s: float,
    rpm: float | None,
    sleep: Callable[[float], None],
) -> ProviderClient:
    return ProviderClient(
        base_url=url,
        headers=headers,
        transport=transport,
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_s=backoff_s,
        limiter=RateLimiter(rpm, sleep=sleep) if rpm else None,
        sleep=sleep,
    )


class HttpAgent:
    """The partner's endpoint: POST `AgentRequest` JSON, read `AgentReply` JSON."""

    kind = "http"

    def __init__(
        self,
        url: str,
        *,
        agent_id: str | None = None,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        rpm: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        u = httpx.URL(url)
        if u.scheme not in ("http", "https") or not u.host:
            raise ValueError(f"agent url must be http(s)://host/path, got {url!r}")
        self.url = url
        self.agent_id = agent_id or url
        self._path = u.raw_path.decode("ascii") or "/"
        self.client = _client(
            f"{u.scheme}://{u.netloc.decode('ascii')}",
            {"content-type": "application/json", **(headers or {})},
            transport=transport,
            timeout_s=timeout_s,
            max_retries=max_retries,
            backoff_s=backoff_s,
            rpm=rpm,
            sleep=sleep,
        )

    def ask(self, req: AgentRequest) -> Answer:
        data = self.client.post(self._path, req.model_dump(mode="json"))
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        model = data.get("model")
        return Answer(
            reply=data,
            model=model if isinstance(model, str) else None,
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
        )

    def close(self) -> None:
        self.client.close()


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def read_json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in a model's text: the whole text, or a fenced or embedded one."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[4:] if s.lower().startswith("json") else s
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        out = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


class ModelAgent:
    """A model behind the provider registry, with the partner's prompt as the system."""

    kind = "model"

    def __init__(
        self,
        provider: Provider,
        model: str,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float | None = 0.0,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        rpm: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        agent_id: str | None = None,
    ) -> None:
        if not prompt.strip():
            raise ValueError("the agent prompt is empty")
        self.provider = provider
        self.model = model
        self.prompt = prompt
        self.gen = GenSettings(max_tokens=max_tokens, temperature=temperature)
        settings = f"max_tokens={max_tokens},temperature={temperature}"
        self.agent_id = agent_id or (
            f"{provider.name}:{model}:prompt-{sha256_hex(prompt)[:12]}:"
            f"{sha256_hex(settings + INSTRUCTION)[:8]}"
        )
        self.client = _client(
            provider.base_url,
            provider.headers(),
            transport=transport,
            timeout_s=timeout_s,
            max_retries=max_retries,
            backoff_s=backoff_s,
            rpm=rpm,
            sleep=sleep,
        )

    def message(self, req: AgentRequest) -> str:
        return INSTRUCTION + "\n\n" + canonical_json(req)

    def ask(self, req: AgentRequest) -> Answer:
        p = self.provider
        body = p.body(self.model, self.prompt, [p.user_message(self.message(req))], None, self.gen)
        data = self.client.post(p.path(self.model), body)
        model = data.get("model") if isinstance(data.get("model"), str) else None
        try:
            turn = p.parse(data, 1)
        except (ValueError, LookupError, TypeError, AttributeError) as exc:
            return Answer(reply=None, detail=f"unreadable response: {exc!r}", model=model)
        tokens = {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens}
        if turn.stop == "refusal":
            return Answer(reply=None, detail="the model refused", model=model, **tokens)
        reply = read_json_object(turn.text)
        detail = None
        if reply is None:
            detail = "no JSON object in the reply" + (
                " (stopped at max_tokens)" if turn.stop == "max_tokens" else ""
            )
        return Answer(reply=reply, detail=detail, model=model or self.model, **tokens)

    def close(self) -> None:
        self.client.close()


# --- cache -----------------------------------------------------------------------------


class ReplyCache:
    """One JSON file per call key. Writes are atomic (temp file, then rename)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> ReplyRecord | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            rec = ReplyRecord.model_validate_json(raw)
        except ValidationError:
            return None  # a damaged entry is asked again, then overwritten
        return rec if rec.cache_key == key else None

    def put(self, rec: ReplyRecord) -> None:
        path = self._path(rec.cache_key)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(rec.model_dump_json() + "\n", encoding="utf-8")
        os.replace(tmp, path)


def call_key(agent_id: str, case_hash: str, v_hash: str) -> str:
    return content_hash([agent_id, case_hash, v_hash])


# --- the candidate ---------------------------------------------------------------------


class AgentUsage(BaseModel):
    """Totals over the agent calls of one backtest run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str
    kind: str
    models: tuple[str, ...]  # model ids the replies reported
    cases: int  # cases the agent was asked about
    cache_hits: int  # answered from the cache: no call this run
    calls_made: int  # sent this run
    outcomes: dict[str, int]
    input_tokens: int  # over every reply used, cached or not
    output_tokens: int
    input_tokens_this_run: int
    output_tokens_this_run: int
    cost_usd: float | None  # over every reply used; None without a price
    cost_usd_this_run: float | None
    latency_ms_total: float  # as recorded when each call was made
    latency_ms_mean: float | None
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    latency_ms_max: float | None
    free_text_sent: bool
    price: Price | None = None


def _pct(xs: Sequence[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))], 3)


class LLMCandidate:
    """A backtest candidate whose decisions come from an `AgentBackend`."""

    def __init__(
        self,
        backend: AgentBackend,
        *,
        cache_dir: Path | str | None = None,
        concurrency: int = 4,
        allow_free_text: bool = False,
        price: Price | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.backend = backend
        self.name = backend.agent_id
        self.cache = ReplyCache(cache_dir) if cache_dir is not None else None
        self.concurrency = concurrency
        self.allow_free_text = allow_free_text
        self.price = price
        self.clock = clock
        self.records: list[ReplyRecord] = []  # the replies used, in case order
        self.fresh: set[str] = set()  # cache keys called this run
        self.requests: list[AgentRequest] = []  # what was (or would have been) sent

    # one call, never raises for agent faults
    def _call(self, req: AgentRequest, key: str, ch: str, vh: str) -> ReplyRecord:
        t0 = self.clock()
        ans: Answer | None = None
        outcome: ReplyOutcome
        detail: str | None = None
        try:
            ans = self.backend.ask(req)
        except httpx.TimeoutException as exc:
            outcome, detail = "timeout", str(exc) or "timeout"
        except (httpx.HTTPError, ProviderHTTPError) as exc:
            outcome, detail = "failed", str(exc) or type(exc).__name__
        except ValueError as exc:  # a 2xx response that is not a JSON object
            outcome, detail = "malformed", str(exc)
        latency = round(max(0.0, self.clock() - t0) * 1000, 3)
        reply: AgentReply | None = None
        if ans is not None:
            if ans.reply is None:
                outcome, detail = "malformed", ans.detail
            else:
                try:
                    reply = AgentReply.model_validate(ans.reply)
                    outcome = "ok"
                except ValidationError as exc:
                    outcome = "malformed"
                    detail = "; ".join(
                        f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}"
                        for e in exc.errors()
                    )
        tin = ans.input_tokens if ans else 0
        tout = ans.output_tokens if ans else 0
        model = (reply.model if reply and reply.model else None) or (ans.model if ans else None)
        return ReplyRecord(
            cache_key=key,
            agent_id=self.backend.agent_id,
            case_key_hash=ch,
            view_hash=vh,
            outcome=outcome,
            action=reply.action if reply else None,
            reason=reply.reason if reply else None,
            detail=detail[:MAX_DETAIL] if detail else None,
            model=model,
            latency_ms=latency,
            input_tokens=tin,
            output_tokens=tout,
            cost_usd=None if self.price is None else self.price.cost(tin, tout),
        )

    def decide_many(self, items: Sequence[tuple[str, DecisionView]]) -> list[CandidateDecision]:
        """Decisions for (case key, view) pairs, in order. Cached replies cost nothing."""
        jobs: list[tuple[AgentRequest, str, str, str]] = []
        for case_key, view in items:
            req = build_request(
                view, case_key, self.backend.agent_id, allow_free_text=self.allow_free_text
            )
            ch, vh = req.case_ref, view_hash(req)
            jobs.append((req, call_key(self.backend.agent_id, ch, vh), ch, vh))
        self.requests.extend(j[0] for j in jobs)
        out: dict[str, ReplyRecord] = {}
        todo: dict[str, tuple[AgentRequest, str, str, str]] = {}
        for job in jobs:
            hit = self.cache.get(job[1]) if self.cache is not None else None
            if hit is not None:
                out[job[1]] = hit
            else:
                todo.setdefault(job[1], job)
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for rec in pool.map(lambda j: self._call(*j), list(todo.values())):
                out[rec.cache_key] = rec
                self.fresh.add(rec.cache_key)
                if self.cache is not None and rec.outcome in CACHED_OUTCOMES:
                    self.cache.put(rec)
        recs = [out[j[1]] for j in jobs]
        self.records.extend(recs)
        return [self._decision(r) for r in recs]

    def decide(self, view: DecisionView) -> CandidateDecision:
        """One view without a case id: the view hash stands in for the case key."""
        key = content_hash(case_payload(view, allow_free_text=self.allow_free_text)[0])
        return self.decide_many([(f"view:{key}", view)])[0]

    def _decision(self, r: ReplyRecord) -> CandidateDecision:
        if r.outcome == "ok":
            return CandidateDecision(action=r.action, ref=self.name)
        return CandidateDecision(action=None, ref=self.name, outcome=r.outcome, detail=r.detail)

    def usage(self) -> AgentUsage:
        """Totals over every decision this candidate gave (one backtest run per instance)."""
        recs = self.records
        fresh = [r for r in recs if r.cache_key in self.fresh]
        lat = [r.latency_ms for r in recs]

        def cost(rows: Sequence[ReplyRecord]) -> float | None:
            if self.price is None:
                return None
            price = self.price
            return round(math.fsum(price.cost(r.input_tokens, r.output_tokens) for r in rows), 6)

        counts = Counter(r.outcome for r in recs)
        return AgentUsage(
            agent_id=self.name,
            kind=self.backend.kind,
            models=tuple(sorted({r.model for r in recs if r.model})),
            cases=len(recs),
            cache_hits=len(recs) - len(fresh),
            calls_made=len(self.fresh),
            outcomes={o: counts.get(o, 0) for o in OUTCOMES},
            input_tokens=sum(r.input_tokens for r in recs),
            output_tokens=sum(r.output_tokens for r in recs),
            input_tokens_this_run=sum(r.input_tokens for r in fresh),
            output_tokens_this_run=sum(r.output_tokens for r in fresh),
            cost_usd=cost(recs),
            cost_usd_this_run=cost(fresh),
            latency_ms_total=round(math.fsum(lat), 3),
            latency_ms_mean=round(statistics.fmean(lat), 3) if lat else None,
            latency_ms_p50=_pct(lat, 0.5),
            latency_ms_p95=_pct(lat, 0.95),
            latency_ms_max=max(lat) if lat else None,
            free_text_sent=self.allow_free_text,
            price=self.price,
        )

    def close(self) -> None:
        self.backend.close()


__all__ = [
    "ACTIONS",
    "INSTRUCTION",
    "OUTCOMES",
    "REQUEST_VERSION",
    "AgentBackend",
    "AgentReply",
    "AgentRequest",
    "AgentUsage",
    "Answer",
    "HttpAgent",
    "LLMCandidate",
    "ModelAgent",
    "ReplyCache",
    "ReplyOutcome",
    "ReplyRecord",
    "build_request",
    "call_key",
    "case_key_hash",
    "case_payload",
    "is_code",
    "read_json_object",
    "view_hash",
]
