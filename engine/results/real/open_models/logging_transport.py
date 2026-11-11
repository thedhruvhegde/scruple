"""An httpx transport that records request and response BODIES, never headers.

Used by the E5 drivers to keep raw transcripts of real API runs. Each request body
is reduced to the messages added since the previous request when that request is
a prefix of this one (the full history is resent every turn), so the log reads as
a transcript: what the harness sent, what the model answered. Credentials live
in headers, which are never read here; the proxy in front of the API injects them
anyway. Account ids in error messages are redacted.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
from pathlib import Path
from typing import Any

import httpx


def env_transport() -> httpx.HTTPTransport:
    """An HTTP transport that honours HTTPS_PROXY and SSL_CERT_FILE, as httpx.Client does."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=ca) if ca else True
    return httpx.HTTPTransport(proxy=proxy, verify=verify)


ORG_ID = re.compile(r"org_[A-Za-z0-9]+")  # account ids appear in 429 messages


class LoggingTransport(httpx.BaseTransport):
    def __init__(self, path: Path, inner: httpx.BaseTransport | None = None) -> None:
        self.path = path
        self.inner = inner or env_transport()
        self._prev: list[Any] = []
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def _entry(self, request: httpx.Request, response: httpx.Response) -> dict[str, Any]:
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {"unparsed": True}
        entry: dict[str, Any] = {"url_path": request.url.path, "status": response.status_code}
        messages = body.get("messages") if isinstance(body, dict) else None
        if isinstance(messages, list):
            n = len(self._prev)
            same = n <= len(messages) and messages[:n] == self._prev
            entry["new_messages"] = messages[n:] if same else messages
            entry["n_messages"] = len(messages)
            entry["request"] = {k: v for k, v in body.items() if k not in ("messages", "tools")}
            entry["tools_sent"] = [t["function"]["name"] for t in body.get("tools") or []]
            self._prev = messages
        try:
            entry["response"] = json.loads(response.content)
        except ValueError:
            entry["response_text"] = response.text[:2000]
        return entry

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        response.read()
        with self._lock, self.path.open("a") as f:
            line = json.dumps(self._entry(request, response), sort_keys=True)
            f.write(ORG_ID.sub("org_REDACTED", line) + "\n")
        return response

    def close(self) -> None:
        self.inner.close()
