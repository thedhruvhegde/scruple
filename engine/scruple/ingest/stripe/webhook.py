"""Stripe webhook verification and dispute event ingest.

The scheme, as implemented by Stripe's own library (stripe/stripe-python,
`stripe/_webhook.py` at ee9db33, MIT) and described in Stripe's webhook docs:

- header `Stripe-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>...][,v0=<hex>]`
- signed payload is `f"{t}.{raw body}"`; `v1` is hex HMAC-SHA256 of it keyed with the
  endpoint secret (`whsec_...`, used as UTF-8 bytes, not decoded)
- several `v1` values may be present (secret rotation); any match accepts; other
  schemes such as `v0` are ignored
- constant-time comparison; default tolerance 300 seconds

Where scruple is stricter than the library (our choice): the timestamp may also not be
more than `tolerance` seconds in the *future*; the header must be well formed (exactly one
`t`, `v1` values are 64 lowercase hex characters, bounded length and count); and event
ids already seen are acknowledged but not processed twice (replay protection inside
the tolerance window, and Stripe's own redeliveries).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections import OrderedDict
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from scruple.clock import Clock, SystemClock
from scruple.ingest.errors import (
    IngestError,
    LiveModeRefusedError,
    SchemaMismatchError,
    SignatureError,
)
from scruple.ingest.limits import MAX_JSON_BYTES, check_size, load_json_object
from scruple.ingest.models import IngestBatch
from scruple.ingest.stripe.mapping import ingest_dispute
from scruple.ingest.stripe.wire import Event

DEFAULT_TOLERANCE_S = 300
MAX_HEADER_LEN = 4096
MAX_SIGNATURES = 8
SIGNATURE_HEADER = "Stripe-Signature"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGITS = re.compile(r"^[0-9]{1,12}$")
_SECRET = re.compile(r"^whsec_[A-Za-z0-9+/=_-]{8,255}$")

# Event types whose `data.object` is a dispute (from the vendored spec's enabled_events).
DISPUTE_EVENT_TYPES = frozenset(
    {
        "charge.dispute.closed",
        "charge.dispute.created",
        "charge.dispute.funds_reinstated",
        "charge.dispute.funds_withdrawn",
        "charge.dispute.updated",
    }
)


def compute_signature(secret: str, timestamp: int, payload: bytes) -> str:
    signed = str(timestamp).encode("ascii") + b"." + payload
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign_header(secret: str, timestamp: int, payload: bytes) -> str:
    """Build a `Stripe-Signature` header (for tests and local replay tools)."""
    return f"t={timestamp},v1={compute_signature(secret, timestamp, payload)}"


def parse_signature_header(header: str) -> tuple[int, tuple[str, ...]]:
    if not header:
        raise SignatureError("missing Stripe-Signature header")
    if len(header) > MAX_HEADER_LEN or not header.isascii():
        raise SignatureError("Stripe-Signature header is too long or not ASCII")
    timestamps: list[str] = []
    sigs: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep or not key:
            raise SignatureError("malformed Stripe-Signature item")
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            if not _HEX64.match(value):
                raise SignatureError("v1 signature is not 64 lowercase hex characters")
            sigs.append(value)
    if len(timestamps) != 1 or not _DIGITS.match(timestamps[0]):
        raise SignatureError("Stripe-Signature needs exactly one numeric t=")
    if not sigs:
        raise SignatureError("no v1 signature in Stripe-Signature")
    if len(sigs) > MAX_SIGNATURES:
        raise SignatureError("too many v1 signatures")
    return int(timestamps[0]), tuple(sigs)


def verify_signature(
    payload: bytes,
    header: str | None,
    secrets: Sequence[str],
    *,
    now: int,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
) -> int:
    """Raise `SignatureError` unless `header` signs `payload` recently. Returns t."""
    if not secrets:
        raise SignatureError("no webhook secret configured")
    for s in secrets:
        if not _SECRET.match(s):
            raise SignatureError("webhook secret must look like whsec_...")
    if tolerance_s <= 0:
        raise SignatureError("tolerance must be positive")
    t, sigs = parse_signature_header(header or "")
    matched = False
    for secret in secrets:
        expected = compute_signature(secret, t, payload)
        for sig in sigs:
            # Evaluate every pair: no early exit that would leak which one matched.
            matched |= hmac.compare_digest(expected, sig)
    if not matched:
        raise SignatureError("no v1 signature matches the payload")
    if abs(now - t) > tolerance_s:
        raise SignatureError(f"timestamp {t} is outside the {tolerance_s}s tolerance")
    return t


class WebhookResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: str
    duplicate: bool
    batch: IngestBatch | None  # None for event types scruple does not ingest


class StripeWebhookReceiver:
    """Verifies, de-duplicates and maps Stripe webhook deliveries.

    Accepted batches are kept in memory (`received`), bounded. Persisting them is the
    caller's job: nothing here writes to scruple's storage (see CLAUDE.md).
    """

    def __init__(
        self,
        secrets: Sequence[str],
        *,
        clock: Clock | None = None,
        tolerance_s: int = DEFAULT_TOLERANCE_S,
        allow_live: bool = False,
        max_body_bytes: int = MAX_JSON_BYTES,
        remember: int = 10_000,
    ) -> None:
        self._secrets = tuple(secrets)
        self._clock = clock or SystemClock()
        self._tolerance = tolerance_s
        self._allow_live = allow_live
        self._max_body = max_body_bytes
        self._remember = remember
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.received: list[WebhookResult] = []

    def handle(self, payload: bytes, header: str | None) -> WebhookResult:
        check_size(payload, self._max_body, "webhook body")
        now = int(self._clock.now().timestamp())
        verify_signature(payload, header, self._secrets, now=now, tolerance_s=self._tolerance)
        obj = load_json_object(payload, max_bytes=self._max_body, what="webhook body")
        try:
            event = Event.model_validate(obj)
        except ValidationError as e:
            raise SchemaMismatchError(f"not a Stripe event: {e.error_count()} errors") from None
        if event.id in self._seen:
            self._seen.move_to_end(event.id)
            return WebhookResult(
                event_id=event.id, event_type=event.type, duplicate=True, batch=None
            )
        batch = None
        if event.type in DISPUTE_EVENT_TYPES:
            if event.livemode and not self._allow_live:
                raise LiveModeRefusedError(f"event {event.id} is live mode")
            batch = ingest_dispute(event.data.object, allow_live=self._allow_live)
        # Only remember an id once it was processed, so a failed mapping can be retried.
        self._seen[event.id] = None
        if len(self._seen) > self._remember:
            self._seen.popitem(last=False)
        result = WebhookResult(
            event_id=event.id, event_type=event.type, duplicate=False, batch=batch
        )
        if batch is not None:
            self.received.append(result)
            del self.received[: -self._remember]
        return result


__all__ = [
    "DISPUTE_EVENT_TYPES",
    "SIGNATURE_HEADER",
    "IngestError",
    "StripeWebhookReceiver",
    "WebhookResult",
    "compute_signature",
    "parse_signature_header",
    "sign_header",
    "verify_signature",
]
