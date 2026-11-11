"""Bounded model checking (z3) of the ACH return model, and the engine bug its
counterexample pointed to."""

from __future__ import annotations

import pytest

from scruple.formal import ach as am
from scruple.formal.bmc import Report, check, induction
from scruple.sim.ach.engine import AchEngine, Dishonor, Reinitiate
from scruple.sim.ach.generator import generate_ach_case
from scruple.sim.ach.models import AchCase

FAST_DEPTH = 7
SAFE = tuple(i.name for i in am.INVARIANTS)
# a retry's own return is timely and not a duplicate, so a dishonor of it never stands
NEVER = {f"DISHONOR_FINAL_R{n}" for n in (1, 2, 3)}


@pytest.fixture(scope="module")
def report() -> Report:
    return check(am.MODEL, FAST_DEPTH)


@pytest.mark.parametrize("name", SAFE)
def test_invariant_holds_to_depth(report: Report, name: str) -> None:
    r = next(x for x in report.results if x.invariant == name)
    assert r.holds, r.counterexample.text() if r.counterexample else name


def test_the_check_is_not_vacuous(report: Report) -> None:
    """Within the depth every command of the first slots fires on some path; the ones
    that cannot (a dishonor of a retry's return standing) never do."""
    fired = {c for c, d in report.covered.items() if d is not None}
    first = {c.name for c in am.COMMANDS if c.name.endswith(("O0", "O1", "R1"))} - NEVER
    agent = {"ACCEPT", "WRITE_OFF", "REINITIATE", "RETRY_CLEARS", "RETRY_RETURNED"}
    assert first | agent <= fired
    assert not fired & NEVER


def test_invariants_are_inductive() -> None:
    ind = induction(am.MODEL, candidates=SAFE)
    assert set(ind.inductive) == set(SAFE), ind.dropped


def _retry_and_dishonor(case: AchCase) -> AchEngine:
    eng = AchEngine(case)
    rid = eng.settled_returns()[0].return_id
    assert eng.act(Reinitiate(company_entry_description="RETRY PYMT")).ok
    eng.act(Dishonor(return_id=rid, code="R68"))
    return eng


def test_dishonor_while_a_retry_is_in_flight_does_not_collect_twice() -> None:
    """Counterexample (depth 5): an R01 return settles, the originator reinitiates and,
    before the retry settles, dishonors the return. The engine accepted that: both
    settled the next banking day, the entry was collected twice and the receivable went
    negative. A generated case showed it for a day (the contest took it back); with an
    untimely R01 and a receiver whose funds have arrived the dishonor stood and the
    entry stayed collected twice. Now the dishonor waits for the retry."""
    eng = _retry_and_dishonor(generate_ach_case(0, "nsf_recoverable", 0.0))
    eng.advance(1)
    assert eng.receivable() >= 0
    untimely = generate_ach_case(0, "untimely_return", 0.0)
    assert untimely.returns[0].code == "R01"
    data = untimely.model_dump()
    data["truth"]["funds_on"] = untimely.returns[0].settles_on
    eng = _retry_and_dishonor(AchCase.model_validate(data))
    eng.run_out()
    out = eng.outcome()
    assert out.allocation.collected_minor <= untimely.amount_minor
    assert eng.receivable() >= 0
