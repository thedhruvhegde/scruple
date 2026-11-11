"""The E5 drivers' provider-limit rules: elide old tool outputs on 413, stop on daily quota."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

PATH = Path(__file__).parents[1] / "results/real/open_models/harness_transport.py"


def _mod() -> Any:
    spec = importlib.util.spec_from_file_location("harness_transport", PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HT = _mod()


def _body(n_tools: int) -> dict[str, Any]:
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "observation"},
    ]
    for i in range(n_tools):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 1000})
    return {"model": "m", "messages": msgs}


def _sent(limit: int) -> tuple[list[dict[str, Any]], httpx.MockTransport]:
    sent: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        sent.append(body)
        if len(req.content) > limit:
            return httpx.Response(413, json={"error": {"message": "Request too large"}})
        return httpx.Response(200, json={"ok": True})

    return sent, httpx.MockTransport(handler)


def _post(t: httpx.BaseTransport, body: dict[str, Any]) -> httpx.Response:
    with httpx.Client(transport=t, base_url="https://api.example") as c:
        return c.post("/v1/chat", json=body, headers={"authorization": "Bearer x"})


def test_no_413_leaves_requests_untouched() -> None:
    sent, inner = _sent(10**9)
    t = HT.LimitTransport(inner)
    body = _body(5)
    assert _post(t, body).status_code == 200
    assert sent == [body]
    assert t.elisions == 0


def test_413_elides_oldest_tool_outputs_until_it_fits_and_remembers() -> None:
    body = _body(6)
    size = len(json.dumps(body))
    sent, inner = _sent(size - 1500)
    t = HT.LimitTransport(inner)
    assert _post(t, body).status_code == 200
    assert t.elisions == 2
    last = sent[-1]["messages"]
    tools = [m for m in last if m["role"] == "tool"]
    assert [m["content"] == HT.ELIDED for m in tools] == [True, True, False, False, False, False]
    assert last[0] == body["messages"][0]
    assert last[1] == body["messages"][1]
    assert all(m.get("tool_call_id") for m in tools)
    # the next, longer request starts from the same elisions
    n = len(sent)
    longer = _body(7)
    _post(t, longer)
    assert [m["content"] == HT.ELIDED for m in sent[n]["messages"] if m["role"] == "tool"][:2] == [
        True,
        True,
    ]


def test_413_with_nothing_left_to_elide_returns_the_413() -> None:
    sent, inner = _sent(10)
    t = HT.LimitTransport(inner)
    assert _post(t, _body(3)).status_code == 413
    kept = [m for m in sent[-1]["messages"][-HT.KEEP_LAST :] if m["role"] == "tool"]
    assert all(m["content"] != HT.ELIDED for m in kept)


@pytest.mark.parametrize(
    ("message", "daily"),
    [
        ("Rate limit reached on tokens per day (TPD): Limit 200000", True),
        ("Rate limit exceeded: free-models-per-day", True),
        ("Rate limit reached on input tokens per minute (ITPM): Limit 7000", False),
    ],
)
def test_429_daily_quota_raises_minute_limit_passes(message: str, daily: bool) -> None:
    inner = httpx.MockTransport(lambda r: httpx.Response(429, json={"error": {"message": message}}))
    t = HT.LimitTransport(inner)
    if daily:
        with pytest.raises(HT.DailyQuota):
            _post(t, _body(1))
    else:
        assert _post(t, _body(1)).status_code == 429
