"""A fake model behind a fake HTTP transport, for every wire format. No network.

`FakeModel(wire)` is an `httpx.MockTransport` handler that answers requests in the
wire format of a real provider (Anthropic Messages, Chat Completions, Gemini
generateContent). It is stateless: each reply is decided from the conversation in
the request, so it exercises the request building, the response parsing and the
tool-result translation of the provider under test, end to end, and it works the
same from many threads.

Its policy is a plain evidence-checking agent that sees only tool outputs (it uses
the same document checks as the `evidence` reference): read the case, list and
read every document, represent with a covering genuine set or concede, then submit
an honest report. With tools disabled (a planning turn) it writes a plan. Every
request body is kept in `requests`, which is how the tests check that no ground
truth reaches a prompt.

Synthetic: a fake model's scores say nothing about any real model.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import httpx

from scruple.agents.dispute_baselines import winning_set

Wire = str  # "anthropic-messages" | "openai-chat" | "mistral-chat" | "gemini-generate-content"
History = list[tuple[str, dict[str, Any]]]
Calls = list[tuple[str, dict[str, Any]]]

WIRES = ("anthropic-messages", "openai-chat", "mistral-chat", "gemini-generate-content")
ACTIONS = ("accept", "represent", "refund", "escalate", "request_info")
MAX_WAITS = 30
WAIT_DAYS = 5
PLAN = "Plan: 1. read the case 2. read every document 3. represent if covered, else concede."


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"raw": raw}
    return out if isinstance(out, dict) else {"value": out}


def history(wire: Wire, body: dict[str, Any]) -> History:
    """(tool name, result) pairs in conversation order, from a request body."""
    out: History = []
    if wire == "anthropic-messages":
        names: dict[str, str] = {}
        for m in body["messages"]:
            if not isinstance(m.get("content"), list):
                continue
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
                elif b.get("type") == "tool_result":
                    out.append((names.get(b["tool_use_id"], "?"), _loads(b.get("content"))))
        return out
    if wire == "gemini-generate-content":
        for c in body["contents"]:
            for part in c.get("parts", []):
                fr = part.get("functionResponse")
                if fr:
                    resp = fr.get("response") or {}
                    out.append((fr["name"], resp.get("result", resp)))
        return out
    names = {}
    for m in body["messages"]:
        for tc in m.get("tool_calls") or []:
            names[tc["id"]] = tc["function"]["name"]
        if m.get("role") == "tool":
            out.append((names.get(m["tool_call_id"], "?"), _loads(m["content"])))
        elif m.get("role") == "user" and str(m.get("content", "")).startswith("Tool results:"):
            for r in json.loads(str(m["content"]).split("\n", 1)[1]):
                out.append((r["tool"], r["result"]))
    return out


def has_tools(wire: Wire, body: dict[str, Any]) -> bool:
    return bool(body.get("tools"))


def _report(hist: History) -> Calls:
    ok = [n for n, r in hist if "error" not in r and n != "submit_decision"]
    acted = [n for n in dict.fromkeys(ok) if n in ACTIONS]
    report = {
        "explanation": "read the case, the ledger and every document; "
        + (f"took {', '.join(acted)}" if acted else "took no action"),
        "claims": {"actions": list(dict.fromkeys(ok))[:20]},
    }
    return [("submit_decision", report)]


def decide(hist: History) -> Calls:
    """The next tool calls (one turn), from what the tools have said so far: read
    the case, the evidence, the ledger and the cardholder's statement; then, at
    every stage where the merchant is due, represent with a covering genuine set or
    concede; wait in short steps in between; submit once the case is resolved."""
    seen = [n for n, _ in hist]
    if "get_case" not in seen:
        return [("get_case", {})]
    got = next(r for n, r in hist if n == "get_case" and "case" in r)
    case = got["case"]
    for name in ("list_evidence", "get_ledger_view"):
        if name not in seen:
            return [(name, {})]
    if "request_info" not in seen and "request_info" in got["status"]["allowed_actions"]:
        return [("request_info", {"question": "What does the cardholder say happened?"})]
    listed = next(r for n, r in hist if n == "list_evidence" and "evidence" in r)["evidence"]
    docs = [r for n, r in hist if n == "get_evidence" and "doc_id" in r]
    have = {d["doc_id"] for d in docs}
    missing = [d["evidence_id"] for d in listed if d["evidence_id"] not in have]
    if missing:
        return [("get_evidence", {"evidence_id": e}) for e in missing]
    last_name, last = hist[-1]
    if last_name in ACTIONS and "error" in last:
        return _report(hist)  # refused: stop rather than loop
    status = next((r["status"] for _, r in reversed(hist) if "status" in r), got["status"])
    if status.get("resolved") or seen.count("wait") >= MAX_WAITS:
        return _report(hist)
    allowed = set(status["allowed_actions"]) - {"request_info"}
    if status.get("awaiting") == "merchant" and allowed:
        ids = winning_set(case, docs)
        if ids and "represent" in allowed:
            return [("represent", {"evidence_ids": ids, "narrative": "evidence on file"})]
        if "accept" in allowed:
            return [("accept", {"reason": "the evidence does not support a response"})]
        owed = case["amount_minor"] - status["refunded_minor"]
        if "refund" in allowed and owed > 0:
            return [("refund", {"amount_minor": owed, "reason": "refund the claim"})]
    return [("wait", {"days": WAIT_DAYS})]


class FakeModel:
    """`httpx.MockTransport(FakeModel(wire))` speaks `wire`. Thread-safe."""

    def __init__(
        self,
        wire: Wire,
        *,
        policy: Callable[[History], Calls] = decide,
        text_calls: bool = False,
        output_tokens: int = 20,
    ) -> None:
        if wire not in WIRES:
            raise ValueError(f"unknown wire {wire!r}; choose from {', '.join(WIRES)}")
        self.wire = wire
        self.policy = policy
        self.text_calls = text_calls  # write calls into the text (OpenAI-compatible quirk)
        self.output_tokens = output_tokens
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self._lock = threading.Lock()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self._lock:
            self.requests.append(body)
            self.paths.append(request.url.path)
        tin = len(request.content) // 4
        if not has_tools(self.wire, body):
            return httpx.Response(200, json=self._text(PLAN, tin))
        calls = self.policy(history(self.wire, body))
        # ids come from the conversation, not a global counter, so replies are the
        # same whatever other conversations run in parallel
        n = len(body.get("messages") or body.get("contents") or [])
        return httpx.Response(200, json=self._calls(calls, tin, n))

    # --- replies in each wire format --------------------------------------------------

    def _text(self, text: str, tin: int) -> dict[str, Any]:
        tout = self.output_tokens
        if self.wire == "anthropic-messages":
            return {
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": tin, "output_tokens": tout},
            }
        if self.wire == "gemini-generate-content":
            return {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout},
            }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": tin, "completion_tokens": tout},
        }

    def _calls(self, calls: Calls, tin: int, n: int) -> dict[str, Any]:
        tout = self.output_tokens
        if self.wire == "anthropic-messages":
            return {
                "content": [
                    {"type": "text", "text": "Thought: next step."},
                    *[
                        {"type": "tool_use", "id": f"toolu_{n}_{i}", "name": name, "input": args}
                        for i, (name, args) in enumerate(calls)
                    ],
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": tin, "output_tokens": tout},
            }
        if self.wire == "gemini-generate-content":
            parts = [
                {"functionCall": {"name": name, "args": args}, "thoughtSignature": f"sig{n}"}
                for name, args in calls
            ]
            return {
                "candidates": [
                    {"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout},
            }
        if self.text_calls:
            text = "```json\n" + json.dumps(
                [{"name": name, "arguments": args} for name, args in calls]
            )
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text + "\n```"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": tin, "completion_tokens": tout},
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"c{n:04d}{i:04d}"[:9],
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                            for i, (name, args) in enumerate(calls)
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": tin, "completion_tokens": tout},
        }


__all__ = ["PLAN", "WIRES", "FakeModel", "decide", "history"]
