"""The blind-spot search (`scruple redteam search`), its genomes and the N-version
differential harness: determinism, the search's sanity checks, and agreement of the
independent checkers with the existing hard checks on a small corpus."""

from __future__ import annotations

import pytest

from scruple.redteam.differential import run as differential
from scruple.redteam.envs import ENV_NAMES, adapter, play
from scruple.redteam.genome import Genome, Op, Seen, mutate, random_op, rng_for, sample_args
from scruple.redteam.search import compare, eval_one, search, wilson


@pytest.mark.parametrize("env", ENV_NAMES)
def test_scenario_banks_are_distinct_and_play(env: str) -> None:
    from scruple.redteam.banks import play_ct, run_bank, scenarios

    sc = scenarios(env, 50)
    assert len({(s, ct) for s, ct, _ in sc}) == 50
    assert scenarios(env, 50) == sc  # seeded
    s, ct, _ = sc[0]
    assert adapter(env).score(play_ct(env, Genome(adapter(env).honest), s, ct)).gate
    res = run_bank(env, 3)
    assert res["families"]["honest"]["gate_fail"] == 0


def test_fault_injection_is_seen_by_both_ledger_versions() -> None:
    from scruple.redteam.nversion_run import faults

    for row in faults((70_000, 70_001)).values():
        assert row["agree"] == row["episodes"]


@pytest.mark.parametrize("env", ENV_NAMES)
def test_honest_genome_is_the_honest_policy(env: str) -> None:
    """The harness adds nothing to an empty genome: honest passes the gate, scores 1
    on the truth utility of the finance kinds, and tells no lie."""
    a = adapter(env)
    for s in range(3):
        r = play(env, Genome(a.honest), s)
        assert a.score(r).gate
        assert not a.false_claim(r)


@pytest.mark.parametrize("env", ENV_NAMES)
def test_random_genomes_never_crash_the_harness(env: str) -> None:
    a = adapter(env)
    rng = rng_for("t", env)
    for i in range(8):
        g = Genome(rng.choice([b for b in a.space.bases if b != "knobs"]))
        for _ in range(3):
            g = mutate(g, a.space, rng)
        g = Genome(g.base, g.knobs, (*g.ops, random_op(a.space, rng)))
        r = play(env, g, i)
        a.score(r)
        a.truth(r)
        assert Genome.from_json(g.to_json()) == g


def test_eval_is_deterministic() -> None:
    g = Genome(
        "honest", (), (Op("insert", "adjust", n=2), Op("claim", field="balanced", value=True))
    )
    a = eval_one(("recon", g.to_json(), (1, 2, 3)))
    b = eval_one(("recon", g.to_json(), (1, 2, 3)))
    assert a == b


def test_search_is_deterministic_and_independent_of_workers() -> None:
    kw = {"train": range(10_000, 10_012), "holdout": range(20_000, 20_020), "validate": 3}
    r1 = search("aml", 14, workers=1, **kw)  # type: ignore[arg-type]
    r2 = search("aml", 14, workers=2, **kw)  # type: ignore[arg-type]
    assert r1.model_dump() == r2.model_dump()


def test_search_rediscovers_a_known_priced_escape() -> None:
    """Sanity check (pre-registered): defensive filing (XM9) is a gate escape the
    soft checks price. The comparison must call it an escape, not a reward escape."""
    seeds = range(20_000, 20_120)
    h = eval_one(("aml", Genome("honest").to_json(), tuple(seeds)))
    d = eval_one(("aml", Genome("defensive").to_json(), tuple(seeds)))
    c = compare("aml", d, h)
    assert c.escape and not c.reward_escape
    assert c.gate == c.honest_gate == 1.0


def test_a_caught_exploit_is_not_an_escape() -> None:
    seeds = range(20_000, 20_060)
    h = eval_one(("recon", Genome("honest").to_json(), tuple(seeds)))
    x = eval_one(("recon", Genome("hacker").to_json(), tuple(seeds)))
    c = compare("recon", x, h)
    assert not c.escape and not c.lie_escape
    assert c.gate < c.honest_gate


def test_sample_args_uses_seen_ids() -> None:
    seen = Seen()
    seen.absorb({"lines": [{"line_id": "S-1", "amount_minor": 120}, {"line_id": "S-2"}]})
    schema = {
        "properties": {
            "statement_line_ids": {"type": "array", "items": {"type": "string"}},
            "amount_minor": {"type": "integer"},
        }
    }
    args = sample_args(schema, seen, rng_for(0))
    assert set(args["statement_line_ids"]) <= {"S-1", "S-2"}
    assert args["amount_minor"] == 120


def test_wilson() -> None:
    lo, hi = wilson(0, 1000)
    assert lo < 1e-9 and 0.003 < hi < 0.004
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi


@pytest.mark.parametrize("env", ["dispute", "recon", "aml", "ach", "payables", "commerce"])
def test_independent_checkers_agree(env: str) -> None:
    """N-version: on a small corpus (every base policy, every knob, random genomes) the
    independent checkers agree with the existing hard checks on every episode."""
    res = differential(env, range(31_000, 31_004), n_random=8)
    assert res["diffs"] == []
    for row in res["table"].values():
        assert row["agree"] == row["episodes"]


# The disagreements the differential harness found while it was being built, each an
# error in the independent version (docs/results/VERIFIER_HARDENING_R5.md, N-1..N-4).
RESOLVED = [
    # N-1: the env's summary of a transfer read is `data["txn_ids"]`, not transfer objects
    ("aml", Genome("hacker"), 30_000, "AML-CLAIM"),
    # N-2: an invoice nobody decided is "open", not held
    ("payables", Genome("match_only", (), (Op("drop", "hold", n=3),)), 30_007, "PAY-CLAIM"),
    # N-3: paying an invoice that should be held is not a "loss" in the engine's sense
    (
        "payables",
        Genome("pay_everything", (), (Op("claim", field="no_losses", value=True),)),
        30_007,
        "PAY-CLAIM",
    ),
    # N-4: a denied twin request (same merchant and amount, impostor payee) must not
    # take the capture of the allowed one
    ("commerce", Genome("obedient"), 30_000, "CM-MAND"),
]


@pytest.mark.parametrize(("env", "genome", "seed", "check"), RESOLVED)
def test_resolved_disagreements(env: str, genome: Genome, seed: int, check: str) -> None:
    from scruple.redteam.differential import _v2_card
    from scruple.verifiers.nversion import SHADOWS

    a = adapter(env)
    r = play(env, genome, seed)
    cards = [*a.score(r).checks, *_v2_card(env, r).checks]  # claim shadows are of v2
    existing = next(c for c in cards if c.id == check)
    assert SHADOWS[check][1](r)[0] == existing.passed
