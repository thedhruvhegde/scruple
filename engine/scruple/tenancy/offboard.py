"""Offboarding a tenant: revoke its keys, purge its case data, retire its id.

`plan_offboarding` says what would happen and changes nothing (the default of
`scruple tenant offboard`). `offboard` does it, in this order, each step audited in
the tenant's own hash chain:

1. `tenant.offboarded` is recorded first, so from then on every credential naming
   the tenant is refused and no new key can be made for it, even if a later step
   fails.
2. Every active API key is revoked (`ApiKeyService.revoke`, one `api_key.revoked`
   entry each).
3. Row-level case data is purged by the retention purge path
   (`scruple.storage.retention.purge_tenant`): every imported-shadow event in the
   gate database and, when the tenant's case store is given, every case, movement
   and webhook case body in it. One `retention.purged` entry per table, with
   `reason: offboarding`, the row count and the digest of what went.

Running it again on an offboarded tenant repeats steps 2 and 3 (nothing is left, so
nothing more happens) and records nothing new, so a run that stopped part way can be
finished.

What stays, as for retention: the audit chain (so `scruple audit verify` still
passes for the tenant), decisions, rule versions, revoked key records, usage events,
imported-shadow summary snapshots (aggregates, no case rows), the key hashes that
refuse re-sent decisions, and webhook event ids and payload hashes in the case
store. These hold no case rows. To remove them too, delete the database, as
DATA_CHECKLIST.md says for a pilot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from scruple.tenancy.directory import TenancyError, TenantDirectory

if TYPE_CHECKING:
    from scruple.auth.keys import ApiKeyService
    from scruple.ingest.store import CaseStore
    from scruple.service import AgentGate
    from scruple.storage.retention import PurgeResult


class OffboardPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant: str
    already_offboarded: bool
    keys_to_revoke: tuple[str, ...]
    gate_rows: dict[str, int]  # table -> rows to purge, gate database
    case_store: str | None
    case_store_rows: dict[str, int]
    kept: tuple[str, ...] = (
        "audit chain",
        "decisions",
        "rule versions",
        "revoked key records",
        "usage events",
        "imported-shadow summaries",
        "decision key hashes",
        "webhook event ids and payload hashes",
    )


class OffboardResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant: str
    recorded: bool  # False when the tenant was already offboarded
    keys_revoked: tuple[str, ...]
    purges: tuple[dict[str, Any], ...]
    audit_head_seq: int | None


def _check_store(tenant: str, store: CaseStore | None) -> None:
    if store is None:
        return
    others = store.webhook_tenants() - {tenant}
    if others:
        raise TenancyError(
            f"case store {store.path} holds deliveries for {', '.join(sorted(others))}; "
            f"a case store is one tenant's file, so it is not {tenant}'s"
        )


def plan_offboarding(
    gate: AgentGate,
    keys: ApiKeyService,
    tenant: str,
    case_store: CaseStore | None = None,
) -> OffboardPlan:
    directory = TenantDirectory(gate.storage, gate.clock)
    if not directory.exists(tenant):
        raise TenancyError(f"no tenant {tenant}")
    _check_store(tenant, case_store)
    now = gate.clock.now()
    return OffboardPlan(
        tenant=tenant,
        already_offboarded=directory.is_offboarded(tenant),
        keys_to_revoke=tuple(k.key_id for k in keys.list(tenant) if k.active(now)),
        gate_rows={"imported_shadow_events": directory.imported_shadow_rows(tenant)},
        case_store=case_store.path if case_store is not None else None,
        case_store_rows=case_store.row_counts(tenant) if case_store is not None else {},
    )


def offboard(
    gate: AgentGate,
    keys: ApiKeyService,
    tenant: str,
    actor: str,
    case_store: CaseStore | None = None,
) -> OffboardResult:
    from scruple.storage.retention import purge_tenant, tenant_gate

    plan = plan_offboarding(gate, keys, tenant, case_store)
    directory = TenantDirectory(gate.storage, gate.clock)
    if not plan.already_offboarded:
        directory.append(
            tenant,
            "offboarded",
            {
                "keys_to_revoke": len(plan.keys_to_revoke),
                "case_store": plan.case_store is not None,
            },
            actor,
        )
    revoked: list[str] = []
    for key_id in plan.keys_to_revoke:
        keys.revoke(key_id, actor, tenant)
        revoked.append(key_id)
    purged: list[PurgeResult] = purge_tenant(gate, tenant, case_store=case_store, actor=actor)
    last = tenant_gate(gate, tenant).storage.last_audit()
    return OffboardResult(
        tenant=tenant,
        recorded=not plan.already_offboarded,
        keys_revoked=tuple(revoked),
        purges=tuple(p.model_dump(mode="json") for p in purged),
        audit_head_seq=last.seq if last is not None else None,
    )


__all__ = ["OffboardPlan", "OffboardResult", "offboard", "plan_offboarding"]
