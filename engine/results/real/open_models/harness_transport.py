"""Two provider-limit rules for the E5 drivers, the same for every model (E5 deviation 4).

`LimitTransport` wraps the logging transport:

- **HTTP 413 (request larger than the per-minute input limit).** Groq refuses a
  request whose input alone exceeds the model's tokens-per-minute limit (7,000 for
  qwen/qwen3.8-27b), so a long episode can never send its next turn. On a 413 the
  oldest tool output still shown in full is replaced by a fixed note and the
  request is sent again, until it fits or no tool output is left to elide. The
  elision is remembered for the rest of the episode, so every later request sends
  the same shortened history. The system prompt, the first observation, the
  model's own messages and the newest `KEEP_LAST` messages are never touched.
  The rule only acts on a 413: an episode that never sees one is unchanged.
- **Daily quota (HTTP 429 naming a per-day limit).** Retrying cannot help, so the
  transport raises `DailyQuota`. The driver stops and leaves the seed or item
  unrecorded, so the next run plays it from the start (pre-registered: a quota
  stop resumes when the quota allows, and is never scored).

Request headers are copied onto the resent request untouched and never read.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

ELIDED = (
    "[elided by the harness: this tool output was removed to fit the API's "
    "per-minute token limit. Call the tool again to see it.]"
)
KEEP_LAST = 4
DAILY = re.compile(
    r"tokens per day|requests per day|\(TPD\)|\(RPD\)|free-models-per-day|per-day", re.I
)


class DailyQuota(Exception):  # noqa: N818 (a stop signal, not an error to handle)
    """A per-day provider quota is spent; stop and resume later."""


class LimitTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.elided: set[int] = set()  # message indices, stable: history only grows
        self.elisions = 0

    def _apply(self, body: dict[str, Any]) -> dict[str, Any]:
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not self.elided:
            return body
        out = [
            {**m, "content": ELIDED} if i in self.elided and m.get("role") == "tool" else m
            for i, m in enumerate(msgs)
        ]
        return {**body, "messages": out}

    def _next_elidable(self, msgs: list[Any]) -> int | None:
        for i, m in enumerate(msgs[: max(0, len(msgs) - KEEP_LAST)]):
            if isinstance(m, dict) and m.get("role") == "tool" and i not in self.elided:
                return i
        return None

    def _send(self, request: httpx.Request, body: dict[str, Any] | None) -> httpx.Response:
        if body is None:
            return self.inner.handle_request(request)
        content = json.dumps(self._apply(body)).encode()
        headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"content-length"]
        sent = httpx.Request(
            request.method, request.url, headers=headers, content=content, extensions={}
        )
        return self.inner.handle_request(sent)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            raw = json.loads(request.content or b"null")
        except ValueError:
            raw = None
        body = raw if isinstance(raw, dict) else None
        while True:
            response = self._send(request, body)
            if response.status_code == 429:
                response.read()
                if DAILY.search(response.text):
                    raise DailyQuota(response.text[:300])
            if response.status_code != 413 or body is None:
                return response
            nxt = self._next_elidable(body.get("messages") or [])
            if nxt is None:
                return response
            response.close()
            self.elided.add(nxt)
            self.elisions += 1

    def close(self) -> None:
        self.inner.close()
