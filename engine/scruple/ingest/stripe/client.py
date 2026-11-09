"""A small read-only Stripe API client for disputes (test mode first).

- Pluggable `httpx` transport: tests pass `httpx.MockTransport`; nothing in the test
  suite makes a live call.
- Test mode by default: only `sk_test_`/`rk_test_` keys are accepted unless
  `allow_live=True`, and live-mode objects are refused by the mapper as well.
- The key is never logged and never appears in `repr`.
- Responses are size-limited before JSON decoding and validated against the wire models.

Endpoints used (from the vendored spec): GET /v1/disputes, GET /v1/disputes/{dispute}.
Pagination uses `starting_after` and `has_more` as documented there; `limit` is 1..100.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
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
    ) -> None:
        if _LIVE_KEY.match(api_key):
            if not allow_live:
                raise LiveModeRefusedError("live Stripe keys are refused; use an sk_test_ key")
        elif not _TEST_KEY.match(api_key):
            raise IngestError("Stripe key must look like sk_test_... (or rk_test_...)")
        self._max = max_response_bytes
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

    def _get(self, path: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise IngestError(f"Stripe request failed: {type(e).__name__}") from None
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
    ) -> DisputeList:
        if not 1 <= limit <= 100:
            raise IngestError("limit must be between 1 and 100")
        params: dict[str, str | int] = {"limit": limit}
        if starting_after is not None:
            if not _ID.match(starting_after):
                raise IngestError("starting_after has unexpected characters")
            params["starting_after"] = starting_after
        if created_gte is not None:
            params["created[gte]"] = created_gte
        obj = self._get("/v1/disputes", params)
        try:
            return DisputeList.model_validate(obj)
        except ValidationError as e:
            raise SchemaMismatchError(f"bad dispute list: {e.error_count()} errors") from None

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
