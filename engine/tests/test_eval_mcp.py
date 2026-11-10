"""The MCP path: an MCP client plays the sim server, logs its calls, and
`scruple eval import-calls` rebuilds and scores the same episodes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from scruple.agents.providers.fake import decide
from scruple.cli.main import main
from scruple.eval import import_calls
from scruple.eval.batch import mcp_call
from scruple.eval.records import load_records
from scruple.mcp.sim_server import build_sim_server

SEEDS = (5001, 5002, 5005)


def _data(result: Any) -> dict[str, Any]:
    assert not result.is_error, getattr(result.content[0], "text", "")
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(getattr(result.content[0], "text", "{}")))


async def _play(c: Client, seed: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """An agent over MCP: the fake evidence policy, logging every call it makes."""
    start = _data(await c.call_tool("start_dispute_episode", {"seed": seed}))
    eid = start["episode_id"]
    log: list[dict[str, Any]] = []
    hist: list[tuple[str, dict[str, Any]]] = []
    for _ in range(60):
        name, args = decide(hist)[0]
        mcp_name = "submit_dispute" if name == "submit_decision" else name
        mcp_args = {"episode_id": eid, **args}
        log.append({"tool": mcp_name, "args": mcp_args})
        res = await c.call_tool(mcp_name, mcp_args)
        out = {"error": str(getattr(res.content[0], "text", ""))} if res.is_error else _data(res)
        hist.append((name, out))
        if name == "submit_decision":
            return log, out
    raise AssertionError("did not finish")


@pytest.fixture(scope="module")
def played() -> list[tuple[int, list[dict[str, Any]], dict[str, Any]]]:
    async def main_() -> list[tuple[int, list[dict[str, Any]], dict[str, Any]]]:
        async with Client(build_sim_server()) as c:
            return [(s, *(await _play(c, s))) for s in SEEDS]

    return anyio.run(main_)


def _write_log(path: Path, played: Any) -> None:
    path.write_text("".join(json.dumps({"seed": s, "calls": log}) + "\n" for s, log, _ in played))


def test_import_calls_reproduces_the_server_scores(played: Any, tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    _write_log(log, played)
    summary = import_calls(log, tmp_path / "out", model="my-agent", strategy="zero-shot")
    recs = {r.seed: r for r in load_records(tmp_path / "out")}
    assert sorted(recs) == list(SEEDS)
    for seed, _log, server_score in played:
        rec = recs[seed]
        assert rec.stop == "submitted" and rec.path == "mcp" and rec.real_model
        assert rec.score["trace_head"] == server_score["trace_head"]
        assert rec.score["cost_usd_minor"] == server_score["cost_usd_minor"]
        assert rec.score["scorecard"]["gate"] == server_score["scorecard"]["gate"]
    assert summary["system"] == "mcp/my-agent/zero-shot"


def test_mcp_call_mapping() -> None:
    assert mcp_call("submit_dispute", {"episode_id": "e", "explanation": "x"}) == (
        "submit_decision",
        {"explanation": "x"},
    )
    assert mcp_call("wait", {"episode_id": "e", "days": 2}) == ("wait", {"days": 2})
    assert mcp_call("get_case", None) == ("get_case", None)


def test_unsubmitted_and_bad_logs(tmp_path: Path) -> None:
    log = tmp_path / "c.jsonl"
    log.write_text(
        json.dumps({"seed": 3, "calls": [{"tool": "get_case", "args": {}}]})
        + "\n\n"
        + json.dumps({"seed": 3, "calls": []})  # a second line for a seed is ignored
        + "\n"
    )
    import_calls(log, tmp_path / "o", model="m", strategy="react")
    (rec,) = load_records(tmp_path / "o")
    assert rec.stop == "not_submitted" and "lapsed" in rec.score["flags"]
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"seed": "x"}\n')
    with pytest.raises(ValueError, match="not a call-log line"):
        import_calls(bad, tmp_path / "b", model="m", strategy="react")
    neg = tmp_path / "neg.jsonl"
    neg.write_text('{"seed": -1, "calls": []}\n')
    with pytest.raises(ValueError, match="bad seed"):
        import_calls(neg, tmp_path / "n", model="m", strategy="react")
    empty = tmp_path / "e.jsonl"
    empty.write_text("\n")
    with pytest.raises(ValueError, match="no episodes"):
        import_calls(empty, tmp_path / "e", model="m", strategy="react")


def test_cli_import_calls(played: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "calls.jsonl"
    _write_log(log, played)
    argv = ["eval", "import-calls", "--calls", str(log), "--model", "agent-x",
            "--strategy", "checklist-first", "--out", str(tmp_path / "o")]  # fmt: skip
    assert main(argv) == 0
    assert "| mcp/agent-x/checklist-first | 3 |" in capsys.readouterr().out
