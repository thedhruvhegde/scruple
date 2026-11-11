"""`retention_apply`: retention as a scheduled job, at most once per tenant per day.

`schedule_retention(queue, storage, day)` queues one `retention_apply` job for every
tenant that has a retention policy, with the idempotency key `retention-apply:<day>`.
Keys are unique per tenant, so however many workers, cron runs or restarts schedule
the same day, each tenant gets one job for it. `scruple worker --schedule-retention`
schedules today's jobs whenever the UTC date changes (and at start);
`scruple retention schedule [--day D]` does it once, for a cron.

The job applies its own tenant's policy as of the start of `day` (00:00 UTC), so the
cutoff is a function of the day, not of when a worker got round to it: a retry, or a
late run, deletes exactly what the first attempt would have. A second run finds
nothing left and records nothing. Every purge is audited in the tenant's chain as
`retention apply` does (`scruple.storage.retention`). A day in the future is refused:
it would delete data the policy still keeps.

The kind is not a `JobKind`: the HTTP API cannot submit it. Retention is the
operator's, scheduled from the worker or the command line.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.jobs.queue import JobQueue
from scruple.jobs.worker import JobContext, PermanentJobError
from scruple.registry.models import ACTOR_PATTERN

RETENTION_KIND = "retention_apply"


class RetentionApplyParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: date
    actor: str = Field(default="retention", pattern=ACTOR_PATTERN)


def idempotency_key(day: date) -> str:
    return f"retention-apply:{day.isoformat()}"


def day_start(day: date) -> datetime:
    return datetime.combine(day, time(0), UTC)


def schedule_retention(
    queue: JobQueue, storage: object, day: date, *, actor: str = "retention"
) -> list[tuple[Any, bool]]:
    """Queue `day`'s job for every tenant with a policy. Returns (job, created) per
    tenant; created is False when that day was already scheduled."""
    from scruple.storage.retention import retention_for

    params = RetentionApplyParams(day=day, actor=actor).model_dump(mode="json")
    out: list[tuple[Any, bool]] = []
    for policy in retention_for(storage).policies():
        out.append(
            queue.submit_raw(
                RETENTION_KIND,
                params,
                tenant_id=policy.tenant_id,
                actor=actor,
                idempotency_key=idempotency_key(day),
                max_attempts=5,
                timeout_s=3_600,
            )
        )
    return out


class DailyScheduler:
    """Calls `schedule_retention` once per UTC day: a worker's tick."""

    def __init__(self, queue: JobQueue, storage: object, clock: Any) -> None:
        self.queue = queue
        self.storage = storage
        self.clock = clock
        self.last: date | None = None

    def tick(self) -> None:
        today = self.clock.now().astimezone(UTC).date()
        if today != self.last:
            schedule_retention(self.queue, self.storage, today)
            self.last = today


def retention_apply(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    from scruple.clock import SystemClock
    from scruple.service import AgentGate
    from scruple.storage import open_storage
    from scruple.storage.retention import apply_retention

    p = RetentionApplyParams.model_validate(params)
    storage = open_storage(ctx.database)
    gate = AgentGate(storage, SystemClock())
    stores: dict[str, Any] = {}
    try:
        if p.day > gate.clock.now().astimezone(UTC).date():
            raise PermanentJobError(f"retention for {p.day} would run ahead of the policy")
        if ctx.case_store and ctx.case_store_tenant == ctx.tenant:
            from scruple.ingest.store import CaseStore

            stores[ctx.tenant] = CaseStore(ctx.case_store, create=False)
        done = apply_retention(
            gate,
            case_stores=stores,
            actor=p.actor,
            now=day_start(p.day),
            tenants={ctx.tenant},
        )
        return {
            "tenant_id": ctx.tenant,
            "day": p.day.isoformat(),
            "cutoff_from": day_start(p.day).isoformat(),
            "rows_deleted": sum(r.rows for r in done),
            "purges": [r.model_dump(mode="json") for r in done],
            "case_store": ctx.tenant in stores,
        }
    finally:
        for cs in stores.values():
            cs.close()
        storage.close()


__all__ = [
    "RETENTION_KIND",
    "DailyScheduler",
    "RetentionApplyParams",
    "day_start",
    "idempotency_key",
    "retention_apply",
    "schedule_retention",
]
