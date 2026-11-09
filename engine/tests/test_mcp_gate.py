"""The gate MCP server, driven through the SDK's in-memory client."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest
from mcp import Client

from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.mcp.gate_server import build_gate_server
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage


def make_gate() -> AgentGate:
    g = AgentGate(
        SQLiteStorage(":memory:"),
        SteppingClock(),
        GateConfig(n_cases=1_500, bootstrap_resamples=300),
    )
    g.bootstrap_baseline()
    return g


def session(fn: Callable[[Client], Awaitable[Any]], gate: AgentGate | None = None) -> Any:
    async def main() -> Any:
        async with Client(build_gate_server(gate or make_gate())) as c:
            return await fn(c)

    return anyio.run(main)


def data(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content[0].text
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))


def submit_args(rule_id: str, spec: Any, **extra: Any) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "spec": spec.model_dump(mode="json"),
        "owner": "payments-ops",
        "agent_id": "dispute-agent-01",
        "episode_ids": ["ep-1"],
        **extra,
    }


def test_tools_listed_and_no_approval_tool() -> None:
    async def fn(c: Client) -> set[str]:
        return {t.name for t in (await c.list_tools()).tools}

    names = session(fn)
    assert names == {
        "list_rules",
        "submit_rule",
        "evaluate_rule",
        "get_evaluation_status",
        "get_report",
        "verify_audit",
    }
    assert not any("approve" in n or "rollback" in n for n in names)


def test_good_rule_full_flow() -> None:
    gate = make_gate()

    async def fn(c: Client) -> list[dict[str, Any]]:
        sub = data(
            await c.call_tool(
                "submit_rule",
                submit_args(GOOD_RULE_ID, GOOD_RULE, claimed_metrics={"mean_regret": -999.0}),
            )
        )
        before = data(
            await c.call_tool("get_evaluation_status", {"rule_id": GOOD_RULE_ID, "version": 1})
        )
        ev = data(await c.call_tool("evaluate_rule", {"rule_id": GOOD_RULE_ID, "version": 1}))
        after = data(
            await c.call_tool("get_evaluation_status", {"rule_id": GOOD_RULE_ID, "version": 1})
        )
        report = data(
            await c.call_tool("get_report", {"decision_id": ev["decisions"][0]["decision_id"]})
        )
        audit = data(await c.call_tool("verify_audit", {}))
        rules = data(await c.call_tool("list_rules", {}))
        return [sub, before, ev, after, report, audit, rules]

    sub, before, ev, after, report, audit, rules = session(fn, gate)
    assert sub["status"] == "candidate"
    assert before["decisions"] == []
    assert [d["outcome"] for d in ev["decisions"]] == ["promote", "canary_passed"]
    assert after["status"] == "canary"
    assert after["awaiting_human_approval"] is True
    assert report["report"].startswith("# Evidence report")
    assert audit["ok"] is True
    assert any(r["rule_id"] == GOOD_RULE_ID for r in rules["rules"])
    # The MCP path wrote through AgentGate: the same state is visible directly.
    assert gate.registry.get(GOOD_RULE_ID, 1).status.value == "canary"


def test_bad_rule_blocked() -> None:
    async def fn(c: Client) -> dict[str, Any]:
        await c.call_tool("submit_rule", submit_args(BAD_RULE_ID, BAD_RULE))
        return data(await c.call_tool("evaluate_rule", {"rule_id": BAD_RULE_ID, "version": 1}))

    ev = session(fn)
    assert ev["status"] == "rejected"
    assert ev["decisions"][0]["outcome"] == "block"


@pytest.mark.parametrize(
    ("tool", "args", "needle"),
    [
        ("get_report", {"decision_id": "dec-99999"}, "unknown decision"),
        ("get_evaluation_status", {"rule_id": "nope-rule", "version": 1}, "nope-rule"),
        ("evaluate_rule", {"rule_id": "nope-rule", "version": 1}, "nope-rule"),
        (
            "submit_rule",
            {
                "rule_id": "x-rule",
                "spec": {"description": "d", "conditions": [], "action": "accept", "priority": 1},
                "owner": "o",
                "agent_id": "a",
            },
            "conditions",
        ),
        (
            "submit_rule",
            {
                "rule_id": "leaky-rule",
                "spec": {
                    "description": "peek at ground truth",
                    "conditions": [{"field": "claim_valid", "op": "eq", "value": False}],
                    "action": "contest",
                    "priority": 1,
                },
                "owner": "o",
                "agent_id": "a",
            },
            "field",
        ),
        ("submit_rule", {**submit_args("Bad ID!", GOOD_RULE)}, "rule_id"),
    ],
)
def test_bad_requests_are_tool_errors(tool: str, args: dict[str, Any], needle: str) -> None:
    async def fn(c: Client) -> Any:
        return await c.call_tool(tool, args)

    r = session(fn)
    assert r.is_error
    assert needle in r.content[0].text


def test_resubmitting_identical_rule_is_rejected() -> None:
    async def fn(c: Client) -> Any:
        await c.call_tool("submit_rule", submit_args(GOOD_RULE_ID, GOOD_RULE))
        return await c.call_tool("submit_rule", submit_args(GOOD_RULE_ID, GOOD_RULE))

    assert session(fn).is_error


def test_evaluating_twice_is_rejected() -> None:
    async def fn(c: Client) -> Any:
        await c.call_tool("submit_rule", submit_args(BAD_RULE_ID, BAD_RULE))
        await c.call_tool("evaluate_rule", {"rule_id": BAD_RULE_ID, "version": 1})
        return await c.call_tool("evaluate_rule", {"rule_id": BAD_RULE_ID, "version": 1})

    r = session(fn)
    assert r.is_error
    assert "not candidate" in r.content[0].text
