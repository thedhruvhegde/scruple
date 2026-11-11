"""A small read-only Stripe API client for disputes (test mode first).

- Pluggable `httpx` transport: tests pass `httpx.MockTransport`; nothing in the test
  suite makes a live call.
- Test mode by default: only `sk_test_`/`rk_test_` keys are accepted unless
  `allow_live=True`, and live-mode objects are refused by the mapper as well.
- The key is never logged and never appears in `repr`.
- Responses are size-limited before JSON decoding and validated against the wire models.

Endpoints used (from the vendored spec): GET /v1/disputes, GET /v1/disputes/{dispute}.
Pagination uses `starting_after` and `has_more` as documented there; `limit` is 1..100;
the `created` range filter (`created[gte]`, `created[lt]`) and `expand[]` are the
spec's deepObject query parameters of the list operation.

Retries (off unless `max_retries` > 0; the pull uses 5): a 429 (Stripe rate
limiting, https://docs.stripe.com/rate-limits, which advises exponential backoff), a
5xx, or a transport error is retried up to `max_retries` times, waiting
`backoff_s * 2**attempt` seconds (no jitter, so tests are deterministic), or the
`Retry-After` header's seconds when that is larger. A response carrying
`Stripe-Should-Retry: false` (https://docs.stripe.com/error-low-level) is not retried.
Any other 4xx fails at once. `sleep` is injectable for tests.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from pydantic import ValidationError

from scruple.ingest.errors import IngestError, LiveModeRefusedError, SchemaMismatchError
from scruple.ingest.limits import MAX_JSON_BYTES, load_json_object
from scruple.ingest.stripe.wire import Dispute, DisputeList

DEFAULT_BASE_URL = "https://api.stripe.com"
_TEST_KEY = re.compile(r"^(sk|rk)_test_[A-Za-z0-9]{8,255}$")
_LIVE_KEY = re.compile(r"^(sk|rk)_live_[A-Za-z0-9]{8,255}$")
_ID = re.compile(r"^[A-Za-z0-9_]{1,255}$")
# expand[] values the pull asks for: the charge carries the payment facts that rules
# read (docs/BACKTEST.md). The dispute's payment_intent is not read by the mapper.
EXPANDABLE = frozenset({"data.charge", "data.payment_intent"})


class StripeAPIError(IngestError):
    """Stripe answered with a non-2xx status (the body's error type and message are kept)."""

    def __init__(self, status: int, error_type: str | None, message: str) -> None:
        super().__init__(f"Stripe API {status}: {error_type or 'error'}: {message[:200]}")
        self.status = status
        self.error_type = error_type


class StripeClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str = DEFAULT_BASE_URL,
        allow_live: bool = False,
        timeout_s: float = 20.0,
        max_response_bytes: int = 8 * MAX_JSON_BYTES,
        max_retries: int = 0,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if _LIVE_KEY.match(api_key):
            if not allow_live:
                raise LiveModeRefusedError("live Stripe keys are refused; use an sk_test_ key")
        elif not _TEST_KEY.match(api_key):
            raise IngestError("Stripe key must look like sk_test_... (or rk_test_...)")
        self._max = max_response_bytes
        self._retries = max(0, max_retries)
        self._backoff = backoff_s
        self._sleep = sleep
        self.retries_used = 0  # total retries across calls (rate limits, 5xx, network)
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"StripeClient(base_url={str(self._http.base_url)!r}, key=<redacted>)"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> StripeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _wait(self, attempt: int, resp: httpx.Response | None) -> None:
        delay = self._backoff * (2**attempt)
        if resp is not None:
            ra = resp.headers.get("retry-after", "")
            if ra.isdigit():
                delay = max(delay, float(ra))
        self.retries_used += 1
        self._sleep(min(delay, 600.0))

    def _send(self, path: str, params: Any) -> httpx.Response:
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            try:
                resp = self._http.get(path, params=params)
            except httpx.HTTPError as e:
                if last or not isinstance(e, httpx.TransportError):
                    raise IngestError(f"Stripe request failed: {type(e).__name__}") from None
                self._wait(attempt, None)
                continue
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if resp.headers.get("stripe-should-retry", "").lower() == "false":
                retryable = False
            if not retryable or last:
                return resp
            self._wait(attempt, resp)
        raise AssertionError("unreachable")  # pragma: no cover

    def _get(self, path: str, params: Any = None) -> dict[str, Any]:
        resp = self._send(path, params)
        body = resp.content
        if len(body) > self._max:
            raise IngestError(f"Stripe response is {len(body)} bytes, limit {self._max}")
        if resp.status_code >= 300:
            try:
                parsed = load_json_object(body, max_bytes=self._max, what="Stripe error")
            except IngestError:
                parsed = {}
            raw_err = parsed.get("error")
            err: dict[str, Any] = raw_err if isinstance(raw_err, dict) else {}
            et = err.get("type")
            msg = err.get("message")
            raise StripeAPIError(
                resp.status_code,
                et if isinstance(et, str) else None,
                msg if isinstance(msg, str) else "",
            )
        return load_json_object(body, max_bytes=self._max, what="Stripe response")

    def get_dispute(self, dispute_id: str) -> Dispute:
        if not _ID.match(dispute_id):
            raise IngestError("dispute id has unexpected characters")
        obj = self._get(f"/v1/disputes/{dispute_id}")
        try:
            return Dispute.model_validate(obj)
        except ValidationError as e:
            raise SchemaMismatchError(f"bad dispute payload: {e.error_count()} errors") from None

    def list_disputes(
        self,
        *,
        limit: int = 100,
        starting_after: str | None = None,
        created_gte: int | None = None,
        created_lt: int | None = None,
        expand: tuple[str, ...] = (),
    ) -> DisputeList:
        return self.list_disputes_raw(
            limit=limit,
            starting_after=starting_after,
            created_gte=created_gte,
            created_lt=created_lt,
            expand=expand,
        )[0]

    def list_disputes_raw(
        self,
        *,
        limit: int = 100,
        starting_after: str | None = None,
        created_gte: int | None = None,
        created_lt: int | None = None,
        expand: tuple[str, ...] = (),
    ) -> tuple[DisputeList, list[dict[str, Any]]]:
        """One page, validated, plus the raw dispute objects in the same order (what an
        export file holds; the wire model drops fields scruple does not read)."""
        if not 1 <= limit <= 100:
            raise IngestError("limit must be between 1 and 100")
        params: list[tuple[str, str | int]] = [("limit", limit)]
        if starting_after is not None:
            if not _ID.match(starting_after):
                raise IngestError("starting_after has unexpected characters")
            params.append(("starting_after", starting_after))
        if created_gte is not None:
            params.append(("created[gte]", created_gte))
        if created_lt is not None:
            params.append(("created[lt]", created_lt))
        for e in expand:
            if e not in EXPANDABLE:
                raise IngestError(f"expand {e!r} is not one scruple reads")
            params.append(("expand[]", e))
        obj = self._get("/v1/disputes", params)
        try:
            page = DisputeList.model_validate(obj)
        except ValidationError as e:
            raise SchemaMismatchError(f"bad dispute list: {e.error_count()} errors") from None
        raw = [d for d in obj.get("data", []) if isinstance(d, dict)]
        if len(raw) != len(page.data):
            raise SchemaMismatchError("dispute list data is not all objects")
        return page, raw

    def iter_disputes(
        self, *, page_size: int = 100, max_pages: int = 1000, created_gte: int | None = None
    ) -> Iterator[Dispute]:
        """All disputes, newest first, following `has_more`. Bounded by `max_pages`."""
        cursor: str | None = None
        for _ in range(max_pages):
            page = self.list_disputes(
                limit=page_size, starting_after=cursor, created_gte=created_gte
            )
            yield from page.data
            if not page.has_more or not page.data:
                return
            cursor = page.data[-1].id
        raise IngestError(f"more than {max_pages} pages of disputes; raise max_pages")
