"""Roles, the permissions they grant, and separation of duties.

    role         permissions                 who
    proposer     read, propose               a learning agent registering rules
    service      read, shadow                a system feeding shadow mode
    approver     read, approve               a named human approver
    risk_admin   read, approve, admin        model risk: approvals, keys, policy
    auditor      read                        read-only review, audit verification

A credential carries roles, scopes (permissions named directly) or both. Roles
alone grant their permissions; scopes alone grant themselves; both grant the
intersection, so a scope list can only narrow what the roles allow. Scope strings
that are not permissions (`openid`, `profile`) are ignored.

Separation of duties, on top of the gate's own rule (the owner, proposer and agent
of a rule can never approve it, `scruple.gate.approval` and `AgentGate.approve`):
no single credential may hold both `propose` and `approve`. Key creation refuses
it and a token that grants both is refused, so the identity that registers rules
and the identity that activates them are always different credentials.

Role names and their grants are configuration of this engine, not a statement of
what any institution requires (ASSUMPTION).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from scruple.auth.models import ApiKeyRecord, Permission

P = Permission


class Role(StrEnum):
    PROPOSER = "proposer"
    SERVICE = "service"
    APPROVER = "approver"
    RISK_ADMIN = "risk_admin"
    AUDITOR = "auditor"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.PROPOSER: frozenset({P.READ, P.PROPOSE}),
    Role.SERVICE: frozenset({P.READ, P.SHADOW}),
    Role.APPROVER: frozenset({P.READ, P.APPROVE}),
    Role.RISK_ADMIN: frozenset({P.READ, P.APPROVE, P.ADMIN}),
    Role.AUDITOR: frozenset({P.READ}),
}

CONFLICTING: tuple[frozenset[Permission], ...] = (frozenset({P.PROPOSE, P.APPROVE}),)


class SeparationOfDutiesError(ValueError):
    pass


def permissions_for(roles: Iterable[str], scopes: Iterable[str]) -> frozenset[Permission]:
    """What a credential may do. Unknown role or scope names grant nothing."""
    known = {p.value: p for p in Permission}
    from_roles: set[Permission] = set()
    role_names = list(roles)
    for r in role_names:
        if r in Role._value2member_map_:
            from_roles |= ROLE_PERMISSIONS[Role(r)]
    named = frozenset(known[s] for s in scopes if s in known)
    if role_names and named:
        return frozenset(from_roles) & named
    return frozenset(from_roles) if role_names else named


def check_separation(permissions: Iterable[Permission]) -> None:
    held = frozenset(permissions)
    for pair in CONFLICTING:
        if pair <= held:
            names = " and ".join(sorted(p.value for p in pair))
            raise SeparationOfDutiesError(
                f"separation of duties: one credential may not hold both {names}"
            )


def key_permissions(rec: ApiKeyRecord) -> frozenset[Permission]:
    """`ApiKeyService(permissions_for=...)`: roles and scopes, duties separated."""
    granted = permissions_for(rec.roles, [s.value for s in rec.scopes])
    check_separation(granted)
    return granted
