"""Claude Agent SDK adapter, exercised in-process. No CLI, no network."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest

from scruple.agents import Episode
from scruple.agents.adapters import claude_sdk
from scruple.agents.tools import TOOL_NAMES

pytest.importorskip("claude_agent_sdk")
from mcp import Client


def server_instance(ep: Episode) -> Any:
    return claude_sdk.episode_server(ep)["instance"]


def test_available() -> None:
    assert claude_sdk.available()


def test_options_lock_down_built_in_tools() -> None:
    opts = claude_sdk.agent_options(Episode(1, "fraud"), model="claude-haiku-5-5", max_turns=5)
    assert opts.tools == []
    assert opts.allowed_tools == [f"mcp__scruple__{n}" for n in TOOL_NAMES]
    assert set(opts.mcp_servers) == {"scruple"}  # type: ignore[arg-type]
    assert opts.max_turns == 5
    assert opts.model == "claude-haiku-5-5"
    assert opts.system_prompt and "synthetic" in str(opts.system_prompt)


def test_server_lists_every_tool_with_schema() -> None:
    async def main() -> list[Any]:
        async with Client(server_instance(Episode(1, "fraud"))) as c:
            return list((await c.list_tools()).tools)

    tools = anyio.run(main)
    assert [t.name for t in tools] == list(TOOL_NAMES)
    by_name = {t.name: t for t in tools}
    assert "evidence_ids" in by_name["represent"].input_schema["properties"]


def test_episode_played_through_the_sdk_server() -> None:
    ep = Episode(1, "fraud")

    async def main() -> list[Any]:
        async with Client(server_instance(ep)) as c:
            return [
                await c.call_tool("list_evidence", {}),
                await c.call_tool("get_evidence", {"evidence_id": "ev-42"}),
                await c.call_tool("represent", {"evidence_ids": ["ev-01"]}),
                await c.call_tool("submit_decision", {"explanation": "contested"}),
                await c.call_tool("refund", {}),
            ]

    listed, missing, acted, submitted, late = anyio.run(main)
    assert not listed.is_error
    assert missing.is_error
    assert "unknown evidence" in missing.content[0].text
    assert json.loads(acted.content[0].text)["action"] == "represent"
    score = json.loads(submitted.content[0].text)
    assert score["synthetic"] is True
    assert late.is_error
    assert "closed" in late.content[0].text
    assert ep.closed


def test_schema_invalid_args_rejected_by_sdk() -> None:
    ep = Episode(1, "fraud")

    async def main() -> Any:
        async with Client(server_instance(ep)) as c:
            return await c.call_tool("represent", {"evidence_ids": []})

    assert anyio.run(main).is_error
    assert ep.action is None


def test_run_episode_with_fake_query() -> None:
    ep = Episode(3, "high_value")
    seen: dict[str, Any] = {}

    async def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        seen["prompt"] = prompt
        inst = options.mcp_servers["scruple"]["instance"]
        async with Client(inst) as c:
            await c.call_tool("escalate", {"reason": "over threshold"})
            yield "assistant turn"
            await c.call_tool("submit_decision", {"explanation": "escalated"})
            yield "result"
        yield "never reached"

    score = anyio.run(lambda: claude_sdk.run_episode(ep, query_fn=fake_query))
    assert "Observation" in seen["prompt"]
    assert score.outcome == "escalated"
    assert score.passed


def test_run_episode_lapses_when_model_never_submits() -> None:
    async def silent(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        yield "thinking"

    score = anyio.run(lambda: claude_sdk.run_episode(Episode(3, "fraud"), query_fn=silent))
    assert "lapsed:model_stopped" in score.flags
