"""Usage metering: what each tenant used, per gate run and per rollout.

Meters (`Meter`):

    gate_run    one per gate evaluation step that ran: a shadow replay, a canary
                or an assurance check (a decision at stage shadow, canary or
                assurance), whatever its outcome
    gate_cases  the cases that evaluation replayed, as the decision records them:
                `shadow.n_cases` for a shadow replay, `canary.live_cases` for a
                canary. Assurance decisions record no case count, so they meter
                a gate run only.
    rollout     one per activation: a version that went live (stage activation,
                outcome activated)

Events are derived from the tenant's own gate decisions (`sync`), each keyed by its
decision id and meter, so syncing is idempotent: run it as often as you like, from
the CLI, the API or a job, and every event is recorded once. An event's time is the
decision's own `created_at`, so the period it falls in does not depend on when it
was synced. Other producers can `record` events with their own keys.

Aggregation (`aggregate`) sums quantities per meter per period (UTC day or month).
Nothing here prices anything: billing (`scruple.billing`) maps aggregates to a
provider. Counts come from what the engine actually did; on the synthetic demo they
count synthetic work.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.gate.models import Decision, DecisionRecord, Outcome, Stage
from scruple.registry.models import RULE_ID_PATTERN
from scruple.storage.sql import TENANT_PATTERN

if TYPE_CHECKING:
    from scruple.clock import Clock
    from scruple.service import AgentGate
    from scruple.storage.sql import Connection

Period = Literal["day", "month"]
GATE_STAGES = frozenset({Stage.SHADOW, Stage.CANARY, Stage.ASSURANCE})
KEY_PATTERN = r"^[A-Za-z0-9._:@-]{1,200}$"


class Meter(StrEnum):
    GATE_RUN = "gate_run"
    GATE_CASES = "gate_cases"
    ROLLOUT = "rollout"


class UsageError(ValueError):
    pass


class UsageEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str = Field(pattern=TENANT_PATTERN)
    event_key: str = Field(pattern=KEY_PATTERN)
    meter: Meter
    quantity: int = Field(ge=0, le=2**62)
    at: datetime
    source: str = Field(min_length=1, max_length=200)
    rule_id: str | None = Field(default=None, pattern=RULE_ID_PATTERN)
    version: int | None = Field(default=None, ge=1)


class UsageAggregate(BaseModel):
    """One meter's total in one period, for one tenant."""

    model_config = ConfigDict(frozen=True)

    tenant: str
    period: Period
    period_start: date
    period_end: date  # exclusive
    meter: Meter
    quantity: int
    events: int


def _ts(d: datetime) -> str:
    if d.tzinfo is None:
        raise UsageError("usage times must be timezone-aware")
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def period_start(d: datetime | date, period: Period) -> date:
    day = d.astimezone(UTC).date() if isinstance(d, datetime) else d
    return day if period == "day" else day.replace(day=1)


def next_period(start: date, period: Period) -> date:
    if period == "day":
        return start + timedelta(days=1)
    return date(start.year + start.month // 12, start.month % 12 + 1, 1)


def cases_of(rec: DecisionRecord) -> int:
    d = rec.decision
    if d.canary is not None:
        return d.canary.live_cases
    if d.shadow is not None:
        return d.shadow.n_cases
    return 0


def events_from_decisions(tenant: str, records: Iterable[DecisionRecord]) -> Iterator[UsageEvent]:
    """The usage a tenant's gate decisions represent (see the module docstring)."""
    for rec in records:
        d = rec.decision
        if d.stage in GATE_STAGES:
            yield _event(tenant, d, Meter.GATE_RUN, 1)
            cases = cases_of(rec)
            if cases > 0:
                yield _event(tenant, d, Meter.GATE_CASES, cases)
        elif d.stage is Stage.ACTIVATION and d.outcome is Outcome.ACTIVATED:
            yield _event(tenant, d, Meter.ROLLOUT, 1)


def _event(tenant: str, d: Decision, meter: Meter, quantity: int) -> UsageEvent:
    return UsageEvent(
        tenant=tenant,
        event_key=f"{d.decision_id}:{meter.value}",
        meter=meter,
        quantity=quantity,
        at=d.created_at,
        source=d.decision_id,
        rule_id=d.rule_id,
        version=d.version,
    )


class UsageStore:
    """Append-only usage events over a backend `Connection` (the gate database)."""

    def __init__(self, conn: Connection, clock: Clock | None = None) -> None:
        from scruple.clock import SystemClock

        self._c = conn
        self._clock = clock or SystemClock()

    def record(self, event: UsageEvent) -> bool:
        """Append `event`; False if its key was already recorded (nothing changes)."""
        return self.record_many([event]) == 1

    def record_many(self, events: Iterable[UsageEvent]) -> int:
        new = 0
        now = _ts(self._clock.now())
        with self._c.transaction():
            for e in events:
                before = self._count(e.tenant, e.event_key)
                self._c.execute(
                    "INSERT INTO usage_events (tenant_id, event_key, meter, quantity, at, source, "
                    "rule_id, version, recorded_at) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (tenant_id, event_key) DO NOTHING",
                    (
                        e.tenant,
                        e.event_key,
                        e.meter.value,
                        e.quantity,
                        _ts(e.at),
                        e.source,
                        e.rule_id,
                        e.version,
                        now,
                    ),
                )
                new += self._count(e.tenant, e.event_key) - before
        return new

    def _count(self, tenant: str, key: str) -> int:
        rows = self._c.query(
            "SELECT COUNT(*) AS n FROM usage_events WHERE tenant_id=? AND event_key=?",
            (tenant, key),
        )
        return int(rows[0]["n"])

    def keys(self, tenant: str) -> set[str]:
        rows = self._c.query("SELECT event_key FROM usage_events WHERE tenant_id=?", (tenant,))
        return {str(r["event_key"]) for r in rows}

    def events(
        self, tenant: str, start: datetime | None = None, end: datetime | None = None
    ) -> list[UsageEvent]:
        """A tenant's events with `start <= at < end`, oldest first."""
        sql = (
            "SELECT event_key, meter, quantity, at, source, rule_id, version FROM usage_events "
            "WHERE tenant_id=?"
        )
        params: list[object] = [tenant]
        if start is not None:
            sql += " AND at >= ?"
            params.append(_ts(start))
        if end is not None:
            sql += " AND at < ?"
            params.append(_ts(end))
        rows = self._c.query(sql + " ORDER BY at, id", params)
        return [
            UsageEvent(
                tenant=tenant,
                event_key=r["event_key"],
                meter=Meter(r["meter"]),
                quantity=int(r["quantity"]),
                at=_parse_ts(r["at"]),
                source=r["source"],
                rule_id=r["rule_id"],
                version=r["version"],
            )
            for r in rows
        ]

    def sync(self, gate: AgentGate) -> int:
        """Record the usage of `gate`'s tenant's decisions not yet recorded."""
        tenant = gate.storage.tenant
        seen = self.keys(tenant)
        fresh = [
            e for e in events_from_decisions(tenant, gate.decisions()) if e.event_key not in seen
        ]
        return self.record_many(fresh) if fresh else 0

    def aggregate(
        self,
        tenant: str,
        period: Period = "month",
        start: date | None = None,
        end: date | None = None,
    ) -> list[UsageAggregate]:
        """Totals per (period, meter), oldest period first. `start` and `end` are
        period-aligned bounds (end exclusive); None is unbounded."""
        lo = period_start(start, period) if start else None
        hi = period_start(end, period) if end else None
        evs = self.events(
            tenant,
            datetime(lo.year, lo.month, lo.day, tzinfo=UTC) if lo else None,
            datetime(hi.year, hi.month, hi.day, tzinfo=UTC) if hi else None,
        )
        sums: dict[tuple[date, Meter], list[int]] = defaultdict(lambda: [0, 0])
        for e in evs:
            k = (period_start(e.at, period), e.meter)
            sums[k][0] += e.quantity
            sums[k][1] += 1
        order = {m: i for i, m in enumerate(Meter)}
        return [
            UsageAggregate(
                tenant=tenant,
                period=period,
                period_start=ps,
                period_end=next_period(ps, period),
                meter=m,
                quantity=q,
                events=n,
            )
            for (ps, m), (q, n) in sorted(sums.items(), key=lambda kv: (kv[0][0], order[kv[0][1]]))
        ]


def usage_store_for(storage: object, clock: Clock | None = None) -> UsageStore | None:
    """The usage store on a gate `Storage`'s database, or None if it has none."""
    from scruple.storage.sql import SQLStorage

    if isinstance(storage, SQLStorage):
        return UsageStore(storage.connection, clock)
    return None


__all__ = [
    "GATE_STAGES",
    "Meter",
    "Period",
    "UsageAggregate",
    "UsageError",
    "UsageEvent",
    "UsageStore",
    "events_from_decisions",
    "next_period",
    "period_start",
    "usage_store_for",
]
