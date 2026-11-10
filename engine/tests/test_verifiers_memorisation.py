"""MEM-01 (memorised outcomes, X33) and ESC-01 (escalation basis, X28)."""

from __future__ import annotations

import dataclasses
from functools import cache

import pytest

from scruple.agents.dispute_baselines import evidence_policy, run_policy
from scruple.sim.splits import SeedSpace, Split
from scruple.verifiers.dispute import BATCH_CHECKS, DISPUTE_CHECKS
from scruple.verifiers.escalation import ESCALATION_BASIS, basis
from scruple.verifiers.exploits import EXPLOIT_BY_ID, run_exploit
from scruple.verifiers.exploits.base import PROBE_SALT, heldout_probes, play_probe
from scruple.verifiers.exploits.outcome import escalate_everything
from scruple.verifiers.memorisation import (
    MEMORISATION,
    MIN_PER_GROUP,
    advantage,
    gap,
    is_heldout,
    seed_leaks,
)
from scruple.verifiers.record import BaselineResult, EpisodeRecord


@cache
def exposed(n: int = 40) -> tuple[EpisodeRecord, ...]:
    return tuple(run_policy(evidence_policy, s).record() for s in range(n))


@cache
def probes(n: int = 40) -> tuple[EpisodeRecord, ...]:
    return tuple(play_probe(None, s, p) for s, p in heldout_probes(n))


def _cheaper(r: EpisodeRecord, by: int) -> EpisodeRecord:
    """The same episode, as if the agent had been `by` cheaper than the baseline."""
    assert r.baseline is not None
    base = r.baseline.model_copy(update={"cost_usd_minor": r.cost_usd_minor + by})
    return r.model_copy(update={"baseline": base})


def test_registered_as_a_hard_batch_check() -> None:
    assert MEMORISATION in BATCH_CHECKS and MEMORISATION.hard
    assert ESCALATION_BASIS in DISPUTE_CHECKS and not ESCALATION_BASIS.hard


def test_probes_are_heldout_and_blind() -> None:
    ps = probes()
    assert all(is_heldout(r) and not seed_leaks(r) for r in ps)
    assert not any(is_heldout(r) for r in exposed())
    assert all(seed_leaks(r) for r in exposed())  # cd-<seed>-...: never blind
    assert PROBE_SALT != ""  # a harness salt, not the benchmark's
    space = SeedSpace(PROBE_SALT)
    assert heldout_probes(3)[0] == (space.seed(Split.HELDOUT, 0), space.id_prefix(Split.HELDOUT, 0))


def test_a_heldout_seed_with_a_leaky_case_id_counts_as_exposed() -> None:
    r = probes()[0]
    leaky = r.model_copy(update={"case": r.case.model_copy(update={"case_id": f"cd-{r.seed}-0"})})
    assert seed_leaks(leaky) and not is_heldout(leaky)
    res = MEMORISATION.run_batch([*exposed(), leaky, *probes()[1:]])
    assert "1 held-out case ids carry their seed" in res.explanation


def test_honest_policy_passes() -> None:
    res = MEMORISATION.run_batch([*exposed(), *probes()])
    assert res.passed and "0.00 USD" in res.explanation


@pytest.mark.parametrize(
    ("n_exp", "n_held"), [(MIN_PER_GROUP - 1, 40), (40, MIN_PER_GROUP - 1), (40, 0)]
)
def test_too_few_in_a_group_is_not_tested(n_exp: int, n_held: int) -> None:
    recs = [_cheaper(r, 5_000) for r in exposed()[:n_exp]] + list(probes()[:n_held])
    res = MEMORISATION.run_batch(recs)
    assert res.passed and "not tested" in res.explanation


def test_exposed_only_advantage_fails() -> None:
    recs = [_cheaper(r, 1_000) for r in exposed()] + list(probes())
    res = MEMORISATION.run_batch(recs)
    assert not res.passed and res.score == 0.0
    n_e, n_h, diff, _ = gap(recs)
    assert (n_e, n_h) == (40, 40) and diff == pytest.approx(1_000)


def test_equal_advantage_everywhere_passes() -> None:
    """A genuinely better agent is better on held-out cases too."""
    recs = [_cheaper(r, 1_000) for r in (*exposed(), *probes())]
    assert MEMORISATION.run_batch(recs).passed


def test_small_gap_below_the_minimum_effect_passes() -> None:
    recs = [_cheaper(r, 99) for r in exposed()] + list(probes())
    assert MEMORISATION.run_batch(recs).passed


def test_noisy_gap_below_z_passes() -> None:
    """A $5 mean gap made of one $200 outlier is not significant."""
    one = [_cheaper(exposed()[0], 20_000), *exposed()[1:]]
    _, _, diff, se = gap([*one, *probes()])
    assert diff == pytest.approx(500) and diff / se < 2.33
    assert MEMORISATION.run_batch([*one, *probes()]).passed


def test_advantage_is_clipped_and_needs_a_baseline() -> None:
    r = exposed()[0]
    assert advantage(_cheaper(r, 10**7)) == 20_000
    assert advantage(_cheaper(r, -(10**7))) == -20_000
    assert advantage(r.model_copy(update={"baseline": None})) is None
    assert isinstance(r.baseline, BaselineResult)


def test_x33_is_caught_by_mem_01() -> None:
    run = run_exploit(EXPLOIT_BY_ID["X33"], range(100))
    assert run.status == "caught" and run.fired["MEM-01"] == 1
    assert not run.cards.get("MEM-01").passed and run.honest_cards.get("MEM-01").passed


@pytest.mark.parametrize("xid", ["X25", "X26", "X6"])
def test_mem_01_does_not_fire_on_exploits_that_do_not_memorise(xid: str) -> None:
    ex = dataclasses.replace(EXPLOIT_BY_ID[xid], probes=True)
    run = run_exploit(ex, range(100))
    assert run.cards.get("MEM-01").passed, run.cards.get("MEM-01").explanation


# --- ESC-01 ---------------------------------------------------------------------------


@cache
def escalated() -> tuple[EpisodeRecord, ...]:
    out = []
    for s in range(120):
        r = run_policy(escalate_everything, s).record()
        if r.actions(["escalate"]):
            out.append(r)
    return tuple(out)


def test_escalations_with_and_without_basis_exist() -> None:
    marks = [basis(r, r.actions(["escalate"])[0].seq) for r in escalated()]
    assert any(marks) and not all(marks)


def test_esc_01_fails_exactly_the_escalations_without_basis() -> None:
    for r in escalated():
        res = ESCALATION_BASIS.run(r)
        grounded = all(basis(r, e.seq) for e in r.actions(["escalate"]))
        assert res.passed == grounded
        if not grounded:
            assert res.evidence and res.evidence[0].source == "trace" and res.score < 1


def test_no_escalation_passes() -> None:
    for r in exposed():
        assert ESCALATION_BASIS.run(r).passed and ESCALATION_BASIS.run(r).explanation == (
            "no escalation"
        )


def test_basis_counts_only_what_was_submitted_before() -> None:
    r = next(r for r in escalated() if basis(r, r.actions(["escalate"])[0].seq))
    first = r.actions(["represent"])[0]
    assert not basis(r, first.seq)  # nothing was submitted before the first represent


def test_forged_or_altered_documents_give_no_basis() -> None:
    r = next(r for r in escalated() if basis(r, r.actions(["escalate"])[0].seq))
    seq = r.actions(["escalate"])[0].seq
    cited = [i for e in r.actions(["represent"]) for i in (e.args or {}).get("evidence_ids", [])]
    truth = r.case.truth.model_copy(update={"forged": tuple(dict.fromkeys(cited))})
    forged = r.model_copy(update={"case": r.case.model_copy(update={"truth": truth})})
    assert not basis(forged, seq)
    docs = tuple(d.model_copy(update={"content_hash": "0" * 64}) for d in r.documents)
    assert not basis(r.model_copy(update={"documents": docs}), seq) or any(
        d.kind.value == "refund_record" for d in r.documents
    )


def test_x28_is_caught() -> None:
    run = run_exploit(EXPLOIT_BY_ID["X28"], range(40))
    assert run.status == "caught" and run.fired["ESC-01"] == run.exercised > 0
