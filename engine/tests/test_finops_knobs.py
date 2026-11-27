"""Difficulty knobs in the case type: parsing, identity at the defaults, the monotone
coupling of `noise` and `adv`, the horizon, and the CLI/MCP paths carrying them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.agents.ach_episode import AchEpisode, parse_case_type
from scruple.agents.aml_episode import AmlEpisode
from scruple.agents.cli import cmd_episode
from scruple.agents.errors import EpisodeClosedError
from scruple.agents.payables_episode import PayablesEpisode
from scruple.agents.recon_episode import ReconEpisode
from scruple.agents.store import EpisodeStore
from scruple.finops.artefacts import aml_notes, recon_inbox
from scruple.finops.knobs import Knobs, parse_knobs, split_case_type, with_knobs
from scruple.sim.aml.generator import generate_aml_case
from scruple.sim.recon.generator import (
    INJECTION,
    MANGLES,
    generate_recon_case,
    mangle,
    normalise_reference,
)


def test_parse_and_format_round_trip() -> None:
    k = parse_knobs("fx@0.5;n=12;breaks=3;noise=0.25;adv=1;h=50", "recon")
    assert k == Knobs(n=12, breaks=3, noise=0.25, adv=1.0, h=50)
    assert k.suffix() == ";n=12;breaks=3;noise=0.25;adv=1;h=50"
    assert parse_knobs(with_knobs("fx@0.5", k), "recon") == k
    assert with_knobs(None, Knobs()) is None
    assert with_knobs("@0.2", Knobs()) == "@0.2"
    assert Knobs().is_default() and not k.is_default()
    assert split_case_type("fx@0.5;h=3") == ("fx@0.5", "h=3")
    assert split_case_type(";h=3") == (None, "h=3")
    assert split_case_type(None) == (None, "")
    assert parse_knobs(None) == Knobs()
    assert parse_knobs("@0.5;;h=2;", "ach") == Knobs(h=2)
    assert k.as_dict()["noise"] == 0.25


@pytest.mark.parametrize(
    ("text", "kind", "msg"),
    [
        ("@0.5;zzz=1", "recon", "unknown knob"),
        ("@0.5;n", "recon", "unknown knob"),
        ("@0.5;n=3", "aml", "does not apply"),
        ("@0.5;noise=2", "recon", "bad value"),
        ("@0.5;n=-1", "recon", "bad value"),
        ("@0.5;h=0", "ach", "bad value"),
        ("@0.5;h=abc", "ach", "bad value"),
        ("@0.5;h=2;h=3", "ach", "twice"),
    ],
)
def test_bad_knobs_are_refused(text: str, kind: str, msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        parse_knobs(text, kind)


def test_episode_case_type_parsers_ignore_knobs() -> None:
    assert parse_case_type("unauthorized@0.8;h=4") == ("unauthorized", 0.8)
    assert parse_case_type(";h=4") == (None, 0.5)


@pytest.mark.parametrize("seed", range(6))
def test_defaults_are_identical_to_the_generators(seed: int) -> None:
    assert ReconEpisode(seed, "@0.5").case == generate_recon_case(seed, 0.5)
    assert AmlEpisode(seed, "@0.5").case == generate_aml_case(seed, None, 0.5)
    assert ReconEpisode(seed, "@0.5;adv=0;noise=0").case == generate_recon_case(seed, 0.5)
    assert ReconEpisode(seed, "@0.5").inbox == ()
    assert AmlEpisode(seed, "@0.5").notes == ()


def test_n_and_breaks_set_the_counts() -> None:
    ep = ReconEpisode(3, "@0.5;n=40;breaks=9")
    assert len(ep.case.truth.clean) == 40
    assert len(ep.case.truth.breaks) == 9
    forced = ReconEpisode(3, "fx+fee_net@0.5;breaks=9")  # forced kinds win
    assert [b.kind.value for b in forced.case.truth.breaks] == ["fx", "fee_net"]


def test_mangle_and_normalise() -> None:
    for style in MANGLES:
        assert normalise_reference(mangle("RCV-12345A", style)) == "RCV12345A"
    assert normalise_reference(None) == ""
    with pytest.raises(ValueError):
        mangle("X", "nope")


@pytest.mark.parametrize("fmt", ["camt053", "bai2"])
def test_noisy_statements_round_trip_through_the_parsers(fmt: str) -> None:
    for seed in range(8):
        ep = ReconEpisode(seed, f"{fmt}@0.6;noise=1;adv=1")  # engine re-parses the file
        got = {x.line_id: x.reference for x in ep.engine.statement.lines}
        want = {x.line_id: x.reference for x in ep.case.bank_lines}
        assert got == want
        assert any(r != r.upper() or r.startswith("REF ") for r in got.values() if r)


def test_bad_generator_rates() -> None:
    with pytest.raises(ValueError):
        generate_recon_case(1, 0.5, ref_noise=1.5)


@settings(max_examples=25, deadline=None)
@given(st.integers(0, 10_000), st.floats(0, 1), st.floats(0, 1))
def test_noise_and_adv_couple_monotonically(seed: int, a: float, b: float) -> None:
    lo, hi = sorted((a, b))

    def mangled(x: float) -> set[str]:
        c = generate_recon_case(seed, 0.5, ref_noise=x, inject=x)
        base = generate_recon_case(seed, 0.5)
        return {
            n.line_id
            for n, o in zip(c.bank_lines, base.bank_lines, strict=True)
            if n.reference != o.reference or n.description != o.description
        }

    assert mangled(lo) <= mangled(hi)
    case = generate_recon_case(seed, 0.5)

    def planted(x: float) -> set[tuple[str, str]]:
        return {(m.kind, m.subject) for m in recon_inbox(case, Knobs(adv=x)) if m.adversarial}

    assert planted(lo) <= planted(hi)
    acase = generate_aml_case(seed, None, 0.5)
    kinds = {n.kind for n in aml_notes(acase, Knobs(adv=lo)) if n.adversarial}
    assert kinds <= {n.kind for n in aml_notes(acase, Knobs(adv=hi)) if n.adversarial}


def test_injection_lands_in_the_statement_text() -> None:
    ep = ReconEpisode(2, "@0.5;adv=1")
    texts = [x["description"] for x in ep.call("get_statement")["lines"]]
    assert INJECTION in texts


@pytest.mark.parametrize("cls", [ReconEpisode, AmlEpisode, AchEpisode, PayablesEpisode])
def test_horizon_lapses_the_episode(cls: type) -> None:
    ep = cls(4, "@0.5;h=2")
    first = ep.tool_names()[0]
    ep.call(first)
    ep.call(first)
    with pytest.raises(EpisodeClosedError, match="budget"):
        ep.call(first)
    assert ep.closed
    assert "lapsed:horizon" in ep.score.flags
    assert ep.record().lapsed


def test_submit_is_free_under_the_horizon() -> None:
    ep = ReconEpisode(4, "@0.5;h=1")
    ep.call("get_task")
    out = ep.call("submit_decision", {"explanation": "done"})
    assert out["episode_id"] == ep.episode_id
    assert not ep.record().lapsed


def test_replay_is_exact_with_knobs() -> None:
    ct = "@0.7;noise=0.5;adv=1;h=4"
    calls = [("get_task", None), ("get_inbox", None), ("get_statement", None)] * 3
    a = ReconEpisode.replay(3, ct, calls)
    b = ReconEpisode.replay(3, ct, calls)
    assert a.closed and b.closed
    assert a.trace.head == b.trace.head


def test_cli_carries_knobs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = str(tmp_path)
    ns = argparse.Namespace(
        episode_cmd="start", store=store, seed=5, case_type="@0.5;adv=1;h=3", kind="recon"
    )
    assert cmd_episode(ns) == 0
    eid = json.loads(capsys.readouterr().out)["episode_id"]
    ns = argparse.Namespace(
        episode_cmd="call", store=store, episode_id=eid, tool="get_inbox", json=None
    )
    assert cmd_episode(ns) == 0
    assert json.loads(capsys.readouterr().out)["messages"]
    ep = EpisodeStore(store).load(eid)
    assert isinstance(ep, ReconEpisode) and ep.max_calls == 3


def test_mcp_serves_knobs_and_the_new_tools() -> None:
    import anyio
    from mcp import Client

    from scruple.mcp.sim_server import EpisodeTable, build_sim_server

    def data(result: Any) -> dict[str, Any]:
        assert not result.is_error, result.content[0].text
        if result.structured_content is not None:
            return dict(result.structured_content)
        return dict(json.loads(result.content[0].text))

    async def main() -> None:
        async with Client(build_sim_server(EpisodeTable(keyed=False, full_scores=True))) as c:
            kinds = data(await c.call_tool("list_workflow_case_types", {}))
            assert kinds["knobs"]["by_kind"]["recon"] == ["adv", "breaks", "h", "n", "noise"]
            ep = data(
                await c.call_tool("start_recon_episode", {"seed": 2, "case_type": "@0.5;adv=1"})
            )
            eid = ep["episode_id"]
            inbox = data(await c.call_tool("recon_get_inbox", {"episode_id": eid}))
            assert inbox["messages"]
            ep = data(
                await c.call_tool("start_aml_episode", {"seed": 2, "case_type": "@0.5;adv=1"})
            )
            notes = data(await c.call_tool("aml_get_case_notes", {"episode_id": ep["episode_id"]}))
            assert notes["notes"]

    anyio.run(main)
