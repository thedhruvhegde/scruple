"""Per-tenant data retention: delete row-level case data after N days, keep the rest.

A policy says "keep row-level case data of tenant T for N days". Applying it deletes,
for that tenant, rows older than `now - N days`:

- gate database: `imported_shadow_events` (the decisions and outcomes production sent
  about real cases), by the time they were stored;
- case store (optional, `CaseStore.purge`): every version of each case opened before
  the cutoff, its ledger movements and the webhook case bodies that carried it.

Offboarding a tenant (`purge_tenant`, `scruple tenant offboard`) runs the same purge
with a cutoff after any stored time, so all of that tenant's row-level data goes.

It keeps everything that is not row-level case data: the audit log, gate decisions,
rule versions, imported-shadow summary snapshots (the aggregates at every point),
ingest run reports and their counts, webhook event ids and payload hashes (still
needed to refuse redeliveries), jobs and their results.

How the audit chain stays verifiable. Nothing in `audit_events` is deleted or edited, so
`scruple audit verify` recomputes the same chain. Each purge appends one audit entry,
`retention.purged`, to the purged tenant's own chain (its `audit_seq` in
`retention_purges` is a seq in that chain; policy changes go there too), whose payload
names the tenant, the table, the cutoff, the number of rows deleted and a SHA-256 digest
over the deleted rows (their keys and body hashes, in key order). The purge is therefore
itself on the tamper-evident record: an auditor holding an anchored head can see that a
deletion happened, when, under which policy, and (from a backup) check that exactly
those rows went. A row that disappears without such an entry is unexplained. Audit
payloads never contain case rows, so no audit entry refers to data that retention
removed.

How deletion gets past the append-only triggers. The triggers stay in place for every
other path. SQLite: the purge drops the one table's DELETE trigger, deletes and
recreates the trigger inside one `BEGIN IMMEDIATE` transaction, so no other writer can
observe the gap. PostgreSQL (migration 0007): the DELETE trigger on
`imported_shadow_events` allows a delete only when the transaction has set
`scruple.retention = 'purge'` (`SET LOCAL`); TRUNCATE stays refused. That setting is
a guard against stray SQL, not an access control: grant DELETE on the table only to
the role that runs retention (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.storage.base import DEFAULT_TENANT
from scruple.storage.sql import TENANT_PATTERN

if TYPE_CHECKING:
    from scruple.ingest.store import CaseStore
    from scruple.service import AgentGate
    from scruple.storage.sql import Connection

EVENT_PURGED = "retention.purged"
EVENT_POLICY = "retention.policy_set"
GUC = "scruple.retention"

_DDL = """
CREATE TABLE IF NOT EXISTS retention_policies (
    tenant_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    days INTEGER NOT NULL CHECK (days >= 1),
    set_at TEXT NOT NULL,
    set_by TEXT NOT NULL,
    PRIMARY KEY (tenant_id, seq)
);
CREATE TABLE IF NOT EXISTS retention_purges (
    tenant_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    store TEXT NOT NULL,
    table_name TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    rows_deleted INTEGER NOT NULL,
    digest_sha256 TEXT NOT NULL,
    audit_seq BIGINT,
    PRIMARY KEY (tenant_id, seq)
);
"""
_TABLES = ("retention_policies", "retention_purges")

SQLITE_DDL = _DDL + "".join(
    f"""CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
"""
    for t in _TABLES
)

# The imported-shadow DELETE trigger, as created by scruple.storage.imported_shadow.
_SQLITE_SHADOW_TRIGGER = """CREATE TRIGGER IF NOT EXISTS imported_shadow_events_no_delete
BEFORE DELETE ON imported_shadow_events
BEGIN SELECT RAISE(ABORT, 'imported_shadow_events is append-only'); END;"""

# Keep in step with migration 0007 (a test compares the catalogs).
POSTGRES_DDL = (
    _DDL
    + "".join(
        f"""
CREATE OR REPLACE TRIGGER {t}_no_update BEFORE UPDATE ON {t}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{t} is append-only');
CREATE OR REPLACE TRIGGER {t}_no_delete BEFORE DELETE ON {t}
    FOR EACH ROW EXECUTE FUNCTION scruple_refuse('{t} is append-only');
CREATE OR REPLACE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t}
    FOR EACH STATEMENT EXECUTE FUNCTION scruple_refuse('{t} is append-only');
"""
        for t in _TABLES
    )
    + f"""
CREATE OR REPLACE FUNCTION scruple_refuse_unless_retention() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('{GUC}', true) = 'purge' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '%', TG_ARGV[0];
END
$$;

CREATE OR REPLACE TRIGGER imported_shadow_events_no_delete BEFORE DELETE
    ON imported_shadow_events FOR EACH ROW
    EXECUTE FUNCTION scruple_refuse_unless_retention('imported_shadow_events is append-only');
"""
)


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(pattern=TENANT_PATTERN)  # the same ids as authentication
    days: int = Field(ge=1, le=36_500)


class PurgeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    store: str  # "gate" or "case_store"
    table: str
    cutoff: datetime
    rows: int
    digest_sha256: str
    audit_seq: int | None


def digest(keys: list[str]) -> str:
    """SHA-256 over the deleted rows' keys (each with its body hash), sorted."""
    h = hashlib.sha256()
    for k in sorted(keys):
        h.update(k.encode("utf-8") + b"\n")
    return h.hexdigest()


class SQLRetention:
    """Policies and purge records over a backend `Connection`, plus the gate purge."""

    def __init__(self, conn: Connection, dialect: str) -> None:
        self._c = conn
        self.dialect = dialect

    def set_policy(self, policy: RetentionPolicy, at: datetime, actor: str) -> None:
        with self._c.transaction():
            rows = self._c.query(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM retention_policies WHERE tenant_id=?",
                (policy.tenant_id,),
            )
            self._c.execute(
                "INSERT INTO retention_policies (tenant_id, seq, days, set_at, set_by) "
                "VALUES (?,?,?,?,?)",
                (policy.tenant_id, int(rows[0]["n"]) + 1, policy.days, at.isoformat(), actor),
            )

    def policies(self) -> list[RetentionPolicy]:
        """The current policy of each tenant (the latest one set)."""
        rows = self._c.query(
            "SELECT p.tenant_id, p.days FROM retention_policies p JOIN ("
            " SELECT tenant_id, MAX(seq) AS s FROM retention_policies GROUP BY tenant_id"
            ") last ON p.tenant_id = last.tenant_id AND p.seq = last.s ORDER BY p.tenant_id"
        )
        return [RetentionPolicy(tenant_id=r["tenant_id"], days=int(r["days"])) for r in rows]

    def record_purge(self, r: PurgeResult, at: datetime) -> None:
        with self._c.transaction():
            rows = self._c.query(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM retention_purges WHERE tenant_id=?",
                (r.tenant_id,),
            )
            self._c.execute(
                "INSERT INTO retention_purges (tenant_id, seq, at, store, table_name, cutoff, "
                "rows_deleted, digest_sha256, audit_seq) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    r.tenant_id,
                    int(rows[0]["n"]) + 1,
                    at.isoformat(),
                    r.store,
                    r.table,
                    r.cutoff.isoformat(),
                    r.rows,
                    r.digest_sha256,
                    r.audit_seq,
                ),
            )

    def purges(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM retention_purges"
        args: tuple[object, ...] = ()
        if tenant_id is not None:
            sql += " WHERE tenant_id=?"
            args = (tenant_id,)
        return [dict(r) for r in self._c.query(sql + " ORDER BY tenant_id, seq", args)]

    def purge_imported_shadow(self, tenant_id: str, cutoff: datetime) -> tuple[int, str]:
        """Delete this tenant's imported-shadow events stored before `cutoff`. Must run
        inside the caller's transaction. Returns (rows, digest)."""
        rows = self._c.query(
            "SELECT rule_id, version, seq, at, kind, body_json, body_sha256 "
            "FROM imported_shadow_events WHERE tenant_id=?",
            (tenant_id,),
        )
        gone = [r for r in rows if datetime.fromisoformat(r["at"]) < cutoff]
        if not gone:
            return 0, digest([])
        self._keep_decision_keys(tenant_id, gone)
        keys = [f"{r['rule_id']}|{r['version']}|{r['seq']}|{r['body_sha256']}" for r in gone]
        if self.dialect == "postgres":
            self._c.execute(f"SET LOCAL {GUC} = 'purge'")
        else:
            self._c.execute("DROP TRIGGER IF EXISTS imported_shadow_events_no_delete")
        for r in gone:
            self._c.execute(
                "DELETE FROM imported_shadow_events WHERE tenant_id=? AND rule_id=? "
                "AND version=? AND seq=?",
                (tenant_id, r["rule_id"], r["version"], r["seq"]),
            )
        if self.dialect == "postgres":
            self._c.execute(f"SET LOCAL {GUC} = ''")
        else:
            self._c.execute(_SQLITE_SHADOW_TRIGGER)
        return len(gone), digest(keys)

    def _keep_decision_keys(self, tenant_id: str, gone: list[Any]) -> None:
        """Before deleting decision batches, keep each decision's key hash (and only
        that) in `imported_shadow_keys`, so a decision re-sent for a purged case is
        still refused (`scruple.storage.imported_shadow`)."""
        from scruple.gate.shadow_imported import ImportedDecisionBatch
        from scruple.storage.imported_shadow import decision_key

        for r in gone:
            if r["kind"] != "decisions":
                continue
            batch = ImportedDecisionBatch.model_validate_json(r["body_json"])
            for d in batch.decisions:
                key = decision_key(tenant_id, r["rule_id"], int(r["version"]), d.case.case_id)
                self._c.execute(
                    "INSERT INTO imported_shadow_keys (tenant_id, rule_id, version, key_sha256, "
                    "seq) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (tenant_id, r["rule_id"], int(r["version"]), key, int(r["seq"])),
                )


def retention_for(storage: object) -> SQLRetention:
    repo = getattr(storage, "retention", None)
    if not isinstance(repo, SQLRetention):
        raise TypeError(f"{type(storage).__name__} has no retention tables")
    return repo


def tenant_gate(gate: AgentGate, tenant: str) -> AgentGate:
    """`gate` over `tenant`'s view of the same storage (same clock and config). Policy
    changes and purges are recorded in the affected tenant's own audit chain, so the
    tenant's auditor, who can verify only that chain, sees every deletion of its data."""
    if getattr(gate.storage, "tenant", DEFAULT_TENANT) == tenant:
        return gate
    from scruple.service import AgentGate

    for_tenant = getattr(gate.storage, "for_tenant", None)
    if for_tenant is None:  # pragma: no cover - every SQL backend has it
        raise TypeError(f"{type(gate.storage).__name__} cannot serve tenant {tenant!r}")
    return AgentGate(for_tenant(tenant), gate.clock, gate.config)


def set_policy(gate: AgentGate, policy: RetentionPolicy, actor: str) -> None:
    """Store a tenant's policy and put the change on that tenant's audit record."""
    repo = retention_for(gate.storage)
    tg = tenant_gate(gate, policy.tenant_id)
    with gate.storage.transaction():
        repo.set_policy(policy, gate.clock.now(), actor)
        tg.record_runtime_event(
            EVENT_POLICY, actor, {"tenant_id": policy.tenant_id, "days": policy.days}
        )


def apply_retention(
    gate: AgentGate,
    *,
    case_stores: dict[str, CaseStore] | None = None,
    actor: str = "retention",
    now: datetime | None = None,
    tenants: Collection[str] | None = None,
) -> list[PurgeResult]:
    """Apply every tenant's current policy (only `tenants`' when given: a scheduled
    job applies its own tenant's). `case_stores` maps a tenant to its case
    store (a case store is one partner's file). Each purge is one transaction with its
    audit entry, so a purge is never on the record without having happened, nor the
    other way round (the case store is a separate file: its purge commits first and
    the audit entry follows; see CaseStore.purge)."""
    repo = retention_for(gate.storage)
    at = now or gate.clock.now()
    out: list[PurgeResult] = []
    for policy in repo.policies():
        if tenants is not None and policy.tenant_id not in tenants:
            continue
        cutoff = at - timedelta(days=policy.days)
        store = (case_stores or {}).get(policy.tenant_id)
        out += _purge(gate, repo, policy.tenant_id, cutoff, store, at, actor, days=policy.days)
    return out


# Offboarding purges everything: a cutoff after any time a row can carry.
END_OF_TIME = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def purge_tenant(
    gate: AgentGate,
    tenant_id: str,
    *,
    case_store: CaseStore | None = None,
    actor: str = "offboarding",
    now: datetime | None = None,
) -> list[PurgeResult]:
    """Delete all of one tenant's row-level case data, whatever its age (offboarding),
    by the same path and with the same audit entries as a policy purge; the entries'
    payload says `reason: offboarding` and has no `days`."""
    repo = retention_for(gate.storage)
    at = now or gate.clock.now()
    return _purge(gate, repo, tenant_id, END_OF_TIME, case_store, at, actor, reason="offboarding")


def _purge(
    gate: AgentGate,
    repo: SQLRetention,
    tenant_id: str,
    cutoff: datetime,
    store: CaseStore | None,
    at: datetime,
    actor: str,
    *,
    days: int | None = None,
    reason: str | None = None,
) -> list[PurgeResult]:
    out: list[PurgeResult] = []

    def record(where: str, table: str, n: int, dig: str) -> None:
        out.append(
            _record(gate, repo, tenant_id, where, table, cutoff, n, dig, at, actor, days, reason)
        )

    with gate.storage.transaction():
        n, dig = repo.purge_imported_shadow(tenant_id, cutoff)
        if n:
            record("gate", "imported_shadow_events", n, dig)
    if store is not None:
        for table, (n, dig) in store.purge(cutoff, tenant_id=tenant_id).items():
            if n:
                with gate.storage.transaction():
                    record("case_store", table, n, dig)
    return out


def _record(
    gate: AgentGate,
    repo: SQLRetention,
    tenant_id: str,
    store: str,
    table: str,
    cutoff: datetime,
    n: int,
    dig: str,
    at: datetime,
    actor: str,
    days: int | None,
    reason: str | None,
) -> PurgeResult:
    payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "store": store,
        "table": table,
        "cutoff": cutoff.isoformat(),
        "rows": n,
        "digest_sha256": dig,
    }
    if days is not None:
        payload["days"] = days
    if reason is not None:
        payload["reason"] = reason
    entry = tenant_gate(gate, tenant_id).record_runtime_event(EVENT_PURGED, actor, payload)
    res = PurgeResult(
        tenant_id=tenant_id,
        store=store,
        table=table,
        cutoff=cutoff,
        rows=n,
        digest_sha256=dig,
        audit_seq=entry.seq,
    )
    repo.record_purge(res, at)
    return res


def cmd_retention(args: Any) -> int:
    """`scruple retention set|show|apply`."""
    import json

    from scruple.service import AgentGate
    from scruple.settings import load_settings

    gate = AgentGate.open(args.db or load_settings().database_dsn())
    try:
        if args.retention_cmd == "set":
            set_policy(gate, RetentionPolicy(tenant_id=args.tenant, days=args.days), args.actor)
        if args.retention_cmd in ("set", "show"):
            pols = retention_for(gate.storage).policies()
            print(json.dumps([p.model_dump() for p in pols], sort_keys=True))
            return 0
        if args.retention_cmd == "schedule":
            from datetime import date

            from scruple.jobs.queue import queue_for
            from scruple.jobs.retention import schedule_retention

            day = date.fromisoformat(args.day) if args.day else gate.clock.now().date()
            queue = queue_for(gate.storage)
            try:
                jobs = schedule_retention(queue, gate.storage, day, actor=args.actor)
            finally:
                from scruple.storage.sqlite import SQLiteStorage

                if not isinstance(gate.storage, SQLiteStorage):  # its own connection
                    queue.close()
            print(
                json.dumps(
                    [
                        {"tenant_id": j.tenant_id, "job_id": j.job_id, "created": created}
                        for j, created in jobs
                    ],
                    sort_keys=True,
                )
            )
            return 0
        stores: dict[str, CaseStore] = {}
        if args.case_store:
            from scruple.ingest.store import CaseStore

            stores[args.tenant] = CaseStore(args.case_store, create=False)
        done = apply_retention(gate, case_stores=stores, actor=args.actor)
        for cs in stores.values():
            cs.close()
        print(json.dumps([r.model_dump(mode="json") for r in done], sort_keys=True))
        return 0
    finally:
        gate.storage.close()


def add_retention_parser(sub: Any, default_db: str) -> None:
    p = sub.add_parser("retention", help="per-tenant retention of row-level case data")
    rs = p.add_subparsers(dest="retention_cmd", required=True)
    for name, help_ in (
        ("set", "set a tenant's policy (days to keep row-level case data)"),
        ("show", "print every tenant's current policy"),
        ("apply", "delete what the policies say is expired (audited)"),
        ("schedule", "queue today's retention_apply job per tenant (once per day)"),
    ):
        sp = rs.add_parser(name, help=help_)
        sp.set_defaults(fn=cmd_retention)
        sp.add_argument(
            "--db",
            default=None,
            help=f"SQLite path or postgresql:// URL (default: $SCRUPLE_DATABASE_URL, "
            f"else {default_db})",
        )
        sp.add_argument("--actor", default="retention", help="who, for the audit log")
        if name in ("set", "apply"):
            sp.add_argument("--tenant", default=DEFAULT_TENANT)
        if name == "set":
            sp.add_argument("--days", type=int, required=True)
        if name == "apply":
            sp.add_argument("--case-store", default=None, help="the tenant's case store file")
        if name == "schedule":
            sp.add_argument("--day", default=None, help="YYYY-MM-DD (default: today, UTC)")


__all__ = [
    "DEFAULT_TENANT",
    "END_OF_TIME",
    "EVENT_POLICY",
    "EVENT_PURGED",
    "POSTGRES_DDL",
    "SQLITE_DDL",
    "PurgeResult",
    "RetentionPolicy",
    "SQLRetention",
    "add_retention_parser",
    "apply_retention",
    "digest",
    "purge_tenant",
    "retention_for",
    "set_policy",
    "tenant_gate",
]
