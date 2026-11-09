"""Approval policy: N of M named approvers, a required role, separation of duties, expiry.

Pure. The policy, each request, each grant and the final quorum are audit events
appended by `AgentGate`; `project_approvals` rebuilds the current request and its
grants from the hash chain, so approvals are as tamper-evident as decisions.

Rules:
- only named approvers may approve; each counts once per request;
- separation of duties: whoever requested the approval, the rule's owner and the
  agent that learned it can never approve it;
- at least `required` distinct, unexpired grants, and (if set) at least one of them
  from an approver holding `required_role`;
- a grant counts for `grant_ttl_seconds`; a request stays open for
  `request_ttl_seconds`. Grants belong to one request: a new request starts again.

Roles and thresholds are configuration, not a statement about what any real
institution requires (ASSUMPTION).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scruple.audit.models import AuditEntry
from scruple.registry.models import ACTOR_PATTERN, RuleVersion

EVENT_POLICY_SET = "approval.policy_set"
EVENT_REQUESTED = "approval.requested"
EVENT_GRANTED = "approval.granted"
EVENT_SATISFIED = "approval.satisfied"
APPROVAL_EVENTS = (EVENT_POLICY_SET, EVENT_REQUESTED, EVENT_GRANTED, EVENT_SATISFIED)

ROLE_PATTERN = r"^[a-z][a-z0-9-]{1,63}$"


class ApprovalError(PermissionError):
    """Base for every refused approval action."""


class NotNamedApproverError(ApprovalError):
    pass


class SeparationOfDutiesError(ApprovalError):
    pass


class ApprovalExpiredError(ApprovalError):
    pass


class DuplicateApprovalError(ApprovalError):
    pass


class NoOpenRequestError(ApprovalError):
    pass


class NamedApprover(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=ACTOR_PATTERN)
    roles: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _roles(self) -> NamedApprover:
        if any(not re.match(ROLE_PATTERN, r) for r in self.roles):
            raise ValueError(f"role names must match {ROLE_PATTERN}")
        return self


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approvers: tuple[NamedApprover, ...] = Field(min_length=1, max_length=100)
    required: int = Field(ge=1)
    required_role: str | None = Field(default=None, pattern=ROLE_PATTERN)
    grant_ttl_seconds: int = Field(default=72 * 3600, ge=60)
    request_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60)

    @model_validator(mode="after")
    def _consistent(self) -> ApprovalPolicy:
        names = [a.name for a in self.approvers]
        if len(set(names)) != len(names):
            raise ValueError("approver names must be unique")
        if self.required > len(names):
            raise ValueError("required approvals exceed the number of named approvers")
        if self.required_role and not any(self.required_role in a.roles for a in self.approvers):
            raise ValueError(f"no named approver holds the required role {self.required_role}")
        return self

    def approver(self, name: str) -> NamedApprover | None:
        return next((a for a in self.approvers if a.name == name), None)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    version: int
    content_hash: str
    proposer: str
    requested_at: datetime
    expires_at: datetime
    seq: int


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(frozen=True)

    approver: str
    roles: tuple[str, ...]
    granted_at: datetime
    expires_at: datetime
    seq: int


class ApprovalStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    satisfied: bool
    counted: tuple[str, ...]
    expired: tuple[str, ...]
    missing: int
    role_satisfied: bool
    request_open: bool
    reasons: tuple[str, ...]


def new_request(
    policy: ApprovalPolicy, rv: RuleVersion, proposer: str, now: datetime, seq: int = 0
) -> ApprovalRequest:
    return ApprovalRequest(
        rule_id=rv.rule_id,
        version=rv.version,
        content_hash=rv.content_hash,
        proposer=proposer,
        requested_at=now,
        expires_at=now + timedelta(seconds=policy.request_ttl_seconds),
        seq=seq,
    )


def check_grant(
    policy: ApprovalPolicy,
    request: ApprovalRequest | None,
    rv: RuleVersion,
    grants: Sequence[ApprovalGrant],
    approver: str,
    now: datetime,
) -> NamedApprover:
    """Raise if `approver` may not approve now; return their policy entry otherwise."""
    if request is None:
        raise NoOpenRequestError(f"{rv.ref} has no open approval request")
    if now >= request.expires_at:
        raise ApprovalExpiredError(f"approval request for {rv.ref} expired")
    if request.content_hash != rv.content_hash:
        raise ApprovalError("approval request does not match the rule's content")
    named = policy.approver(approver)
    if named is None:
        raise NotNamedApproverError(f"{approver} is not a named approver")
    if approver == request.proposer:
        raise SeparationOfDutiesError("separation of duties: the proposer cannot approve")
    if approver in (rv.owner, rv.provenance.agent_id):
        raise SeparationOfDutiesError(
            "separation of duties: the rule's owner or learning agent cannot approve"
        )
    if any(g.approver == approver and now < g.expires_at for g in grants):
        raise DuplicateApprovalError(f"{approver} has already approved this request")
    return named


def new_grant(
    policy: ApprovalPolicy, named: NamedApprover, now: datetime, seq: int = 0
) -> ApprovalGrant:
    return ApprovalGrant(
        approver=named.name,
        roles=named.roles,
        granted_at=now,
        expires_at=now + timedelta(seconds=policy.grant_ttl_seconds),
        seq=seq,
    )


def evaluate_approvals(
    policy: ApprovalPolicy,
    request: ApprovalRequest | None,
    grants: Sequence[ApprovalGrant],
    now: datetime,
) -> ApprovalStatus:
    request_open = request is not None and now < request.expires_at
    live: dict[str, ApprovalGrant] = {}
    expired: list[str] = []
    for g in grants:
        named = policy.approver(g.approver)
        if named is None or (request is not None and g.approver == request.proposer):
            continue  # removed from the policy since, or never allowed: does not count
        if now >= g.expires_at:
            expired.append(g.approver)
        else:
            live[g.approver] = g
    # Roles come from the current policy, so removing a role revokes its weight.
    role_ok = policy.required_role is None or any(
        policy.required_role in _roles(policy, n) for n in live
    )
    counted = tuple(sorted(live))
    missing = max(0, policy.required - len(counted))
    reasons: list[str] = []
    if not request_open:
        reasons.append("no open approval request")
    if missing:
        reasons.append(f"needs {missing} more approval(s) of {policy.required}")
    if not role_ok:
        reasons.append(f"needs an approval from role {policy.required_role}")
    return ApprovalStatus(
        satisfied=request_open and not missing and role_ok,
        counted=counted,
        expired=tuple(sorted(set(expired) - set(live))),
        missing=missing,
        role_satisfied=role_ok,
        request_open=request_open,
        reasons=tuple(reasons),
    )


def _roles(policy: ApprovalPolicy, name: str) -> tuple[str, ...]:
    named = policy.approver(name)
    return () if named is None else named.roles


def project_policy(entries: Sequence[AuditEntry]) -> ApprovalPolicy | None:
    policy: ApprovalPolicy | None = None
    for e in entries:
        if e.event_type == EVENT_POLICY_SET:
            policy = ApprovalPolicy.model_validate(e.payload["policy"])
    return policy


def project_approvals(
    entries: Sequence[AuditEntry], rule_id: str, version: int
) -> tuple[ApprovalRequest | None, tuple[ApprovalGrant, ...]]:
    """The latest request for this version (None once satisfied) and its grants."""
    request: ApprovalRequest | None = None
    grants: list[ApprovalGrant] = []
    for e in entries:
        if e.event_type not in APPROVAL_EVENTS or e.event_type == EVENT_POLICY_SET:
            continue
        p: dict[str, Any] = e.payload
        if p.get("rule_id") != rule_id or int(p.get("version", -1)) != version:
            continue
        if e.event_type == EVENT_REQUESTED:
            request = ApprovalRequest(
                rule_id=rule_id,
                version=version,
                content_hash=p["content_hash"],
                proposer=p["proposer"],
                requested_at=datetime.fromisoformat(p["requested_at"]),
                expires_at=datetime.fromisoformat(p["expires_at"]),
                seq=e.seq,
            )
            grants = []
        elif e.event_type == EVENT_GRANTED and request is not None:
            if int(p["request_seq"]) != request.seq:
                continue
            grants.append(
                ApprovalGrant(
                    approver=p["approver"],
                    roles=tuple(p["roles"]),
                    granted_at=datetime.fromisoformat(p["granted_at"]),
                    expires_at=datetime.fromisoformat(p["expires_at"]),
                    seq=e.seq,
                )
            )
        elif e.event_type == EVENT_SATISFIED:
            request, grants = None, []
    return request, tuple(grants)
