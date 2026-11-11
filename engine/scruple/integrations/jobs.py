"""Job handlers for the integrations (`scruple worker` runs them).

The kinds are not `JobKind`s: the HTTP API cannot submit them; only the dispatcher
and replays queue them. Each handler opens the job's tenant's storage from the
`JobContext` and a `Runtime` from the environment (`Runtime.from_env`). Tests build
handlers with `make_handlers(runtime=..., clock=...)` and run a thread-isolated
worker, so no request leaves the process.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from scruple.clock import Clock
from scruple.integrations.runtime import Runtime
from scruple.integrations.tickets import PROVIDERS, TICKET_KIND, sync
from scruple.integrations.webhooks import WEBHOOK_KIND, deliver
from scruple.jobs.worker import Handler, JobContext, PermanentJobError
from scruple.storage.base import DEFAULT_TENANT


def _storage(ctx: JobContext) -> Any:
    from scruple.storage import open_storage

    storage = open_storage(ctx.database)
    for_tenant = getattr(storage, "for_tenant", None)
    if for_tenant is not None:
        return for_tenant(ctx.tenant)
    if ctx.tenant != DEFAULT_TENANT:  # pragma: no cover - every SQL backend has it
        raise PermanentJobError(f"this database cannot serve tenant {ctx.tenant!r}")
    return storage


def make_handlers(
    runtime: Runtime | Callable[[], Runtime] | None = None, clock: Clock | None = None
) -> dict[str, Handler]:
    def rt() -> Runtime:
        if runtime is None:
            return Runtime.from_env()
        return runtime if isinstance(runtime, Runtime) else runtime()

    def webhook(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
        delivery = params.get("delivery_id")
        if not isinstance(delivery, str):
            raise PermanentJobError("params.delivery_id is required")
        storage = _storage(ctx)
        try:
            return deliver(
                storage,
                rt(),
                delivery,
                job_id=ctx.job_id,
                attempt=max(1, ctx.attempt),
                clock=clock,
            )
        finally:
            storage.close()

    def ticket(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
        provider, event_id = params.get("provider"), params.get("event_id")
        if provider not in PROVIDERS or not isinstance(event_id, str):
            raise PermanentJobError("params.provider and params.event_id are required")
        storage = _storage(ctx)
        try:
            return sync(storage, rt(), str(provider), event_id, clock=clock)
        finally:
            storage.close()

    return {WEBHOOK_KIND: webhook, TICKET_KIND: ticket}


def ticket_sync(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    return make_handlers()[TICKET_KIND](ctx, params)


def webhook_deliver(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    return make_handlers()[WEBHOOK_KIND](ctx, params)


__all__ = ["make_handlers", "ticket_sync", "webhook_deliver"]
