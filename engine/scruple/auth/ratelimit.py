"""Token-bucket rate limits, in memory, behind a pluggable backend.

Each caller has a bucket of `burst` tokens that refills at `per_minute / 60`
tokens a second; a request takes one token. An empty bucket answers 429 with
`Retry-After` (whole seconds until a token is back). Buckets are keyed by the
principal's `limit_key` (method, tenant, credential id), so each API key or token
subject has its own; anonymous requests share one bucket per client address.

`InMemoryBuckets` is per process: with several workers, each enforces the limit
on its own share. A shared backend (Redis, the database) implements
`RateLimitBackend.take` with the same semantics.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Limit:
    per_minute: int
    burst: int

    def __post_init__(self) -> None:
        if self.per_minute < 1 or self.burst < 1:
            raise ValueError("rate limits must be at least 1")


@dataclass(frozen=True)
class Taken:
    allowed: bool
    remaining: int
    retry_after: int
    """Seconds until a token is available (0 when allowed)."""


class RateLimitBackend(Protocol):
    def take(self, key: str, limit: Limit, cost: float = 1.0) -> Taken: ...
    def peek(self, key: str, limit: Limit) -> Taken:
        """Would `take` succeed? Does not consume a token."""
        ...


class InMemoryBuckets:
    """Thread-safe token buckets; least recently used buckets beyond `max_keys`
    are dropped (a dropped bucket comes back full, which only ever allows more)."""

    def __init__(
        self, monotonic: Callable[[], float] = time.monotonic, max_keys: int = 100_000
    ) -> None:
        self._now = monotonic
        self._max = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _level(self, key: str, limit: Limit, now: float) -> float:
        tokens, at = self._buckets.get(key, (float(limit.burst), now))
        rate = limit.per_minute / 60.0
        return min(float(limit.burst), tokens + (now - at) * rate)

    def _result(self, tokens: float, limit: Limit, cost: float, allowed: bool) -> Taken:
        if allowed:
            return Taken(True, int(tokens), 0)
        rate = limit.per_minute / 60.0
        return Taken(False, 0, max(1, math.ceil((cost - tokens) / rate)))

    def take(self, key: str, limit: Limit, cost: float = 1.0) -> Taken:
        with self._lock:
            now = self._now()
            tokens = self._level(key, limit, now)
            allowed = tokens >= cost
            if allowed:
                tokens -= cost
            self._buckets[key] = (tokens, now)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
            return self._result(tokens, limit, cost, allowed)

    def peek(self, key: str, limit: Limit) -> Taken:
        with self._lock:
            tokens = self._level(key, limit, self._now())
            return self._result(tokens, limit, 1.0, tokens >= 1.0)
