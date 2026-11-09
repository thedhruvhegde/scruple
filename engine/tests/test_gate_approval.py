"""Approval workflow: N of M named approvers, required role, SoD, expiry, audit."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from scruple.clock import SteppingClock
from scruple.gate.approval import (
    APPROVAL_EVENTS,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalGrant,
    ApprovalPolicy,
    DuplicateApprovalError,
    NamedApprover,
    NoOpenRequestError,
    NotNamedApproverError,
    SeparationOfDutiesError,
    check_grant,
    evaluate_approvals,
    new_grant,
    new_request,
)
from scruple.gate.config import GateConfig
from scruple.gate.models import Outcome, Stage
from scruple.registry import RuleStatus
from scruple.service import AgentGate
from scruple.service import ApprovalError as ServiceApprovalError
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import APPROVER, PROV
from tests.helpers import rv

T0 = datetime(2026, 3, 1, tzinfo=UTC)
ALICE = NamedApprover(name="alice@demo.bank", roles=("model-risk",))
BOB = NamedApprover(name="bob@demo.bank", roles=("ops-lead",))
CAROL = NamedApprover(name="carol@demo.bank", roles=("model-risk", "compliance"))
DAVE = NamedApprover(name="dave@demo.bank", roles=())
POLICY = ApprovalPolicy(approvers=(ALICE, BOB, CAROL, DAVE), required=2, required_role="model-risk")
RULE = rv(GOOD_RULE_ID, GOOD_RULE, owner="ops-team")  # agent_id agent-7
PROPOSER = "proposer@demo.bank"


class ManualClock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


def _grants(*people: NamedApprover, at: datetime = T0) -> list[ApprovalGrant]:
    return [new_grant(POLICY, p, at) for p in people]


# --- policy validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"approvers": (), "required": 1},
        {"approvers": (ALICE,), "required": 2},
        {"approvers": (ALICE, ALICE), "required": 1},
        {"approvers": (BOB,), "required": 1, "required_role": "model-risk"},
        {"approvers": (ALICE,), "required": 0},
        {"approvers": (ALICE,), "required": 1, "grant_ttl_seconds": 1},
    ],
)
def test_invalid_policies_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ApprovalPolicy.model_validate(kwargs)


def test_role_names_validated() -> None:
    with pytest.raises(ValidationError):
        NamedApprover(name="x@y", roles=("Model Risk",))


# --- truth tables -----------------------------------------------------------------------

PEOPLE = {"A": ALICE, "B": BOB, "C": CAROL, "D": DAVE}

# 2 of 4, at least one model-risk (alice or carol).
TABLE = [
    ("", False),
    ("A", False),
    ("B", False),
    ("C", False),
    ("D", False),
    ("AB", True),
    ("AC", True),
    ("AD", True),
    ("BC", True),
    ("BD", False),  # two approvals, but nobody from model-risk
    ("CD", True),
    ("ABC", True),
    ("ABD", True),
    ("BCD", True),
    ("ACD", True),
    ("ABCD", True),
]


def test_table_covers_every_subset() -> None:
    subsets = {"".join(c) for n in range(5) for c in itertools.combinations("ABCD", n)}
    assert {row for row, _ in TABLE} == subsets


@pytest.mark.parametrize(("who", "ok"), TABLE)
def test_two_of_four_with_role_truth_table(who: str, ok: bool) -> None:
    req = new_request(POLICY, RULE, PROPOSER, T0)
    st = evaluate_approvals(POLICY, req, _grants(*(PEOPLE[c] for c in who)), T0)
    assert st.satisfied is ok
    assert st.counted == tuple(sorted(PEOPLE[c].name for c in who))
    assert st.missing == max(0, 2 - len(who))


@pytest.mark.parametrize(
    ("required", "role", "who", "ok"),
    [
        (1, None, "D", True),
        (1, None, "", False),
        (1, "compliance", "A", False),
        (1, "compliance", "C", True),
        (3, None, "ABD", True),
        (3, None, "AB", False),
        (3, "compliance", "ABD", False),
        (3, "compliance", "ABC", True),
        (4, "model-risk", "ABCD", True),
    ],
)
def test_threshold_and_role_table(required: int, role: str | None, who: str, ok: bool) -> None:
    policy = POLICY.model_copy(update={"required": required, "required_role": role})
    req = new_request(policy, RULE, PROPOSER, T0)
    grants = [new_grant(policy, PEOPLE[c], T0) for c in who]
    assert evaluate_approvals(policy, req, grants, T0).satisfied is ok


@pytest.mark.parametrize(
    ("grant_age_h", "request_age_h", "ok"),
    [
        (0, 0, True),
        (71, 71, True),
        (72, 72, False),  # grants expire after 72h
        (1, 24 * 7, False),  # request expired, even with fresh grants
        (1, 24 * 7 - 1, True),
    ],
)
def test_expiry_table(grant_age_h: int, request_age_h: int, ok: bool) -> None:
    now = T0 + timedelta(hours=request_age_h)
    req = new_request(POLICY, RULE, PROPOSER, T0)
    grants = _grants(ALICE, BOB, at=now - timedelta(hours=grant_age_h))
    st = evaluate_approvals(POLICY, req, grants, now)
    assert st.satisfied is ok


def test_expired_grants_are_reported() -> None:
    req = new_request(POLICY, RULE, PROPOSER, T0)
    old = _grants(ALICE, at=T0)
    fresh = _grants(BOB, at=T0 + timedelta(hours=80))
    st = evaluate_approvals(POLICY, req, old + fresh, T0 + timedelta(hours=80))
    assert st.expired == (ALICE.name,) and st.counted == (BOB.name,)
    assert not st.satisfied and not st.role_satisfied


def test_grants_from_removed_approvers_do_not_count() -> None:
    req = new_request(POLICY, RULE, PROPOSER, T0)
    smaller = ApprovalPolicy(approvers=(ALICE, BOB), required=2)
    st = evaluate_approvals(smaller, req, _grants(ALICE, CAROL), T0)
    assert st.counted == (ALICE.name,) and not st.satisfied


# --- separation of duties ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("approver", "error"),
    [
        (PROPOSER, NotNamedApproverError),  # proposer is not named: refused anyway
        ("stranger@demo.bank", NotNamedApproverError),
        (ALICE.name, None),
    ],
)
def test_check_grant_named(approver: str, error: type[Exception] | None) -> None:
    req = new_request(POLICY, RULE, PROPOSER, T0)
    if error is None:
        assert check_grant(POLICY, req, RULE, [], approver, T0) == ALICE
    else:
        with pytest.raises(error):
            check_grant(POLICY, req, RULE, [], approver, T0)


@pytest.mark.parametrize("who", ["proposer", "owner", "agent"])
def test_separation_of_duties(who: str) -> None:
    """A named approver who proposed, owns or learned the rule cannot approve it."""
    person = {"proposer": ALICE.name, "owner": "ops-team", "agent": "agent-7"}[who]
    policy = POLICY.model_copy(
        update={
            "approvers": (
                *POLICY.approvers,
                NamedApprover(name="ops-team"),
                NamedApprover(name="agent-7"),
            )
        }
    )
    proposer = ALICE.name if who == "proposer" else PROPOSER
    req = new_request(policy, RULE, proposer, T0)
    with pytest.raises(SeparationOfDutiesError):
        check_grant(policy, req, RULE, [], person, T0)


def test_proposer_grant_never_counts_even_if_recorded() -> None:
    req = new_request(POLICY, RULE, ALICE.name, T0)
    st = evaluate_approvals(POLICY, req, _grants(ALICE, CAROL), T0)
    assert st.counted == (CAROL.name,) and not st.satisfied


def test_duplicate_and_expired_request() -> None:
    req = new_request(POLICY, RULE, PROPOSER, T0)
    with pytest.raises(DuplicateApprovalError):
        check_grant(POLICY, req, RULE, _grants(ALICE), ALICE.name, T0)
    late = T0 + timedelta(days=8)
    with pytest.raises(ApprovalExpiredError):
        check_grant(POLICY, req, RULE, [], ALICE.name, late)
    with pytest.raises(NoOpenRequestError):
        check_grant(POLICY, None, RULE, [], ALICE.name, T0)
    other = rv(GOOD_RULE_ID, GOOD_RULE.model_copy(update={"priority": 51}))
    with pytest.raises(ApprovalError, match="content"):
        check_grant(POLICY, req, other, [], ALICE.name, T0)


def test_all_errors_are_permission_errors() -> None:
    assert issubclass(SeparationOfDutiesError, ServiceApprovalError)
    assert issubclass(ServiceApprovalError, PermissionError)


# --- through the service, audited ----------------------------------------------------------


def _ready(clock: ManualClock | None = None) -> tuple[AgentGate, ManualClock]:
    clock = clock or ManualClock()
    cfg = GateConfig(n_cases=2_000, bootstrap_resamples=300)
    g = AgentGate(SQLiteStorage(":memory:"), clock, cfg)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    recs = g.evaluate(GOOD_RULE_ID, 1)
    assert recs[-1].decision.outcome is Outcome.CANARY_PASSED
    return g, clock


def _verify(g: AgentGate) -> None:
    rep = g.verify_integrity()
    assert rep.ok, rep.problems


def test_quorum_activates_and_every_event_is_chained() -> None:
    g, clock = _ready()
    g.set_approval_policy(POLICY, APPROVER)
    _verify(g)
    st = g.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    assert st.request_open and not st.satisfied
    _verify(g)
    clock.advance(minutes=5)
    st, rec = g.grant_approval(GOOD_RULE_ID, 1, BOB.name)
    assert rec is None and st.missing == 1
    _verify(g)
    with pytest.raises(DuplicateApprovalError):
        g.grant_approval(GOOD_RULE_ID, 1, BOB.name)
    clock.advance(minutes=5)
    st, rec = g.grant_approval(GOOD_RULE_ID, 1, ALICE.name)
    assert st.satisfied and rec is not None
    assert rec.decision.stage is Stage.ACTIVATION and rec.decision.outcome is Outcome.ACTIVATED
    assert "alice@demo.bank, bob@demo.bank" in rec.decision.reasons[0]
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.ACTIVE
    _verify(g)
    kinds = [e.event_type for e in g.audit.entries() if e.event_type in APPROVAL_EVENTS]
    assert kinds == [
        "approval.policy_set",
        "approval.requested",
        "approval.granted",
        "approval.granted",
        "approval.satisfied",
    ]
    tail = [e.event_type for e in g.audit.entries()[-4:]]
    assert tail == ["approval.granted", "decision.recorded", "approval.satisfied", "rule.activated"]


def test_role_missing_blocks_activation() -> None:
    g, _ = _ready()
    g.set_approval_policy(POLICY, APPROVER)
    g.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    g.grant_approval(GOOD_RULE_ID, 1, BOB.name)
    st, rec = g.grant_approval(GOOD_RULE_ID, 1, DAVE.name)
    assert rec is None and not st.role_satisfied
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.CANARY
    st, rec = g.grant_approval(GOOD_RULE_ID, 1, CAROL.name)
    assert rec is not None and st.counted == (BOB.name, CAROL.name, DAVE.name)


def test_service_separation_of_duties() -> None:
    g, _ = _ready()
    g.set_approval_policy(POLICY, APPROVER)
    g.request_approval(GOOD_RULE_ID, 1, ALICE.name)  # alice proposes
    n = len(g.audit.entries())
    with pytest.raises(SeparationOfDutiesError, match="proposer"):
        g.grant_approval(GOOD_RULE_ID, 1, ALICE.name)
    assert len(g.audit.entries()) == n  # refused attempts write nothing
    g.grant_approval(GOOD_RULE_ID, 1, BOB.name)
    st, rec = g.grant_approval(GOOD_RULE_ID, 1, CAROL.name)
    assert rec is not None and ALICE.name not in st.counted


def test_expired_request_needs_a_new_one_and_old_grants_lapse() -> None:
    g, clock = _ready()
    g.set_approval_policy(POLICY, APPROVER)
    g.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    g.grant_approval(GOOD_RULE_ID, 1, ALICE.name)
    clock.advance(days=8)
    with pytest.raises(ApprovalExpiredError):
        g.grant_approval(GOOD_RULE_ID, 1, BOB.name)
    st = g.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    assert st.counted == ()  # alice's grant belonged to the old request
    g.grant_approval(GOOD_RULE_ID, 1, BOB.name)
    _, rec = g.grant_approval(GOOD_RULE_ID, 1, ALICE.name)
    assert rec is not None
    _verify(g)


def test_policy_disables_single_approver_path_and_needs_operator() -> None:
    g, _ = _ready()
    with pytest.raises(ServiceApprovalError, match="may not change"):
        g.set_approval_policy(POLICY, ALICE.name)
    g.set_approval_policy(POLICY, APPROVER)
    assert g.approval_policy() == POLICY
    with pytest.raises(ServiceApprovalError, match="approval policy is in force"):
        g.approve(GOOD_RULE_ID, 1, APPROVER)
    with pytest.raises(NoOpenRequestError):
        g.grant_approval(GOOD_RULE_ID, 1, ALICE.name)
    g.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    with pytest.raises(ServiceApprovalError, match="open approval request"):
        g.request_approval(GOOD_RULE_ID, 1, PROPOSER)


def test_requests_need_a_policy_and_a_passed_canary() -> None:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), GateConfig(n_cases=2_000))
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    with pytest.raises(NoOpenRequestError, match="no approval policy"):
        g.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    g.set_approval_policy(POLICY, APPROVER)
    with pytest.raises(ServiceApprovalError, match="not passed canary"):
        g.request_approval(GOOD_RULE_ID, 1, PROPOSER)


def test_tampered_grant_breaks_the_chain(tmp_path: object) -> None:
    g = AgentGate.open(f"{tmp_path}/g.db", ManualClock(), GateConfig(n_cases=2_000))
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    g.set_approval_policy(POLICY, APPROVER)
    g.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    g.grant_approval(GOOD_RULE_ID, 1, BOB.name)
    _verify(g)
    g.storage.unsafe_execute("DROP TRIGGER audit_no_update")  # type: ignore[attr-defined]
    g.storage.unsafe_execute(  # type: ignore[attr-defined]
        "UPDATE audit_events SET payload_json = replace(payload_json, 'bob@', 'carol@') "
        "WHERE event_type = 'approval.granted'"
    )
    rep = g.verify_integrity()
    assert not rep.ok and "edited" in (rep.chain.problem or "")


def test_approval_after_ramp_completes() -> None:
    from scruple.gate.canary import RampSchedule
    from scruple.sim.generator import generate_cases

    g, _ = _ready()
    g.start_canary_ramp(GOOD_RULE_ID, 1, RampSchedule(stages=(10.0,)))
    g.set_approval_policy(POLICY, APPROVER)
    rec, state = g.advance_canary(GOOD_RULE_ID, 1, generate_cases(77, 2_000, prefix="z"))
    assert state.status == "completed"
    g.request_approval(GOOD_RULE_ID, 1, PROPOSER)
    g.grant_approval(GOOD_RULE_ID, 1, ALICE.name)
    _, act = g.grant_approval(GOOD_RULE_ID, 1, BOB.name)
    assert act is not None and rec.decision.decision_id in act.decision.reasons[-1]
