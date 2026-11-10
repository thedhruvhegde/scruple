"""HTTP routes for inbound integrations, mounted by `scruple.api.app` with one line.

POST /ingest/stripe/webhook verifies the `Stripe-Signature` header over the raw body
before anything is parsed. The endpoint secret comes from the constructor or from
`SCRUPLE_STRIPE_WEBHOOK_SECRETS` (comma separated, for rotation). Without a secret
the route answers 503 rather than accepting unsigned data.

POST /ingest/adyen/webhook verifies every notification item's `hmacSignature` with
the keys in `SCRUPLE_ADYEN_HMAC_KEYS` (comma separated hex, for rotation); one bad
item rejects the delivery. Without a key the route answers 503.

Persistence: with a case store (the constructor, or `SCRUPLE_CASE_STORE`), every
accepted delivery is written to it (`CaseStore.write_webhook`): idempotent by event
id, so a redelivery after a restart is acknowledged as a duplicate and stores nothing;
safe against out-of-order delivery. If the write fails the route answers 503 and the
event is not remembered, so the provider's retry is processed. Without a store the
routes verify and acknowledge only (`stored: false`).

Adyen event ids. Adyen's notification items carry no event id field, so an item is
identified by `pspReference`, `eventCode` and `eventDate` together. ASSUMPTION: that
triple is unique per notification and identical on redelivery; it is scruple's
choice, not a key Adyen documents.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scruple.api.strict import RAW_BODY
from scruple.ingest.adyen import ingest_notification
from scruple.ingest.errors import IngestError, InputTooLargeError, SignatureError
from scruple.ingest.limits import MAX_JSON_BYTES
from scruple.ingest.models import IngestBatch
from scruple.ingest.store import DEFAULT_TENANT, CaseStore, RawPanRefusedError, StoreError
from scruple.ingest.stripe.webhook import SIGNATURE_HEADER, StripeWebhookReceiver

SECRETS_ENV = "SCRUPLE_STRIPE_WEBHOOK_SECRETS"
ADYEN_KEYS_ENV = "SCRUPLE_ADYEN_HMAC_KEYS"
CASE_STORE_ENV = "SCRUPLE_CASE_STORE"


class WebhookAck(BaseModel):
    received: bool
    event_id: str
    duplicate: bool
    cases: int
    movements: int
    stored: bool = False  # written to the case store
    stale: int = 0  # cases kept but not applied: a newer event was already stored


class AdyenAck(BaseModel):
    received: bool
    cases: int
    skipped: int
    duplicates: int
    stale: int
    stored: bool


def _split(env: str) -> list[str]:
    return [s.strip() for s in os.environ.get(env, "").split(",") if s.strip()]


def _from_env() -> StripeWebhookReceiver | None:
    secrets = _split(SECRETS_ENV)
    return StripeWebhookReceiver(secrets) if secrets else None


def _store_from_env() -> CaseStore | None:
    path = os.environ.get(CASE_STORE_ENV, "").strip()
    return CaseStore(path) if path else None


async def _body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_JSON_BYTES):
        raise HTTPException(413, "body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_JSON_BYTES:
            raise HTTPException(413, "body too large")
    return bytes(body)


def ingest_router(
    stripe: StripeWebhookReceiver | None = None,
    *,
    store: CaseStore | None = None,
    adyen_keys: list[str] | None = None,
    tenant_id: str = DEFAULT_TENANT,
    synthetic: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/ingest", tags=["ingest"])
    receiver = stripe if stripe is not None else _from_env()
    cases = store if store is not None else _store_from_env()
    keys = adyen_keys if adyen_keys is not None else _split(ADYEN_KEYS_ENV)

    @router.post("/stripe/webhook", openapi_extra={RAW_BODY: True})
    async def stripe_webhook(request: Request) -> WebhookAck:
        if receiver is None:
            raise HTTPException(503, "Stripe webhook secret is not configured")
        body = await _body(request)
        try:
            result = receiver.handle(body, request.headers.get(SIGNATURE_HEADER))
        except SignatureError as e:
            raise HTTPException(400, f"signature rejected: {e}") from None
        except InputTooLargeError:
            raise HTTPException(413, "body too large") from None
        except IngestError as e:
            raise HTTPException(422, str(e)[:300]) from None
        batch = result.batch
        duplicate, stored, stale = result.duplicate, False, 0
        if batch is not None and cases is not None:
            when = datetime.fromtimestamp(result.created or 0, UTC)
            try:
                w = cases.write_webhook(
                    source="stripe",
                    event_id=result.event_id,
                    event_type=result.event_type,
                    event_at=when,
                    payload_sha256=hashlib.sha256(body).hexdigest(),
                    batch=batch,
                    tenant_id=tenant_id,
                    synthetic=synthetic,
                )
            except (StoreError, RawPanRefusedError, OSError) as e:
                receiver.forget(result.event_id)  # let the provider's retry through
                raise HTTPException(503, f"could not store the event: {str(e)[:200]}") from None
            duplicate, stored, stale = w.duplicate, not w.duplicate, w.stale
        return WebhookAck(
            received=True,
            event_id=result.event_id,
            duplicate=duplicate,
            cases=len(batch.cases) if batch else 0,
            movements=len(batch.movements) if batch else 0,
            stored=stored,
            stale=stale,
        )

    @router.post("/adyen/webhook", openapi_extra={RAW_BODY: True})
    async def adyen_webhook(request: Request) -> AdyenAck:
        if not keys:
            raise HTTPException(503, "Adyen HMAC key is not configured")
        body = await _body(request)
        try:
            res = ingest_notification(body, keys)
        except SignatureError as e:
            raise HTTPException(400, f"signature rejected: {e}") from None
        except InputTooLargeError:
            raise HTTPException(413, "body too large") from None
        except IngestError as e:
            raise HTTPException(422, str(e)[:300]) from None
        dupes = stale = 0
        if cases is not None:
            for case in res.cases:
                event_id = f"{case.external_id}:{case.source_reason}:{case.opened_at.isoformat()}"
                item = hashlib.sha256(
                    case.model_dump_json().encode() + hashlib.sha256(body).digest()
                ).hexdigest()
                try:
                    w = cases.write_webhook(
                        source="adyen",
                        event_id=event_id,
                        event_type=case.source_reason,
                        event_at=case.opened_at,
                        payload_sha256=item,
                        batch=IngestBatch(cases=(case,)),
                        tenant_id=tenant_id,
                        synthetic=synthetic,
                    )
                except (StoreError, RawPanRefusedError, OSError) as e:
                    raise HTTPException(503, f"could not store the event: {str(e)[:200]}") from None
                dupes += w.duplicate
                stale += w.stale
        return AdyenAck(
            received=True,
            cases=len(res.cases),
            skipped=len(res.skipped),
            duplicates=dupes,
            stale=stale,
            stored=cases is not None,
        )

    return router
