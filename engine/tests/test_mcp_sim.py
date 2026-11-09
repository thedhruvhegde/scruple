"""The simulator MCP server, driven through the SDK's in-memory client."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest
from mcp import Client

from scruple.agents import Episode
from scruple.agents.tools import TOOL_NAMES
from scruple.mcp.sim_server import EpisodeTable, build_sim_server

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


def test_lists_episode_tools() -> None:
    async def fn(c: Client) -> list[Any]:
        return list((await c.list_tools()).tools)

    tools = {t.name: t for t in session(fn)}
    assert set(tools) == {*TOOL_NAMES, "start_episode", "list_case_types"}
    for name in TOOL_NAMES:
        assert "episode_id" in tools[name].input_schema["required"]
    assert tools["represent"].input_schema["properties"]["evidence_ids"]["type"] == "array"
    assert "seed" in tools["start_episode"].input_schema["required"]


def test_full_episode_over_mcp() -> None:
    async def fn(c: Client) -> list[dict[str, Any]]:
        start = data(await c.call_tool("start_episode", {"seed": 1, "case_type": "fraud"}))
        eid = start["episode_id"]
        out = [start]
        script: list[tuple[str, dict[str, Any]]] = [
            ("get_case", {}),
            ("list_evidence", {}),
            ("get_evidence", {"evidence_id": "ev-01"}),
            ("get_ledger_view", {}),
            ("request_info", {"question": "anything?"}),
            ("represent", {"evidence_ids": ["ev-01"]}),
            ("submit_decision", {"explanation": "authenticated, contested"}),
        ]
        for tool, args in script:
            out.append(data(await c.call_tool(tool, {"episode_id": eid, **args})))
        return out

    results = session(fn)
    start, score = results[0], results[-1]
    assert set(start["observation"]["fields"]) >= {"reason_code", "three_ds"}
    assert "fraud" not in start["episode_id"]
    assert score["case_type"] == "fraud"
    assert score["action"] == "represent"
    assert score["synthetic"] is True
    # Same seed and calls in-process give the same score.
    ep = Episode(1, "fraud")
    ep.call("represent", {"evidence_ids": ["ev-01"]})
    local = ep.call("submit_decision", {"explanation": "authenticated, contested"})
    assert local["cost_usd_minor"] == score["cost_usd_minor"]
    assert local["verifiers"] == score["verifiers"]


def test_actions_and_reads_after_submit_are_rejected() -> None:
    async def fn(c: Client) -> list[Any]:
        eid = data(await c.call_tool("start_episode", {"seed": 2}))["episode_id"]
        await c.call_tool(
            "submit_decision", {"episode_id": eid, "explanation": "x", "action": "escalate"}
        )
        return [
            await c.call_tool("refund", {"episode_id": eid}),
            await c.call_tool("get_case", {"episode_id": eid}),
            await c.call_tool("submit_decision", {"episode_id": eid, "explanation": "again"}),
        ]

    for r in session(fn):
        assert r.is_error
        assert "closed" in r.content[0].text


def test_second_action_rejected() -> None:
    async def fn(c: Client) -> Any:
        eid = data(await c.call_tool("start_episode", {"seed": 2}))["episode_id"]
        await c.call_tool("accept_liability", {"episode_id": eid})
        return await c.call_tool("represent", {"episode_id": eid, "evidence_ids": ["ev-01"]})

    r = session(fn)
    assert r.is_error
    assert "already taken" in r.content[0].text


@pytest.mark.parametrize(
    ("tool", "args", "needle"),
    [
        ("get_case", {"episode_id": "ep-nope"}, "unknown episode"),
        ("start_episode", {"seed": 1, "case_type": "bogus"}, "unknown case type"),
        ("start_episode", {"seed": "one"}, "seed"),
        ("represent", {"episode_id": "X", "evidence_ids": "ev-01"}, "evidence_ids"),
        ("submit_decision", {"episode_id": "X", "explanation": "x", "action": "steal"}, "action"),
    ],
)
def test_bad_calls_are_tool_errors(tool: str, args: dict[str, Any], needle: str) -> None:
    async def fn(c: Client) -> Any:
        eid = data(await c.call_tool("start_episode", {"seed": 3}))["episode_id"]
        real = {k: (eid if v == "X" else v) for k, v in args.items()}
        return await c.call_tool(tool, real)

    r = session(fn)
    assert r.is_error
    assert needle in r.content[0].text


def test_episode_errors_pass_through() -> None:
    async def fn(c: Client) -> list[Any]:
        eid = data(await c.call_tool("start_episode", {"seed": 3}))["episode_id"]
        return [
            await c.call_tool("get_evidence", {"episode_id": eid, "evidence_id": "ev-99"}),
            await c.call_tool("submit_decision", {"episode_id": eid, "explanation": "x"}),
            await c.call_tool("represent", {"episode_id": eid, "evidence_ids": []}),
        ]

    missing, no_action, empty = session(fn)
    assert "unknown evidence" in missing.content[0].text
    assert "take an action" in no_action.content[0].text
    assert empty.is_error


def test_restarting_a_seed_gives_a_new_id_and_same_case() -> None:
    async def fn(c: Client) -> list[dict[str, Any]]:
        return [
            data(await c.call_tool("start_episode", {"seed": 4, "case_type": "high_value"}))
            for _ in range(2)
        ]

    a, b = session(fn)
    assert a["episode_id"] != b["episode_id"]
    assert a["observation"]["fields"] == b["observation"]["fields"]


def test_servers_are_deterministic() -> None:
    async def fn(c: Client) -> dict[str, Any]:
        eid = data(await c.call_tool("start_episode", {"seed": 9}))["episode_id"]
        await c.call_tool("refund", {"episode_id": eid, "reason": "goodwill"})
        return data(await c.call_tool("submit_decision", {"episode_id": eid, "explanation": "x"}))

    assert session(fn) == session(fn)


def test_list_case_types() -> None:
    async def fn(c: Client) -> dict[str, Any]:
        return data(await c.call_tool("list_case_types", {}))

    out = session(fn)
    assert "duplicate_dispute" in out["case_types"]
    assert out["synthetic"] is True


def test_episode_table_is_shared_with_caller() -> None:
    table = EpisodeTable()

    async def fn(c: Client) -> str:
        return str(data(await c.call_tool("start_episode", {"seed": 5}))["episode_id"])

    eid = session(fn, build_sim_server(table))
    assert eid in table.episodes
