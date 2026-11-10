"""Storage interfaces: one repository per aggregate, plus the `Storage` facade.

There are three history aggregates, each with its own repository protocol:

- `RuleVersionRepository`: versioned rules. Content is immutable; only `status` and
  the `rollback_to` pointer may change, and nothing is ever deleted.
- `AuditRepository`: the hash-chained audit log. Append-only.
- `DecisionRepository`: gate decisions (stored as JSON bodies). Append-only.

`ApiKeyRepository` holds API keys (scruple.auth): mutable only in `revoked_at` and
`last_used_at`, never deleted.

`Transactional` is the unit of work. Every backend (SQLite, Postgres) implements all
of them in one object, `Storage`, because a promotion step must write a status change,
a decision and its audit entries atomically. Callers that need only one aggregate
should depend on that narrower protocol.

Append-only guarantees are enforced twice: by these interfaces (there are no
update/delete methods for history) and by each backend's database (triggers), so a
stray SQL statement also fails. Neither stops someone with raw database access; the
hash chain in `scruple.audit` and `AgentGate.verify_integrity` detect that case.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # runtime imports would cycle: registry -> storage -> registry
    from contextlib import AbstractContextManager
    from datetime import datetime

    from scruple.audit.models import AuditEntry
    from scruple.auth.models import ApiKeyRecord
    from scruple.registry.models import RuleStatus, RuleVersion


@runtime_checkable
class Transactional(Protocol):
    def transaction(self) -> AbstractContextManager[None]:
        """Re-entrant unit of work. Only the outermost level commits or rolls back."""
        ...


@runtime_checkable
class RuleVersionRepository(Protocol):
    def insert_rule_version(self, rv: RuleVersion) -> None: ...
    def update_rule_status(self, rule_id: str, version: int, status: RuleStatus) -> None: ...
    def set_rollback_to(self, rule_id: str, version: int, rollback_to: int | None) -> None: ...
    def get_rule_version(self, rule_id: str, version: int) -> RuleVersion | None: ...
    def list_rule_versions(self, rule_id: str | None = None) -> list[RuleVersion]:
        """All versions ordered by (rule_id, version), or one rule's ordered by version."""
        ...


@runtime_checkable
class AuditRepository(Protocol):
    def append_audit(self, entry: AuditEntry) -> None: ...
    def last_audit(self) -> AuditEntry | None: ...
    def list_audit(self) -> list[AuditEntry]:
        """Every entry ordered by seq."""
        ...


@runtime_checkable
class DecisionRepository(Protocol):
    def insert_decision(self, decision_id: str, rule_id: str, version: int, body: str) -> None: ...
    def get_decision(self, decision_id: str) -> str | None: ...
    def list_decisions(self, rule_id: str | None = None) -> list[str]:
        """Decision bodies ordered by decision_id."""
        ...

    def count_decisions(self) -> int: ...


@runtime_checkable
class ApiKeyRepository(Protocol):
    """API keys (scruple.auth). Not history, but never deleted: only `revoked_at`
    (once) and `last_used_at` change. Lookup by id spans tenants, because the key
    id is all a request carries; the record says which tenant it belongs to."""

    def insert_api_key(self, rec: ApiKeyRecord) -> None: ...
    def get_api_key(self, key_id: str) -> ApiKeyRecord | None: ...
    def list_api_keys(self, tenant: str) -> list[ApiKeyRecord]:
        """One tenant's keys ordered by created_at, key_id."""
        ...

    def revoke_api_key(self, key_id: str, at: datetime) -> None: ...
    def touch_api_key(self, key_id: str, at: datetime) -> None: ...


@runtime_checkable
class Storage(
    Transactional,
    RuleVersionRepository,
    AuditRepository,
    DecisionRepository,
    ApiKeyRepository,
    Protocol,
):
    """Everything the service needs from a backend, in one transactional object.

    A store is bound to one tenant (`tenant`); history reads and writes see only
    that tenant's rows. `for_tenant` gives a view of the same connection bound to
    another tenant."""

    tenant: str

    def for_tenant(self, tenant: str) -> Storage: ...
    def close(self) -> None: ...


class StorageError(Exception):
    """A backend failure, translated from the driver's own exception type."""


class DuplicateKeyError(StorageError):
    """A record with this key already exists (rule version, audit seq, decision id)."""


class ImmutableHistoryError(StorageError):
    """The database refused to change or delete history (append-only trigger fired)."""
