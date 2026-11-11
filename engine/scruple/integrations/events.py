"""Gate events, read from the audit log: the audit log is the outbox.

Every state change the gate makes is already an audit entry, written in the same
transaction as the change. The dispatcher (`dispatch`) reads a tenant's entries after
its cursor and turns the ones integrations care about into `GateEvent`s, then queues
the work for each (a webhook delivery per subscribed endpoint, a ticket update per
configured tracker, a Slack message per approval request) as jobs with idempotency
keys derived from the event. So:

- nothing is sent for a change that rolled back (there is no entry);
- nothing is lost if a process dies: the cursor only moves after the jobs are queued,
  and queueing the same work twice returns the first job (idempotency key);
- the gate's verdicts never wait on, or fail because of, Slack, Jira or a webhook.

Event ids are `evt_` plus 24 hex of SHA-256(tenant, audit entry hash): stable across
re-dispatch and unique per entry.

Events sent:

    decision.recorded    every gate decision (shadow, canary, activation, rollback, ...)
    approval.requested   a version is waiting for named approvers
    approval.granted     one approver approved
    approval.satisfied   the quorum is met (the activation decision follows)
    approval.rejected    an approver rejected the version
"""

from __future__ import annotations

import hashlib
from typing import Any

from scruple.audit.models import AuditEntry
from scruple.integrations.store import GateEvent
from scruple.service import AgentGate

EVENT_TYPES = (
    "decision.recorded",
    "approval.requested",
    "approval.granted",
    "approval.satisfied",
    "approval.rejected",
)


def event_id(tenant: str, entry_hash: str) -> str:
    return "evt_" + hashlib.sha256(f"{tenant}\x00{entry_hash}".encode()).hexdigest()[:24]


def _decision_data(gate: AgentGate, p: dict[str, Any]) -> dict[str, Any]:
    rec = gate.decision(str(p["decision_id"]))
    out: dict[str, Any] = {
        "decision_id": p["decision_id"],
        "stage": p["stage"],
        "outcome": p["outcome"],
        "rule_id": p["rule_id"],
        "version": p["version"],
        "content_hash": p["content_hash"],
        "decision_sha256": p["decision_sha256"],
    }
    if "policy_sha256" in p:
        out["policy_sha256"] = p["policy_sha256"]
    if rec is not None:
        d = rec.decision
        out |= {
            "summary": d.summary,
            "reasons": list(d.reasons),
            "rule_description": d.rule_description,
            "owner": d.owner,
            "agent_id": d.agent_id,
            "actor": d.actor,
            "report_path": f"/decisions/{d.decision_id}/report",
            "data": "synthetic",  # every gate number comes from the simulator
        }
    return out


def to_event(gate: AgentGate, entry: AuditEntry) -> GateEvent | None:
    if entry.event_type not in EVENT_TYPES:
        return None
    tenant = gate.storage.tenant
    p = dict(entry.payload)
    if entry.event_type == "decision.recorded":
        data = _decision_data(gate, p)
    else:
        data = {k: v for k, v in p.items() if k != "policy"}
        if entry.event_type == "approval.requested":
            data["request_seq"] = entry.seq
    data.setdefault("actor", entry.actor)
    return GateEvent(
        id=event_id(tenant, entry.entry_hash),
        type=entry.event_type,
        tenant=tenant,
        created_at=entry.at,
        audit_seq=entry.seq,
        audit_entry_hash=entry.entry_hash,
        data=data,
    )


__all__ = ["EVENT_TYPES", "event_id", "to_event"]
