"""Integration state in the gate database, one tenant per store.

    integration_settings   append-only versions of IntegrationSettings (latest wins)
    integration_events     every gate event sent out, by event id (the payload sent)
    integration_cursor     the last audit seq the dispatcher has turned into events
    webhook_endpoints      endpoints (mutable: active flag, sealed secrets)
    webhook_deliveries     one row per (endpoint, event): status, attempts, last error
    webhook_attempts       append-only log of every attempt, replays included
    ticket_links           one issue per candidate (rule version) per provider
    ticket_updates         one update per decision per provider (idempotency)
    link_nonces            used approval-link nonces and Slack request signatures

Tables are created when a tenant first configures something (`scruple.storage.lazy`);
reads of a database that never used integrations create nothing.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.integrations.config import IntegrationSettings
from scruple.jobs.queue import ts
from scruple.storage.lazy import connection_of, ensure_tables, table_exists

DDL = (
    """CREATE TABLE IF NOT EXISTS integration_settings (
    tenant_id TEXT NOT NULL, number BIGINT NOT NULL, body_json TEXT NOT NULL,
    actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, number))""",
    """CREATE TABLE IF NOT EXISTS integration_events (
    tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, audit_seq BIGINT NOT NULL,
    event_type TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id))""",
    """CREATE TABLE IF NOT EXISTS integration_cursor (
    tenant_id TEXT NOT NULL PRIMARY KEY, last_seq BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS webhook_endpoints (
    tenant_id TEXT NOT NULL, endpoint_id TEXT NOT NULL, url TEXT NOT NULL,
    events_json TEXT NOT NULL, secrets_json TEXT NOT NULL, active INTEGER NOT NULL,
    description TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, endpoint_id))""",
    """CREATE TABLE IF NOT EXISTS webhook_deliveries (
    tenant_id TEXT NOT NULL, delivery_id TEXT NOT NULL, endpoint_id TEXT NOT NULL,
    event_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
    last_status_code INTEGER, last_error TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, delivered_at TEXT, replays INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, delivery_id))""",
    """CREATE TABLE IF NOT EXISTS webhook_attempts (
    tenant_id TEXT NOT NULL, delivery_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
    at TEXT NOT NULL, status_code INTEGER, error TEXT, duration_ms INTEGER NOT NULL,
    job_id TEXT NOT NULL, PRIMARY KEY (tenant_id, delivery_id, attempt_no))""",
    """CREATE TABLE IF NOT EXISTS ticket_links (
    tenant_id TEXT NOT NULL, provider TEXT NOT NULL, candidate TEXT NOT NULL,
    issue_id TEXT, issue_key TEXT, url TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider, candidate))""",
    """CREATE TABLE IF NOT EXISTS ticket_updates (
    tenant_id TEXT NOT NULL, provider TEXT NOT NULL, decision_id TEXT NOT NULL,
    candidate TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (tenant_id, provider, decision_id))""",
    """CREATE TABLE IF NOT EXISTS link_nonces (
    tenant_id TEXT NOT NULL, nonce TEXT NOT NULL, purpose TEXT NOT NULL,
    used_at TEXT NOT NULL, PRIMARY KEY (tenant_id, nonce))""",
)
TABLES = (
    "integration_settings",
    "integration_events",
    "integration_cursor",
    "webhook_endpoints",
    "webhook_deliveries",
    "webhook_attempts",
    "ticket_links",
    "ticket_updates",
    "link_nonces",
)


class Endpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint_id: str
    url: str
    events: tuple[str, ...]
    secrets: tuple[dict[str, Any], ...]  # [{"sealed": ..., "created_at": ..., "retire_at": ...}]
    active: bool
    description: str
    created_by: str
    created_at: datetime

    def public(self) -> dict[str, Any]:
        """Everything except the sealed secrets."""
        out = self.model_dump(mode="json", exclude={"secrets"})
        out["secrets"] = [
            {"created_at": s["created_at"], "retire_at": s.get("retire_at")} for s in self.secrets
        ]
        return out


class Delivery(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivery_id: str
    endpoint_id: str
    event_id: str
    status: str  # pending | delivered | failed
    attempts: int
    last_status_code: int | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None
    replays: int


class Attempt(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt_no: int
    at: datetime
    status_code: int | None
    error: str | None
    duration_ms: int
    job_id: str


class GateEvent(BaseModel):
    """What goes out: one gate event, built from one audit entry."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    tenant: str
    created_at: datetime
    audit_seq: int
    audit_entry_hash: str
    data: dict[str, Any]


def _dt(v: Any) -> datetime | None:
    return None if v is None else datetime.fromisoformat(str(v))


class IntegrationStore:
    def __init__(self, storage: object, tenant: str | None = None) -> None:
        self._conn, self._dialect = connection_of(storage)
        self.tenant: str = tenant or str(getattr(storage, "tenant", "default"))
        self._ready = False

    @classmethod
    def tenants(cls, storage: object) -> list[str]:
        """Tenants that have configured anything (for the dispatcher's tick)."""
        conn, dialect = connection_of(storage)
        found: set[str] = set()
        for table in ("integration_settings", "webhook_endpoints"):
            if table_exists(conn, dialect, table):
                found |= {
                    str(r["tenant_id"])
                    for r in conn.query(f"SELECT DISTINCT tenant_id FROM {table}")  # noqa: S608
                }
        return sorted(found)

    def transaction(self) -> Any:
        return self._conn.transaction()

    def ensure(self) -> IntegrationStore:
        if not self._ready:
            ensure_tables(self._conn, DDL)
            self._ready = True
        return self

    def _q(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self.ensure()
        return [dict(r) for r in self._conn.query(sql, params)]

    def has_cursor(self) -> bool:
        return bool(self._q("SELECT 1 FROM integration_cursor WHERE tenant_id=?", (self.tenant,)))

    def _x(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.ensure()
        self._conn.execute(sql, params)

    # --- settings -----------------------------------------------------------------

    def settings(self) -> IntegrationSettings:
        rows = self._q(
            "SELECT body_json FROM integration_settings WHERE tenant_id=? "
            "ORDER BY number DESC LIMIT 1",
            (self.tenant,),
        )
        if not rows:
            return IntegrationSettings()
        return IntegrationSettings.model_validate_json(rows[0]["body_json"])

    def put_settings(self, s: IntegrationSettings, actor: str, now: datetime) -> int:
        with self.transaction():
            rows = self._q(
                "SELECT MAX(number) AS n FROM integration_settings WHERE tenant_id=?",
                (self.tenant,),
            )
            n = int(rows[0]["n"] or 0) + 1
            self._x(
                "INSERT INTO integration_settings (tenant_id, number, body_json, actor, "
                "created_at) VALUES (?,?,?,?,?)",
                (self.tenant, n, s.model_dump_json(), actor, ts(now)),
            )
        return n

    # --- events and the dispatch cursor -------------------------------------------

    def cursor(self) -> int:
        rows = self._q("SELECT last_seq FROM integration_cursor WHERE tenant_id=?", (self.tenant,))
        return int(rows[0]["last_seq"]) if rows else 0

    def set_cursor(self, seq: int) -> None:
        with self.transaction():
            if self._q("SELECT 1 FROM integration_cursor WHERE tenant_id=?", (self.tenant,)):
                self._x(
                    "UPDATE integration_cursor SET last_seq=? WHERE tenant_id=? AND last_seq<?",
                    (seq, self.tenant, seq),
                )
            else:
                self._x(
                    "INSERT INTO integration_cursor (tenant_id, last_seq) VALUES (?,?)",
                    (self.tenant, seq),
                )

    def put_event(self, e: GateEvent) -> None:
        self._x(
            "INSERT INTO integration_events (tenant_id, event_id, audit_seq, event_type, "
            "body_json, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (tenant_id, event_id) DO NOTHING",
            (self.tenant, e.id, e.audit_seq, e.type, e.model_dump_json(), ts(e.created_at)),
        )

    def event(self, event_id: str) -> GateEvent | None:
        rows = self._q(
            "SELECT body_json FROM integration_events WHERE tenant_id=? AND event_id=?",
            (self.tenant, event_id),
        )
        return GateEvent.model_validate_json(rows[0]["body_json"]) if rows else None

    def events(self, limit: int = 100) -> list[GateEvent]:
        rows = self._q(
            "SELECT body_json FROM integration_events WHERE tenant_id=? "
            "ORDER BY audit_seq DESC LIMIT ?",
            (self.tenant, limit),
        )
        return [GateEvent.model_validate_json(r["body_json"]) for r in rows]

    # --- webhook endpoints ----------------------------------------------------------

    def add_endpoint(self, ep: Endpoint) -> None:
        self._x(
            "INSERT INTO webhook_endpoints (tenant_id, endpoint_id, url, events_json, "
            "secrets_json, active, description, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self.tenant,
                ep.endpoint_id,
                ep.url,
                json.dumps(list(ep.events)),
                json.dumps(list(ep.secrets)),
                int(ep.active),
                ep.description,
                ep.created_by,
                ts(ep.created_at),
            ),
        )

    def _endpoint(self, r: dict[str, Any]) -> Endpoint:
        return Endpoint(
            endpoint_id=r["endpoint_id"],
            url=r["url"],
            events=tuple(json.loads(r["events_json"])),
            secrets=tuple(json.loads(r["secrets_json"])),
            active=bool(r["active"]),
            description=r["description"],
            created_by=r["created_by"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )

    def endpoints(self) -> list[Endpoint]:
        rows = self._q(
            "SELECT * FROM webhook_endpoints WHERE tenant_id=? ORDER BY created_at, endpoint_id",
            (self.tenant,),
        )
        return [self._endpoint(r) for r in rows]

    def endpoint(self, endpoint_id: str) -> Endpoint | None:
        rows = self._q(
            "SELECT * FROM webhook_endpoints WHERE tenant_id=? AND endpoint_id=?",
            (self.tenant, endpoint_id),
        )
        return self._endpoint(rows[0]) if rows else None

    def update_endpoint(
        self,
        endpoint_id: str,
        *,
        active: bool | None = None,
        secrets: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        if active is not None:
            self._x(
                "UPDATE webhook_endpoints SET active=? WHERE tenant_id=? AND endpoint_id=?",
                (int(active), self.tenant, endpoint_id),
            )
        if secrets is not None:
            self._x(
                "UPDATE webhook_endpoints SET secrets_json=? WHERE tenant_id=? AND endpoint_id=?",
                (json.dumps(list(secrets)), self.tenant, endpoint_id),
            )

    # --- deliveries -----------------------------------------------------------------

    def add_delivery(
        self, delivery_id: str, endpoint_id: str, event_id: str, now: datetime
    ) -> bool:
        rows = self._q(
            "INSERT INTO webhook_deliveries (tenant_id, delivery_id, endpoint_id, event_id, "
            "status, attempts, created_at, updated_at, replays) "
            "VALUES (?,?,?,?,'pending',0,?,?,0) "
            "ON CONFLICT (tenant_id, delivery_id) DO NOTHING RETURNING delivery_id",
            (self.tenant, delivery_id, endpoint_id, event_id, ts(now), ts(now)),
        )
        return bool(rows)

    def _delivery(self, r: dict[str, Any]) -> Delivery:
        return Delivery(
            delivery_id=r["delivery_id"],
            endpoint_id=r["endpoint_id"],
            event_id=r["event_id"],
            status=r["status"],
            attempts=int(r["attempts"]),
            last_status_code=r["last_status_code"],
            last_error=r["last_error"],
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
            delivered_at=_dt(r["delivered_at"]),
            replays=int(r["replays"]),
        )

    def delivery(self, delivery_id: str) -> Delivery | None:
        rows = self._q(
            "SELECT * FROM webhook_deliveries WHERE tenant_id=? AND delivery_id=?",
            (self.tenant, delivery_id),
        )
        return self._delivery(rows[0]) if rows else None

    def deliveries(
        self, endpoint_id: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[Delivery]:
        sql = "SELECT * FROM webhook_deliveries WHERE tenant_id=?"
        params: list[Any] = [self.tenant]
        if endpoint_id:
            sql += " AND endpoint_id=?"
            params.append(endpoint_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, delivery_id DESC LIMIT ?"
        params.append(limit)
        return [self._delivery(r) for r in self._q(sql, tuple(params))]

    def record_attempt(
        self,
        delivery_id: str,
        *,
        now: datetime,
        status_code: int | None,
        error: str | None,
        duration_ms: int,
        job_id: str,
        final: str | None,
    ) -> int:
        """Log one attempt; `final` is the delivery's new status (delivered/failed) or
        None while it may still be retried. Returns the attempt number."""
        with self.transaction():
            rows = self._q(
                "SELECT MAX(attempt_no) AS n FROM webhook_attempts "
                "WHERE tenant_id=? AND delivery_id=?",
                (self.tenant, delivery_id),
            )
            n = int(rows[0]["n"] or 0) + 1
            self._x(
                "INSERT INTO webhook_attempts (tenant_id, delivery_id, attempt_no, at, "
                "status_code, error, duration_ms, job_id) VALUES (?,?,?,?,?,?,?,?)",
                (self.tenant, delivery_id, n, ts(now), status_code, error, duration_ms, job_id),
            )
            status = final or "pending"
            self._x(
                "UPDATE webhook_deliveries SET status=?, attempts=attempts+1, "
                "last_status_code=?, last_error=?, updated_at=?, "
                "delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END "
                "WHERE tenant_id=? AND delivery_id=?",
                (
                    status,
                    status_code,
                    error,
                    ts(now),
                    status,
                    ts(now),
                    self.tenant,
                    delivery_id,
                ),
            )
        return n

    def mark_replay(self, delivery_id: str, now: datetime) -> int:
        with self.transaction():
            self._x(
                "UPDATE webhook_deliveries SET replays=replays+1, status='pending', "
                "updated_at=? WHERE tenant_id=? AND delivery_id=?",
                (ts(now), self.tenant, delivery_id),
            )
            d = self.delivery(delivery_id)
        assert d is not None
        return d.replays

    def attempts(self, delivery_id: str) -> list[Attempt]:
        rows = self._q(
            "SELECT * FROM webhook_attempts WHERE tenant_id=? AND delivery_id=? "
            "ORDER BY attempt_no",
            (self.tenant, delivery_id),
        )
        return [
            Attempt(
                attempt_no=int(r["attempt_no"]),
                at=datetime.fromisoformat(r["at"]),
                status_code=r["status_code"],
                error=r["error"],
                duration_ms=int(r["duration_ms"]),
                job_id=r["job_id"],
            )
            for r in rows
        ]

    # --- tickets --------------------------------------------------------------------

    def ticket(self, provider: str, candidate: str) -> dict[str, Any] | None:
        """The candidate's issue: `issue_id` is None while its creation is in progress."""
        rows = self._q(
            "SELECT issue_id, issue_key, url, created_at FROM ticket_links "
            "WHERE tenant_id=? AND provider=? AND candidate=?",
            (self.tenant, provider, candidate),
        )
        return dict(rows[0]) if rows else None

    def claim_ticket(self, provider: str, candidate: str, now: datetime) -> bool:
        """Take the right to create the candidate's issue (True once per candidate)."""
        rows = self._q(
            "INSERT INTO ticket_links (tenant_id, provider, candidate, created_at) "
            "VALUES (?,?,?,?) ON CONFLICT (tenant_id, provider, candidate) DO NOTHING "
            "RETURNING candidate",
            (self.tenant, provider, candidate, ts(now)),
        )
        return bool(rows)

    def reclaim_ticket(
        self, provider: str, candidate: str, stale_before: datetime, now: datetime
    ) -> bool:
        """Take over a creation that has been in progress since before `stale_before`."""
        rows = self._q(
            "UPDATE ticket_links SET created_at=? WHERE tenant_id=? AND provider=? "
            "AND candidate=? AND issue_id IS NULL AND created_at<? RETURNING candidate",
            (ts(now), self.tenant, provider, candidate, ts(stale_before)),
        )
        return bool(rows)

    def complete_ticket(
        self, provider: str, candidate: str, issue_id: str, issue_key: str, url: str
    ) -> None:
        self._x(
            "UPDATE ticket_links SET issue_id=?, issue_key=?, url=? "
            "WHERE tenant_id=? AND provider=? AND candidate=? AND issue_id IS NULL",
            (issue_id, issue_key, url, self.tenant, provider, candidate),
        )

    def release_ticket(self, provider: str, candidate: str) -> None:
        """Give up a creation claim (the attempt failed; a retry claims again)."""
        self._x(
            "DELETE FROM ticket_links WHERE tenant_id=? AND provider=? AND candidate=? "
            "AND issue_id IS NULL",
            (self.tenant, provider, candidate),
        )

    def tickets(self) -> list[dict[str, Any]]:
        return self._q(
            "SELECT provider, candidate, issue_id, issue_key, url, created_at FROM ticket_links "
            "WHERE tenant_id=? ORDER BY created_at, provider, candidate",
            (self.tenant,),
        )

    def ticket_updated(self, provider: str, decision_id: str) -> bool:
        return bool(
            self._q(
                "SELECT 1 FROM ticket_updates WHERE tenant_id=? AND provider=? AND decision_id=?",
                (self.tenant, provider, decision_id),
            )
        )

    def mark_ticket_updated(
        self, provider: str, decision_id: str, candidate: str, now: datetime
    ) -> None:
        self._x(
            "INSERT INTO ticket_updates (tenant_id, provider, decision_id, candidate, at) "
            "VALUES (?,?,?,?,?) ON CONFLICT (tenant_id, provider, decision_id) DO NOTHING",
            (self.tenant, provider, decision_id, candidate, ts(now)),
        )

    # --- single use -----------------------------------------------------------------

    def consume_nonce(self, nonce: str, purpose: str, now: datetime) -> bool:
        """True the first time; False if this nonce was used before."""
        rows = self._q(
            "INSERT INTO link_nonces (tenant_id, nonce, purpose, used_at) VALUES (?,?,?,?) "
            "ON CONFLICT (tenant_id, nonce) DO NOTHING RETURNING nonce",
            (self.tenant, nonce, purpose, ts(now)),
        )
        return bool(rows)

    def nonce_used(self, nonce: str) -> bool:
        return bool(
            self._q("SELECT 1 FROM link_nonces WHERE tenant_id=? AND nonce=?", (self.tenant, nonce))
        )


__all__ = [
    "DDL",
    "TABLES",
    "Attempt",
    "Delivery",
    "Endpoint",
    "GateEvent",
    "IntegrationStore",
]
