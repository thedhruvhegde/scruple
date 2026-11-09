"""Golden episodes (roadmap 164): every workflow, fixed seeds, byte-for-byte.

Each trace in `tests/golden/episodes.py` is rebuilt and compared with its file in
tests/golden/. Any drift fails: a changed event, posting, fee, decision, audit
entry or hash. After an intended change run `uv run python -m tests.golden.regen`
and review the diff like code. Synthetic data only.
"""

from __future__ import annotations

import difflib
import json

import pytest

from tests.golden.episodes import TRACES, dumps
from tests.golden.regen import HERE, main, path_for


def _drift(name: str, want: str, got: str) -> str:
    diff = difflib.unified_diff(
        want.splitlines(), got.splitlines(), "golden", "now", n=1, lineterm=""
    )
    head = "\n".join(list(diff)[:40])
    return f"{name} drifted (regen: uv run python -m tests.golden.regen {name}):\n{head}"


@pytest.mark.parametrize("name", sorted(TRACES))
def test_golden_episode_is_byte_identical(name: str) -> None:
    path = path_for(name)
    assert path.exists(), f"missing {path.name}: run uv run python -m tests.golden.regen"
    want = path.read_text(encoding="utf-8")
    got = dumps(TRACES[name]())
    assert got == want, _drift(name, want, got)


def test_every_golden_file_has_a_trace() -> None:
    files = {p.stem for p in HERE.glob("*.json")}
    assert files == set(TRACES)


@pytest.mark.parametrize("name", sorted(TRACES))
def test_golden_files_are_canonical(name: str) -> None:
    text = path_for(name).read_text(encoding="utf-8")
    assert text == dumps(json.loads(text)), "hand-edited or non-canonical golden file"


def test_golden_episodes_cover_the_workflows() -> None:
    names = " ".join(TRACES)
    for needle in ("sim_batch", "disputes_allocation", "disputes_collaboration", "gate_"):
        assert needle in names
    promo = json.loads(path_for("gate_promotion_seed1").read_text())
    kinds = {e["event_type"] for e in promo["audit"]}
    assert {
        "rule.registered",
        "rule.status_changed",
        "canary.ramp_started",
        "canary.ramp_completed",
        "rule.activated",
        "rule.rolled_back",
    } <= kinds
    assert promo["integrity_ok"] is True
    for w in ("allocation", "collaboration"):
        d = json.loads(path_for(f"disputes_{w}_seed7").read_text())
        assert d["actions"] and all(o is not None for o in d["outcomes"].values())


def test_regen_refuses_unknown_names(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["no-such-episode"]) == 2
    assert "unknown golden episode" in capsys.readouterr().err
