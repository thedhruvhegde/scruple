"""`payables` episodes on every surface: the CLI file store, MCP, and golden episodes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from scruple.agents.cli import cmd_episode
from scruple.agents.payables_baselines import reference_policy, run_payables_policy
from scruple.agents.payables_episode import TOKENS, PayablesEpisode
from scruple.agents.payables_tools import PAYABLES_TOOL_NAMES
from scruple.agents.store import KINDS, EpisodeStore
from scruple.mcp.sim_server import build_sim_server

GOLDEN = Path(__file__).parent / "golden_workflows"  # not tests/golden: that set is closed
GOLDEN_SEEDS = (1, 7, 2026)


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_kind_is_registered() -> None:
    assert "payables" in KINDS


def test_cli_plays_a_payables_episode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = str(tmp_path)
    start = _ns(store=store, episode_cmd="start", seed=3, case_type="clean@0", kind="payables")
    assert cmd_episode(start) == 0
    started = json.loads(capsys.readouterr().out)
    eid = started["episode_id"]
    assert started["observation"]["kind"] == "payables"
    first = started["observation"]["case"]["invoices"][0]["invoice_id"]
    pay = json.dumps({"invoice_id": first})
    assert (
        cmd_episode(_ns(store=store, episode_cmd="call", episode_id=eid, tool="pay", json=pay)) == 0
    )
    capsys.readouterr()
    assert (
        cmd_episode(_ns(store=store, episode_cmd="call", episode_id=eid, tool="pay", json=pay)) == 1
    )
    assert "error" in json.loads(capsys.readouterr().out)
    done = _ns(
        store=store, episode_cmd="submit", episode_id=eid, json='{"explanation": "paid one"}'
    )
    assert cmd_episode(done) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["dispositions"][first] == "paid"
    assert cmd_episode(_ns(store=store, episode_cmd="show", episode_id=eid)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["kind"] == "payables" and shown["closed"] and shown["action"] == "pay"
    assert cmd_episode(_ns(store=store, episode_cmd="tools", kind="payables")) == 0
    names = [t["name"] for t in json.loads(capsys.readouterr().out)]
    assert names == list(PAYABLES_TOOL_NAMES)


def test_store_reloads_a_payables_episode(tmp_path: Path) -> None:
    store = EpisodeStore(tmp_path)
    ep = store.start(8, "duplicate@0", "payables")
    assert isinstance(ep, PayablesEpisode)
    store.call(ep.episode_id, "pay", {"invoice_id": ep.case.invoices[0].invoice_id})
    again = store.load(ep.episode_id)
    assert isinstance(again, PayablesEpisode)
    want = PayablesEpisode.replay(
        8, "duplicate@0", [("pay", {"invoice_id": ep.case.invoices[0].invoice_id})]
    )
    assert again.trace.head == want.trace.head


class Recorder:
    def __init__(self, ep: PayablesEpisode) -> None:
        self.ep = ep
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, args: Any = None) -> dict[str, Any]:
        self.calls.append((name, dict(args or {})))
        out: dict[str, Any] = self.ep.call(name, dict(args or {}))
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self.ep, name)


def _data(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content[0].text
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))


@pytest.mark.parametrize(
    "case_type", [None, "duplicate+phantom_vendor@0.9", "new_vendor+short_receipt"]
)
def test_payables_reference_policy_replays_over_mcp(case_type: str | None) -> None:
    local = Recorder(PayablesEpisode(5, case_type))
    reference_policy(local)  # type: ignore[arg-type]
    want = local.call("submit_decision", {"explanation": "reference policy over mcp"})

    async def main() -> dict[str, Any]:
        async with Client(build_sim_server()) as c:
            kinds = _data(await c.call_tool("list_workflow_case_types", {}))
            assert kinds["payables"]["tokens"] == list(TOKENS)
            args: dict[str, Any] = {"seed": 5}
            if case_type is not None:
                args["case_type"] = case_type
            eid = _data(await c.call_tool("start_payables_episode", args))["episode_id"]
            wrong = await c.call_tool("aml_get_alert", {"episode_id": eid})
            assert wrong.is_error and "use the payables_* tools" in getattr(
                wrong.content[0], "text", ""
            )
            last: dict[str, Any] = {}
            for tool, a in local.calls:
                last = _data(await c.call_tool("payables_" + tool, {"episode_id": eid, **a}))
            return last

    assert anyio.run(main) == want


def golden_trace(seed: int) -> dict[str, Any]:
    ep = run_payables_policy(reference_policy, seed, "@0.7")
    rec = ep.record()
    assert ep.score is not None and ep.score.scorecard is not None
    return {
        "seed": seed,
        "synthetic": True,
        "kinds": {t.invoice_id: t.kind.value for t in rec.case.truth.invoices},
        "trace_head": rec.trace_head,
        "events": list(rec.events),
        "outcome": rec.outcome.model_dump(mode="json"),
        "scorecard": ep.score.scorecard.summary(),
    }


@pytest.mark.parametrize("seed", GOLDEN_SEEDS)
def test_golden_payables_episodes(seed: int) -> None:
    path = GOLDEN / f"payables_reference_seed{seed}.json"
    got = json.dumps(golden_trace(seed), sort_keys=True, indent=1) + "\n"
    if os.environ.get("SCRUPLE_REGEN_GOLDEN"):
        path.write_text(got)
    assert path.read_text() == got, "regenerate with SCRUPLE_REGEN_GOLDEN=1"


def test_the_cli_parser_knows_every_workflow_tool() -> None:
    from scruple.agents.aml_tools import AML_TOOL_NAMES
    from scruple.agents.cli import ALL_TOOL_NAMES

    assert set(PAYABLES_TOOL_NAMES) | set(AML_TOOL_NAMES) <= set(ALL_TOOL_NAMES)
