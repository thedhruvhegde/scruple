"""No leakage: zoo agents decide from tool outputs only, never from ground truth.

Four independent lines of defence are pinned here:

1. source: agent modules never name the truth, the issuer, the session or the engine;
   the driver touches the episode only through its public tool API;
2. data: every decision context from real episodes holds whitelisted case fields and
   no hidden field anywhere in it;
3. objects: a trained agent's object graph holds no episode, session, engine, case,
   truth, issuer or record object;
4. behaviour: zoo episodes never read protected data and never fail the scope check,
   and the zoo refuses to train or tune on the held-out split.
"""

from __future__ import annotations

import ast
import io
import pickle
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.env import DISPUTE_CASE_FIELDS, HIDDEN_FIELDS, DisputeSession
from scruple.sim.disputes import CardDispute, DisputeEngine
from scruple.sim.issuer import SeededIssuer
from scruple.sim.splits import SeedSpace, Split, split_of
from scruple.verifiers.record import EpisodeRecord
from scruple.zoo.bandit import LinUCBAgent
from scruple.zoo.context import DecisionContext, legal_acts
from scruple.zoo.planner import PlannerAgent, PlannerModel
from scruple.zoo.protocol import ZooAgent, ZooPolicy
from scruple.zoo.qlearn import QAgent
from scruple.zoo.rollout import Outcome, dev_cases, run_case, split_cases, train_cases
from scruple.zoo.rules import ExpertAgent, RandomAgent
from scruple.zoo.supervised import ClassifierAgent, branch_data, fit_model

ZOO = Path(__file__).resolve().parents[1] / "scruple" / "zoo"
AGENT_MODULES = ("context", "rules", "supervised", "bandit", "qlearn", "planner")
FORBIDDEN_NAMES = {
    "truth",
    "ground_truth_for_reference",
    "claim_valid",
    "liability_minor",
    "forged",
    "family",
    "SeededIssuer",
    "IssuerParams",
    "session",
    "engine",
    "DisputeSession",
    "DisputeEngine",
    "CardDispute",
}
FORBIDDEN_IMPORTS = ("scruple.sim", "scruple.agents.env", "scruple.verifiers")
EXTRA_CASE_FIELDS = {
    "reason_title",
    "category",
    "workflow",
    "required_evidence",
    "supporting_evidence",
    "ce3_eligible",
    "merchant_tier",
}
EPISODE_API = {"call", "trace", "closed", "submit", "episode_id"}


def _names(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.arg):
            yield node.arg


@pytest.mark.parametrize("module", AGENT_MODULES)
def test_agent_modules_never_name_hidden_state(module: str) -> None:
    tree = ast.parse((ZOO / f"{module}.py").read_text())
    used = set(_names(tree))
    assert not used & FORBIDDEN_NAMES, sorted(used & FORBIDDEN_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith(FORBIDDEN_IMPORTS), mod
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(FORBIDDEN_IMPORTS), alias.name


def test_the_driver_uses_only_the_public_episode_api() -> None:
    tree = ast.parse((ZOO / "protocol.py").read_text())
    touched = {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "ep"
    }
    assert touched <= EPISODE_API, touched - EPISODE_API
    assert "ground_truth_for_reference" not in set(_names(tree))


# --- real episodes ------------------------------------------------------------------


class Spy:
    """Wraps an agent and records exactly what the driver handed it."""

    def __init__(self, inner: ZooAgent) -> None:
        self.inner = inner
        self.seen: list[tuple[Any, Any]] = []
        self.name = inner.name
        self.kind = inner.kind

    def act(self, ctx: DecisionContext, legal: tuple[Any, ...]) -> Any:
        self.seen.append((ctx, legal))
        return self.inner.act(ctx, legal)


@cache
def played() -> tuple[list[Spy], list[Outcome]]:
    agents: list[ZooAgent] = [
        RandomAgent(seed=5),
        ExpertAgent(),
        LinUCBAgent(alpha=1.0),
        QAgent(epsilon=0.5, seed=1),
    ]
    spies = [Spy(a) for a in agents]
    outs = [run_case(ZooPolicy(s), c) for s in spies for c in train_cases(12)]
    return spies, outs


def _keys(x: Any) -> Iterator[str]:
    if isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from _keys(v)
    elif isinstance(x, list | tuple):
        for v in x:
            yield from _keys(v)


def test_agents_receive_only_contexts_and_legal_acts() -> None:
    spies, _ = played()
    assert all(s.seen for s in spies)
    for s in spies:
        for ctx, legal in s.seen:
            assert type(ctx) is DecisionContext
            assert legal == legal_acts(ctx)


def test_contexts_hold_whitelisted_fields_and_nothing_hidden() -> None:
    spies, _ = played()
    contexts = [ctx for s in spies for ctx, _ in s.seen]
    assert len(contexts) > 40
    allowed = set(DISPUTE_CASE_FIELDS) | EXTRA_CASE_FIELDS
    for ctx in contexts:
        assert set(ctx.case) <= allowed
        dumped = ctx.model_dump(mode="json")
        assert set(dumped) == {"episode_id", "step", "case", "status", "docs", "statement"}
        assert not set(_keys(dumped)) & HIDDEN_FIELDS


def test_zoo_episodes_never_read_protected_data() -> None:
    _, outs = played()
    assert outs
    for o in outs:
        assert "SCOPE-01" not in o.hard_failures
        assert "SR-03" not in o.hard_failures


def test_episode_trace_has_no_protected_read() -> None:
    from scruple.agents.dispute_baselines import run_policy
    from scruple.verifiers.trace import EventKind

    for c in train_cases(4):
        ep = run_policy(ZooPolicy(ExpertAgent()), c.seed, None, id_prefix=c.id_prefix)
        assert not [e for e in ep.trace if e.kind is EventKind.PROTECTED_READ]
        assert "ground_truth_read" not in (ep.score.flags if ep.score else ())


# --- trained object graphs ----------------------------------------------------------

FORBIDDEN_TYPES = (
    DisputeEpisode,
    DisputeSession,
    DisputeEngine,
    CardDispute,
    SeededIssuer,
    EpisodeRecord,
)


class _Scan(pickle.Pickler):
    def __init__(self, f: io.BytesIO) -> None:
        super().__init__(f)
        self.bad: list[str] = []

    def persistent_id(self, obj: Any) -> None:
        if isinstance(obj, FORBIDDEN_TYPES):
            self.bad.append(type(obj).__name__)
        return None


def _scan(obj: Any) -> list[str]:
    p = _Scan(io.BytesIO())
    p.dump(obj)
    return p.bad


def test_trained_agents_hold_no_episode_case_or_issuer_objects() -> None:
    _, outs = played()
    cases = train_cases(6)
    data = branch_data(cases)
    trained: list[Any] = [
        ClassifierAgent(fit_model(data, "scorecard", "logreg"), name="c"),
        PlannerAgent(PlannerModel("scorecard").fit(outs)),
    ]
    bandit, q = LinUCBAgent(), QAgent()
    for o in outs:
        bandit.learn(o, "scorecard")
        q.learn(o, "scorecard")
    trained += [bandit.frozen(), q.frozen(), ExpertAgent(), RandomAgent()]
    for agent in trained:
        assert _scan(ZooPolicy(agent)) == []
    assert _scan(outs[0]) == []  # experience is contexts and numbers only


def test_the_scan_would_catch_a_leak() -> None:
    from scruple.agents.env import select_dispute

    c = train_cases(1)[0]
    assert _scan({"oops": select_dispute(c.seed, None, c.id_prefix)}) == ["CardDispute"]


# --- splits -------------------------------------------------------------------------


def test_the_zoo_refuses_the_heldout_split() -> None:
    with pytest.raises(PermissionError):
        split_cases(Split.HELDOUT, 1)


def test_train_and_dev_cases_live_in_their_namespaces() -> None:
    tr, dv = train_cases(50), dev_cases(50)
    assert all(split_of(c.seed) is Split.TRAIN for c in tr)
    assert all(split_of(c.seed) is Split.DEV for c in dv)
    assert not {c.seed for c in tr} & {c.seed for c in dv}
    assert all(c.id_prefix.startswith("tr-") for c in tr)
    assert all(c.id_prefix.startswith("dv-") for c in dv)
    assert train_cases(5, start=45) == tr[45:]
    space = SeedSpace("zoo-test-salt-0123456789")
    held = {space.seed(Split.HELDOUT, i) for i in range(50)}
    assert not held & {c.seed for c in tr + dv}
