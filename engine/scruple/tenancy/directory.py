"""The tenants a gate database knows, and what was done to each.

A tenant exists implicitly as soon as anything is stored for it (a key, a token's
first audited write, a retention policy). `scruple tenant create` records it
explicitly; `quota` sets its limits; `offboard` retires it (`scruple.tenancy.offboard`).
Each change is one row in the append-only `tenant_events` table and one audit entry
in the tenant's own hash chain (`tenant.created`, `tenant.quota_set`,
`tenant.offboarded`), written in one transaction.

Offboarding is final: an offboarded tenant id is never served again. Its API keys
are revoked, new keys for it are refused (`ApiKeyService.create`), and any other
credential naming it (an OIDC token, a console session) is refused at
authentication (`scruple.auth.authn`). Its audit chain stays, so the id stays taken.

Quota. One limit fits what usage metering (`scruple.usage.meter`) already counts:
`gate_runs_per_day`, the `gate_run` meter (shadow, canary and assurance decisions)
per UTC day. It is checked before a shadow replay starts (`AgentGate.evaluate`):
when the tenant's gate runs today have reached the limit, the evaluation is refused
(`QuotaExceededError`, HTTP 429) and nothing is recorded. Assurance checks of the
live policy are never refused, since they guard what is already live, but they
count. A running evaluation is not cut short, so one evaluation may take the count
past the limit by its canary run.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.storage.lazy import connection_of, ensure_tables, table_exists
from scruple.storage.sql import TENANT_PATTERN
from scruple.storage.tenants import SQLITE_DDL, TABLE

if TYPE_CHECKING:
    from scruple.clock import Clock

EVENT_CREATED = "tenant.created"
EVENT_QUOTA = "tenant.quota_set"
EVENT_OFFBOARDED = "tenant.offboarded"
Kind = Literal["created", "quota_set", "offboarded"]
_AUDIT = {"created": EVENT_CREATED, "quota_set": EVENT_QUOTA, "offboarded": EVENT_OFFBOARDED}

# Tables whose rows name a tenant: a tenant id in any of them is a known tenant.
_SOURCES = (
    "api_keys",
    "audit_events",
    "rule_versions",
    "decisions",
    "retention_policies",
    "imported_shadow_events",
    "usage_events",
)


class TenancyError(ValueError):
    """A tenant administration request that cannot be done (bad state, unknown id)."""


class TenantOffboardedError(TenancyError):
    """The tenant was offboarded; nothing more is done for it."""


class QuotaExceededError(Exception):
    """The tenant used its quota for today (HTTP 429, `Retry-After` until the next
    UTC midnight)."""

    def __init__(self, detail: str, retry_after: int) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class Quota(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_runs_per_day: int | None = Field(default=None, ge=1, le=10_000_000)


class TenantEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant: str
    number: int
    kind: Kind
    body: dict[str, Any]
    actor: str
    created_at: datetime


class TenantRecord(BaseModel):
    """A tenant's state, folded from its events."""

    model_config = ConfigDict(frozen=True)

    tenant: str
    status: Literal["active", "offboarded"]
    explicit: bool  # created with `scruple tenant create`, not only implied by data
    created_at: datetime | None = None
    created_by: str | None = None
    offboarded_at: datetime | None = None
    offboarded_by: str | None = None
    quota: Quota = Quota()


def _fold(tenant: str, events: list[TenantEvent]) -> TenantRecord:
    rec: dict[str, Any] = {"tenant": tenant, "status": "active", "explicit": False}
    for e in events:
        if e.kind == "created":
            rec.update(explicit=True, created_at=e.created_at, created_by=e.actor)
        elif e.kind == "quota_set":
            rec["quota"] = Quota.model_validate(e.body["quota"])
        elif e.kind == "offboarded":
            rec.update(status="offboarded", offboarded_at=e.created_at, offboarded_by=e.actor)
    return TenantRecord.model_validate(rec)


class TenantDirectory:
    """Every tenant in a gate database, over a gate `Storage`'s connection. Not bound
    to a tenant: this is the operator's view (the command line)."""

    def __init__(self, storage: object, clock: Clock | None = None) -> None:
        from scruple.clock import SystemClock

        self._storage = storage
        self._conn, self._dialect = connection_of(storage)
        self.clock: Clock = clock or SystemClock()

    def _has_table(self) -> bool:
        return table_exists(self._conn, self._dialect, TABLE)

    def events(self, tenant: str) -> list[TenantEvent]:
        if not self._has_table():
            return []
        rows = self._conn.query(
            f"SELECT number, kind, body_json, actor, created_at FROM {TABLE} "  # noqa: S608
            "WHERE tenant_id=? ORDER BY number",
            (tenant,),
        )
        return [
            TenantEvent(
                tenant=tenant,
                number=int(r["number"]),
                kind=r["kind"],
                body=json.loads(r["body_json"]),
                actor=r["actor"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def known(self) -> list[str]:
        """Every tenant id that has anything stored, sorted."""
        found: set[str] = set()
        for table in (*_SOURCES, TABLE):
            if table == TABLE and not self._has_table():
                continue
            rows = self._conn.query(f"SELECT DISTINCT tenant_id FROM {table}")  # noqa: S608
            found.update(str(r["tenant_id"]) for r in rows)
        return sorted(found)

    def get(self, tenant: str) -> TenantRecord:
        return _fold(tenant, self.events(tenant))

    def list(self) -> list[TenantRecord]:
        return [self.get(t) for t in self.known()]

    def exists(self, tenant: str) -> bool:
        return tenant in self.known()

    def is_offboarded(self, tenant: str) -> bool:
        """One indexed read (per-request checks)."""
        if not self._has_table():
            return False
        rows = self._conn.query(
            f"SELECT 1 AS x FROM {TABLE} WHERE tenant_id=? AND kind='offboarded' LIMIT 1",  # noqa: S608
            (tenant,),
        )
        return bool(rows)

    def imported_shadow_rows(self, tenant: str) -> int:
        rows = self._conn.query(
            "SELECT COUNT(*) AS n FROM imported_shadow_events WHERE tenant_id=?", (tenant,)
        )
        return int(rows[0]["n"])

    def quota(self, tenant: str) -> Quota:
        if not self._has_table():
            return Quota()
        return self.get(tenant).quota

    def append(self, tenant: str, kind: Kind, body: dict[str, Any], actor: str) -> TenantEvent:
        """Store the event and its audit entry in the tenant's chain, atomically."""
        from scruple.audit.log import AuditLog

        ensure_tables(self._conn, SQLITE_DDL, self._dialect)
        for_tenant = getattr(self._storage, "for_tenant", None)
        if for_tenant is None:  # pragma: no cover - every SQL backend has it
            raise TypeError("storage cannot serve another tenant")
        store = for_tenant(tenant)
        now = self.clock.now()
        with store.transaction():
            rows = self._conn.query(
                f"SELECT MAX(number) AS n FROM {TABLE} WHERE tenant_id=?",  # noqa: S608
                (tenant,),
            )
            number = int(rows[0]["n"] or 0) + 1
            self._conn.execute(
                f"INSERT INTO {TABLE} (tenant_id, number, kind, body_json, actor, created_at) "  # noqa: S608
                "VALUES (?,?,?,?,?,?)",
                (tenant, number, kind, json.dumps(body, sort_keys=True), actor, now.isoformat()),
            )
            AuditLog(store, self.clock).append(_AUDIT[kind], actor, {"tenant_id": tenant, **body})
        return TenantEvent(
            tenant=tenant, number=number, kind=kind, body=body, actor=actor, created_at=now
        )

    # --- commands ------------------------------------------------------------------

    def create(self, tenant: str, actor: str) -> TenantRecord:
        _check_id(tenant)
        rec = self.get(tenant)
        if rec.status == "offboarded":
            raise TenantOffboardedError(f"tenant {tenant} was offboarded; ids are not reused")
        if rec.explicit:
            raise TenancyError(f"tenant {tenant} already exists")
        self.append(tenant, "created", {"implicit_before": self.exists(tenant)}, actor)
        return self.get(tenant)

    def set_quota(self, tenant: str, quota: Quota, actor: str) -> TenantRecord:
        _check_id(tenant)
        if self.is_offboarded(tenant):
            raise TenantOffboardedError(f"tenant {tenant} was offboarded")
        if not self.exists(tenant):
            raise TenancyError(f"no tenant {tenant}; create it first")
        self.append(tenant, "quota_set", {"quota": quota.model_dump()}, actor)
        return self.get(tenant)


def _check_id(tenant: str) -> None:
    import re

    if not re.match(TENANT_PATTERN, tenant):
        raise TenancyError(f"invalid tenant id {tenant!r}")


def directory_for(storage: object, clock: Clock | None = None) -> TenantDirectory | None:
    """The directory over a gate storage's database, or None for a backend without SQL."""
    try:
        return TenantDirectory(storage, clock)
    except TypeError:
        return None


def gate_runs_today(gate: Any, now: datetime) -> int:
    """The tenant's `gate_run` meter for the UTC day of `now`, from its decisions."""
    from scruple.usage.meter import Meter, events_from_decisions

    start = datetime.combine(now.astimezone(UTC).date(), time(), UTC)
    return sum(
        e.quantity
        for e in events_from_decisions(gate.storage.tenant, gate.decisions())
        if e.meter is Meter.GATE_RUN and e.at >= start
    )


def check_gate_quota(gate: Any) -> None:
    """Refuse new gate work for an offboarded tenant or one over today's quota."""
    directory = directory_for(gate.storage, gate.clock)
    if directory is None:
        return
    tenant = gate.storage.tenant
    if directory.is_offboarded(tenant):
        raise TenantOffboardedError(f"tenant {tenant} was offboarded")
    limit = directory.quota(tenant).gate_runs_per_day
    if limit is None:
        return
    now = gate.clock.now()
    used = gate_runs_today(gate, now)
    if used >= limit:
        midnight = datetime.combine(now.astimezone(UTC).date() + timedelta(days=1), time(), UTC)
        raise QuotaExceededError(
            f"tenant {tenant} used its quota of {limit} gate runs today (UTC); used {used}",
            retry_after=max(1, math.ceil((midnight - now).total_seconds())),
        )


__all__ = [
    "EVENT_CREATED",
    "EVENT_OFFBOARDED",
    "EVENT_QUOTA",
    "Quota",
    "QuotaExceededError",
    "TenancyError",
    "TenantDirectory",
    "TenantEvent",
    "TenantOffboardedError",
    "TenantRecord",
    "check_gate_quota",
    "directory_for",
    "gate_runs_today",
]
