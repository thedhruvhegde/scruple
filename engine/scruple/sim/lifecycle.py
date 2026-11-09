"""Dispute lifecycle state machine with explicit allowed transitions.

Stages: inquiry -> chargeback -> representment -> pre-arbitration -> arbitration ->
resolved (S7, S8, S11 in docs/SOURCES.md). The transition table is hardcoded and
exhaustive: any (stage, event) pair not listed raises `IllegalTransitionError`.

Two workflows:

- collaboration (Visa processing errors and consumer disputes, and every Mastercard
  dispute per A-14): the merchant represents, then the issuer either accepts the
  representment or files pre-arbitration; the merchant then accepts or escalates.
- allocation (Visa fraud and authorization, S8, S9): there is no issuer-reviewed
  representment step. The merchant's (acquirer's) response is itself a
  pre-arbitration, which the issuer accepts or declines; a decline can go to
  arbitration.

`Stage.REPRESENTMENT` is the network's name for the merchant's response under
Mastercard (second presentment) and the Visa collaboration flow.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from scruple.sim.models import Stage
from scruple.sim.networks import Workflow

S = Stage


class Event(StrEnum):
    FILE_CHARGEBACK = "file_chargeback"  # issuer turns an inquiry into a chargeback
    RESOLVE_INQUIRY = "resolve_inquiry"  # inquiry answered or refunded; no chargeback
    ACCEPT = "accept"  # merchant accepts liability
    REPRESENT = "represent"  # merchant responds with evidence
    ISSUER_ACCEPT = "issuer_accept"  # issuer accepts the merchant's response
    ISSUER_PRE_ARB = "issuer_pre_arb"  # issuer rejects and files pre-arbitration
    ESCALATE = "escalate"  # take the case to network arbitration
    RULING = "ruling"  # network arbitration decision
    DEADLINE_MISSED = "deadline_missed"  # the party due to act did not act in time
    WITHDRAW = "withdraw"  # issuer or cardholder withdraws the dispute


E = Event

_COMMON: dict[tuple[Stage, Event], Stage] = {
    (S.INQUIRY, E.FILE_CHARGEBACK): S.CHARGEBACK,
    (S.INQUIRY, E.RESOLVE_INQUIRY): S.RESOLVED,
    (S.INQUIRY, E.DEADLINE_MISSED): S.CHARGEBACK,
    (S.INQUIRY, E.WITHDRAW): S.RESOLVED,
    (S.CHARGEBACK, E.ACCEPT): S.RESOLVED,
    (S.CHARGEBACK, E.DEADLINE_MISSED): S.RESOLVED,
    (S.CHARGEBACK, E.WITHDRAW): S.RESOLVED,
    (S.PRE_ARBITRATION, E.ACCEPT): S.RESOLVED,
    (S.PRE_ARBITRATION, E.ESCALATE): S.ARBITRATION,
    (S.PRE_ARBITRATION, E.DEADLINE_MISSED): S.RESOLVED,
    (S.PRE_ARBITRATION, E.WITHDRAW): S.RESOLVED,
    (S.ARBITRATION, E.RULING): S.RESOLVED,
}

TRANSITIONS: dict[Workflow, dict[tuple[Stage, Event], Stage]] = {
    Workflow.COLLABORATION: {
        **_COMMON,
        (S.CHARGEBACK, E.REPRESENT): S.REPRESENTMENT,
        (S.REPRESENTMENT, E.ISSUER_ACCEPT): S.RESOLVED,
        (S.REPRESENTMENT, E.ISSUER_PRE_ARB): S.PRE_ARBITRATION,
        (S.REPRESENTMENT, E.DEADLINE_MISSED): S.RESOLVED,
        (S.REPRESENTMENT, E.WITHDRAW): S.RESOLVED,
    },
    Workflow.ALLOCATION: {
        **_COMMON,
        (S.CHARGEBACK, E.REPRESENT): S.PRE_ARBITRATION,
        (S.PRE_ARBITRATION, E.ISSUER_ACCEPT): S.RESOLVED,
    },
}

OPENING_STAGES = frozenset({S.INQUIRY, S.CHARGEBACK})


class IllegalTransitionError(ValueError):
    pass


class Transition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_stage: Stage
    event: Event
    to_stage: Stage
    on: date


def next_stage(workflow: Workflow, stage: Stage, event: Event) -> Stage:
    """Pure transition function. Raises on anything not in the table."""
    try:
        return TRANSITIONS[workflow][(stage, event)]
    except KeyError:
        raise IllegalTransitionError(
            f"{workflow.value}: {event.value} is not allowed in stage {stage.value}"
        ) from None


def allowed_events(workflow: Workflow, stage: Stage) -> tuple[Event, ...]:
    return tuple(e for e in Event if (stage, e) in TRANSITIONS[workflow])


class Lifecycle:
    """One case's position in the lifecycle plus its append-only history."""

    def __init__(self, workflow: Workflow, opened_on: date, stage: Stage = S.INQUIRY) -> None:
        if stage not in OPENING_STAGES:
            raise IllegalTransitionError(f"a dispute cannot open in stage {stage.value}")
        self.workflow = workflow
        self.opened_on = opened_on
        self._stage = stage
        self._entered_on = opened_on
        self._history: list[Transition] = []

    @property
    def stage(self) -> Stage:
        return self._stage

    @property
    def entered_on(self) -> date:
        """The date the current stage was entered (deadlines count from here)."""
        return self._entered_on

    @property
    def history(self) -> tuple[Transition, ...]:
        return tuple(self._history)

    @property
    def is_terminal(self) -> bool:
        return self._stage is S.RESOLVED

    @property
    def path(self) -> tuple[Stage, ...]:
        first = self._history[0].from_stage if self._history else self._stage
        return (first, *(t.to_stage for t in self._history))

    def allowed(self) -> tuple[Event, ...]:
        return allowed_events(self.workflow, self._stage)

    def can(self, event: Event) -> bool:
        return (self._stage, event) in TRANSITIONS[self.workflow]

    def fire(self, event: Event, on: date) -> Stage:
        if on < self._entered_on:
            raise IllegalTransitionError(
                f"{event.value} on {on} is before the stage was entered ({self._entered_on})"
            )
        to = next_stage(self.workflow, self._stage, event)
        self._history.append(Transition(from_stage=self._stage, event=event, to_stage=to, on=on))
        self._stage = to
        self._entered_on = on
        return to
