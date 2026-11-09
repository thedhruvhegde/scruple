"""Registry service: the only way rule versions are created or change status."""

from __future__ import annotations

from typing import Any

from scruple.audit.log import AuditLog
from scruple.clock import Clock
from scruple.domain import RuleSpec
from scruple.hashing import content_hash
from scruple.registry.models import Provenance, RuleStatus, RuleVersion
from scruple.registry.state import PRIVILEGED, InvalidTransitionError, check_transition
from scruple.storage import Storage

MAX_VERSION = 2**63 - 1  # the largest version a SQL BIGINT / SQLite INTEGER can hold


class RuleNotFoundError(KeyError):
    pass


class DuplicateContentError(ValueError):
    pass


class Registry:
    def __init__(self, storage: Storage, audit: AuditLog, clock: Clock) -> None:
        self._s = storage
        self._audit = audit
        self._clock = clock

    # --- creation ------------------------------------------------------------

    def register_candidate(
        self, rule_id: str, spec: RuleSpec, owner: str, provenance: Provenance
    ) -> RuleVersion:
        return self._create(rule_id, spec, owner, provenance, RuleStatus.CANDIDATE, owner)

    def import_baseline(self, rule_id: str, spec: RuleSpec, owner: str, actor: str) -> RuleVersion:
        """Bring an existing, already-approved rule under control as ACTIVE."""
        prov = Provenance(agent_id=actor, notes="imported baseline rule")
        return self._create(rule_id, spec, owner, prov, RuleStatus.ACTIVE, actor)

    def _create(
        self,
        rule_id: str,
        spec: RuleSpec,
        owner: str,
        provenance: Provenance,
        status: RuleStatus,
        actor: str,
    ) -> RuleVersion:
        h = content_hash(spec)
        with self._s.transaction():
            existing = self._s.list_rule_versions(rule_id)
            if any(v.content_hash == h for v in existing):
                # Refusing re-submission stops "resubmit until a lucky seed passes".
                raise DuplicateContentError(f"{rule_id} already has a version with hash {h}")
            bk = content_hash(spec.behavior_key())
            same = [v.ref for v in existing if content_hash(v.spec.behavior_key()) == bk]
            if same:
                # Rewording the description does not make it a new rule.
                raise DuplicateContentError(f"behaviour identical to {', '.join(same)}")
            if status is RuleStatus.ACTIVE and any(v.status is RuleStatus.ACTIVE for v in existing):
                raise InvalidTransitionError(f"{rule_id} already has an active version")
            rv = RuleVersion(
                rule_id=rule_id,
                version=len(existing) + 1,
                content_hash=h,
                spec=spec,
                owner=owner,
                provenance=provenance,
                status=status,
                created_at=self._clock.now(),
            )
            self._s.insert_rule_version(rv)
            event = "rule.baseline_imported" if status is RuleStatus.ACTIVE else "rule.registered"
            self._log(
                event,
                actor,
                rv,
                {
                    "status": status.value,
                    "owner": owner,
                    "provenance": provenance.model_dump(mode="json"),
                    "spec": spec.model_dump(mode="json"),
                },
            )
        return rv

    # --- reads ---------------------------------------------------------------

    def get(self, rule_id: str, version: int) -> RuleVersion:
        if not 1 <= version <= MAX_VERSION:  # out of SQL integer range: cannot exist
            raise RuleNotFoundError(f"{rule_id}@v{version}")
        rv = self._s.get_rule_version(rule_id, version)
        if rv is None:
            raise RuleNotFoundError(f"{rule_id}@v{version}")
        return rv

    def versions(self, rule_id: str) -> list[RuleVersion]:
        return self._s.list_rule_versions(rule_id)

    def all_versions(self) -> list[RuleVersion]:
        return self._s.list_rule_versions()

    def active_rules(self) -> list[RuleVersion]:
        rules = [v for v in self._s.list_rule_versions() if v.status is RuleStatus.ACTIVE]
        return sorted(rules, key=lambda r: (r.spec.priority, r.rule_id))

    def active_version(self, rule_id: str) -> RuleVersion | None:
        for v in self._s.list_rule_versions(rule_id):
            if v.status is RuleStatus.ACTIVE:
                return v
        return None

    # --- status changes --------------------------------------------------------

    def transition(
        self,
        rule_id: str,
        version: int,
        to: RuleStatus,
        actor: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> RuleVersion:
        with self._s.transaction():
            rv = self.get(rule_id, version)
            check_transition(rv.status, to)
            if (rv.status, to) in PRIVILEGED:
                raise InvalidTransitionError(
                    f"{rv.status.value} -> {to.value} must use activate() or rollback()"
                )
            return self._set_status(rv, to, actor, reason, "rule.status_changed", extra)

    def activate(self, rule_id: str, version: int, actor: str, reason: str) -> RuleVersion:
        with self._s.transaction():
            rv = self.get(rule_id, version)
            check_transition(rv.status, RuleStatus.ACTIVE)
            if rv.status is not RuleStatus.CANARY:
                raise InvalidTransitionError("only a canary version can be activated")
            previous = self.active_version(rule_id)
            if previous is not None:
                self._s.update_rule_status(rule_id, previous.version, RuleStatus.RETIRED)
            rollback_to = None if previous is None else previous.version
            self._s.set_rollback_to(rule_id, version, rollback_to)
            rv = rv.model_copy(update={"rollback_to": rollback_to})
            return self._set_status(
                rv,
                RuleStatus.ACTIVE,
                actor,
                reason,
                "rule.activated",
                {"retired_version": rollback_to, "rollback_to": rollback_to},
            )

    def rollback(
        self, rule_id: str, actor: str, reason: str
    ) -> tuple[RuleVersion, RuleVersion | None]:
        """Retire the active version and restore the one it replaced (if any)."""
        with self._s.transaction():
            current = self.active_version(rule_id)
            if current is None:
                raise InvalidTransitionError(f"{rule_id} has no active version to roll back")
            self._s.update_rule_status(rule_id, current.version, RuleStatus.RETIRED)
            restored: RuleVersion | None = None
            if current.rollback_to is not None:
                prev = self.get(rule_id, current.rollback_to)
                check_transition(prev.status, RuleStatus.ACTIVE)
                self._s.update_rule_status(rule_id, prev.version, RuleStatus.ACTIVE)
                restored = prev.model_copy(update={"status": RuleStatus.ACTIVE})
            retired = current.model_copy(update={"status": RuleStatus.RETIRED})
            self._log(
                "rule.rolled_back",
                actor,
                retired,
                {
                    "reason": reason,
                    "from_status": "active",
                    "to_status": "retired",
                    "restored_version": None if restored is None else restored.version,
                },
            )
            return retired, restored

    def _set_status(
        self,
        rv: RuleVersion,
        to: RuleStatus,
        actor: str,
        reason: str,
        event: str,
        extra: dict[str, Any] | None,
    ) -> RuleVersion:
        src = rv.status
        self._s.update_rule_status(rv.rule_id, rv.version, to)
        updated = rv.model_copy(update={"status": to})
        payload: dict[str, Any] = {
            "from_status": src.value,
            "to_status": to.value,
            "reason": reason,
        }
        payload.update(extra or {})
        self._log(event, actor, updated, payload)
        return updated

    def _log(self, event: str, actor: str, rv: RuleVersion, payload: dict[str, Any]) -> None:
        body = {"rule_id": rv.rule_id, "version": rv.version, "content_hash": rv.content_hash}
        body.update(payload)
        self._audit.append(event, actor, body)
