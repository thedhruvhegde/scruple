"""Putting a policy in force for a tenant, and running a gate under it.

`apply_policy(gate, policy, actor)`: in one transaction, store the policy as the
tenant's next version, append `policy.set` to the tenant's audit chain and, when
the policy has an `approval` section that differs from the approval policy in
force, put that in force too (`AgentGate.set_approval_policy`, which only an
operator in `GateConfig.approvers` may do, so a policy cannot be used to lower a
quorum by someone the gate would not let do it directly). Applying the policy that
is already current changes nothing and returns the current record.

`configured(gate)`: the same gate with the tenant's current policy compiled onto
its config (`Policy.gate_config`), or the gate itself when there is none.
"""

from __future__ import annotations

import threading

from scruple.policy.models import Policy
from scruple.policy.store import EVENT_POLICY_APPLIED, PolicyRecord, PolicyStore
from scruple.service import AgentGate


def apply_policy(gate: AgentGate, policy: Policy, actor: str) -> tuple[PolicyRecord, bool]:
    """Returns (record, changed)."""
    store = PolicyStore(gate.storage)
    policy.gate_config(gate.config)  # refuse a policy that does not compile, before writing
    with gate.storage.transaction():
        current = store.current()
        if current is not None and current.policy_sha256 == policy.policy_sha256:
            return current, False
        if policy.approval is not None and gate.approval_policy() != policy.approval:
            gate.set_approval_policy(policy.approval, actor)
        rec = store.append(policy, actor, gate.clock.now())
        gate.record_runtime_event(
            EVENT_POLICY_APPLIED,
            actor,
            {
                "number": rec.number,
                "name": policy.name,
                "policy_version": policy.policy_version,
                "policy_sha256": rec.policy_sha256,
                "previous_sha256": None if current is None else current.policy_sha256,
            },
        )
    return rec, True


def current_policy(gate: AgentGate) -> PolicyRecord | None:
    return PolicyStore(gate.storage).current()


def configured(gate: AgentGate) -> AgentGate:
    rec = current_policy(gate)
    if rec is None:
        return gate
    cfg = rec.policy.gate_config(gate.config)
    if cfg == gate.config:
        return gate
    return AgentGate(gate.storage, gate.clock, cfg)


class PolicyGates:
    """Gates under each tenant's current policy, rebuilt when the policy changes.

    `gate(base)` reads only the current policy's hash per call, so a policy applied
    by another process (the CLI, another replica) takes effect on the next request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[int, str], AgentGate] = {}

    def gate(self, base: AgentGate) -> AgentGate:
        try:
            store = PolicyStore(base.storage)
        except TypeError:  # a Storage without SQL (test doubles) has no policies
            return base
        sha = store.current_sha256()
        if sha is None:
            return base
        key = (id(base), sha)
        with self._lock:
            found = self._cache.get(key)
        if found is not None:
            return found
        rec = store.current()
        if rec is None:  # pragma: no cover - a policy cannot be removed
            return base
        g = AgentGate(base.storage, base.clock, rec.policy.gate_config(base.config))
        with self._lock:
            return self._cache.setdefault((id(base), rec.policy_sha256), g)


__all__ = ["PolicyGates", "apply_policy", "configured", "current_policy"]
