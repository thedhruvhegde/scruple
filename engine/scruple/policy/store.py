"""Per-tenant policies in the gate database, versioned and append-only.

`tenant_policies` holds every policy a tenant has put in force, numbered from 1; the
highest number is current. Rows are never updated or deleted, and every one is
audited (`policy.set` in the tenant's chain, with the policy's hash and number), so
which policy governed a decision is answerable from the audit log alone: each
`decision.recorded` entry made under a policy carries its `policy_sha256`.

The table is created on the first `apply_policy` (`scruple.storage.lazy`); a
database that never had a policy is unchanged and reads as "no policy".
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from scruple.policy.models import Policy
from scruple.storage.lazy import connection_of, ensure_tables, table_exists

TABLE = "tenant_policies"
DDL = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    tenant_id TEXT NOT NULL,
    number BIGINT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    body_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, number)
)""",
)
EVENT_POLICY_APPLIED = "policy.set"


class PolicyRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant: str
    number: int
    policy_sha256: str
    policy: Policy
    actor: str
    created_at: datetime


class PolicyStore:
    """One tenant's policies, over a gate `Storage`'s connection."""

    def __init__(self, storage: object) -> None:
        self._conn, self._dialect = connection_of(storage)
        self.tenant: str = getattr(storage, "tenant", "default")

    def _rows(self, where: str = "", params: tuple[object, ...] = ()) -> list[PolicyRecord]:
        if not table_exists(self._conn, self._dialect, TABLE):
            return []
        rows = self._conn.query(
            f"SELECT number, policy_sha256, body_json, actor, created_at FROM {TABLE} "  # noqa: S608
            f"WHERE tenant_id=? {where} ORDER BY number",
            (self.tenant, *params),
        )
        return [
            PolicyRecord(
                tenant=self.tenant,
                number=int(r["number"]),
                policy_sha256=r["policy_sha256"],
                policy=Policy.model_validate_json(r["body_json"]),
                actor=r["actor"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def history(self) -> list[PolicyRecord]:
        return self._rows()

    def current(self) -> PolicyRecord | None:
        rows = self._rows()
        return rows[-1] if rows else None

    def current_sha256(self) -> str | None:
        """The current policy's hash only (one indexed read; for per-request checks)."""
        if not table_exists(self._conn, self._dialect, TABLE):
            return None
        rows = self._conn.query(
            f"SELECT policy_sha256 FROM {TABLE} WHERE tenant_id=? "  # noqa: S608
            "ORDER BY number DESC LIMIT 1",
            (self.tenant,),
        )
        return str(rows[0]["policy_sha256"]) if rows else None

    def get(self, number: int) -> PolicyRecord | None:
        rows = self._rows("AND number=?", (number,))
        return rows[0] if rows else None

    def append(self, policy: Policy, actor: str, now: datetime) -> PolicyRecord:
        """Store `policy` as the next number. Call inside the gate's transaction."""
        ensure_tables(self._conn, DDL)
        with self._conn.transaction():
            rows = self._conn.query(
                f"SELECT MAX(number) AS n FROM {TABLE} WHERE tenant_id=?",  # noqa: S608
                (self.tenant,),
            )
            number = int(rows[0]["n"] or 0) + 1
            rec = PolicyRecord(
                tenant=self.tenant,
                number=number,
                policy_sha256=policy.policy_sha256,
                policy=policy,
                actor=actor,
                created_at=now,
            )
            self._conn.execute(
                f"INSERT INTO {TABLE} (tenant_id, number, policy_sha256, body_json, actor, "  # noqa: S608
                "created_at) VALUES (?,?,?,?,?,?)",
                (
                    self.tenant,
                    number,
                    rec.policy_sha256,
                    policy.model_dump_json(),
                    actor,
                    now.isoformat(),
                ),
            )
        return rec


__all__ = ["DDL", "EVENT_POLICY_APPLIED", "TABLE", "PolicyRecord", "PolicyStore"]
