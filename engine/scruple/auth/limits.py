"""Per-caller request limits: rate (token buckets) and request body size.

- Each authenticated caller has a bucket keyed by its credential: an API key's own
  `rate_per_minute` / `burst` when set, else the default. Anonymous requests share
  one bucket per client address.
- Refused credentials are counted per client address in a separate, small bucket.
  Once it is empty, requests that carry credentials from that address get 429
  before any key is hashed, so guessing keys (or making the server pay scrypt for
  garbage) is throttled.
- Bodies over the caller's limit (the key's `max_body_bytes`, else the default)
  get 413: at once when `Content-Length` says so, else as soon as the streamed
  body passes the limit.
- Development-only `X-Actor` principals are not rate limited (the synthetic demo
  and test suites drive them hard); their bodies are still size limited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scruple.auth.ratelimit import InMemoryBuckets, Limit, RateLimitBackend

if TYPE_CHECKING:
    from scruple.settings import Settings


DEFAULT = Limit(per_minute=600, burst=100)
ANONYMOUS = Limit(per_minute=120, burst=30)
AUTH_FAILURES = Limit(per_minute=30, burst=10)


@dataclass(frozen=True)
class Limits:
    default: Limit = DEFAULT
    anonymous: Limit = ANONYMOUS
    auth_failures: Limit = AUTH_FAILURES
    max_body_bytes: int = 2 * 1024 * 1024
    backend: RateLimitBackend = field(default_factory=InMemoryBuckets)

    @classmethod
    def from_settings(cls, s: Settings, backend: RateLimitBackend | None = None) -> Limits:
        return cls(
            default=Limit(s.rate_limit_per_minute, s.rate_limit_burst),
            anonymous=Limit(s.rate_limit_anonymous_per_minute, s.rate_limit_anonymous_burst),
            auth_failures=Limit(
                s.rate_limit_auth_failures_per_minute, s.rate_limit_auth_failures_burst
            ),
            max_body_bytes=s.max_body_bytes,
            backend=backend or InMemoryBuckets(),
        )
