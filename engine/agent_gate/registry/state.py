"""The rule status state machine. Hardcoded on purpose: it is a control, not config."""

from __future__ import annotations

from agent_gate.registry.models import RuleStatus

S = RuleStatus

ALLOWED: dict[RuleStatus, frozenset[RuleStatus]] = {
    S.CANDIDATE: frozenset({S.SHADOW, S.REJECTED}),
    S.SHADOW: frozenset({S.CANARY, S.CANDIDATE, S.REJECTED}),  # CANDIDATE = hold
    S.CANARY: frozenset({S.ACTIVE, S.REJECTED}),  # REJECTED = canary rollback
    S.ACTIVE: frozenset({S.RETIRED}),
    S.RETIRED: frozenset({S.ACTIVE}),  # only via Registry.rollback (restore)
    S.REJECTED: frozenset(),
}

# Transitions that must go through a dedicated Registry method, never `transition()`.
PRIVILEGED: frozenset[tuple[RuleStatus, RuleStatus]] = frozenset(
    {(S.CANARY, S.ACTIVE), (S.ACTIVE, S.RETIRED), (S.RETIRED, S.ACTIVE)}
)


class InvalidTransitionError(Exception):
    pass


def check_transition(src: RuleStatus, dst: RuleStatus) -> None:
    if dst not in ALLOWED[src]:
        raise InvalidTransitionError(f"illegal status transition {src.value} -> {dst.value}")
