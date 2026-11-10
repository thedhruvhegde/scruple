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

import json
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol

from scruple.audit.models import AuditEntry
from scruple.domain import RuleSpec
from scruple.registry.models import Provenance, RuleStatus, RuleVersion

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


# --- repositories ---------------------------------------------------------------

_RULE_COLS = (
    "rule_id, version, content_hash, spec_json, owner, provenance_json, created_at, "
    "status, rollback_to"
)
_AUDIT_COLS = "seq, at, event_type, actor, payload_json, prev_hash, entry_hash"


class SQLRuleVersions:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def insert_rule_version(self, rv: RuleVersion) -> None:
        self._c.execute(
            f"INSERT INTO rule_versions ({_RULE_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",  # noqa: S608
            encode_rule(rv),
        )

    def update_rule_status(self, rule_id: str, version: int, status: RuleStatus) -> None:
        self._c.execute(
            "UPDATE rule_versions SET status=? WHERE rule_id=? AND version=?",
            (status.value, rule_id, version),
        )

    def set_rollback_to(self, rule_id: str, version: int, rollback_to: int | None) -> None:
        self._c.execute(
            "UPDATE rule_versions SET rollback_to=? WHERE rule_id=? AND version=?",
            (rollback_to, rule_id, version),
        )

    def get_rule_version(self, rule_id: str, version: int) -> RuleVersion | None:
        rows = self._c.query(
            f"SELECT {_RULE_COLS} FROM rule_versions WHERE rule_id=? AND version=?",  # noqa: S608
            (rule_id, version),
        )
        return decode_rule(rows[0]) if rows else None

    def list_rule_versions(self, rule_id: str | None = None) -> list[RuleVersion]:
        if rule_id is None:
            rows = self._c.query(
                f"SELECT {_RULE_COLS} FROM rule_versions ORDER BY rule_id, version"  # noqa: S608
            )
        else:
            rows = self._c.query(
                f"SELECT {_RULE_COLS} FROM rule_versions WHERE rule_id=? "  # noqa: S608
                "ORDER BY version",
                (rule_id,),
            )
        return [decode_rule(r) for r in rows]


class SQLAudit:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def append_audit(self, entry: AuditEntry) -> None:
        self._c.execute(
            f"INSERT INTO audit_events ({_AUDIT_COLS}) VALUES (?,?,?,?,?,?,?)",  # noqa: S608
            encode_audit(entry),
        )

    def last_audit(self) -> AuditEntry | None:
        rows = self._c.query(
            f"SELECT {_AUDIT_COLS} FROM audit_events ORDER BY seq DESC LIMIT 1"  # noqa: S608
        )
        return decode_audit(rows[0]) if rows else None

    def list_audit(self) -> list[AuditEntry]:
        rows = self._c.query(f"SELECT {_AUDIT_COLS} FROM audit_events ORDER BY seq")  # noqa: S608
        return [decode_audit(r) for r in rows]


class SQLDecisions:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def insert_decision(self, decision_id: str, rule_id: str, version: int, body: str) -> None:
        self._c.execute(
            "INSERT INTO decisions (decision_id, rule_id, version, body_json) VALUES (?,?,?,?)",
            (decision_id, rule_id, version, body),
        )

    def get_decision(self, decision_id: str) -> str | None:
        rows = self._c.query("SELECT body_json FROM decisions WHERE decision_id=?", (decision_id,))
        return str(rows[0]["body_json"]) if rows else None

    def list_decisions(self, rule_id: str | None = None) -> list[str]:
        if rule_id is None:
            rows = self._c.query("SELECT body_json FROM decisions ORDER BY decision_id")
        else:
            rows = self._c.query(
                "SELECT body_json FROM decisions WHERE rule_id=? ORDER BY decision_id", (rule_id,)
            )
        return [str(r["body_json"]) for r in rows]

    def count_decisions(self) -> int:
        rows = self._c.query("SELECT COUNT(*) AS n FROM decisions")
        return int(rows[0]["n"])


# --- facade ---------------------------------------------------------------------


class SQLStorage:
    """The `Storage` facade: one connection, three repositories, one unit of work.

    Every write runs inside `transaction()`, so a single call is atomic on its own
    and several calls nest into one atomic unit.
    """

    def __init__(self, conn: Connection) -> None:
        from scruple.storage.episodes import SQLEpisodes
        from scruple.storage.imported_shadow import SQLImportedShadow

        self._conn = conn
        self.rules = SQLRuleVersions(conn)
        self.audit = SQLAudit(conn)
        self.decisions = SQLDecisions(conn)
        # Runtime state beyond the three history aggregates (each its own repository).
        self.imported_shadow = SQLImportedShadow(conn)
        self.episodes = SQLEpisodes(conn)

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
