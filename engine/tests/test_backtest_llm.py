"""Backtest an LLM agent on history: the HTTP endpoint and the model runner.

No real API is called: the endpoint is a local HTTP server (or an httpx mock
transport), the model is a fake Anthropic / OpenAI-compatible transport.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from scruple.agents.providers import Price, make_provider
from scruple.backtest import (
    AgentRequest,
    BacktestConfig,
    BacktestReport,
    DecisionView,
    History,
    HttpAgent,
    LLMCandidate,
    ModelAgent,
    PaymentIndex,
    build_view,
    run_backtest,
    to_markdown,
)
from scruple.backtest.candidate import ModuleCandidate
from scruple.backtest.llm import (
    INSTRUCTION,
    Answer,
    build_request,
    case_payload,
    is_code,
    read_json_object,
)
from scruple.backtest.view import OUTCOME_FACTS, OUTCOME_TIME_FACTS
from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.cli.main import main
from scruple.domain import Action
from scruple.ingest.models import PaymentFacts
from tests.test_backtest_view import case, history, moves

PAN = "4242424242424242"


def trio() -> History:
    a = case(1, "won", submitted=True)
    b = case(2, "lost")
    c = case(3, "lost", submitted=True, amount=30_000)
    return history([a, b, c], moves(a, credit_days=40) + moves(b) + moves(c))


def run(h: History, cand: Any, **cfg: Any) -> BacktestReport:
    return run_backtest(
        h, cand, profile=DEFAULT_PROFILE, config=BacktestConfig(include_cases=True, **cfg)
    )


def policy(body: dict[str, Any]) -> dict[str, Any]:
    """The fake partner agent: contest small cases, accept large ones."""
    action = "contest" if body["case"]["amount_minor"] < 20_000 else "accept"
    return {
        "action": action,
        "reason": "amount rule",
        "model": "partner-model-1",
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


class Recorder:
    """An `httpx.MockTransport` handler: answers with `reply(body)` and keeps every body."""

    def __init__(self, reply: Callable[[dict[str, Any]], httpx.Response]) -> None:
        self.reply = reply
        self.bodies: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self.lock:
            self.bodies.append(body)
        return self.reply(body)


def ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=policy(body))


def http_candidate(
    handler: Callable[[httpx.Request], httpx.Response], tmp: Path | None = None, **kw: Any
) -> LLMCandidate:
    agent = HttpAgent(
        "https://agent.example/v1/decide",
        agent_id="partner-v1",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        max_retries=kw.pop("max_retries", 3),
    )
    return LLMCandidate(agent, cache_dir=tmp, **kw)


# --- what is sent ---------------------------------------------------------------------


def rich_history() -> History:
    facts = PaymentFacts(
        card_brand="visa",
        three_ds_authenticated=True,
        delivery_confirmed=True,
        shipping_carrier="UPS",
        amount_refunded_minor=500,  # outcome-time
        refund_state="partial",  # outcome-time
        evidence_submission_count=1,  # outcome-time
        defendable=True,  # outcome-time
    )
    c = case(1, "won", submitted=True).model_copy(update={"facts": facts})
    return history([c], moves(c, credit_days=40))


def test_payload_is_the_decision_view_and_holds_no_outcome_fields() -> None:
    h = rich_history()
    view = build_view(h.cases[0], PaymentIndex(h.cases))
    req = build_request(view, h.cases[0].case.case_id, "partner-v1")
    assert set(req.case) == set(view.known())
    leaked = set(req.case) & (set(OUTCOME_FACTS) | set(OUTCOME_TIME_FACTS))
    assert not leaked
    text = req.model_dump_json()
    for name in (*OUTCOME_FACTS, *OUTCOME_TIME_FACTS, "won", "dp_1", "ch_1"):
        assert name not in text, name
    # the dispute debit is known; the later reinstatement credit is not
    ledger = req.case["ledger"]
    assert isinstance(ledger, list) and ledger
    assert all(
        isinstance(p["merchant_net_minor"], int) and p["merchant_net_minor"] < 0 for p in ledger
    )
    assert req.case["delivery_confirmed"] is True and req.case["card_brand"] == "visa"


def test_sent_payload_matches_what_a_decide_policy_sees() -> None:
    seen: list[DecisionView] = []

    def spy(v: DecisionView) -> None:
        seen.append(v)

    h = trio()
    run(h, ModuleCandidate(spy, "spy"))
    rec = Recorder(ok)
    run(h, http_candidate(rec))
    bodies = sorted(rec.bodies, key=lambda b: b["case"]["opened_at"])
    assert [b["case"] for b in bodies] == [case_payload(v)[0] for v in seen]
    for b in bodies:
        AgentRequest.model_validate(b)  # the documented schema


def test_free_text_is_withheld_unless_the_partner_opts_in() -> None:
    reason = f"Cardholder Jane Doe says card {PAN} was stolen"
    h = history([case(1, reason=reason)])
    view = build_view(h.cases[0], PaymentIndex(h.cases))
    payload, withheld = case_payload(view)
    assert payload["reason"] is None and withheld == ("reason",)
    assert payload["network"] is None  # None stays None, not "withheld"
    opted, w2 = case_payload(view, allow_free_text=True)
    assert w2 == () and isinstance(opted["reason"], str)
    assert PAN not in opted["reason"] and "[PAN]" in opted["reason"]
    assert "Jane Doe" not in opted["reason"]
    for code in ("fraudulent", "10.4", "4853", "visa", "UPS", "product_not_received"):
        assert is_code(code)
    for text in ("two words", PAN, "a@b.example", "x" * 65):
        assert not is_code(text)


# --- the HTTP endpoint ------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    behaviour: Callable[[dict[str, Any]], tuple[int, bytes, float]]
    bodies: list[dict[str, Any]]


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.bodies.append(body)
        status, payload, delay = self.server.behaviour(body)
        if delay:
            time.sleep(delay)
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[_Server]:
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.behaviour = lambda b: (200, json.dumps(policy(b)).encode(), 0.0)
    srv.bodies = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def url(srv: _Server) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/decide"


def test_http_agent_against_a_local_server(server: _Server, tmp_path: Path) -> None:
    cand = LLMCandidate(HttpAgent(url(server), agent_id="partner-v1"), cache_dir=tmp_path)
    r = run(trio(), cand)
    acts = {x.case_id: x.candidate_action for x in r.cases}
    assert acts == {
        "stripe:dp_1": Action.CONTEST,
        "stripe:dp_2": Action.CONTEST,
        "stripe:dp_3": Action.ACCEPT,
    }
    assert {x.decided_by for x in r.cases} == {"partner-v1"}
    assert r.agent_outcomes == {"ok": 3}
    a = r.agent
    assert a is not None and a.kind == "http" and a.models == ("partner-model-1",)
    assert (a.cases, a.calls_made, a.cache_hits) == (3, 3, 0)
    assert (a.input_tokens, a.output_tokens) == (300, 30)
    assert a.latency_ms_total > 0 and a.latency_ms_p95 is not None
    assert a.cost_usd is None
    assert len(server.bodies) == 3


def test_timeout_is_its_own_outcome(server: _Server) -> None:
    server.behaviour = lambda b: (200, json.dumps(policy(b)).encode(), 1.0)
    agent = HttpAgent(url(server), timeout_s=0.1, max_retries=1, sleep=lambda _s: None)
    r = run(trio(), LLMCandidate(agent))
    assert r.agent_outcomes == {"timeout": 3}
    assert r.n_included == 0
    assert r.excluded["agent reply timeout (no decision)"] == 3
    assert len(server.bodies) == 6  # one retry each
    assert {f.outcome for f in r.agent_failures} == {"timeout"}


def test_retries_with_backoff_then_succeeds() -> None:
    calls = {"n": 0}
    waits: list[float] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, text="busy")
        return ok(json.loads(request.content))

    agent = HttpAgent(
        "https://agent.example/decide",
        transport=httpx.MockTransport(flaky),
        sleep=waits.append,
        backoff_s=0.5,
    )
    cand = LLMCandidate(agent, concurrency=1)
    [d] = cand.decide_many([("k", build_view(trio().cases[0], PaymentIndex(trio().cases)))])
    assert d.outcome == "ok" and d.action is Action.CONTEST
    assert waits == [0.5, 1.0]


def test_concurrency_is_limited() -> None:
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow(request: httpx.Request) -> httpx.Response:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.02)
        with lock:
            state["now"] -= 1
        return ok(json.loads(request.content))

    cases = [case(i, amount=1_000 + i) for i in range(1, 13)]
    h = history(cases, [m for c in cases for m in moves(c)])
    r = run(h, http_candidate(slow, concurrency=2))
    assert r.agent_outcomes == {"ok": 12}
    assert 1 <= state["peak"] <= 2


def bad_replies(body: dict[str, Any]) -> httpx.Response:
    amount = body["case"]["amount_minor"]
    if amount == 10_000 and body["case"]["opened_at"].endswith("01:00:00+00:00"):
        return httpx.Response(200, json={"action": "refund"})  # not an action
    if amount == 10_000:
        return httpx.Response(200, text="I think you should contest")  # not JSON
    return httpx.Response(400, json={"error": "bad request"})


def test_malformed_and_failed_replies_are_counted_not_defaulted() -> None:
    r = run(trio(), http_candidate(Recorder(bad_replies), max_retries=0))
    assert r.agent_outcomes == {"failed": 1, "malformed": 2}
    assert r.n_included == 0 and r.verdict == "insufficient evidence"
    assert r.excluded["agent reply malformed (no decision)"] == 2
    assert r.excluded["agent reply failed (no decision)"] == 1
    details = {f.case_id: f.detail for f in r.agent_failures}
    assert "action" in (details["stripe:dp_1"] or "")
    assert "HTTP 400" in (details["stripe:dp_3"] or "")
    md = to_markdown(r)
    assert "## Agent calls" in md and "malformed 2" in md and "left out" in md


def test_on_agent_error_escalate_and_historical() -> None:
    esc = run(
        trio(), http_candidate(Recorder(bad_replies), max_retries=0), on_agent_error="escalate"
    )
    assert esc.n_included == 3 and esc.agent_outcomes == {"failed": 1, "malformed": 2}
    assert {x.candidate_action for x in esc.cases} == {Action.ESCALATE}
    assert {x.agent_outcome for x in esc.cases} == {"failed", "malformed"}
    hist = run(
        trio(), http_candidate(Recorder(bad_replies), max_retries=0), on_agent_error="historical"
    )
    assert hist.n_estimated == 0 and hist.verdict == "no change"
    assert "kept the historical action" in to_markdown(hist)


# --- cache ------------------------------------------------------------------------------


def test_a_rerun_is_answered_from_the_cache(tmp_path: Path) -> None:
    rec = Recorder(ok)
    price = Price(input_per_mtok=3.0, output_per_mtok=15.0)
    first = run(trio(), http_candidate(rec, tmp_path, price=price))
    again = run(trio(), http_candidate(rec, tmp_path, price=price))
    assert len(rec.bodies) == 3
    a, b = first.agent, again.agent
    assert a is not None and b is not None
    assert (b.calls_made, b.cache_hits) == (0, 3) and b.cost_usd_this_run == 0
    assert b.cost_usd == a.cost_usd == pytest.approx(3 * (100 * 3.0 + 10 * 15.0) / 1e6)
    assert b.latency_ms_total == a.latency_ms_total  # recorded, so reproducible
    assert [x.candidate_action for x in first.cases] == [x.candidate_action for x in again.cases]
    assert len(list(tmp_path.glob("*.json"))) == 3
    # another agent id, or a changed view, is a new key
    other = LLMCandidate(
        HttpAgent(
            "https://agent.example/v1/decide",
            agent_id="partner-v2",
            transport=httpx.MockTransport(rec),
        ),
        cache_dir=tmp_path,
    )
    run(trio(), other)
    assert len(rec.bodies) == 6
    run(trio(), http_candidate(rec, tmp_path), decision_delay_hours=1.0)
    assert len(rec.bodies) == 9


def test_failures_are_not_cached_but_malformed_replies_are(tmp_path: Path) -> None:
    rec = Recorder(bad_replies)
    run(trio(), http_candidate(rec, tmp_path, max_retries=0))
    run(trio(), http_candidate(rec, tmp_path, max_retries=0))
    assert len(rec.bodies) == 4  # 3, then only the failed one again
    (tmp_path / "junk.json").write_text("{")
    assert len(list(tmp_path.glob("*.json"))) == 3


# --- the model runner ---------------------------------------------------------------------


class FakeAnthropic:
    def __init__(self, text: Callable[[dict[str, Any]], str], stop: str = "end_turn") -> None:
        self.text = text
        self.stop = stop
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(
            200,
            json={
                "model": "claude-test-1",
                "content": [{"type": "text", "text": self.text(body)}],
                "stop_reason": self.stop,
                "usage": {"input_tokens": 400, "output_tokens": 20},
            },
        )


def request_of(body: dict[str, Any]) -> dict[str, Any]:
    text = body["messages"][0]["content"][0]["text"]
    assert text.startswith(INSTRUCTION)
    out = read_json_object(text[len(INSTRUCTION) :])
    assert out is not None
    return out


def model_candidate(handler: Any, prompt: str = "You decide disputes.", **kw: Any) -> LLMCandidate:
    provider = make_provider("anthropic", api_key="test-key")
    agent = ModelAgent(
        provider, "claude-test", prompt, transport=httpx.MockTransport(handler), max_retries=0
    )
    return LLMCandidate(agent, **kw)


def test_model_runner_with_a_fake_provider(tmp_path: Path) -> None:
    fake = FakeAnthropic(
        lambda b: "```json\n" + json.dumps(policy(request_of(b))) + "\n```",
    )
    price = Price(input_per_mtok=1.0, output_per_mtok=5.0)
    r = run(trio(), model_candidate(fake, "PARTNER PROMPT", price=price, cache_dir=tmp_path))
    assert r.agent_outcomes == {"ok": 3}
    assert {x.candidate_action for x in r.cases} == {Action.CONTEST, Action.ACCEPT}
    assert all(b["system"] == "PARTNER PROMPT" and b["temperature"] == 0.0 for b in fake.bodies)
    for b in fake.bodies:
        AgentRequest.model_validate(request_of(b))
    a = r.agent
    assert a is not None and a.kind == "model"
    assert a.agent_id.startswith("anthropic:claude-test:prompt-")
    assert a.models == ("partner-model-1",)  # the reply's own model field wins
    assert (a.input_tokens, a.output_tokens) == (1_200, 60)
    assert a.cost_usd == pytest.approx(3 * (400 * 1.0 + 20 * 5.0) / 1e6)
    # a different prompt is a different agent: no cache reuse
    other = model_candidate(fake, "ANOTHER PROMPT", cache_dir=tmp_path)
    assert other.name != a.agent_id


def test_model_runner_records_the_api_model_id_and_bad_text() -> None:
    fake = FakeAnthropic(lambda b: "contest, probably")
    r = run(trio(), model_candidate(fake))
    assert r.agent_outcomes == {"malformed": 3}
    assert r.agent is not None and r.agent.models == ("claude-test-1",)
    assert r.agent.input_tokens == 1_200  # tokens of a malformed reply are still counted
    assert {f.detail for f in r.agent_failures} == {"no JSON object in the reply"}
    refused = run(trio(), model_candidate(FakeAnthropic(lambda b: "", stop="refusal")))
    assert {f.detail for f in refused.agent_failures} == {"the model refused"}


def test_model_runner_openai_compatible_wire() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        user = body["messages"][-1]["content"]
        req = read_json_object(user[len(INSTRUCTION) :])
        assert req is not None
        return httpx.Response(
            200,
            json={
                "model": "local-model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": json.dumps(policy(req))},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5},
            },
        )

    provider = make_provider("openai-compatible", base_url="http://llm.local/v1", api_key="x")
    agent = ModelAgent(provider, "m", "prompt", transport=httpx.MockTransport(handler))
    r = run(trio(), LLMCandidate(agent))
    assert r.agent_outcomes == {"ok": 3}
    assert seen[0]["messages"][0] == {"role": "system", "content": "prompt"}
    assert r.agent is not None and r.agent.input_tokens == 150


def test_read_json_object() -> None:
    assert read_json_object('{"action": "accept"}') == {"action": "accept"}
    assert read_json_object('Sure. {"action": "contest"} Done.') == {"action": "contest"}
    assert read_json_object("[1, 2]") is None
    assert read_json_object("no") is None


def test_a_backend_that_returns_no_reply_is_malformed() -> None:
    class Empty:
        kind = "test"
        agent_id = "empty"

        def ask(self, req: AgentRequest) -> Answer:
            return Answer(reply=None, detail="nothing")

        def close(self) -> None:
            pass

    r = run(trio(), LLMCandidate(Empty()))
    assert r.agent_outcomes == {"malformed": 3}


# --- end to end: scruple ingest, then scruple backtest with --agent-url ------------------


def test_cli_ingest_then_backtest_an_agent_endpoint(
    server: _Server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "disputes.json"
    src.write_text(json.dumps(stripe_disputes(DEFAULT_PROFILE, 60, seed=4)))
    db = tmp_path / "cases.db"
    assert main(["ingest", "stripe", "--in", str(src), "--store", str(db)]) == 0
    out = tmp_path / "report"
    args = [
        "backtest", "run", "--history", str(db), "--format", "store",
        "--agent-url", url(server), "--agent-id", "partner-v1", "--out", str(out),
        "--no-pdf", "--include-cases", "--price-in", "2", "--price-out", "8",
    ]  # fmt: skip
    assert main(args) == 0
    n = len(server.bodies)
    assert n > 0
    doc = json.loads((out / "report.json").read_text())
    assert doc["agent"]["calls_made"] == n and doc["agent"]["cost_usd"] > 0
    assert doc["candidate"] == "partner-v1"
    assert "## Agent calls" in (out / "report.md").read_text()
    capsys.readouterr()
    assert main(args) == 0
    assert len(server.bodies) == n  # all from OUT/agent-cache
    assert f"0 calls, {n} cached" in capsys.readouterr().out
    assert main([*args[:6], "--candidate", "x.json", *args[6:]]) == 2  # exactly one candidate


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no ANTHROPIC_API_KEY set")
def test_real_model_tiny_run() -> None:  # pragma: no cover - needs a key and the network
    provider = make_provider("anthropic")
    agent = ModelAgent(
        provider,
        os.environ.get("SCRUPLE_TEST_MODEL", "claude-haiku-4-5"),
        "You decide card disputes for a merchant. Contest when the merchant can win.",
    )
    r = run(trio(), LLMCandidate(agent, concurrency=1))
    assert sum(r.agent_outcomes.values()) == 3
