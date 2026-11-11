"""The ACH model in scruple.formal steps exactly like the real AchEngine."""

from __future__ import annotations

import collections

import pytest

from scruple.formal import ach as am
from scruple.formal.ach_conformance import AchConformance, run_ach
from scruple.formal.ops import ModelError
from scruple.sim.ach.engine import AcceptReturn, Dishonor
from scruple.sim.ach.generator import generate_ach_case, generate_ach_cases
from scruple.sim.ach.models import FAMILIES


@pytest.mark.parametrize("difficulty", [0.0, 0.5, 1.0])
@pytest.mark.parametrize("seed", [1, 2])
def test_engine_steps_are_model_steps(seed: int, difficulty: float) -> None:
    seen: collections.Counter[str] = collections.Counter()
    for i, case in enumerate(generate_ach_cases(seed, 120, difficulty=difficulty)):
        seen.update(run_ach(case, i, invariants=False).commands)
    assert sum(seen.values()) > 400


@pytest.mark.parametrize("family", [f.value for f in FAMILIES])
def test_every_family_conforms(family: str) -> None:
    for seed in range(25):
        run_ach(generate_ach_case(seed, family, 0.7), seed, invariants=False)


def test_most_commands_are_exercised() -> None:
    seen: collections.Counter[str] = collections.Counter()
    for i, case in enumerate(generate_ach_cases(3, 400, difficulty=1.0)):
        seen.update(run_ach(case, i, invariants=False).commands)
    # R3 dishonors are rare: they need three retries and then a dishonor
    rare = {f"DISHONOR_{x}_R3" for x in ("FINAL",)} | {f"DISHONOR_FINAL_R{n}" for n in (1, 2, 3)}
    assert {c.name for c in am.COMMANDS} - rare <= set(seen)


def _contest_mutant(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = am.improper_return
    monkeypatch.setattr(am, "improper_return", lambda s, k, ops: ops.not_(orig(s, k, ops)))


def _busy_mutant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(am, "busy", lambda s, ops: s["retry"])


@pytest.mark.parametrize("mutate", [_contest_mutant, _busy_mutant], ids=lambda f: f.__name__)
def test_a_drifted_model_is_caught(mutate: object, monkeypatch: pytest.MonkeyPatch) -> None:
    assert callable(mutate)
    mutate(monkeypatch)
    cases = generate_ach_cases(1, 300, difficulty=1.0)

    def run_all() -> None:
        for i, case in enumerate(cases):
            run_ach(case, i, invariants=False)

    with pytest.raises(ModelError):
        run_all()


def test_refusals_agree_with_the_model() -> None:
    conf = AchConformance(generate_ach_case(4, "account_closed", 0.0), invariants=False)
    assert conf.act(AcceptReturn())
    assert not conf.act(AcceptReturn())  # nothing left receivable: both refuse
    rid = conf.engine.settled_returns()[0].return_id
    assert not conf.act(Dishonor(return_id=rid, code="R68"))
    assert conf.trace.refused == 2
