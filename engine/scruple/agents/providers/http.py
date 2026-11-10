"""HTTP for providers: retries with backoff, `Retry-After`, a shared rate limit.

Retried: 408, 409, 429, 5xx and 529 responses, and transport errors (connection
reset, timeouts). The wait is the server's `Retry-After` (seconds, capped) when it
sends one, else `backoff_s * 2**attempt`. Every wait goes through an injected
`sleep`, so tests run instantly and deterministically.

`RateLimiter` spaces requests to at most `rpm` per minute across every thread that
shares it (a batch run shares one limiter between its workers).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from scruple.agents.providers.base import ProviderHTTPError

RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
MAX_RETRY_AFTER_S = 120.0


class RateLimiter:
    """At most `rpm` requests per minute; `None` means no limit. Thread-safe."""

    def __init__(
        self,
        rpm: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rpm is not None and rpm <= 0:
            raise ValueError("rpm must be positive")
        self.rpm = rpm
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()
        self.waited_s = 0.0

    def acquire(self) -> float:
        """Block until a request may go out. Returns the seconds waited."""
        if self.rpm is None:
            return 0.0
        interval = 60.0 / self.rpm
        with self._lock:  # reserve a slot, then wait outside the lock
            now = self._clock()
            slot = max(now, self._next)
            self._next = slot + interval
            wait = slot - now
            self.waited_s += wait
        if wait > 0:
            self._sleep(wait)
        return wait


def retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        return None  # an HTTP date: fall back to backoff


@dataclass
class HttpStats:
    requests: int = 0
    retries: int = 0
    slept_s: float = 0.0


@dataclass
class ProviderClient:
    base_url: str
    headers: dict[str, str]
    transport: httpx.BaseTransport | None = None
    timeout_s: float = 120.0
    max_retries: int = 4
    backoff_s: float = 1.0
    limiter: RateLimiter | None = None
    sleep: Callable[[float], None] = time.sleep
    stats: HttpStats = field(default_factory=HttpStats)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            headers=self.headers,
            transport=self.transport,
            timeout=httpx.Timeout(self.timeout_s),
        )

    def _wait(self, seconds: float) -> None:
        self.stats.retries += 1
        self.stats.slept_s += seconds
        self.sleep(seconds)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        while True:
            if self.limiter is not None:
                self.limiter.acquire()
            self.stats.requests += 1
            try:
                resp = self._client.post(path, json=body)
            except httpx.TransportError:
                if attempt >= self.max_retries:
                    raise
                self._wait(self.backoff_s * 2.0**attempt)
                attempt += 1
                continue
            if resp.status_code < 400:
                data = resp.json()
                if not isinstance(data, dict):
                    raise ValueError("response body is not a JSON object")
                return data
            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                after = retry_after(resp)
                self._wait(after if after is not None else self.backoff_s * 2.0**attempt)
                attempt += 1
                continue
            raise ProviderHTTPError(resp.status_code, resp.text)

    def close(self) -> None:
        self._client.close()


__all__ = ["RETRY_STATUS", "HttpStats", "ProviderClient", "RateLimiter", "retry_after"]
