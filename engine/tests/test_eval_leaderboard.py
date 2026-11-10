"""Leaderboard across (provider, model, strategy): pairing, warnings, CLI. Fake models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scruple.agents.providers import FakeModel, RunSettings
from scruple.agents.providers.fake import History, decide
from scruple.agents.store import EpisodeStore
from scruple.cli.main import main
from scruple.eval import EvalConfig, import_store, run_eval
from scruple.eval.leaderboard import build_leaderboard
from scruple.eval.scoring import paired_bootstrap, reference_rows

SEEDS = (5001, 5002, 5003, 5004, 5005, 5006)


def concede(hist: History) -> list[tuple[str, dict[str, Any]]]:
    """A worse fake: never represents."""
    calls = decide(hist)
    if calls[0][0] == "represent":
        return [("accept", {"reason": "always concede"})]
    return calls


def run(
    tmp: Path, name: str, *, provider: str = "anthropic", seeds: tuple[int, ...] = SEEDS, **kw: Any
) -> Path:
    out = tmp / name
    wire = {"gemini": "gemini-generate-content", "anthropic": "anthropic-messages"}.get(
        provider, "openai-chat"
    )
    fm = FakeModel(wire, **kw)
    run_eval(
        EvalConfig(
            provider=provider,
            model=name,
            strategy="zero-shot",
            seeds=seeds,
            transport="fake",
            concurrency=3,
        ),
        out,
        transport_factory=fm.transport,
    )
    return out


@pytest.fixture(scope="module")
def dirs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("lb")
    return {
        "good": run(tmp, "good"),
        "concede": run(tmp, "concede", provider="gemini", policy=concede),
        "half": run(tmp, "half", provider="openai", seeds=SEEDS[:3]),
    }


def test_entries_are_paired_on_their_own_seeds(dirs: dict[str, Path]) -> None:
    board = build_leaderboard(list(dirs.values()), n_boot=500)
    by = {e.system: e for e in board.entries}
    assert set(by) == {
        "anthropic/good/zero-shot",
        "gemini/concede/zero-shot",
        "openai/half/zero-shot",
    }
    refs = {r["seed"]: r for r in reference_rows(SEEDS) if r["system"] == "ref:evidence"}
    half = by["openai/half/zero-shot"]
    assert half.n == 3 and half.seeds == "5001-5003"
    rows = json.loads((dirs["half"] / "summary.json").read_text())["rows"]
    mine = {r["seed"]: r["cost_usd"] for r in rows if r["system"] == half.system}
    want = paired_bootstrap(mine, {s: refs[s]["cost_usd"] for s in mine}, 500)
    assert half.cost_vs_evidence == want
    # the fake evidence agent matches the evidence reference exactly
    assert by["anthropic/good/zero-shot"].cost_vs_evidence == (0.0, 0.0, 0.0)
    assert [r.system for r in board.references] == [
        "ref:evidence",
        "ref:always_accept",
        "ref:oracle",
    ]
    assert board.references[0].cost_vs_evidence is None
    assert board.seeds == "5001-5006"


def test_sorted_by_reward_then_cost(dirs: dict[str, Path]) -> None:
    board = build_leaderboard(list(dirs.values()), n_boot=200)
    keys = [(-e.mean_reward, e.mean_cost_usd) for e in board.entries]
    assert keys == sorted(keys)


def test_honest_warnings(dirs: dict[str, Path]) -> None:
    board = build_leaderboard(list(dirs.values()), n_boot=200)
    for e in board.entries:
        assert any("fake model" in w for w in e.warnings)
        assert any("small n" in w for w in e.warnings)
    assert any("different seeds" in w for w in board.warnings)
    assert any("not a ranking" in w for w in board.warnings)
    assert all(not r.warnings for r in board.references)
    md = board.markdown()
    assert "FAKE models only" in md
    assert "Synthetic cases" in md
    assert "order is not established" in md
    assert "Notes:" in md and "Warnings:" in md


def test_lapses_and_transient_failures_are_flagged(tmp_path: Path) -> None:
    def down(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    out = tmp_path / "down"
    run_eval(
        EvalConfig(
            provider="anthropic",
            model="down",
            strategy="zero-shot",
            seeds=(1, 2),
            transport="fake",
            settings=RunSettings(max_retries=0),
        ),
        out,
        transport_factory=lambda: httpx.MockTransport(down),
    )
    (e,) = build_leaderboard([out], n_boot=200).entries
    assert e.lapsed == 2 and e.stops == {"http_error": 2}
    assert any("lapsed" in w for w in e.warnings)
    assert any("resume the run" in w for w in e.warnings)


def test_cli_brief_and_api_runs_mix_with_a_warning(tmp_path: Path, dirs: dict[str, Path]) -> None:
    store = EpisodeStore(tmp_path / "store")
    eid = store.start(5001, None, "dispute").episode_id
    store.call(eid, "accept", {"reason": "x"})
    store.call(eid, "submit_decision", {"explanation": "accepted"})
    import_store(tmp_path / "store", tmp_path / "cli", model="m", strategy="zero-shot")
    board = build_leaderboard([dirs["good"], tmp_path / "cli"], n_boot=200)
    cli = next(e for e in board.entries if e.path == "cli-brief")
    assert cli.real_model and not any("fake model" in w for w in cli.warnings)
    assert any("different paths are mixed (api, cli-brief)" in w for w in board.warnings)
    assert "a mix of real and FAKE models" in board.markdown()


def test_one_system_over_several_directories(tmp_path: Path) -> None:
    a = run(tmp_path, "same", seeds=(1, 2))
    b_dir = tmp_path / "b"
    b_dir.mkdir()
    b = run(b_dir, "same", seeds=(3,))
    (e,) = build_leaderboard([a, b], n_boot=200).entries
    assert e.n == 3 and e.seeds == "1-3"
    with pytest.raises(ValueError, match="twice"):
        build_leaderboard([a, a], n_boot=200)


def test_bad_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no evaluation records"):
        build_leaderboard([tmp_path], n_boot=200)
    with pytest.raises(ValueError, match="no run directories"):
        build_leaderboard([], n_boot=200)
    with pytest.raises(ValueError, match="n_boot"):
        build_leaderboard([tmp_path], n_boot=10)


def test_cli_leaderboard(
    dirs: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "board"
    argv = ["eval", "leaderboard", *map(str, dirs.values()), "--out", str(out), "--n-boot", "300"]
    assert main(argv) == 0
    printed = capsys.readouterr().out
    assert printed == (out / "LEADERBOARD.md").read_text()
    data = json.loads((out / "leaderboard.json").read_text())
    assert data["synthetic_cases"] is True
    assert data["n_boot"] == 300
    assert len(data["entries"]) == 3 and len(data["references"]) == 3
