"""HTTP routes for inbound integrations, mounted by `scruple.api.app` with one line.

POST /ingest/stripe/webhook verifies the `Stripe-Signature` header over the raw body
before anything is parsed. The endpoint secret comes from the constructor or from
`SCRUPLE_STRIPE_WEBHOOK_SECRETS` (comma separated, for rotation). Without a secret
the route answers 503 rather than accepting unsigned data.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scruple.ingest.errors import IngestError, InputTooLargeError, SignatureError
from scruple.ingest.limits import MAX_JSON_BYTES
from scruple.ingest.stripe.webhook import SIGNATURE_HEADER, StripeWebhookReceiver

SECRETS_ENV = "SCRUPLE_STRIPE_WEBHOOK_SECRETS"


class WebhookAck(BaseModel):
    received: bool
    event_id: str
    duplicate: bool
    cases: int
    movements: int


def _from_env() -> StripeWebhookReceiver | None:
    raw = os.environ.get(SECRETS_ENV, "")
    secrets = [s.strip() for s in raw.split(",") if s.strip()]
    return StripeWebhookReceiver(secrets) if secrets else None


def ingest_router(stripe: StripeWebhookReceiver | None = None) -> APIRouter:
    router = APIRouter(prefix="/ingest", tags=["ingest"])
    receiver = stripe if stripe is not None else _from_env()

    @router.post("/stripe/webhook")
    async def stripe_webhook(request: Request) -> WebhookAck:
        if receiver is None:
            raise HTTPException(503, "Stripe webhook secret is not configured")
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_JSON_BYTES):
            raise HTTPException(413, "body too large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_JSON_BYTES:
                raise HTTPException(413, "body too large")
        try:
            result = receiver.handle(bytes(body), request.headers.get(SIGNATURE_HEADER))
        except SignatureError as e:
            raise HTTPException(400, f"signature rejected: {e}") from None
        except InputTooLargeError:
            raise HTTPException(413, "body too large") from None
        except IngestError as e:
            raise HTTPException(422, str(e)[:300]) from None
        batch = result.batch
        return WebhookAck(
            received=True,
            event_id=result.event_id,
            duplicate=result.duplicate,
            cases=len(batch.cases) if batch else 0,
            movements=len(batch.movements) if batch else 0,
        )

    return router
