"""Repository implementations shared by every SQL backend.

Each aggregate has its own repository class. They hold no connection state of their
own: they run portable SQL (`?` placeholders) through a `Connection`, which each
backend (SQLite, Postgres) adapts to its driver, its placeholder style, its
transaction model and its error types. `SQLStorage` is the facade that composes the
three repositories behind the `Storage` protocol.

Row encoding is identical on every backend (timestamps as ISO 8601 text, JSON as
text), so what was hashed is byte-for-byte what is read back.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, Self

from scruple.audit.models import AuditEntry
from scruple.domain import RuleSpec
from scruple.registry.models import Provenance, RuleStatus, RuleVersion

# Kept in step with scruple.auth.models (a test checks): importing it here would
# cycle through the registry.
DEFAULT_TENANT = "default"
TENANT_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"

if TYPE_CHECKING:  # scruple.auth.models -> registry -> storage: import late
    from scruple.auth.models import ApiKeyRecord

Row = Mapping[str, Any]
Params = Sequence[object]


class Connection(Protocol):
    """What a repository needs from a backend connection."""

    def transaction(self) -> AbstractContextManager[None]: ...
    def execute(self, sql: str, params: Params = ()) -> None: ...
    def query(self, sql: str, params: Params = ()) -> list[Row]: ...
    def close(self) -> None: ...


# --- codecs ---------------------------------------------------------------------


def encode_rule(rv: RuleVersion) -> tuple[object, ...]:
    return (
        rv.rule_id,
        rv.version,
        rv.content_hash,
        rv.spec.model_dump_json(),
        rv.owner,
        rv.provenance.model_dump_json(),
        rv.created_at.isoformat(),
        rv.status.value,
        rv.rollback_to,
    )


def decode_rule(row: Row) -> RuleVersion:
    return RuleVersion(
        rule_id=row["rule_id"],
        version=row["version"],
        content_hash=row["content_hash"],
        spec=RuleSpec.model_validate_json(row["spec_json"]),
        owner=row["owner"],
        provenance=Provenance.model_validate_json(row["provenance_json"]),
        status=RuleStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        rollback_to=row["rollback_to"],
    )


def encode_audit(entry: AuditEntry) -> tuple[object, ...]:
    return (
        entry.seq,
        entry.at.isoformat(),
        entry.event_type,
        entry.actor,
        json.dumps(entry.payload, sort_keys=True),
        entry.prev_hash,
        entry.entry_hash,
    )


def decode_audit(row: Row) -> AuditEntry:
    return AuditEntry(
        seq=row["seq"],
        at=datetime.fromisoformat(row["at"]),
        event_type=row["event_type"],
        actor=row["actor"],
        payload=json.loads(row["payload_json"]),
        prev_hash=row["prev_hash"],
        entry_hash=row["entry_hash"],
    )


def _ts(d: datetime | None) -> str | None:
    return d.isoformat() if d is not None else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s is not None else None


def encode_key(rec: ApiKeyRecord) -> tuple[object, ...]:
    return (
        rec.key_id,
        rec.tenant,
        rec.subject,
        json.dumps(list(rec.roles)),
        json.dumps([s.value for s in rec.scopes]),
        rec.secret_hash,
        rec.created_at.isoformat(),
        rec.created_by,
        rec.description,
        _ts(rec.expires_at),
        _ts(rec.revoked_at),
        _ts(rec.last_used_at),
        rec.rate_per_minute,
        rec.burst,
        rec.max_body_bytes,
    )


def decode_key(row: Row) -> ApiKeyRecord:
    from scruple.auth.models import ApiKeyRecord, Permission

    return ApiKeyRecord(
        key_id=row["key_id"],
        tenant=row["tenant_id"],
        subject=row["subject"],
        roles=tuple(json.loads(row["roles_json"])),
        scopes=tuple(Permission(s) for s in json.loads(row["scopes_json"])),
        secret_hash=row["secret_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        created_by=row["created_by"],
        description=row["description"],
        expires_at=_dt(row["expires_at"]),
        revoked_at=_dt(row["revoked_at"]),
        last_used_at=_dt(row["last_used_at"]),
        rate_per_minute=row["rate_per_minute"],
        burst=row["burst"],
        max_body_bytes=row["max_body_bytes"],
    )


# --- repositories ---------------------------------------------------------------

_RULE_COLS = (
    "rule_id, version, content_hash, spec_json, owner, provenance_json, created_at, "
    "status, rollback_to"
)
_AUDIT_COLS = "seq, at, event_type, actor, payload_json, prev_hash, entry_hash"


class _Scoped:
    """A repository bound to one tenant: every statement filters on `tenant_id`."""

    def __init__(self, conn: Connection, tenant: str = DEFAULT_TENANT) -> None:
        self._c = conn
        self._t = tenant


class SQLRuleVersions(_Scoped):
    def insert_rule_version(self, rv: RuleVersion) -> None:
        self._c.execute(
            f"INSERT INTO rule_versions (tenant_id, {_RULE_COLS}) "  # noqa: S608
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self._t, *encode_rule(rv)),
        )

    def update_rule_status(self, rule_id: str, version: int, status: RuleStatus) -> None:
        self._c.execute(
            "UPDATE rule_versions SET status=? WHERE tenant_id=? AND rule_id=? AND version=?",
            (status.value, self._t, rule_id, version),
        )

    def set_rollback_to(self, rule_id: str, version: int, rollback_to: int | None) -> None:
        self._c.execute(
            "UPDATE rule_versions SET rollback_to=? WHERE tenant_id=? AND rule_id=? AND version=?",
            (rollback_to, self._t, rule_id, version),
        )

    def get_rule_version(self, rule_id: str, version: int) -> RuleVersion | None:
        rows = self._c.query(
            f"SELECT {_RULE_COLS} FROM rule_versions "  # noqa: S608
            "WHERE tenant_id=? AND rule_id=? AND version=?",
            (self._t, rule_id, version),
        )
        return decode_rule(rows[0]) if rows else None

    def list_rule_versions(self, rule_id: str | None = None) -> list[RuleVersion]:
        if rule_id is None:
            rows = self._c.query(
                f"SELECT {_RULE_COLS} FROM rule_versions WHERE tenant_id=? "  # noqa: S608
                "ORDER BY rule_id, version",
                (self._t,),
            )
        else:
            rows = self._c.query(
                f"SELECT {_RULE_COLS} FROM rule_versions "  # noqa: S608
                "WHERE tenant_id=? AND rule_id=? ORDER BY version",
                (self._t, rule_id),
            )
        return [decode_rule(r) for r in rows]


class SQLAudit(_Scoped):
    def append_audit(self, entry: AuditEntry) -> None:
        self._c.execute(
            f"INSERT INTO audit_events (tenant_id, {_AUDIT_COLS}) "  # noqa: S608
            "VALUES (?,?,?,?,?,?,?,?)",
            (self._t, *encode_audit(entry)),
        )

    def last_audit(self) -> AuditEntry | None:
        rows = self._c.query(
            f"SELECT {_AUDIT_COLS} FROM audit_events WHERE tenant_id=? "  # noqa: S608
            "ORDER BY seq DESC LIMIT 1",
            (self._t,),
        )
        return decode_audit(rows[0]) if rows else None

    def list_audit(self) -> list[AuditEntry]:
        rows = self._c.query(
            f"SELECT {_AUDIT_COLS} FROM audit_events WHERE tenant_id=? ORDER BY seq",  # noqa: S608
            (self._t,),
        )
        return [decode_audit(r) for r in rows]


class SQLDecisions(_Scoped):
    def insert_decision(self, decision_id: str, rule_id: str, version: int, body: str) -> None:
        self._c.execute(
            "INSERT INTO decisions (tenant_id, decision_id, rule_id, version, body_json) "
            "VALUES (?,?,?,?,?)",
            (self._t, decision_id, rule_id, version, body),
        )

    def get_decision(self, decision_id: str) -> str | None:
        rows = self._c.query(
            "SELECT body_json FROM decisions WHERE tenant_id=? AND decision_id=?",
            (self._t, decision_id),
        )
        return str(rows[0]["body_json"]) if rows else None

    def list_decisions(self, rule_id: str | None = None) -> list[str]:
        if rule_id is None:
            rows = self._c.query(
                "SELECT body_json FROM decisions WHERE tenant_id=? ORDER BY decision_id",
                (self._t,),
            )
        else:
            rows = self._c.query(
                "SELECT body_json FROM decisions WHERE tenant_id=? AND rule_id=? "
                "ORDER BY decision_id",
                (self._t, rule_id),
            )
        return [str(r["body_json"]) for r in rows]

    def count_decisions(self) -> int:
        rows = self._c.query("SELECT COUNT(*) AS n FROM decisions WHERE tenant_id=?", (self._t,))
        return int(rows[0]["n"])


_KEY_COLS = (
    "key_id, tenant_id, subject, roles_json, scopes_json, secret_hash, created_at, "
    "created_by, description, expires_at, revoked_at, last_used_at, rate_per_minute, "
    "burst, max_body_bytes"
)


class SQLApiKeys:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def insert_api_key(self, rec: ApiKeyRecord) -> None:
        self._c.execute(
            f"INSERT INTO api_keys ({_KEY_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: S608
            encode_key(rec),
        )

    def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        rows = self._c.query(
            f"SELECT {_KEY_COLS} FROM api_keys WHERE key_id=?",  # noqa: S608
            (key_id,),
        )
        return decode_key(rows[0]) if rows else None

    def list_api_keys(self, tenant: str) -> list[ApiKeyRecord]:
        rows = self._c.query(
            f"SELECT {_KEY_COLS} FROM api_keys WHERE tenant_id=? "  # noqa: S608
            "ORDER BY created_at, key_id",
            (tenant,),
        )
        return [decode_key(r) for r in rows]

    def revoke_api_key(self, key_id: str, at: datetime) -> None:
        self._c.execute(
            "UPDATE api_keys SET revoked_at=? WHERE key_id=? AND revoked_at IS NULL",
            (at.isoformat(), key_id),
        )

    def touch_api_key(self, key_id: str, at: datetime) -> None:
        self._c.execute(
            "UPDATE api_keys SET last_used_at=? WHERE key_id=?", (at.isoformat(), key_id)
        )


# --- facade ---------------------------------------------------------------------


class SQLStorage:
    """The `Storage` facade: one connection, the repositories, one unit of work.

    Every write runs inside `transaction()`, so a single call is atomic on its own
    and several calls nest into one atomic unit.

    A store is bound to one tenant (`tenant`, default "default"): rules, audit
    entries and decisions are read and written for that tenant only, so each tenant
    has its own registry, decision numbering and audit hash chain. `for_tenant(t)`
    returns a view of the same connection bound to tenant `t`. API keys are the
    exception: they are looked up by id across tenants (the record names its tenant).
    """

    def __init__(self, conn: Connection, tenant: str = DEFAULT_TENANT) -> None:
        from scruple.storage.episodes import SQLEpisodes
        from scruple.storage.imported_shadow import SQLImportedShadow

        if not re.match(TENANT_PATTERN, tenant):
            raise ValueError(f"invalid tenant id {tenant!r}")
        self._conn = conn
        self.tenant = tenant
        self.rules = SQLRuleVersions(conn, tenant)
        self.audit = SQLAudit(conn, tenant)
        self.decisions = SQLDecisions(conn, tenant)
        self.api_keys = SQLApiKeys(conn)
        # Runtime state beyond the history aggregates (each its own repository). These
        # take the tenant on every call, so one instance serves every tenant view.
        self.imported_shadow = SQLImportedShadow(conn)
        self.episodes = SQLEpisodes(conn)

    def for_tenant(self, tenant: str) -> Self:
        """The same connection and transaction, bound to `tenant`. Closing any
        view closes the shared connection."""
        if tenant == self.tenant:
            return self
        view = copy.copy(self)
        SQLStorage.__init__(view, self._conn, tenant)
        return view

    def close(self) -> None:
        self._conn.close()

    def transaction(self) -> AbstractContextManager[None]:
        return self._conn.transaction()

    def unsafe_execute(self, sql: str) -> None:
        """Raw SQL, for tests and the tamper demo only."""
        self._conn.execute(sql)

    # rule versions
    def insert_rule_version(self, rv: RuleVersion) -> None:
        with self.transaction():
            self.rules.insert_rule_version(rv)

    def update_rule_status(self, rule_id: str, version: int, status: RuleStatus) -> None:
        with self.transaction():
            self.rules.update_rule_status(rule_id, version, status)

    def set_rollback_to(self, rule_id: str, version: int, rollback_to: int | None) -> None:
        with self.transaction():
            self.rules.set_rollback_to(rule_id, version, rollback_to)

    def get_rule_version(self, rule_id: str, version: int) -> RuleVersion | None:
        return self.rules.get_rule_version(rule_id, version)

    def list_rule_versions(self, rule_id: str | None = None) -> list[RuleVersion]:
        return self.rules.list_rule_versions(rule_id)

    # audit
    def append_audit(self, entry: AuditEntry) -> None:
        with self.transaction():
            self.audit.append_audit(entry)

    def last_audit(self) -> AuditEntry | None:
        return self.audit.last_audit()

    def list_audit(self) -> list[AuditEntry]:
        return self.audit.list_audit()

    # decisions
    def insert_decision(self, decision_id: str, rule_id: str, version: int, body: str) -> None:
        with self.transaction():
            self.decisions.insert_decision(decision_id, rule_id, version, body)

    def get_decision(self, decision_id: str) -> str | None:
        return self.decisions.get_decision(decision_id)

    def list_decisions(self, rule_id: str | None = None) -> list[str]:
        return self.decisions.list_decisions(rule_id)

    def count_decisions(self) -> int:
        return self.decisions.count_decisions()

    # api keys
    def insert_api_key(self, rec: ApiKeyRecord) -> None:
        with self.transaction():
            self.api_keys.insert_api_key(rec)

    def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        return self.api_keys.get_api_key(key_id)

    def list_api_keys(self, tenant: str) -> list[ApiKeyRecord]:
        return self.api_keys.list_api_keys(tenant)

    def revoke_api_key(self, key_id: str, at: datetime) -> None:
        with self.transaction():
            self.api_keys.revoke_api_key(key_id, at)

    def touch_api_key(self, key_id: str, at: datetime) -> None:
        with self.transaction():
            self.api_keys.touch_api_key(key_id, at)
