"""Stripe usage-based billing: send usage totals as billing meter events.

Endpoint (vendored spec, tests/fixtures/stripe_billing/spec3.billing.json, API version
2026-09-30.endive): `POST /v1/billing/meter_events`, form-encoded, with `event_name`
(the meter's event name in Stripe), `payload[<customer key>]` (default
`stripe_customer_id`), `payload[<value key>]` (default `value`, a string),
`identifier` (deduplicated by Stripe for at least 24 hours) and `timestamp` (Unix
seconds, within the past 35 days or up to 5 minutes ahead). The response is a
`billing.meter_event`. Every request also carries an `Idempotency-Key` header
(https://docs.stripe.com/api/idempotent_requests) equal to the identifier, and the
`Stripe-Version` the vendored spec describes.

A subscription bills these events through a metered price on a Stripe meter with the
same event name; creating the meter, the price and the subscription is done in Stripe,
not here. Which tenant is which Stripe customer, and which scruple meter is which
Stripe event name, is configuration (`BillingConfig`).

Test mode by default: only `sk_test_` / `rk_test_` keys are accepted unless
`allow_live=True`, and a response that says `livemode: true` while in test mode is an
error. The transport is injectable (`httpx.MockTransport` in tests); the test suite
makes no network call. The key is never logged and never in `repr`.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

DEFAULT_BASE_URL = "https://api.stripe.com"
STRIPE_VERSION = "2026-09-30.endive"
METER_EVENTS = "/v1/billing/meter_events"
MAX_RESPONSE_BYTES = 64 * 1024
_TEST_KEY = re.compile(r"^(sk|rk)_test_[A-Za-z0-9]{8,255}$")
_LIVE_KEY = re.compile(r"^(sk|rk)_live_[A-Za-z0-9]{8,255}$")
_EVENT_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_CUSTOMER = re.compile(r"^cus_[A-Za-z0-9]{1,250}$")
_PAYLOAD_KEY = re.compile(r"^[a-z0-9_]{1,40}$")


class BillingError(ValueError):
    pass


class LiveModeRefusedError(BillingError):
    pass


class StripeBillingAPIError(BillingError):
    def __init__(self, status: int, error_type: str | None, message: str) -> None:
        super().__init__(f"Stripe API {status}: {error_type or 'error'}: {message[:200]}")
        self.status = status
        self.error_type = error_type

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


class MeterEvent(BaseModel):
    """`billing.meter_event` as scruple reads it (required fields of the vendored schema)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    object: Literal["billing.meter_event"]
    created: StrictInt
    event_name: str = Field(max_length=100)
    identifier: str = Field(max_length=5000)
    livemode: StrictBool
    payload: dict[str, str]
    timestamp: StrictInt


class StripeBillingClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        base_url: str = DEFAULT_BASE_URL,
        allow_live: bool = False,
        timeout_s: float = 20.0,
    ) -> None:
        if _LIVE_KEY.match(api_key):
            if not allow_live:
                raise LiveModeRefusedError("live Stripe keys are refused; use an sk_test_ key")
            self.livemode = True
        elif _TEST_KEY.match(api_key):
            self.livemode = False
        else:
            raise BillingError("Stripe key must look like sk_test_... (or rk_test_...)")
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Stripe-Version": STRIPE_VERSION,
            },
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"StripeBillingClient(base_url={str(self._http.base_url)!r}, key=<redacted>)"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> StripeBillingClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_meter_event(
        self,
        *,
        event_name: str,
        customer: str,
        value: int,
        identifier: str,
        timestamp: int,
        customer_key: str = "stripe_customer_id",
        value_key: str = "value",
    ) -> MeterEvent:
        if not _EVENT_NAME.match(event_name):
            raise BillingError("event_name has unexpected characters or length")
        if not _CUSTOMER.match(customer):
            raise BillingError("customer must be a Stripe customer id (cus_...)")
        if not (_PAYLOAD_KEY.match(customer_key) and _PAYLOAD_KEY.match(value_key)):
            raise BillingError("payload keys must be short snake_case names")
        if not 1 <= len(identifier) <= 100:
            raise BillingError("identifier must be 1 to 100 characters")
        if value <= 0:
            raise BillingError("a meter event must carry a positive value")
        form = {
            "event_name": event_name,
            "identifier": identifier,
            "timestamp": str(timestamp),
            f"payload[{customer_key}]": customer,
            f"payload[{value_key}]": str(value),
        }
        try:
            resp = self._http.post(METER_EVENTS, data=form, headers={"Idempotency-Key": identifier})
        except httpx.HTTPError as e:
            raise StripeBillingAPIError(0, "network", type(e).__name__) from None
        body = _json(resp)
        if resp.status_code >= 300:
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            assert isinstance(err, dict)
            et, msg = err.get("type"), err.get("message")
            raise StripeBillingAPIError(
                resp.status_code,
                et if isinstance(et, str) else None,
                msg if isinstance(msg, str) else "",
            )
        try:
            event = MeterEvent.model_validate(body)
        except ValidationError as e:
            raise BillingError(f"bad meter event response: {e.error_count()} errors") from None
        if event.livemode and not self.livemode:
            raise LiveModeRefusedError("Stripe answered in live mode to a test-mode key")
        if event.identifier != identifier or event.event_name != event_name:
            raise BillingError("Stripe answered for a different meter event")
        return event


def _json(resp: httpx.Response) -> dict[str, Any]:
    import json

    if len(resp.content) > MAX_RESPONSE_BYTES:
        raise BillingError(f"Stripe response over {MAX_RESPONSE_BYTES} bytes")
    try:
        data = json.loads(resp.content or b"{}")
    except ValueError:
        if resp.status_code >= 300:
            return {}
        raise BillingError("Stripe response is not JSON") from None
    if not isinstance(data, dict):
        raise BillingError("Stripe response is not a JSON object")
    return data


__all__ = [
    "METER_EVENTS",
    "STRIPE_VERSION",
    "BillingError",
    "LiveModeRefusedError",
    "MeterEvent",
    "StripeBillingAPIError",
    "StripeBillingClient",
]
