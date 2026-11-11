"""The full formal run (`make formal`): every model to its default depth. Slow."""

from __future__ import annotations

import pytest

from scruple.formal.run import DEFAULT_DEPTH, run


@pytest.mark.slow
def test_every_invariant_holds_to_the_default_depth_and_is_inductive() -> None:
    results = run()
    assert {m["model"] for m in results} == set(DEFAULT_DEPTH)
    for m in results:
        assert m["depth"] == DEFAULT_DEPTH[m["model"]]
        for r in m["invariants"]:
            assert r["holds_to_depth"], (m["model"], r["name"], r["counterexample"])
            assert r["inductive"], (m["model"], r["name"])
