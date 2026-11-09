from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.sim.lifecycle import (
    OPENING_STAGES,
    TRANSITIONS,
    Event,
    IllegalTransitionError,
    Lifecycle,
    allowed_events,
    next_stage,
)
from scruple.sim.models import Stage
from scruple.sim.networks import Workflow

D0 = date(2026, 3, 2)
ALL = [(w, s, e) for w in Workflow for s in Stage for e in Event]


def _at(workflow: Workflow, stage: Stage) -> Lifecycle | None:
    """A lifecycle driven to `stage` along a legal path, or None if unreachable."""
    frontier: list[tuple[Stage, Stage, list[Event]]] = [(s, s, []) for s in sorted(OPENING_STAGES)]
    seen: set[Stage] = set()
    while frontier:
        start, cur, path = frontier.pop(0)
        if cur is stage:
            lc = Lifecycle(workflow, D0, start)
            for ev in path:
                lc.fire(ev, D0)
            return lc
        if cur in seen:
            continue
        seen.add(cur)
        for (frm, ev), to in TRANSITIONS[workflow].items():
            if frm is cur:
                frontier.append((start, to, [*path, ev]))
    return None


@pytest.mark.parametrize(("workflow", "stage", "event"), ALL, ids=lambda x: str(x))
def test_every_pair_is_either_allowed_or_raises(
    workflow: Workflow, stage: Stage, event: Event
) -> None:
    expected = TRANSITIONS[workflow].get((stage, event))
    if expected is None:
        with pytest.raises(IllegalTransitionError):
            next_stage(workflow, stage, event)
    else:
        assert next_stage(workflow, stage, event) is expected
    lc = _at(workflow, stage)
    if lc is None:
        return
    assert lc.can(event) is (expected is not None)
    if expected is None:
        with pytest.raises(IllegalTransitionError):
            lc.fire(event, D0)
        assert lc.stage is stage  # unchanged after a refused event
    else:
        assert lc.fire(event, D0 + timedelta(days=1)) is expected
        assert lc.history[-1].event is event


@pytest.mark.parametrize("workflow", list(Workflow))
def test_resolved_is_terminal(workflow: Workflow) -> None:
    assert allowed_events(workflow, Stage.RESOLVED) == ()


@pytest.mark.parametrize("workflow", list(Workflow))
@pytest.mark.parametrize("stage", [s for s in Stage if s is not Stage.RESOLVED])
def test_every_live_stage_can_resolve(workflow: Workflow, stage: Stage) -> None:
    if _at(workflow, stage) is None:
        return
    reach = {stage}
    changed = True
    while changed:
        changed = False
        for (frm, _), to in TRANSITIONS[workflow].items():
            if frm in reach and to not in reach:
                reach.add(to)
                changed = True
    assert Stage.RESOLVED in reach


def test_reachable_stages_per_workflow() -> None:
    assert _at(Workflow.ALLOCATION, Stage.REPRESENTMENT) is None
    for w in Workflow:
        for s in (Stage.PRE_ARBITRATION, Stage.ARBITRATION, Stage.RESOLVED):
            assert _at(w, s) is not None


def test_allocation_response_goes_straight_to_pre_arbitration() -> None:
    lc = Lifecycle(Workflow.ALLOCATION, D0, Stage.CHARGEBACK)
    assert lc.fire(Event.REPRESENT, D0) is Stage.PRE_ARBITRATION
    assert lc.fire(Event.ISSUER_ACCEPT, D0) is Stage.RESOLVED


def test_full_collaboration_path() -> None:
    lc = Lifecycle(Workflow.COLLABORATION, D0)
    for i, ev in enumerate(
        (Event.FILE_CHARGEBACK, Event.REPRESENT, Event.ISSUER_PRE_ARB, Event.ESCALATE, Event.RULING)
    ):
        lc.fire(ev, D0 + timedelta(days=i))
    assert lc.path == (
        Stage.INQUIRY,
        Stage.CHARGEBACK,
        Stage.REPRESENTMENT,
        Stage.PRE_ARBITRATION,
        Stage.ARBITRATION,
        Stage.RESOLVED,
    )
    assert lc.is_terminal
    assert lc.entered_on == D0 + timedelta(days=4)


@pytest.mark.parametrize("stage", [s for s in Stage if s not in OPENING_STAGES])
def test_cannot_open_mid_lifecycle(stage: Stage) -> None:
    with pytest.raises(IllegalTransitionError):
        Lifecycle(Workflow.COLLABORATION, D0, stage)


def test_time_cannot_run_backwards() -> None:
    lc = Lifecycle(Workflow.COLLABORATION, D0)
    lc.fire(Event.FILE_CHARGEBACK, D0 + timedelta(days=3))
    with pytest.raises(IllegalTransitionError, match="before the stage"):
        lc.fire(Event.ACCEPT, D0 + timedelta(days=2))


@given(
    workflow=st.sampled_from(list(Workflow)),
    start=st.sampled_from(sorted(OPENING_STAGES)),
    events=st.lists(st.sampled_from(list(Event)), max_size=30),
)
def test_random_event_sequences_never_corrupt_state(
    workflow: Workflow, start: Stage, events: list[Event]
) -> None:
    lc = Lifecycle(workflow, D0, start)
    for i, ev in enumerate(events):
        before = lc.stage
        legal = lc.can(ev)
        try:
            lc.fire(ev, D0 + timedelta(days=i))
        except IllegalTransitionError:
            assert not legal
            assert lc.stage is before
        else:
            assert legal
    # history is a connected chain from the opening stage
    prev = start
    for t in lc.history:
        assert t.from_stage is prev
        assert TRANSITIONS[workflow][(t.from_stage, t.event)] is t.to_stage
        prev = t.to_stage
    assert prev is lc.stage
    assert len(lc.history) <= 5  # longest path: inquiry .. arbitration -> resolved
