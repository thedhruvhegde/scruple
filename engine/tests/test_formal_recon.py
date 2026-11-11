"""Reconciliation: the model steps like the real ReconEngine, and its money invariants
hold (bounded model checking and induction with z3)."""

from __future__ import annotations

import collections
from dataclasses import replace

import pytest

from scruple.formal import recon as rm
from scruple.formal.bmc import Report, check, induction
from scruple.formal.ops import ModelError
from scruple.formal.recon_conformance import ReconConformance, run_recon
from scruple.sim.recon.engine import Adjust, Escalate, Match
from scruple.sim.recon.generator import generate_recon_case

FAST_DEPTH = 6
NAMES = tuple(i.name for i in rm.MODEL.invariants)


@pytest.mark.parametrize("difficulty", [0.0, 0.5, 1.0])
def test_engine_actions_are_model_steps(difficulty: float) -> None:
    seen: collections.Counter[str] = collections.Counter()
    for seed in range(12):
        tr = run_recon(generate_recon_case(seed, difficulty=difficulty), seed, steps=15)
        seen.update(c.split("_")[0] for c in tr.commands)
    assert {"MATCH", "UNMATCH", "ADJUST", "MARK", "ESCALATE"} <= set(seen)


def test_a_drifted_model_is_caught() -> None:
    """A model that lets an adjustment cite an escalated line fails conformance."""
    case = generate_recon_case(3, difficulty=0.5)
    conf = ReconConformance(case, 4, invariants=False)
    sid = conf.s_ids[0]
    assert conf.act(Escalate(statement_line_ids=(sid,)))
    loose = tuple(
        replace(c, guard=lambda s, p, i, ops: True) if c.name == "ADJUST_0" else c
        for c in conf.model.commands
    )
    conf.model = replace(conf.model, commands=loose)
    with pytest.raises(ModelError):
        conf.act(Adjust(adjust_kind="fee", amount_minor=-5, statement_line_ids=(sid,)))


def test_refusals_agree_with_the_model() -> None:
    case = generate_recon_case(5, difficulty=0.0)
    conf = ReconConformance(case, 4)
    s0, l0 = conf.s_ids[0], conf.l_ids[0]
    assert conf.act(Match(statement_line_ids=(s0,), ledger_line_ids=(l0,)))
    assert not conf.act(Match(statement_line_ids=(s0,), ledger_line_ids=(conf.l_ids[1],)))
    assert not conf.act(Adjust(adjust_kind="fee", amount_minor=0))
    assert conf.trace.refused == 2


@pytest.fixture(scope="module")
def report() -> Report:
    return check(rm.MODEL, FAST_DEPTH)


@pytest.mark.parametrize("name", NAMES)
def test_invariant_holds_to_depth(report: Report, name: str) -> None:
    r = next(x for x in report.results if x.invariant == name)
    assert r.holds, r.counterexample.text() if r.counterexample else name


def test_every_command_fires(report: Report) -> None:
    assert all(d is not None for d in report.covered.values()), report.covered


def test_invariants_are_inductive() -> None:
    assert set(induction(rm.MODEL).inductive) == set(NAMES)


@pytest.mark.parametrize("name", ["uncited_adjustment_closes", "matched_line_also_adjusted"])
def test_the_engine_leaves_justification_to_the_verifier(name: str) -> None:
    """By design (REC-EVID judges it): the engine accepts an adjustment that cites no
    statement line, or one already matched. The checker shows both reachable, which is
    why `no_money_invented` is stated for a closed reconciliation only."""
    rep = check(rm.witnesses(rm.MODEL), 3, only=(name,))
    r = rep.results[0]
    assert not r.holds and r.counterexample is not None
    _, broken = r.counterexample.replay(rm.witnesses(rm.MODEL))
    assert name in broken
