"""The frozen benchmark v1 (results/bench/v1/manifest.json) stays what was published.

Without the salt only the published file is checked: its hash, the generator mix it
was built with, and the reference scores. With `SCRUPLE_HELDOUT_SALT` set to the v1
salt, the first block is regenerated and scored (the full check is `scruple bench
verify`, also run by the slow test).
"""

from __future__ import annotations

import os

import pytest

from scruple.agents.env import DISPUTE_MIX
from scruple.bench.cli import DEFAULT_BENCH
from scruple.bench.core import REFERENCE_AGENTS, BenchFile, score_agent, verify
from scruple.bench.spec import DEFAULT_CASES, INDEX_STRIDE
from scruple.hashing import content_hash
from scruple.sim.splits import SeedSpace

V1_HASH = "1286baf3be84b4f9aa7af0606f4434897cf97db4a11c3db548d9549d2cb13f91"


def v1() -> BenchFile:
    return BenchFile.load(DEFAULT_BENCH)


def v1_space() -> SeedSpace | None:
    salt = os.environ.get("SCRUPLE_HELDOUT_SALT")
    if not salt:
        return None
    space = SeedSpace(salt)
    return space if space.salt_commitment() == v1().manifest.salt_commitment else None


def test_v1_is_frozen() -> None:
    f = v1()
    m = f.manifest
    assert f.manifest_hash == V1_HASH == m.manifest_hash
    assert (m.version, m.n_cases, m.block, m.index_stride) == (1, DEFAULT_CASES, 500, INDEX_STRIDE)
    assert m.mix_hash == content_hash(DISPUTE_MIX), "the episode mix changed: build v2"
    assert len(m.block_digests) == m.n_cases // m.block
    assert sum(m.mix.families.values()) == sum(m.mix.tiers.values()) == m.n_cases
    assert sum(m.mix.stages.values()) == sum(m.mix.networks.values()) == m.n_cases
    assert m.synthetic


def test_v1_reference_scores() -> None:
    ref = v1().reference
    assert tuple(ref) == REFERENCE_AGENTS
    for s in ref.values():
        assert s.n_cases == DEFAULT_CASES and s.synthetic
        for e in (s.reward, s.gate, s.cost, s.correct, s.win):
            assert e.low <= e.mean <= e.high and e.n == DEFAULT_CASES
        assert sum(g.n for g in s.by_family.values()) == DEFAULT_CASES
        assert s.batch_blocks == DEFAULT_CASES // 500
    ev, acc = ref["evidence"], ref["always_accept"]
    assert ev.regret.mean == 0.0
    assert ev.cost.high < acc.cost.low  # the evidence policy is cheaper, beyond the CIs
    assert acc.win.mean == 0.0


def test_v1_first_block_regenerates() -> None:
    space = v1_space()
    if space is None:
        pytest.skip("SCRUPLE_HELDOUT_SALT is not the v1 salt")
    f = v1()
    run, _ = score_agent(f, "always_accept", space, limit=1)
    assert run.prefix and run.score.n_cases == f.manifest.block


@pytest.mark.slow
def test_v1_verifies() -> None:
    space = v1_space()
    if space is None:
        pytest.skip("SCRUPLE_HELDOUT_SALT is not the v1 salt")
    assert verify(v1(), space, jobs=4) == []
