"""ach, recon and real-scenario episodes over the simulator MCP server.

Each test drives the server through the SDK's in-memory client. The reference
policies are run on a local episode first; the same call log replayed over MCP must
reach the same scorecard, which pins that the MCP surface is a faithful transport.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest
from mcp import Client

from scruple.agents.ach_baselines import reference_policy as ach_reference
from scruple.agents.ach_episode import AchEpisode
from scruple.agents.ach_tools import ACH_TOOL_NAMES
from scruple.agents.recon_baselines import reference_policy as recon_reference
from scruple.agents.recon_episode import ReconEpisode
from scruple.agents.recon_tools import RECON_TOOL_NAMES
from scruple.mcp.sim_server import EpisodeTable, build_sim_server
from scruple.realdata.situations import ScenarioEpisode
from scruple.sim.ach.models import AchFamily

Session = Callable[[Client], Awaitable[Any]]


def session(fn: Session, server: Any = None) -> Any:
    async def main() -> Any:
        async with Client(server or build_sim_server()) as c:
            return await fn(c)

    return anyio.run(main)


def data(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content[0].text
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))


class Recorder:
    """Wraps an episode and keeps every call, so a policy's moves can be replayed."""

    def __init__(self, ep: Any) -> None:
        self.ep = ep
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, args: Any = None) -> dict[str, Any]:
        # An MCP client always sends an object, so the local call gets one too.
        self.calls.append((name, dict(args or {})))
        out: dict[str, Any] = self.ep.call(name, dict(args or {}))
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self.ep, name)


def test_workflow_tools_are_listed_with_typed_schemas() -> None:
    async def fn(c: Client) -> dict[str, Any]:
        return {t.name: t for t in (await c.list_tools()).tools}

    tools = session(fn)
    for prefix, names in (("ach_", ACH_TOOL_NAMES), ("recon_", RECON_TOOL_NAMES)):
        for name in names:
            schema = tools[prefix + name].input_schema
            assert "episode_id" in schema["required"], name
    assert {"start_ach_episode", "start_recon_episode", "start_scenario_episode"} <= set(tools)
    match = tools["recon_match"].input_schema
    assert {"episode_id", "statement_line_ids", "ledger_line_ids"} <= set(match["required"])
    assert match["properties"]["statement_line_ids"]["type"] == "array"
    dishonor = tools["ach_dishonor"].input_schema["properties"]["code"]
    assert dishonor["enum"] == ["R67", "R68"]
    assert tools["ach_wait"].input_schema["properties"]["days"]["type"] == "integer"


def test_list_workflow_case_types() -> None:
    async def fn(c: Client) -> dict[str, Any]:
        return data(await c.call_tool("list_workflow_case_types", {}))

    out = session(fn)
    assert out["ach"]["families"] == [f.value for f in AchFamily]
    assert "camt053" in out["recon"]["formats"]
    assert out["scenario"]["scenario_ids"]
    assert "synthetic" in out["data"] and "CFPB" in out["data"]


@pytest.mark.parametrize("family", [f.value for f in AchFamily])
def test_ach_reference_policy_replays_over_mcp(family: str) -> None:
    local = Recorder(AchEpisode(3, family))
    ach_reference(local)  # type: ignore[arg-type]
    report = {"explanation": "reference policy, replayed over mcp"}
    want = local.call("submit_decision", report)

    async def fn(c: Client) -> dict[str, Any]:
        start = data(await c.call_tool("start_ach_episode", {"seed": 3, "case_type": family}))
        assert start["observation"]["kind"] == "ach"
        eid = start["episode_id"]
        assert eid == AchEpisode(3, family).episode_id
        last: dict[str, Any] = {}
        for tool, args in local.calls:
            last = data(await c.call_tool("ach_" + tool, {"episode_id": eid, **args}))
        return last

    assert session(fn) == want


@pytest.mark.parametrize("case_type", [None, "fee_line+camt053", "duplicate_bank+bai2@0.9"])
def test_recon_reference_policy_replays_over_mcp(case_type: str | None) -> None:
    local = Recorder(ReconEpisode(5, case_type))
    recon_reference(local)  # type: ignore[arg-type]
    want = local.call("submit_decision", {"explanation": "reference policy over mcp"})

    async def fn(c: Client) -> dict[str, Any]:
        args: dict[str, Any] = {"seed": 5}
        if case_type is not None:
            args["case_type"] = case_type
        eid = data(await c.call_tool("start_recon_episode", args))["episode_id"]
        last: dict[str, Any] = {}
        for tool, a in local.calls:
            last = data(await c.call_tool("recon_" + tool, {"episode_id": eid, **a}))
        return last

    assert session(fn) == want


def test_scenario_episode_plays_dispute_tools() -> None:
    table = EpisodeTable()

    async def fn(c: Client) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        ids = data(await c.call_tool("list_workflow_case_types", {}))["scenario"]["scenario_ids"]
        start = data(await c.call_tool("start_scenario_episode", {"scenario_id": ids[0]}))
        eid = start["episode_id"]
        case = data(await c.call_tool("get_case", {"episode_id": eid}))
        await c.call_tool("accept", {"episode_id": eid, "reason": "concede"})
        done = data(
            await c.call_tool("submit_dispute", {"episode_id": eid, "explanation": "conceded"})
        )
        return start, case, done

    start, case, done = session(fn, build_sim_server(table))
    ep = table.episodes[start["episode_id"]]
    assert isinstance(ep, ScenarioEpisode)
    assert ep.closed
    assert case and done


def test_unknown_scenario_and_bad_case_types_are_tool_errors() -> None:
    async def fn(c: Client) -> list[Any]:
        return [
            await c.call_tool("start_scenario_episode", {"scenario_id": "real-nope"}),
            await c.call_tool("start_ach_episode", {"seed": 1, "case_type": "nope"}),
            await c.call_tool("start_ach_episode", {"seed": 1, "case_type": "@2"}),
            await c.call_tool("start_recon_episode", {"seed": 1, "case_type": "nope"}),
        ]

    results = session(fn)
    assert all(r.is_error for r in results)
    assert "unknown ach family" in results[1].content[0].text
    assert "unknown recon" in results[3].content[0].text


def test_tools_refuse_episodes_of_another_kind() -> None:
    async def fn(c: Client) -> list[Any]:
        ach = data(await c.call_tool("start_ach_episode", {"seed": 2}))["episode_id"]
        recon = data(await c.call_tool("start_recon_episode", {"seed": 2}))["episode_id"]
        dispute = data(await c.call_tool("start_dispute_episode", {"seed": 2}))["episode_id"]
        return [
            await c.call_tool("accept", {"episode_id": ach}),
            await c.call_tool("get_case", {"episode_id": recon}),
            await c.call_tool("recon_get_task", {"episode_id": ach}),
            await c.call_tool("ach_get_case", {"episode_id": recon}),
            await c.call_tool("ach_accept", {"episode_id": dispute}),
            await c.call_tool("recon_get_task", {"episode_id": dispute}),
        ]

    results = session(fn)
    assert all(r.is_error for r in results)
    assert "use the ach_* tools" in results[0].content[0].text
    assert "use the recon_* tools" in results[1].content[0].text
    assert "is a dispute episode" in results[4].content[0].text


def test_refused_workflow_calls_count_as_tool_errors() -> None:
    table = EpisodeTable()

    async def fn(c: Client) -> tuple[str, list[Any]]:
        eid = data(await c.call_tool("start_recon_episode", {"seed": 4}))["episode_id"]
        bad = await c.call_tool(
            "recon_match",
            {"episode_id": eid, "statement_line_ids": ["S-999"], "ledger_line_ids": ["B-999"]},
        )
        empty = await c.call_tool(
            "recon_match", {"episode_id": eid, "statement_line_ids": [], "ledger_line_ids": []}
        )
        return eid, [bad, empty]

    eid, results = session(fn, build_sim_server(table))
    assert all(r.is_error for r in results)
    assert "invalid arguments for match" in results[1].content[0].text
    assert table.episodes[eid].tool_errors == 2


def test_closed_workflow_episode_refuses_more_calls() -> None:
    async def fn(c: Client) -> Any:
        eid = data(await c.call_tool("start_ach_episode", {"seed": 9}))["episode_id"]
        data(await c.call_tool("ach_submit_decision", {"episode_id": eid, "explanation": "x"}))
        return await c.call_tool("ach_get_case", {"episode_id": eid})

    result = session(fn)
    assert result.is_error and "closed" in result.content[0].text


def test_restarting_a_workflow_seed_gives_new_id_same_case() -> None:
    async def fn(c: Client) -> list[dict[str, Any]]:
        return [
            data(await c.call_tool("start_ach_episode", {"seed": 7, "case_type": "stop_payment"}))
            for _ in range(2)
        ]

    a, b = session(fn)
    assert a["episode_id"] != b["episode_id"]
    assert a["observation"]["case"] == b["observation"]["case"]
