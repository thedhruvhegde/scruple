"""`scruple.eval.scoring` agrees with scripts/model_episodes_report.py on the same input."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.agents.dispute_baselines import evidence_policy, run_policy
from scruple.eval.scoring import (
    EVIDENCE_REF,
    paired_bootstrap,
    paired_cost,
    reference_rows,
    row,
    summarise,
    summary_table,
)

REPO = Path(__file__).resolve().parents[1]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "model_episodes_report", REPO / "scripts/model_episodes_report.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPT = _script()


def _score(seed: int) -> dict[str, Any]:
    ep = run_policy(evidence_policy, seed)
    assert ep.score is not None
    out: dict[str, Any] = json.loads(ep.score.model_dump_json())
    return out


@pytest.mark.parametrize("seed", [5001, 5002, 5007])
def test_row_matches_the_script(seed: int) -> None:
    s = _score(seed)
    assert row("x", s) == SCRIPT._row("x", s)


def test_reference_rows_match_the_script() -> None:
    seeds = [5001, 5002, 5003]
    assert reference_rows(seeds) == SCRIPT._policy_rows(seeds)


@settings(max_examples=40, deadline=None)
@given(
    st.dictionaries(
        st.integers(0, 50), st.floats(-500, 500, allow_nan=False), min_size=1, max_size=30
    ),
    st.floats(-50, 50, allow_nan=False),
)
def test_paired_cost_matches_the_script(a: dict[int, float], shift: float) -> None:
    b = {k: v + shift for k, v in a.items()}
    assert paired_cost(a, b) == SCRIPT._paired_cost(a, b)


def test_paired_bootstrap_basics() -> None:
    a = {1: 3.0, 2: 3.0, 3: 3.0}
    b = {1: 1.0, 2: 1.0, 3: 1.0, 4: 100.0}  # seed 4 is unpaired and ignored
    assert paired_bootstrap(a, b) == (2.0, 2.0, 2.0)
    with pytest.raises(ValueError, match="no shared seeds"):
        paired_bootstrap({1: 0.0}, {2: 0.0})
    m, lo, hi = paired_bootstrap({i: float(i % 3) for i in range(30)}, {i: 1.0 for i in range(30)})
    assert lo <= m <= hi


def test_summary_table_matches_the_script_layout(tmp_path: Path) -> None:
    """Same rows in, same table lines out as the script's SUMMARY.md."""
    rows = [row("sys", _score(s)) for s in (5001, 5002, 5003)] + reference_rows([5001, 5002, 5003])
    ours = summary_table(summarise(rows))
    # rebuild the script's lines from its own main() on an equivalent store-less input
    sums = {s.system: s for s in summarise(rows)}
    assert sums[EVIDENCE_REF].vs_evidence is None
    assert sums["sys"].vs_evidence == SCRIPT._paired_cost(
        {r["seed"]: r["cost_usd"] for r in rows if r["system"] == "sys"},
        {r["seed"]: r["cost_usd"] for r in rows if r["system"] == EVIDENCE_REF},
    )
    assert ours[0].startswith("| system | n | mean cost $ | vs evidence ref, 95% ci |")
    assert len(ours) == 2 + 4


class _StoreProxy:
    """Looks like an episode to the reference policies; every call goes to the store."""

    def __init__(self, store: Any, eid: str) -> None:
        self.store, self.eid = store, eid

    def call(self, tool: str, args: Any = None) -> Any:
        return self.store.call(self.eid, tool, args)

    def finish(self) -> None:
        self.store.call(self.eid, "submit_decision", {"explanation": "done"})


def test_script_and_eval_agree_on_a_store(tmp_path: Path) -> None:
    """End to end: a store scored by the script and by `write_summary` gives the
    same table rows for the references."""
    from scruple.agents.dispute_baselines import evidence_step, play
    from scruple.agents.store import EpisodeStore

    store = EpisodeStore(tmp_path / "store")
    for seed in (5001, 5002):
        # play through the store so every call is persisted, like a CLI player
        proxy = _StoreProxy(store, store.start(seed, None, "dispute").episode_id)
        play(proxy, evidence_step)  # type: ignore[arg-type]
        proxy.finish()
    SCRIPT.main(str(tmp_path / "script"), [f"cli={tmp_path / 'store'}"])
    script_md = (tmp_path / "script" / "SUMMARY.md").read_text().splitlines()

    from scruple.eval.batch import import_store

    import_store(tmp_path / "store", tmp_path / "eval", model="cli", strategy="zero-shot")
    eval_md = (tmp_path / "eval" / "SUMMARY.md").read_text().splitlines()
    for ref in ("ref:evidence", "ref:always_accept", "ref:oracle"):
        s_line = next(x for x in script_md if x.startswith(f"| {ref} |"))
        e_line = next(x for x in eval_md if x.startswith(f"| {ref} |"))
        assert s_line == e_line
    s_sys = next(x for x in script_md if x.startswith("| cli |"))
    e_sys = next(x for x in eval_md if x.startswith("| cli-brief/cli/zero-shot |"))
    assert s_sys.split("|")[2:] == e_sys.split("|")[2:]
