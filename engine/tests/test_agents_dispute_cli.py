"""`dispute` episodes through the terminal store, the CLI and the MCP sim server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.dispute_tools import DISPUTE_TOOL_NAMES
from scruple.agents.errors import EpisodeError
from scruple.agents.store import EpisodeStore
from scruple.cli.main import main
from scruple.mcp.sim_server import build_sim_server


def test_store_round_trips_a_dispute_episode(tmp_path: Path) -> None:
    store = EpisodeStore(tmp_path)
    ep = store.start(3, None, "dispute")
    eid = ep.episode_id
    store.call(eid, "list_evidence", {})
    with pytest.raises(EpisodeError):
        store.call(eid, "escalate", {})
    store.call(eid, "wait", {"days": 2})
    out = store.call(eid, "submit_decision", {"explanation": "done", "confidence": 0.2})
    assert out["episode_id"] == eid
    again = store.load(eid)
    assert again.closed and isinstance(again, DisputeEpisode)
    assert again.trace.head == out["trace_head"]


def test_store_rejects_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kind"):
        EpisodeStore(tmp_path).start(1, None, "nope")


def test_cli_dispute_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    st = ["--store", str(tmp_path)]
    assert main(["episode", "start", "--seed", "3", "--kind", "dispute", *st]) == 0
    eid = json.loads(capsys.readouterr().out)["episode_id"]
    assert main(["episode", "call", eid, "wait", '{"days": 1}', *st]) == 0
    capsys.readouterr()
    assert main(["episode", "submit", eid, '{"explanation": "idle"}', *st]) == 0
    score = json.loads(capsys.readouterr().out)
    assert score["resolution"] in ("lost", "won", "accepted", "partial")
    assert main(["episode", "show", eid, *st]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["kind"] == "dispute" and shown["closed"]
    assert main(["episode", "tools", "--kind", "dispute", *st]) == 0
    names = {t["name"] for t in json.loads(capsys.readouterr().out)}
    assert names == set(DISPUTE_TOOL_NAMES)


def _data(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content[0].text
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))


def test_mcp_plays_a_dispute_episode_like_in_process() -> None:
    async def fn(c: Client) -> list[dict[str, Any]]:
        fams = _data(await c.call_tool("list_case_families", {}))
        start = _data(await c.call_tool("start_dispute_episode", {"seed": 3, "family": None}))
        eid = start["episode_id"]
        out = [fams, start]
        script: list[tuple[str, dict[str, Any]]] = [
            ("get_case", {}),
            ("list_evidence", {}),
            ("request_info", {"question": "what happened?"}),
            ("upload_evidence", {"kind": "communication_log", "issued_on": "2026-04-15"}),
            ("wait", {"days": 3}),
            ("accept", {"reason": "conceding"}),
            ("submit_dispute", {"explanation": "accepted", "confidence": 0.1}),
        ]
        for tool, args in script:
            out.append(_data(await c.call_tool(tool, {"episode_id": eid, **args})))
        refused = await c.call_tool("refund", {"episode_id": eid, "amount_minor": 5})
        out.append({"refused": refused.is_error})
        return out

    res = anyio.run(lambda: _session(fn))
    fams, start, score, refused = res[0], res[1], res[-2], res[-1]
    assert "true_fraud" in fams["families"]
    assert set(start["observation"]["tools"]) == set(DISPUTE_TOOL_NAMES)
    assert score["synthetic"] is True and refused["refused"] is True
    local = DisputeEpisode(3)
    local.call("get_case", {})
    local.call("list_evidence", {})
    local.call("request_info", {"question": "what happened?"})
    local.call(
        "upload_evidence", {"kind": "communication_log", "issued_on": "2026-04-15", "fields": {}}
    )
    local.call("wait", {"days": 3})
    local.call("accept", {"reason": "conceding"})
    mine = local.call("submit_decision", {"explanation": "accepted", "confidence": 0.1})
    assert mine["trace_head"] == score["trace_head"]
    assert mine["cost_usd_minor"] == score["cost_usd_minor"]


async def _session(fn: Any) -> Any:
    async with Client(build_sim_server()) as c:
        return await fn(c)


def test_mcp_rejects_unknown_family() -> None:
    async def fn(c: Client) -> bool:
        r = await c.call_tool("start_dispute_episode", {"seed": 1, "family": "nope"})
        return bool(r.is_error)

    assert anyio.run(lambda: _session(fn))
