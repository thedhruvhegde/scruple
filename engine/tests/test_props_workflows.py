"""Every workflow state machine as a hypothesis state machine (roadmap 162).

- `LifecycleMachine`: the dispute lifecycle (`scruple.sim.lifecycle`), both workflows.
- `RegistryMachine`: rule versions through `Registry` (register, transition,
  activate, rollback, import), against a model built from `registry.state.ALLOWED`.
- `PromotionMachine`: a version that passed the one-shot canary, driven through the
  staged canary ramp, the kill switch, legacy approval, N-of-M approvals with
  expiry, and rollback, by any actor, in any order.

For each: no illegal transition is reachable (every move the audit log records is
in the hardcoded table, and every refused move changes nothing), and the invariants
hold after every step. All data is synthetic.
"""

from __future__ import annotations

import shutil
import tempfile
from collections import deque
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from scruple.domain import Action, Condition, RuleSpec
from scruple.gate.approval import ApprovalPolicy, NamedApprover
from scruple.gate.canary import CANARY_EVENTS, Arm, RampSchedule
from scruple.gate.config import GateConfig
from scruple.gate.models import Outcome, Stage
from scruple.registry import (
    DuplicateContentError,
    InvalidTransitionError,
    Registry,
    RuleStatus,
)
from scruple.registry.state import ALLOWED, PRIVILEGED
from scruple.service import AgentGate, ApprovalError
from scruple.sim.generator import generate_cases
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.sim.lifecycle import (
    OPENING_STAGES,
    TRANSITIONS,
    Event,
    IllegalTransitionError,
    Lifecycle,
    next_stage,
)
from scruple.sim.models import Stage as CaseStage
from scruple.sim.networks import Workflow
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

MACHINE = settings(
    max_examples=20,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
S = RuleStatus


class StepClock:
    """Like SteppingClock (1s per call) but can also jump forward."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        t = self.t
        self.t = t + timedelta(seconds=1)
        return t

    def jump(self, seconds: int) -> None:
        self.t += timedelta(seconds=seconds)


def status_moves(entries: list[Any]) -> list[tuple[str, int, str, str]]:
    """(rule_id, version, from, to) for every status change the audit log records."""
    out = []
    for e in entries:
        p = e.payload
        if e.event_type in ("rule.status_changed", "rule.activated"):
            out.append((p["rule_id"], int(p["version"]), p["from_status"], p["to_status"]))
            if e.event_type == "rule.activated" and p.get("retired_version"):
                out.append((p["rule_id"], int(p["retired_version"]), "active", "retired"))
        elif e.event_type == "rule.rolled_back":
            out.append((p["rule_id"], int(p["version"]), "active", "retired"))
            if p.get("restored_version"):
                out.append((p["rule_id"], int(p["restored_version"]), "retired", "active"))
    return out


def assert_moves_legal(entries: list[Any]) -> None:
    status: dict[tuple[str, int], str] = {}
    for e in entries:
        p = e.payload
        if e.event_type in ("rule.registered", "rule.baseline_imported"):
            status[(p["rule_id"], int(p["version"]))] = p["status"]
    for rid, v, src, dst in status_moves(entries):
        assert status[(rid, v)] == src, f"{rid}@v{v} moved from {src} but was {status[(rid, v)]}"
        assert S(dst) in ALLOWED[S(src)], f"illegal {src} -> {dst} for {rid}@v{v}"
        status[(rid, v)] = dst


# ------------------------------------------------------------------ dispute lifecycle
class LifecycleMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.life: Lifecycle | None = None
        self.day = date(2026, 1, 5)

    @initialize(
        workflow=st.sampled_from(list(Workflow)), stage=st.sampled_from(sorted(OPENING_STAGES))
    )
    def open(self, workflow: Workflow, stage: CaseStage) -> None:
        self.life = Lifecycle(workflow, self.day, stage)
        self.workflow = workflow

    @rule(event=st.sampled_from(list(Event)), days=st.integers(0, 40))
    def fire(self, event: Event, days: int) -> None:
        assert self.life is not None
        before = (self.life.stage, self.life.entered_on, self.life.history)
        legal = (self.life.stage, event) in TRANSITIONS[self.workflow]
        assert self.life.can(event) == legal
        on = self.life.entered_on + timedelta(days=days)
        if not legal:
            with pytest.raises(IllegalTransitionError):
                self.life.fire(event, on)
            assert (self.life.stage, self.life.entered_on, self.life.history) == before
            return
        to = self.life.fire(event, on)
        assert to == next_stage(self.workflow, before[0], event)
        assert self.life.entered_on == on

    @rule(event=st.sampled_from(list(Event)), back=st.integers(1, 30))
    def backdated_events_are_refused(self, event: Event, back: int) -> None:
        assert self.life is not None
        before = self.life.history
        with pytest.raises(IllegalTransitionError):
            self.life.fire(event, self.life.entered_on - timedelta(days=back))
        assert self.life.history == before

    @invariant()
    def history_is_a_legal_chain(self) -> None:
        if self.life is None:
            return
        h = self.life.history
        stage = self.life.path[0]
        assert stage in OPENING_STAGES
        for t in h:
            assert t.from_stage == stage
            assert TRANSITIONS[self.workflow][(t.from_stage, t.event)] == t.to_stage
            stage = t.to_stage
        assert stage == self.life.stage
        assert [t.on for t in h] == sorted(t.on for t in h)

    @invariant()
    def resolved_is_terminal(self) -> None:
        if self.life is None:
            return
        assert self.life.is_terminal == (self.life.stage is CaseStage.RESOLVED)
        if self.life.is_terminal:
            assert self.life.allowed() == ()
        else:
            assert self.life.allowed(), "a live stage must have a way out"


TestLifecycleMachine = LifecycleMachine.TestCase
TestLifecycleMachine.settings = MACHINE


@pytest.mark.parametrize("workflow", list(Workflow))
def test_every_reachable_stage_can_reach_resolved(workflow: Workflow) -> None:
    """Liveness of the table: no reachable dead end, and RESOLVED is reachable."""
    graph: dict[CaseStage, set[CaseStage]] = {}
    for (src, _), dst in TRANSITIONS[workflow].items():
        graph.setdefault(src, set()).add(dst)
    seen, todo = set(OPENING_STAGES), deque(OPENING_STAGES)
    while todo:
        for nxt in graph.get(todo.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert CaseStage.RESOLVED in seen
    for s in seen - {CaseStage.RESOLVED}:
        assert graph.get(s), f"{s} is a dead end"
    assert CaseStage.RESOLVED not in graph, "resolved must be terminal"


# ------------------------------------------------------------------------ registry
def spec(n: int) -> RuleSpec:
    """Distinct behaviour per n (so the duplicate-behaviour guard does not fire)."""
    return RuleSpec(
        description=f"rule {n}",
        conditions=(Condition(field="amount_usd_minor", op="gt", value=1_000 + n),),
        action=Action.CONTEST,
        priority=10,
    )


class RegistryMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.storage = SQLiteStorage(":memory:")
        self.gate = AgentGate(self.storage, StepClock(datetime(2026, 1, 1, tzinfo=UTC)))
        self.reg: Registry = self.gate.registry
        self.model: dict[tuple[str, int], RuleStatus] = {}
        self.pointer: dict[tuple[str, int], int | None] = {}
        self.specs: dict[str, set[int]] = {}
        self.n = 0

    def teardown(self) -> None:
        self.storage.close()

    def versions(self, rid: str) -> list[int]:
        return sorted(v for r, v in self.model if r == rid)

    def active(self, rid: str) -> int | None:
        return next((v for v in self.versions(rid) if self.model[(rid, v)] is S.ACTIVE), None)

    @rule(rid=st.sampled_from(["r-a", "r-b"]), reuse=st.booleans())
    def register(self, rid: str, reuse: bool) -> None:
        used = self.specs.setdefault(rid, set())
        if reuse and used:
            with pytest.raises(DuplicateContentError):
                self.reg.register_candidate(rid, spec(min(used)), "ops-team", PROV)
            return
        self.n += 1
        rv = self.reg.register_candidate(rid, spec(self.n), "ops-team", PROV)
        used.add(self.n)
        assert rv.version == len(self.versions(rid)) + 1 and rv.status is S.CANDIDATE
        self.model[(rid, rv.version)] = S.CANDIDATE
        self.pointer[(rid, rv.version)] = None

    @rule(rid=st.sampled_from(["r-a", "r-b"]))
    def import_baseline(self, rid: str) -> None:
        if self.active(rid) is not None:
            self.n += 1
            with pytest.raises(InvalidTransitionError):
                self.reg.import_baseline(rid, spec(self.n), "ops-team", "agent-gate")
            return
        self.n += 1
        rv = self.reg.import_baseline(rid, spec(self.n), "ops-team", "agent-gate")
        self.specs.setdefault(rid, set()).add(self.n)
        self.model[(rid, rv.version)] = S.ACTIVE
        self.pointer[(rid, rv.version)] = None

    @precondition(lambda self: bool(self.model))
    @rule(data=st.data(), to=st.sampled_from(list(RuleStatus)))
    def transition(self, data: st.DataObject, to: RuleStatus) -> None:
        rid, v = data.draw(st.sampled_from(sorted(self.model)))
        src = self.model[(rid, v)]
        legal = to in ALLOWED[src] and (src, to) not in PRIVILEGED
        if not legal:
            with pytest.raises(InvalidTransitionError):
                self.reg.transition(rid, v, to, "agent-gate", "test")
            return
        assert self.reg.transition(rid, v, to, "agent-gate", "test").status is to
        self.model[(rid, v)] = to

    @precondition(lambda self: bool(self.model))
    @rule(data=st.data())
    def activate(self, data: st.DataObject) -> None:
        rid, v = data.draw(st.sampled_from(sorted(self.model)))
        if self.model[(rid, v)] is not S.CANARY:
            with pytest.raises(InvalidTransitionError):
                self.reg.activate(rid, v, "model-risk@demo.bank", "test")
            return
        prev = self.active(rid)
        rv = self.reg.activate(rid, v, "model-risk@demo.bank", "test")
        assert rv.rollback_to == prev
        if prev is not None:
            self.model[(rid, prev)] = S.RETIRED
        self.model[(rid, v)] = S.ACTIVE
        self.pointer[(rid, v)] = prev

    @rule(rid=st.sampled_from(["r-a", "r-b", "r-none"]))
    def rollback(self, rid: str) -> None:
        cur = self.active(rid)
        if cur is None:
            with pytest.raises(InvalidTransitionError):
                self.reg.rollback(rid, "model-risk@demo.bank", "test")
            return
        back = self.pointer[(rid, cur)]
        retired, restored = self.reg.rollback(rid, "model-risk@demo.bank", "test")
        assert retired.version == cur and retired.status is S.RETIRED
        self.model[(rid, cur)] = S.RETIRED
        if back is None:
            assert restored is None
        else:
            assert restored is not None and restored.version == back
            self.model[(rid, back)] = S.ACTIVE

    @invariant()
    def registry_matches_the_model(self) -> None:
        actual = {(r.rule_id, r.version): r.status for r in self.reg.all_versions()}
        assert actual == self.model
        for (rid, v), ptr in self.pointer.items():
            assert self.reg.get(rid, v).rollback_to == ptr

    @invariant()
    def at_most_one_active_per_rule(self) -> None:
        for rid in {r for r, _ in self.model}:
            assert sum(s is S.ACTIVE for (r, _), s in self.model.items() if r == rid) <= 1

    @invariant()
    def audit_records_only_legal_moves_and_integrity_holds(self) -> None:
        entries = self.gate.audit.entries()
        assert_moves_legal(entries)
        report = self.gate.verify_integrity()
        assert report.ok, report.problems


TestRegistryMachine = RegistryMachine.TestCase
TestRegistryMachine.settings = MACHINE


# ------------------------------------------------------- canary ramp and approvals
APPROVER = "model-risk@demo.bank"
NAMED = ("alice@demo.bank", "bob@demo.bank", "carol@demo.bank")
ACTORS = (APPROVER, *NAMED, PROV.agent_id, "ops-team", "mallory@demo.bank", "agent-gate")
POLICY = ApprovalPolicy(
    approvers=(
        NamedApprover(name=NAMED[0], roles=("model-risk",)),
        NamedApprover(name=NAMED[1]),
        NamedApprover(name=NAMED[2], roles=("model-risk",)),
    ),
    required=2,
    required_role="model-risk",
    grant_ttl_seconds=3_600,
    request_ttl_seconds=7_200,
)
CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300, approvers=(APPROVER,))
SCHEDULES = (
    RampSchedule(stages=(5.0, 25.0)),
    RampSchedule(stages=(10.0, 20.0, 40.0), min_matched=5),
)
EXPECTED = (ApprovalError, InvalidTransitionError)
# Mostly the people who can act, so deep paths are reached; sometimes anyone else.
actors = st.one_of(st.sampled_from((APPROVER, *NAMED)), st.sampled_from(ACTORS))


@cache
def _canary_passed_db() -> Path:
    """A database where the good rule has passed the one-shot canary (built once)."""
    root = Path(tempfile.mkdtemp(prefix="scruple-props-"))
    db = root / "base.db"
    g = AgentGate(SQLiteStorage(str(db)), StepClock(datetime(2026, 1, 1, tzinfo=UTC)), CFG)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    recs = g.evaluate(GOOD_RULE_ID, 1)
    assert recs[-1].decision.outcome is Outcome.CANARY_PASSED
    g.storage.close()
    return db


class PromotionMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.dir = Path(tempfile.mkdtemp(prefix="scruple-promo-"))
        shutil.copy(_canary_passed_db(), self.dir / "g.db")
        self.storage = SQLiteStorage(str(self.dir / "g.db"))
        self.clock = StepClock(datetime(2026, 2, 1, tzinfo=UTC))
        self.g = AgentGate(self.storage, self.clock, CFG)
        self.batch = 0

    def teardown(self) -> None:
        self.storage.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _try(self, fn: Any, *args: Any) -> Any:
        """Run one operation: it either succeeds or is refused with a domain error, in
        which case nothing at all changed."""
        head = self.g.audit.head()
        statuses = [(r.ref, r.status) for r in self.g.registry.all_versions()]
        n_dec = self.storage.count_decisions()
        try:
            return fn(*args)
        except EXPECTED:
            assert self.g.audit.head() == head
            assert [(r.ref, r.status) for r in self.g.registry.all_versions()] == statuses
            assert self.storage.count_decisions() == n_dec
            return None

    @rule(schedule=st.sampled_from(SCHEDULES))
    def start_ramp(self, schedule: RampSchedule) -> None:
        self._try(self.g.start_canary_ramp, GOOD_RULE_ID, 1, schedule)

    @rule(n=st.sampled_from([150, 800]))
    def advance(self, n: int) -> None:
        self.batch += 1
        cases = generate_cases(700 + self.batch, n, prefix=f"p{self.batch}-")
        self._try(self.g.advance_canary, GOOD_RULE_ID, 1, cases)

    @rule(actor=actors)
    def breach(self, actor: str) -> None:
        self._try(self.g.breach_canary, GOOD_RULE_ID, 1, actor, "external alert")

    @rule(actor=actors)
    def approve(self, actor: str) -> None:
        self._try(self.g.approve, GOOD_RULE_ID, 1, actor)

    @rule(actor=actors)
    def set_policy(self, actor: str) -> None:
        self._try(self.g.set_approval_policy, POLICY, actor)

    @rule(actor=actors)
    def request(self, actor: str) -> None:
        self._try(self.g.request_approval, GOOD_RULE_ID, 1, actor)

    @rule(actor=actors)
    def grant(self, actor: str) -> None:
        self._try(self.g.grant_approval, GOOD_RULE_ID, 1, actor)

    @rule(actor=actors)
    def rollback(self, actor: str) -> None:
        self._try(self.g.rollback, GOOD_RULE_ID, actor, "rolled back in a test")

    @rule()
    def re_evaluate(self) -> None:
        self._try(self.g.evaluate, GOOD_RULE_ID, 1)

    @rule(seconds=st.sampled_from([60, 1_800, 4_000, 8_000]))
    def wait(self, seconds: int) -> None:
        self.clock.jump(seconds)

    # --- invariants ---------------------------------------------------------------
    @invariant()
    def integrity_and_legal_moves(self) -> None:
        entries = self.g.audit.entries()
        assert_moves_legal(entries)
        report = self.g.verify_integrity()
        assert report.ok, report.problems
        actives = [r for r in self.g.registry.all_versions() if r.status is S.ACTIVE]
        assert len({r.rule_id for r in actives}) == len(actives)

    @invariant()
    def activation_only_through_the_controls(self) -> None:
        """When the version went live: the latest canary decision before it had
        passed, no ramp was still in progress, and if a policy was in force a quorum
        had been recorded for this request."""
        entries = self.g.audit.entries()
        ramp: str | None = None
        last_canary: str | None = None
        policy = False
        satisfied = False
        for e in entries:
            p = e.payload
            mine = p.get("rule_id") == GOOD_RULE_ID and int(p.get("version", 0)) == 1
            if e.event_type == "approval.policy_set":
                policy = True
            if not mine:
                continue
            if e.event_type == "decision.recorded" and p["stage"] == Stage.CANARY.value:
                last_canary = p["outcome"]
            if e.event_type in CANARY_EVENTS:
                ramp = e.event_type
            if e.event_type == "approval.satisfied":
                satisfied = True
            if e.event_type == "approval.requested":
                satisfied = False
            if e.event_type == "rule.activated":
                assert last_canary == Outcome.CANARY_PASSED.value
                assert ramp in (None, "canary.ramp_completed"), f"activated while {ramp}"
                if policy:
                    assert satisfied, "activated under a policy without a quorum"

    @invariant()
    def breach_rejects_and_routes_everything_to_baseline(self) -> None:
        state = self.g.canary_state(GOOD_RULE_ID, 1)
        rv = self.g.registry.get(GOOD_RULE_ID, 1)
        if state is None:
            return
        if state.status == "breached":
            assert rv.status is S.REJECTED
            assert state.percent == 0.0
            assert all(
                self.g.canary_arm(GOOD_RULE_ID, 1, f"case-{i}") is Arm.BASELINE for i in range(50)
            )
        else:
            assert state.percent == state.schedule.stages[state.stage_index]

    @invariant()
    def grants_respect_separation_of_duties(self) -> None:
        proposer: str | None = None
        for e in self.g.audit.entries():
            if e.event_type == "approval.requested":
                proposer = e.payload["proposer"]
            if e.event_type == "approval.granted":
                a = e.payload["approver"]
                assert a in NAMED
                assert a not in (proposer, "ops-team", PROV.agent_id)


TestPromotionMachine = PromotionMachine.TestCase
TestPromotionMachine.settings = settings(MACHINE, max_examples=40, stateful_step_count=20)
