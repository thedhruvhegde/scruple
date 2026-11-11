"""`aml` episodes on every surface: the CLI file store, MCP, and golden episodes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from scruple.agents.aml_baselines import reference_policy, run_aml_policy
from scruple.agents.aml_episode import AmlEpisode
from scruple.agents.aml_tools import AML_TOOL_NAMES
from scruple.agents.cli import cmd_episode
from scruple.agents.store import KINDS, EpisodeStore
from scruple.mcp.sim_server import build_sim_server
from scruple.sim.aml.models import AmlFamily

GOLDEN = Path(__file__).parent / "golden_workflows"  # not tests/golden: that set is closed
GOLDEN_SEEDS = (1, 7, 2026)


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_kind_is_registered() -> None:
    assert "aml" in KINDS


def test_unknown_family_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown aml family"):
        AmlEpisode(1, "nope")


def test_cli_plays_an_aml_episode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = str(tmp_path)
    assert (
        cmd_episode(
            _ns(store=store, episode_cmd="start", seed=3, case_type="payroll@0.2", kind="aml")
        )
        == 0
    )
    started = json.loads(capsys.readouterr().out)
    eid = started["episode_id"]
    assert started["observation"]["kind"] == "aml"
    for tool, raw in (("get_alert", None), ("close_alert", "{}")):
        assert (
            cmd_episode(_ns(store=store, episode_cmd="call", episode_id=eid, tool=tool, json=raw))
            == 0
        )
        capsys.readouterr()
    assert (
        cmd_episode(
            _ns(store=store, episode_cmd="call", episode_id=eid, tool="escalate", json="{}")
        )
        == 1
    )
    assert "error" in json.loads(capsys.readouterr().out)
    assert (
        cmd_episode(
            _ns(
                store=store, episode_cmd="submit", episode_id=eid, json='{"explanation": "payroll"}'
            )
        )
        == 0
    )
    card = json.loads(capsys.readouterr().out)
    assert card["family"] == "payroll" and card["decision"] == "close" and card["correct"]
    assert "AML-PROC" in card["scorecard"]["hard_failures"]  # it never read the transfers
    assert cmd_episode(_ns(store=store, episode_cmd="show", episode_id=eid)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["kind"] == "aml" and shown["closed"] and shown["action"] == "close_alert"
    assert len(shown["calls"]) == 4
    assert cmd_episode(_ns(store=store, episode_cmd="tools", kind="aml")) == 0
    assert [t["name"] for t in json.loads(capsys.readouterr().out)] == list(AML_TOOL_NAMES)


def test_store_reloads_an_aml_episode(tmp_path: Path) -> None:
    store = EpisodeStore(tmp_path)
    ep = store.start(8, "structuring@0", "aml")
    store.call(ep.episode_id, "request_info", {"question": "source of the cash?"})
    again = store.load(ep.episode_id)
    assert isinstance(again, AmlEpisode)
    want = AmlEpisode.replay(
        8, "structuring@0", [("request_info", {"question": "source of the cash?"})]
    )
    assert again.trace.head == want.trace.head


# --- MCP --------------------------------------------------------------------------------
class Recorder:
    def __init__(self, ep: AmlEpisode) -> None:
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


@pytest.mark.parametrize("family", [f.value for f in AmlFamily])
def test_aml_reference_policy_replays_over_mcp(family: str) -> None:
    local = Recorder(AmlEpisode(3, family))
    reference_policy(local)  # type: ignore[arg-type]
    want = local.call("submit_decision", {"explanation": "reference policy over mcp"})

    async def main() -> dict[str, Any]:
        async with Client(build_sim_server()) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert {"aml_" + n for n in AML_TOOL_NAMES} <= set(tools)
            sar = tools["aml_file_sar"].input_schema
            assert "episode_id" in sar["required"]
            kinds = _data(await c.call_tool("list_workflow_case_types", {}))
            assert kinds["aml"]["families"] == [f.value for f in AmlFamily]
            start = _data(await c.call_tool("start_aml_episode", {"seed": 3, "case_type": family}))
            eid = start["episode_id"]
            assert eid == AmlEpisode(3, family).episode_id
            wrong = await c.call_tool("get_case", {"episode_id": eid})
            assert wrong.is_error and "use the aml_* tools" in getattr(wrong.content[0], "text", "")
            last: dict[str, Any] = {}
            for tool, args in local.calls:
                last = _data(await c.call_tool("aml_" + tool, {"episode_id": eid, **args}))
            return last

    assert anyio.run(main) == want


# --- golden episodes --------------------------------------------------------------------
def golden_trace(seed: int) -> dict[str, Any]:
    ep = run_aml_policy(reference_policy, seed, "@0.7")
    rec = ep.record()
    assert ep.score is not None and ep.score.scorecard is not None
    return {
        "seed": seed,
        "synthetic": True,
        "family": rec.case.truth.family.value,
        "trace_head": rec.trace_head,
        "events": list(rec.events),
        "outcome": rec.outcome.model_dump(mode="json"),
        "scorecard": ep.score.scorecard.summary(),
    }


@pytest.mark.parametrize("seed", GOLDEN_SEEDS)
def test_golden_aml_episodes(seed: int) -> None:
    path = GOLDEN / f"aml_reference_seed{seed}.json"
    got = json.dumps(golden_trace(seed), sort_keys=True, indent=1) + "\n"
    if os.environ.get("SCRUPLE_REGEN_GOLDEN"):
        path.write_text(got)
    assert path.read_text() == got, "regenerate with SCRUPLE_REGEN_GOLDEN=1"
