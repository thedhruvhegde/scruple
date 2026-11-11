"""HTTP routes for inbound integrations, mounted by `scruple.api.app` with one line.

POST /ingest/stripe/webhook verifies the `Stripe-Signature` header over the raw body
before anything is parsed. The endpoint secret comes from the constructor or from
`SCRUPLE_STRIPE_WEBHOOK_SECRETS` (comma separated, for rotation). Without a secret
the route answers 503 rather than accepting unsigned data.

POST /ingest/adyen/webhook verifies every notification item's `hmacSignature` with
the keys in `SCRUPLE_ADYEN_HMAC_KEYS` (comma separated hex, for rotation); one bad
item rejects the delivery. Without a key the route answers 503.

Tenancy: webhooks authenticate by the provider's signature, not an API key, so the
signing secret decides the tenant. Each entry of `SCRUPLE_STRIPE_WEBHOOK_SECRETS` and
`SCRUPLE_ADYEN_HMAC_KEYS` is either a bare secret, which belongs to the configured
tenant (`tenant_id`, or `SCRUPLE_INGEST_TENANT`, default "default"), or
`tenant=secret`, which belongs to that tenant (`bank-a=whsec_...,bank-b=whsec_...`).
A secret may belong to one tenant only. A Stripe delivery is written for the tenant
whose secret signed it; an Adyen delivery for the tenant whose keys verify every item
(items signed by two tenants' keys reject the delivery). A case store is one tenant's
file, so with persistence every tenant needs its own: `SCRUPLE_CASE_STORE` is the
configured tenant's, `SCRUPLE_CASE_STORES` (`bank-a=/data/a.db,bank-b=/data/b.db`)
names the others, and a tenant with secrets but no store refuses to start. One
tenant's delivery is never written to, or deduplicated against, another's store.

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
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from scruple.api.strict import RAW_BODY
from scruple.auth import signed
from scruple.ingest.adyen import AdyenResult, ingest_notification
from scruple.ingest.errors import IngestError, InputTooLargeError, SignatureError
from scruple.ingest.limits import MAX_JSON_BYTES
from scruple.ingest.models import IngestBatch
from scruple.ingest.store import DEFAULT_TENANT, CaseStore, RawPanRefusedError, StoreError
from scruple.ingest.stripe.webhook import (
    SIGNATURE_HEADER,
    StripeWebhookReceiver,
    signing_tenant,
)
from scruple.storage.sql import TENANT_PATTERN

SECRETS_ENV = "SCRUPLE_STRIPE_WEBHOOK_SECRETS"
ADYEN_KEYS_ENV = "SCRUPLE_ADYEN_HMAC_KEYS"
CASE_STORE_ENV = "SCRUPLE_CASE_STORE"
CASE_STORES_ENV = "SCRUPLE_CASE_STORES"
TENANT_ENV = "SCRUPLE_INGEST_TENANT"


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


def _check_tenant(tenant: str, what: str) -> str:
    if not re.match(TENANT_PATTERN, tenant):
        raise ValueError(f"invalid tenant {tenant!r} in {what}")
    return tenant


def by_tenant(entries: Sequence[str], default: str, what: str) -> dict[str, list[str]]:
    """`secret` or `tenant=secret` entries -> {tenant: [secrets]}. A Stripe secret
    starts with `whsec_` and an Adyen key is hex, so neither is mistaken for a prefix."""
    out: dict[str, list[str]] = {}
    owner: dict[str, str] = {}
    for raw in entries:
        tenant, sep, secret = raw.partition("=")
        if not sep or raw.startswith("whsec_"):
            tenant, secret = default, raw
        tenant, secret = _check_tenant(tenant.strip(), what), secret.strip()
        if not secret:
            raise ValueError(f"empty secret for tenant {tenant!r} in {what}")
        if owner.setdefault(secret, tenant) != tenant:
            raise ValueError(f"a secret in {what} is mapped to two tenants")
        if secret not in out.setdefault(tenant, []):
            out[tenant].append(secret)
    return out


def _stores_from_env(default: str) -> dict[str, CaseStore]:
    out: dict[str, CaseStore] = {}
    path = os.environ.get(CASE_STORE_ENV, "").strip()
    if path:
        out[default] = CaseStore(path)
    for tenant, paths in by_tenant(_split(CASE_STORES_ENV), default, CASE_STORES_ENV).items():
        if len(paths) != 1 or tenant in out:
            raise ValueError(f"tenant {tenant!r} has more than one case store")
        out[tenant] = CaseStore(paths[0])
    return out


def _store_key(store: CaseStore) -> str:
    return str(Path(store.path).resolve()) if str(store.path) != ":memory:" else str(id(store))


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
    tenant_id: str | None = None,
    synthetic: bool = False,
    stripe_by_tenant: Mapping[str, StripeWebhookReceiver] | None = None,
    adyen_keys_by_tenant: Mapping[str, Sequence[str]] | None = None,
    stores: Mapping[str, CaseStore] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/ingest", tags=["ingest"])
    tenant_id = tenant_id or os.environ.get(TENANT_ENV, "").strip() or DEFAULT_TENANT
    if not re.match(TENANT_PATTERN, tenant_id):
        raise ValueError(f"invalid {TENANT_ENV} {tenant_id!r}")
    # The signing secret identifies the sender and so the tenant (module docstring).
    receivers: dict[str, StripeWebhookReceiver]
    if stripe_by_tenant is not None:
        receivers = dict(stripe_by_tenant)
    elif stripe is not None:
        receivers = {tenant_id: stripe}
    else:
        receivers = {
            t: StripeWebhookReceiver(secrets)
            for t, secrets in by_tenant(_split(SECRETS_ENV), tenant_id, SECRETS_ENV).items()
        }
    by_tenant(  # refuses a secret shared by two tenants, and bad tenant ids
        [f"{t}={sec}" for t, r in receivers.items() for sec in r.secrets], tenant_id, "stripe"
    )
    adyen: dict[str, list[str]]
    if adyen_keys_by_tenant is not None:
        adyen = {t: list(k) for t, k in adyen_keys_by_tenant.items() if k}
        by_tenant([f"{t}={k}" for t, ks in adyen.items() for k in ks], tenant_id, "adyen")
    elif adyen_keys is not None:
        adyen = {tenant_id: list(adyen_keys)} if adyen_keys else {}
    else:
        adyen = by_tenant(_split(ADYEN_KEYS_ENV), tenant_id, ADYEN_KEYS_ENV)
    if stores is not None:
        case_stores = dict(stores)
    elif store is not None:
        case_stores = {tenant_id: store}
    else:
        case_stores = _stores_from_env(tenant_id)
    if case_stores:  # persistence on: every tenant that can sign needs its own file
        missing = sorted((set(receivers) | set(adyen)) - set(case_stores))
        if missing:
            raise ValueError(
                f"tenant(s) {', '.join(missing)} have webhook secrets but no case store "
                f"(a case store is one tenant's file; see {CASE_STORES_ENV})"
            )
        keys_seen = [_store_key(cs) for cs in case_stores.values()]
        if len(set(keys_seen)) != len(keys_seen):
            raise ValueError("two tenants share one case store; each needs its own file")
    stripe_secrets = {t: r.secrets for t, r in receivers.items()}

    def stripe_receiver(body: bytes, header: str | None) -> tuple[str, StripeWebhookReceiver]:
        if len(receivers) == 1:
            [(t, r)] = receivers.items()
            return t, r
        t = signing_tenant(body, header, stripe_secrets)
        return t, receivers[t]

    def adyen_tenant(body: bytes) -> tuple[str, AdyenResult]:
        error: SignatureError | None = None
        for t in sorted(adyen):
            try:
                return t, ingest_notification(body, adyen[t])
            except SignatureError as e:  # not this tenant's keys; try the next
                error = error or e
        assert error is not None
        raise error

    @router.post(
        "/stripe/webhook", openapi_extra={RAW_BODY: True}, dependencies=[signed("stripe-signature")]
    )
    async def stripe_webhook(request: Request) -> WebhookAck:
        if not receivers:
            raise HTTPException(503, "Stripe webhook secret is not configured")
        body = await _body(request)
        header = request.headers.get(SIGNATURE_HEADER)
        try:
            tenant, receiver = stripe_receiver(body, header)
            result = receiver.handle(body, header)
        except SignatureError as e:
            raise HTTPException(400, f"signature rejected: {e}") from None
        except InputTooLargeError:
            raise HTTPException(413, "body too large") from None
        except IngestError as e:
            raise HTTPException(422, str(e)[:300]) from None
        batch = result.batch
        duplicate, stored, stale = result.duplicate, False, 0
        cases = case_stores.get(tenant)
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
                    tenant_id=tenant,
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

    @router.post(
        "/adyen/webhook", openapi_extra={RAW_BODY: True}, dependencies=[signed("adyen-hmac")]
    )
    async def adyen_webhook(request: Request) -> AdyenAck:
        if not adyen:
            raise HTTPException(503, "Adyen HMAC key is not configured")
        body = await _body(request)
        try:
            tenant, res = adyen_tenant(body)
        except SignatureError as e:
            raise HTTPException(400, f"signature rejected: {e}") from None
        except InputTooLargeError:
            raise HTTPException(413, "body too large") from None
        except IngestError as e:
            raise HTTPException(422, str(e)[:300]) from None
        dupes = stale = 0
        cases = case_stores.get(tenant)
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
                        tenant_id=tenant,
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
