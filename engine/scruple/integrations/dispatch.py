"""The dispatcher: audit entries after the cursor -> events -> queued jobs.

See `scruple.integrations.events` for why the audit log is the outbox. One pass
(`dispatch`) for one tenant:

1. read the entries after the tenant's cursor and turn the relevant ones into events;
2. in one transaction, store the events and one delivery row per subscribed endpoint;
3. after that commits, queue the jobs (idempotency keys from the delivery or event, so
   a pass repeated after a crash queues nothing twice); and only then
4. move the cursor past the last entry read.

A job is queued only after the rows it reads are committed, so a worker on another
connection (Postgres) never claims a job whose delivery it cannot see yet.

`Dispatcher.tick` runs a pass for every tenant with integrations at most every
`interval_s`; `scruple worker --dispatch-integrations` calls it before each claim and
`scruple integrations dispatch` runs one pass by hand.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from scruple.integrations.events import to_event
from scruple.integrations.store import GateEvent, IntegrationStore
from scruple.integrations.tickets import MAX_ATTEMPTS as TICKET_ATTEMPTS
from scruple.integrations.tickets import TICKET_KIND
from scruple.integrations.tickets import TIMEOUT_S as TICKET_TIMEOUT
from scruple.integrations.tickets import configured as configured_trackers
from scruple.integrations.webhooks import MAX_ATTEMPTS as WEBHOOK_ATTEMPTS
from scruple.integrations.webhooks import TIMEOUT_S as WEBHOOK_TIMEOUT
from scruple.integrations.webhooks import WEBHOOK_KIND, delivery_id
from scruple.jobs.queue import JobQueue
from scruple.service import AgentGate

log = logging.getLogger("scruple.integrations")


class DispatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant: str
    events: int
    jobs: int
    cursor: int


@dataclass(frozen=True)
class PlannedJob:
    kind: str
    params: dict[str, object]
    key: str
    max_attempts: int
    timeout_s: int


Planner = Callable[[IntegrationStore, GateEvent], list[PlannedJob]]


def _webhooks(store: IntegrationStore, event: GateEvent) -> list[PlannedJob]:
    out = []
    for ep in store.endpoints():
        if ep.active and event.type in ep.events:
            did = delivery_id(ep.endpoint_id, event.id)
            store.add_delivery(did, ep.endpoint_id, event.id, event.created_at)
            out.append(
                PlannedJob(
                    WEBHOOK_KIND,
                    {"delivery_id": did},
                    f"webhook:{did}",
                    WEBHOOK_ATTEMPTS,
                    WEBHOOK_TIMEOUT,
                )
            )
    return out


def _tickets(store: IntegrationStore, event: GateEvent) -> list[PlannedJob]:
    if event.type != "decision.recorded":
        return []
    decision = event.data["decision_id"]
    return [
        PlannedJob(
            TICKET_KIND,
            {"provider": provider, "event_id": event.id},
            f"ticket:{provider}:{decision}",
            TICKET_ATTEMPTS,
            TICKET_TIMEOUT,
        )
        for provider in configured_trackers(store.settings())
    ]


# Each integration turns an event into the jobs it needs (and any rows those read).
PLANNERS: list[Planner] = [_webhooks, _tickets]


def dispatch(gate: AgentGate, queue: JobQueue, *, limit: int = 1_000) -> DispatchResult:
    store = IntegrationStore(gate.storage).ensure()
    cursor = store.cursor()
    entries = [e for e in gate.audit.entries() if e.seq > cursor][:limit]
    planned: list[PlannedJob] = []
    events = 0
    with store.transaction():
        for entry in entries:
            event = to_event(gate, entry)
            if event is None:
                continue
            events += 1
            store.put_event(event)
            for plan in PLANNERS:
                planned.extend(plan(store, event))
    for job in planned:
        queue.submit_raw(
            job.kind,
            dict(job.params),
            tenant_id=store.tenant,
            actor="integrations",
            idempotency_key=job.key,
            max_attempts=job.max_attempts,
            timeout_s=job.timeout_s,
        )
    if entries:
        store.set_cursor(entries[-1].seq)
    return DispatchResult(
        tenant=store.tenant,
        events=events,
        jobs=len(planned),
        cursor=entries[-1].seq if entries else cursor,
    )


@dataclass
class Dispatcher:
    """A pass for every tenant with integrations, at most every `interval_s`."""

    queue: JobQueue
    storage: object  # the gate Storage (any tenant view)
    interval_s: float = 5.0
    now: Callable[[], float] = time.monotonic
    _last: float | None = field(default=None, init=False)

    def tick(self) -> list[DispatchResult]:
        t = self.now()
        if self._last is not None and t - self._last < self.interval_s:
            return []
        self._last = t
        out = []
        for tenant in IntegrationStore.tenants(self.storage):
            view = self.storage.for_tenant(tenant)  # type: ignore[attr-defined]
            try:
                out.append(dispatch(AgentGate(view), self.queue))
            except Exception:  # one tenant's failure must not stop the others
                log.exception("integration dispatch failed for tenant %s", tenant)
        return out


__all__ = ["PLANNERS", "DispatchResult", "Dispatcher", "PlannedJob", "dispatch"]
