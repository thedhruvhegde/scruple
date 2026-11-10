"""The provider-agnostic episode loop: limits, retries, rate limits, accounting,
self-review holds, determinism and the ground-truth leak check. No network."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.providers import (
    FakeModel,
    Price,
    ProviderClient,
    ProviderHTTPError,
    ProviderRunner,
    RateLimiter,
    RunSettings,
    make_provider,
)
from scruple.agents.providers.fake import History, decide
from scruple.agents.providers.http import retry_after
from scruple.agents.strategies import STRATEGIES, LoopConfig, get_strategy
from scruple.agents.strategies.base import ApiPlan
from scruple.sim.families import CaseFamily
from scruple.verifiers.trace import EventKind

Handler = Callable[[httpx.Request], httpx.Response]
FORBIDDEN = (
    "claim_valid",
    "ground_truth",
    "liability_minor",
    "forged",
    "missing_evidence",
    *(f.value for f in CaseFamily),
)


def runner(
    handler: Handler,
    *,
    name: str = "anthropic",
    loop: LoopConfig | None = None,
    strategy: str = "zero-shot",
    sleeps: list[float] | None = None,
    **kw: Any,
) -> ProviderRunner:
    plan = get_strategy(strategy).api_plan()
    if loop is not None:
        plan = ApiPlan(strategy=plan.strategy, system=plan.system, loop=loop)
    sink = sleeps if sleeps is not None else []
    return ProviderRunner(
        make_provider(name, api_key="k"),
        "m",
        plan,
        transport=httpx.MockTransport(handler),
        sleep=sink.append,
        **kw,
    )


def anthropic_text(text: str = "thinking", stop: str = "end_turn", tokens: int = 10) -> Any:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": tokens, "output_tokens": tokens},
    }


def anthropic_call(name: str, args: Any, tokens: int = 10) -> Any:
    return {
        "content": [{"type": "tool_use", "id": f"t{name}", "name": name, "input": args}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": tokens, "output_tokens": tokens},
    }


def scripted(*replies: Any) -> Handler:
    queue = list(replies)

    def handler(_req: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    return handler


# --- stops -----------------------------------------------------------------------------


def test_nudges_then_model_stopped() -> None:
    r = runner(scripted(anthropic_text()), loop=LoopConfig(nudges=2))
    run = r.run(DisputeEpisode(1))
    assert run.stop == "model_stopped"
    assert run.turns == 3  # one reply plus two nudged replies
    assert "lapsed" in run.score["flags"]
    assert "lapsed:model_stopped" in run.score["flags"]


def test_refusal_stops_at_once() -> None:
    run = runner(scripted(anthropic_text("no", stop="refusal"))).run(DisputeEpisode(1))
    assert (run.stop, run.detail, run.turns) == ("refusal", "no", 1)


def test_refusal_in_the_planning_turn() -> None:
    run = runner(scripted(anthropic_text("no", stop="refusal")), strategy="plan-then-act").run(
        DisputeEpisode(1)
    )
    assert run.stop == "refusal"


def test_max_tokens_stop() -> None:
    run = runner(scripted(anthropic_text(stop="max_tokens")), loop=LoopConfig(nudges=0)).run(
        DisputeEpisode(1)
    )
    assert run.stop == "max_tokens"


def test_max_turns() -> None:
    run = runner(scripted(anthropic_call("get_case", {})), loop=LoopConfig(max_turns=3)).run(
        DisputeEpisode(1)
    )
    assert (run.stop, run.turns, len(run.calls)) == ("max_turns", 3, 3)


def test_token_budget() -> None:
    loop = LoopConfig(token_budget=100)
    run = runner(scripted(anthropic_call("get_case", {}, tokens=30)), loop=loop).run(
        DisputeEpisode(1)
    )
    assert run.stop == "token_budget"
    assert run.input_tokens + run.output_tokens >= 100
    assert run.turns == 2


def test_tool_errors_are_shown_and_counted() -> None:
    seen: list[Any] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body["messages"][-1])
        if len(seen) == 1:
            return httpx.Response(200, json=anthropic_call("no_such_tool", {"x": 1}))
        if len(seen) == 2:
            return httpx.Response(200, json=anthropic_call("wait", "not an object"))
        return httpx.Response(200, json=anthropic_call("submit_decision", {"explanation": "x"}))

    run = runner(handler).run(DisputeEpisode(1))
    assert run.stop == "submitted"
    assert [c.ok for c in run.calls] == [False, False, True]
    assert run.score["tool_errors"] == 2
    err = seen[1]["content"][0]
    assert err["is_error"] is True
    assert "unknown tool" in err["content"]


def test_http_error_after_retries() -> None:
    sleeps: list[float] = []
    r = runner(
        scripted(httpx.Response(503, text="busy")),
        sleeps=sleeps,
        settings=RunSettings(max_retries=3, backoff_s=0.5),
    )
    run = r.run(DisputeEpisode(1))
    assert run.stop == "http_error"
    assert "503" in (run.detail or "")
    assert sleeps == [0.5, 1.0, 2.0]
    assert (run.requests, run.retries) == (4, 3)


def test_non_retryable_status_fails_at_once() -> None:
    sleeps: list[float] = []
    run = runner(scripted(httpx.Response(401, text="bad key")), sleeps=sleeps).run(
        DisputeEpisode(1)
    )
    assert run.stop == "http_error" and sleeps == []


def test_retry_after_is_honoured_then_succeeds() -> None:
    sleeps: list[float] = []
    r = runner(
        scripted(
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(529, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            anthropic_call("submit_decision", {"explanation": "done"}),
        ),
        sleeps=sleeps,
    )
    run = r.run(DisputeEpisode(1))
    assert run.stop == "submitted"
    assert sleeps == [7.0, 2.0]  # header, then backoff (attempt 1) for an HTTP date
    assert run.retries == 2


def test_retry_after_parsing() -> None:
    assert retry_after(httpx.Response(429, headers={"retry-after": "3.5"})) == 3.5
    assert retry_after(httpx.Response(429, headers={"retry-after": "9999"})) == 120.0
    assert retry_after(httpx.Response(429, headers={"retry-after": "-2"})) == 0.0
    assert retry_after(httpx.Response(429)) is None


def test_transport_errors_retry_then_timeout_stop() -> None:
    sleeps: list[float] = []
    r = runner(
        scripted(httpx.ReadTimeout("slow")), sleeps=sleeps, settings=RunSettings(max_retries=1)
    )
    run = r.run(DisputeEpisode(1))
    assert run.stop == "timeout"
    assert len(sleeps) == 1
    run2 = runner(
        scripted(
            httpx.ConnectError("down"), anthropic_call("submit_decision", {"explanation": "x"})
        )
    ).run(DisputeEpisode(1))
    assert run2.stop == "submitted" and run2.retries == 1


@pytest.mark.parametrize(
    "reply",
    [{"content": "not a list"}, {"nothing": True}, httpx.Response(200, json=[1, 2])],
)
def test_bad_responses(reply: Any) -> None:
    run = runner(scripted(reply)).run(DisputeEpisode(1))
    assert run.stop == "bad_response"


def test_client_raises_provider_http_error() -> None:
    c = ProviderClient(
        "http://x", {}, transport=httpx.MockTransport(scripted(httpx.Response(400, text="no")))
    )
    with pytest.raises(ProviderHTTPError) as err:
        c.post("/p", {})
    assert err.value.status == 400
    c.close()


# --- rate limiting -----------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_rate_limiter_spaces_requests() -> None:
    clock = FakeClock()
    lim = RateLimiter(30, clock=clock, sleep=clock.sleep)  # one every 2 s
    waits = [lim.acquire() for _ in range(4)]
    assert waits == [0.0, 2.0, 2.0, 2.0]
    assert clock.t == 6.0
    clock.t += 10
    assert lim.acquire() == 0.0
    assert RateLimiter(None).acquire() == 0.0
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)


def test_rate_limiter_is_shared_across_threads() -> None:
    lock = threading.Lock()
    clock = FakeClock()

    def sleep(s: float) -> None:
        with lock:
            clock.sleep(0)  # threads do not advance a shared clock; record only

    lim = RateLimiter(60, clock=clock, sleep=sleep)
    threads = [threading.Thread(target=lim.acquire) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert lim.waited_s == pytest.approx(sum(range(10)))  # slots 0, 1, ..., 9 s


def test_runner_uses_the_limiter() -> None:
    clock = FakeClock()
    lim = RateLimiter(6, clock=clock, sleep=clock.sleep)
    fm = FakeModel("anthropic-messages")
    run = runner(fm, limiter=lim).run(DisputeEpisode(5001))
    assert run.stop == "submitted"
    assert clock.t == pytest.approx(10.0 * (run.requests - 1))


# --- accounting, seeds, holds ------------------------------------------------------------


def test_cost_needs_a_price() -> None:
    fm = FakeModel("openai-chat")
    run = runner(fm, name="openai").run(DisputeEpisode(5001))
    assert run.cost_usd is None
    price = Price(input_per_mtok=2.0, output_per_mtok=10.0)
    run2 = runner(FakeModel("openai-chat"), name="openai", price=price).run(DisputeEpisode(5001))
    want = (run2.input_tokens * 2.0 + run2.output_tokens * 10.0) / 1e6
    assert run2.cost_usd == pytest.approx(want)
    with pytest.raises(ValueError):
        Price(input_per_mtok=-1, output_per_mtok=0)


def test_seed_can_be_turned_off() -> None:
    fm = FakeModel("openai-chat")
    runner(fm, name="openai", settings=RunSettings(seed_from_episode=False)).run(DisputeEpisode(9))
    assert all("seed" not in b for b in fm.requests)


def test_held_submit_is_never_seen_by_the_episode() -> None:
    fm = FakeModel("anthropic-messages")
    r = runner(fm, loop=LoopConfig(critique_holds=2))
    ep = DisputeEpisode(5001)
    run = r.run(ep)
    assert run.held_submits == 2
    submits = [e for e in ep.trace if e.kind is EventKind.TOOL_CALL and e.tool == "submit_decision"]
    assert len(submits) == 1
    held_result = fm.requests[-2]["messages"][-1]["content"][0]["content"]
    assert "Not submitted yet" in held_result


def test_submit_result_is_redacted() -> None:
    ep = DisputeEpisode(5001)
    r = runner(scripted(anthropic_call("submit_decision", {"explanation": "x"})))
    msgs: list[Any] = []
    orig = r.provider.tool_messages

    def spy(results: Any, turn: Any) -> Any:
        out = orig(results, turn)
        msgs.extend(out)
        return out

    r.provider.tool_messages = spy  # type: ignore[method-assign]
    run = r.run(ep)
    assert run.stop == "submitted"
    assert json.loads(msgs[0]["content"][0]["content"]) == {"submitted": True}


def test_runs_are_deterministic() -> None:
    def once() -> Any:
        return runner(FakeModel("gemini-generate-content"), name="gemini").run(DisputeEpisode(77))

    a, b = once(), once()
    assert a == b
    assert a.score["trace_head"] == b.score["trace_head"]


def test_replay_of_the_call_log_reproduces_the_score() -> None:
    run = runner(FakeModel("anthropic-messages"), strategy="self-critique").run(
        DisputeEpisode(5004)
    )
    ep = DisputeEpisode.replay(5004, None, run.replay_calls())
    assert ep.score is not None
    assert ep.score.trace_head == run.score["trace_head"]
    assert ep.score.cost_usd_minor == run.score["cost_usd_minor"]


# --- ground truth never reaches a prompt ------------------------------------------------


def nosy(hist: History) -> list[tuple[str, dict[str, Any]]]:
    """Read everything the tools offer (statement, ledger, waits), then the honest path."""
    seen = [n for n, _ in hist]
    for name, args in (
        ("get_ledger_view", {}),
        ("request_info", {"question": "What happened?"}),
        ("wait", {"days": 3}),
    ):
        if "get_case" in seen and name not in seen:
            return [(name, args)]
    return decide(hist)


@pytest.mark.parametrize(
    "wire_name",
    [
        ("anthropic-messages", "anthropic"),
        ("openai-chat", "openai"),
        ("gemini-generate-content", "gemini"),
        ("mistral-chat", "mistral"),
    ],
)
@pytest.mark.parametrize("strategy", list(STRATEGIES))
def test_no_ground_truth_in_any_request(wire_name: tuple[str, str], strategy: str) -> None:
    wire, name = wire_name
    families: set[str] = set()
    for seed in range(5001, 5013):
        fm = FakeModel(wire, policy=nosy)
        ep = DisputeEpisode(seed)
        run = runner(fm, name=name, strategy=strategy).run(ep)
        assert run.stop == "submitted"
        families.add(run.score["family"])
        blob = json.dumps(fm.requests)
        for word in FORBIDDEN:
            assert word not in blob, (seed, word)
        assert not any(e.kind is EventKind.PROTECTED_READ for e in ep.trace)
        assert "ground_truth_read" not in run.score["flags"]
    assert len(families) >= 3  # the check ran across several hidden families
