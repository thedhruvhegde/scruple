"""`scruple episode` CLI and the file store behind it."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from scruple.agents import Episode, EpisodeError
from scruple.agents.store import EpisodeStore, UnknownEpisodeError
from scruple.cli.main import main


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_start_call_submit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = str(tmp_path)
    code, start = run(
        capsys, "episode", "start", "--seed", "1", "--case-type", "fraud", "--store", s
    )
    assert code == 0
    eid = start["episode_id"]
    assert set(start["observation"]["fields"]) >= {"three_ds", "reason_code"}
    assert (tmp_path / f"{eid}.json").exists()

    code, ev = run(capsys, "episode", "call", eid, "list_evidence", "--store", s)
    assert code == 0
    assert ev["evidence"][0]["evidence_id"] == "ev-01"
    code, _ = run(
        capsys, "episode", "call", eid, "represent", '{"evidence_ids": ["ev-01"]}', "--store", s
    )
    assert code == 0
    code, err = run(capsys, "episode", "call", eid, "refund", "{}", "--store", s)
    assert code == 1
    assert "already taken" in err["error"]

    code, score = run(
        capsys, "episode", "submit", eid, '{"explanation": "contested"}', "--store", s
    )
    assert code == 0
    assert score["action"] == "represent"
    assert score["case_type"] == "fraud"
    assert score["tool_errors"] == 1
    assert score["synthetic"] is True

    # Same seed and calls in one process give the same scorecard.
    ep = Episode(1, "fraud")
    ep.call("list_evidence")
    ep.call("represent", {"evidence_ids": ["ev-01"]})
    with contextlib.suppress(EpisodeError):
        ep.call("refund", {})
    local = ep.call("submit_decision", {"explanation": "contested"})
    assert {k: v for k, v in local.items() if k != "episode_id"} == {
        k: v for k, v in score.items() if k != "episode_id"
    }


def test_closed_episode_rejects_and_is_not_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = str(tmp_path)
    _, start = run(capsys, "episode", "start", "--seed", "2", "--store", s)
    eid = start["episode_id"]
    run(
        capsys, "episode", "submit", eid, '{"explanation": "x", "action": "escalate"}', "--store", s
    )
    log_before = EpisodeStore(s).log(eid)
    attempts = {
        "get_case": "{}",
        "accept_liability": "{}",
        "submit_decision": '{"explanation": "y"}',
    }
    for tool, raw in attempts.items():
        code, err = run(capsys, "episode", "call", eid, tool, raw, "--store", s)
        assert code == 1
        assert "closed" in err["error"]
    assert EpisodeStore(s).log(eid) == log_before


def test_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = str(tmp_path)
    _, start = run(capsys, "episode", "start", "--seed", "3", "--store", s)
    eid = start["episode_id"]
    run(capsys, "episode", "call", eid, "get_case", "--store", s)
    code, shown = run(capsys, "episode", "show", eid, "--store", s)
    assert code == 0
    assert shown["closed"] is False
    assert shown["calls"] == [{"tool": "get_case", "args": None}]
    run(capsys, "episode", "submit", eid, '{"explanation": "x", "action": "refund"}', "--store", s)
    _, shown = run(capsys, "episode", "show", eid, "--store", s)
    assert shown["closed"] is True
    assert shown["score"]["action"] == "refund"


def test_tools(capsys: pytest.CaptureFixture[str]) -> None:
    code, tools = run(capsys, "episode", "tools")
    assert code == 0
    assert tools[0]["name"] == "get_case"
    assert all("input_schema" in t for t in tools)


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"'])
def test_bad_json_args(raw: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = str(tmp_path)
    _, start = run(capsys, "episode", "start", "--seed", "4", "--store", s)
    code = main(["episode", "call", start["episode_id"], "get_evidence", raw, "--store", s])
    assert code == 2
    assert "error" in capsys.readouterr().err


def test_schema_invalid_args_are_tool_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = str(tmp_path)
    _, start = run(capsys, "episode", "start", "--seed", "4", "--store", s)
    code, err = run(
        capsys, "episode", "call", start["episode_id"], "get_evidence", '{"id": 1}', "--store", s
    )
    assert code == 1
    assert "invalid arguments" in err["error"]


@pytest.mark.parametrize("eid", ["ep-000000000000", "../../etc/passwd", "ep-ZZZ"])
def test_unknown_or_malformed_ids(
    eid: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = run(capsys, "episode", "call", eid, "get_case", "--store", str(tmp_path))
    assert code == 1
    assert "episode" in err["error"]
    code, err = run(capsys, "episode", "show", eid, "--store", str(tmp_path))
    assert code == 1


def test_unknown_case_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["episode", "start", "--seed", "1", "--case-type", "nope", "--store", str(tmp_path)])
        == 2
    )


def test_store_ids_are_unique_and_deterministic(tmp_path: Path) -> None:
    a = EpisodeStore(tmp_path / "a")
    b = EpisodeStore(tmp_path / "b")
    ids_a = [a.start(1, "fraud").episode_id for _ in range(3)]
    ids_b = [b.start(1, "fraud").episode_id for _ in range(3)]
    assert len(set(ids_a)) == 3
    assert ids_a == ids_b


def test_store_replay_matches_live(tmp_path: Path) -> None:
    store = EpisodeStore(tmp_path)
    ep = store.start(7, "partial_refund")
    store.call(ep.episode_id, "get_ledger_view", {})
    store.call(ep.episode_id, "accept_liability", {"reason": "valid"})
    view = store.call(ep.episode_id, "get_ledger_view", {})
    assert view["entries"][-1]["kind"] == "chargeback_debit"
    loaded = store.load(ep.episode_id)
    assert loaded.action is not None
    assert loaded.tool_calls == 3


def test_store_rejects_bad_ids(tmp_path: Path) -> None:
    with pytest.raises(UnknownEpisodeError):
        EpisodeStore(tmp_path).load("../x")


def test_default_store_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCRUPLE_EPISODE_STORE", str(tmp_path / "env-store"))
    assert EpisodeStore().root == tmp_path / "env-store"
