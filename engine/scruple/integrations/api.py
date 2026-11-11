"""`/integrations`: settings, webhook endpoints, the delivery log, replays, dispatch.

Every route is the caller's tenant's only and needs `admin`, except where noted.

    GET  /integrations/settings                              current settings
    POST /integrations/settings                              new settings version (audited)
    POST /integrations/dispatch                              one dispatcher pass now
    GET  /integrations/webhooks                              endpoints (no secrets)
    POST /integrations/webhooks                              register; the secret is shown once
    POST /integrations/webhooks/{endpoint_id}/rotate         new secret, old one overlaps 24 h
    POST /integrations/webhooks/{endpoint_id}/disable        stop deliveries
    POST /integrations/webhooks/{endpoint_id}/enable
    GET  /integrations/webhooks/deliveries                   the delivery log
    GET  /integrations/webhooks/deliveries/{delivery_id}     one delivery and its attempts
    POST /integrations/webhooks/deliveries/{delivery_id}/replay
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query

from scruple.auth import CurrentPrincipal, Permission, require
from scruple.integrations.config import IntegrationSettings
from scruple.integrations.dispatch import DispatchResult, dispatch
from scruple.integrations.ops import set_settings
from scruple.integrations.runtime import Runtime, SealError
from scruple.integrations.store import IntegrationStore
from scruple.integrations.webhooks import (
    EndpointIn,
    WebhookError,
    create_endpoint,
    replay,
    rotate_secret,
    set_active,
)
from scruple.jobs.queue import JobQueue
from scruple.service import AgentGate

ADMIN = require(Permission.ADMIN)
EndpointId = Annotated[str, Path(pattern=r"^whe_[0-9a-f]{16}$")]
DeliveryId = Annotated[str, Path(pattern=r"^whd_[0-9a-f]{24}$")]


def integrations_router(
    gate_for: Callable[[str], AgentGate],
    queue: JobQueue | None,
    runtime: Runtime | Callable[[], Runtime] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/integrations", tags=["integrations"])

    def rt() -> Runtime:
        if runtime is None:
            return Runtime.from_env()
        return runtime if isinstance(runtime, Runtime) else runtime()

    def need_queue() -> JobQueue:
        if queue is None:
            raise HTTPException(503, "this database has no job queue")
        return queue

    def store(p: CurrentPrincipal) -> tuple[AgentGate, IntegrationStore]:
        g = gate_for(p.tenant)
        return g, IntegrationStore(g.storage)

    @router.get("/settings", dependencies=[ADMIN])
    def get_settings(p: CurrentPrincipal) -> IntegrationSettings:
        return store(p)[1].settings()

    @router.post("/settings", dependencies=[ADMIN])
    def put_settings(body: IntegrationSettings, p: CurrentPrincipal) -> dict[str, Any]:
        try:
            n = set_settings(gate_for(p.tenant), body, p.subject)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"number": n, "settings": body.model_dump(mode="json")}

    @router.post("/dispatch", dependencies=[ADMIN])
    def run_dispatch(p: CurrentPrincipal) -> DispatchResult:
        return dispatch(gate_for(p.tenant), need_queue())

    @router.get("/webhooks", dependencies=[ADMIN])
    def endpoints(p: CurrentPrincipal) -> list[dict[str, Any]]:
        return [e.public() for e in store(p)[1].endpoints()]

    @router.post("/webhooks", status_code=201, dependencies=[ADMIN])
    def add_endpoint(body: EndpointIn, p: CurrentPrincipal) -> dict[str, Any]:
        try:
            ep, secret = create_endpoint(gate_for(p.tenant), rt(), body, p.subject)
        except WebhookError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SealError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {
            "endpoint": ep.public(),
            "secret": secret,
            "note": "the signing secret is shown only now; store it with the receiver",
        }

    @router.post("/webhooks/{endpoint_id}/rotate", dependencies=[ADMIN])
    def rotate(endpoint_id: EndpointId, p: CurrentPrincipal) -> dict[str, Any]:
        try:
            secret = rotate_secret(gate_for(p.tenant), rt(), endpoint_id, p.subject)
        except KeyError:
            raise HTTPException(404, "unknown endpoint") from None
        except SealError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"endpoint_id": endpoint_id, "secret": secret}

    def _toggle(endpoint_id: str, active: bool, p: CurrentPrincipal) -> dict[str, Any]:
        try:
            ep = set_active(gate_for(p.tenant), endpoint_id, active, p.subject)
        except KeyError:
            raise HTTPException(404, "unknown endpoint") from None
        return ep.public()

    @router.post("/webhooks/{endpoint_id}/disable", dependencies=[ADMIN])
    def disable(endpoint_id: EndpointId, p: CurrentPrincipal) -> dict[str, Any]:
        return _toggle(endpoint_id, False, p)

    @router.post("/webhooks/{endpoint_id}/enable", dependencies=[ADMIN])
    def enable(endpoint_id: EndpointId, p: CurrentPrincipal) -> dict[str, Any]:
        return _toggle(endpoint_id, True, p)

    @router.get("/webhooks/deliveries", dependencies=[ADMIN])
    def deliveries(
        p: CurrentPrincipal,
        endpoint_id: Annotated[str | None, Query(pattern=r"^whe_[0-9a-f]{16}$")] = None,
        status: Literal["pending", "delivered", "failed"] | None = None,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> list[dict[str, Any]]:
        rows = store(p)[1].deliveries(endpoint_id, status, limit)
        return [d.model_dump(mode="json") for d in rows]

    @router.get("/webhooks/deliveries/{delivery_id}", dependencies=[ADMIN])
    def delivery(delivery_id: DeliveryId, p: CurrentPrincipal) -> dict[str, Any]:
        s = store(p)[1]
        d = s.delivery(delivery_id)
        if d is None:
            raise HTTPException(404, "unknown delivery")
        return {
            **d.model_dump(mode="json"),
            "attempts_log": [a.model_dump(mode="json") for a in s.attempts(delivery_id)],
        }

    @router.post("/webhooks/deliveries/{delivery_id}/replay", dependencies=[ADMIN])
    def replay_delivery(delivery_id: DeliveryId, p: CurrentPrincipal) -> dict[str, Any]:
        try:
            n = replay(gate_for(p.tenant), need_queue(), delivery_id, p.subject)
        except KeyError:
            raise HTTPException(404, "unknown delivery") from None
        return {"delivery_id": delivery_id, "replay": n}

    return router


__all__ = ["integrations_router"]
