"""The golden episodes: one deterministic trace per workflow per seed.

Every trace is plain JSON data built from public APIs, with an injected clock and
explicit seeds only, so the bytes never depend on the machine, the run or the
Python hash seed. All data is synthetic.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from scruple.clock import SteppingClock
from scruple.gate.approval import ApprovalPolicy, NamedApprover
from scruple.gate.canary import RampSchedule
from scruple.gate.config import GateConfig
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    FULL_MIX,
    Accept,
    AgentAction,
    DisputeEngine,
    Escalate,
    Represent,
    RequestInfo,
    generate_cases,
)
from scruple.sim.engine import Simulator
from scruple.sim.generator import generate_cases as generate_batch
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID, baseline_policy
from scruple.sim.merchants import generate_merchants
from scruple.sim.networks import Workflow
from scruple.storage import SQLiteStorage

Trace = dict[str, Any]
SEEDS = (1, 7, 2026)
PROV = Provenance(agent_id="dispute-agent-1", episode_ids=("ep-1", "ep-2", "ep-3"))
APPROVER = "model-risk@demo.bank"
CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300)


def dumps(trace: Trace) -> str:
    """The canonical bytes of a trace file: sorted keys, one key per line."""
    return json.dumps(trace, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


# ------------------------------------------------------------- batch simulator
def batch_run(seed: int) -> Trace:
    """The original Simulator under the baseline policy (results and journal)."""
    run = Simulator(generate_batch(seed, 40)).run(baseline_policy())
    return {"digest": run.digest(), "run": run.model_dump(mode="json")}


# -------------------------------------------------------------- dispute engine
def _agent(engine: DisputeEngine, cid: str, turn: int) -> AgentAction:
    """A scripted agent: ask, then represent with everything, accept a pre-arb on odd
    turns and escalate it on even ones."""
    obs = engine.observe(cid)
    allowed = obs.allowed_actions
    if turn == 0:
        return RequestInfo()
    if "represent" in allowed:
        return Represent(evidence_ids=tuple(d.doc_id for d in obs.evidence))
    if "escalate" in allowed and turn % 2 == 0:
        return Escalate()
    return Accept()


def dispute_episode(seed: int, workflow: Workflow) -> Trace:
    """One workflow of the event-sourced engine played to the end, day by day."""
    cases = [c for c in generate_cases(seed, 60, FULL_MIX) if c.workflow is workflow][:8]
    clock = ManualClock()
    clock.set_date(min(c.opened_on for c in cases))
    merchants = generate_merchants(seed)
    e = DisputeEngine(cases, clock, merchants=merchants)
    turns: dict[str, int] = {}
    results = []
    for _ in range(400):
        e.sync()
        for cid in e.open_cases():
            if e.allowed_actions(cid) and e.observe(cid).today >= e.case(cid).opened_on:
                t = turns.get(cid, 0)
                res = e.act(cid, _agent(e, cid, t))
                turns[cid] = t + 1
                results.append(res.model_dump(mode="json"))
        if not e.open_cases():
            break
        clock.advance(days=1)
    e.post_monitoring_fines()
    outcomes = {}
    for cid in e.case_ids:
        out = e.outcome(cid)
        outcomes[cid] = None if out is None else out.model_dump(mode="json")
    return {
        "workflow": workflow.value,
        "cases": [c.case_id for c in cases],
        "actions": results,
        "events": json.loads(e.events_json()),
        "outcomes": outcomes,
        "incidents": [i.model_dump(mode="json") for i in e.incidents],
        "state_digest": e.state_digest(),
    }


# ----------------------------------------------------------------- the gate
def _gate() -> AgentGate:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    g.bootstrap_baseline()
    return g


def _audit(g: AgentGate) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in g.audit.entries()]


def promotion(seed: int) -> Trace:
    """Good rule: shadow, canary, staged ramp, approval, rollback. Bad rule: blocked."""
    g = _gate()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    g.evaluate(BAD_RULE_ID, 1)
    g.start_canary_ramp(GOOD_RULE_ID, 1, RampSchedule(stages=(10.0, 25.0)))
    for batch in range(4):
        cases = generate_batch(seed * 100 + batch, 3_000, prefix=f"g{batch}-")
        _, state = g.advance_canary(GOOD_RULE_ID, 1, cases)
        if state.status != "ramping":
            break
    assert state.status == "completed", f"golden ramp did not complete for seed {seed}"
    g.approve(GOOD_RULE_ID, 1, APPROVER)
    g.run_assurance(seed=seed)
    g.rollback(GOOD_RULE_ID, APPROVER, "golden episode rollback")
    return {
        "audit": _audit(g),
        "decisions": [d.model_dump(mode="json") for d in g.decisions()],
        "registry": [r.model_dump(mode="json") for r in g.registry.all_versions()],
        "integrity_ok": g.verify_integrity().ok,
        "head": g.audit.head(),
    }


def approvals() -> Trace:
    """N-of-M approval: policy, request, a refused self-approval, quorum, activation."""
    g = _gate()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    policy = ApprovalPolicy(
        approvers=(
            NamedApprover(name="alice@demo.bank", roles=("model-risk",)),
            NamedApprover(name="bob@demo.bank"),
            NamedApprover(name="carol@demo.bank"),
        ),
        required=2,
        required_role="model-risk",
    )
    g.set_approval_policy(policy, APPROVER)
    steps: list[dict[str, Any]] = []
    steps.append(g.request_approval(GOOD_RULE_ID, 1, "bob@demo.bank").model_dump(mode="json"))
    for who in ("bob@demo.bank", "carol@demo.bank", "alice@demo.bank"):
        try:
            status, rec = g.grant_approval(GOOD_RULE_ID, 1, who)
            steps.append(
                {
                    "approver": who,
                    "status": status.model_dump(mode="json"),
                    "decision": None if rec is None else rec.decision.decision_id,
                }
            )
        except Exception as exc:  # the refusal is part of the trace
            steps.append({"approver": who, "refused": type(exc).__name__, "detail": str(exc)})
    return {"steps": steps, "audit": _audit(g), "head": g.audit.head()}


TRACES: dict[str, Callable[[], Trace]] = {
    **{f"sim_batch_seed{s}": (lambda s=s: batch_run(s)) for s in SEEDS},  # type: ignore[misc]
    **{
        f"disputes_{w.value}_seed{s}": (lambda s=s, w=w: dispute_episode(s, w))  # type: ignore[misc]
        for s in SEEDS
        for w in Workflow
    },
    "gate_promotion_seed1": lambda: promotion(1),
    "gate_promotion_seed7": lambda: promotion(7),
    "gate_approvals": approvals,
}
