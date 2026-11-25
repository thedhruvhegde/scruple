"""`commerce` episodes: the simulator, the Guard in the loop, the scorecard, the stores,
the CLI and the MCP server, and the exploits against it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scruple.agents.commerce_baselines import careful_policy, obedient_policy, run_commerce
from scruple.agents.commerce_episode import CommerceEpisode, parse_commerce_case_type
from scruple.agents.commerce_tools import COMMERCE_TOOL_NAMES
from scruple.agents.errors import EpisodeError
from scruple.agents.store import KINDS, EpisodeStore
from scruple.cli.main import main
from scruple.guard.evidence import verify_chain
from scruple.sim.commerce import Scenario, generate_commerce_case
from scruple.sim.commerce.engine import CommerceEngine
from scruple.storage.episodes import EPISODE_KINDS
from scruple.verifiers.commerce import CM_CHECKS, score_commerce
from scruple.verifiers.commerce_exploits import (
    COMMERCE_EXPLOITS,
    commerce_coverage,
    run_commerce_exploit,
)

SEEDS = range(40)


def test_kind_is_registered_everywhere() -> None:
    assert "commerce" in KINDS and "commerce" in EPISODE_KINDS


def test_generator_is_deterministic_and_signed() -> None:
    a, b = generate_commerce_case(5), generate_commerce_case(5)
    assert a == b
    assert a.mandate.signature
    assert a != generate_commerce_case(6)
    for s in range(60):
        c = generate_commerce_case(s)
        assert c.truth.expected_quantity <= c.task.quantity
        assert c.truth.best_cost_minor <= c.task.budget_minor


@pytest.mark.parametrize("scenario", [s.value for s in Scenario])
def test_every_scenario_can_be_requested(scenario: str) -> None:
    c = generate_commerce_case(11, [scenario])
    assert Scenario(scenario) in c.truth.scenarios


def test_case_type_parsing() -> None:
    assert parse_commerce_case_type(None) == (None, True)
    assert parse_commerce_case_type("injection+noguard") == (("injection",), False)
    assert parse_commerce_case_type("noguard") == (None, False)
    with pytest.raises(ValueError, match="unknown commerce token"):
        parse_commerce_case_type("bogus")


def test_careful_policy_passes_every_check_on_the_seed_mix() -> None:
    for s in range(120):
        ep = run_commerce(careful_policy, s)
        assert ep.score is not None
        card = ep.score.scorecard
        assert card is not None
        assert card.gate, (s, card.failed)
        assert [c.id for c in card.checks] == [c.id for c in CM_CHECKS]
        assert ep.score.task_units == ep.score.expected_units


def test_guard_blocks_what_an_obedient_agent_would_pay() -> None:
    lost_guard = lost_off = 0
    for s in SEEDS:
        on = run_commerce(obedient_policy, s, "injection+beneficiary_change")
        off = run_commerce(obedient_policy, s, "injection+beneficiary_change+noguard")
        assert on.score.paid_to_unknown_payees_minor == 0  # type: ignore[union-attr]
        assert on.score.gate  # type: ignore[union-attr]
        assert not off.score.gate  # type: ignore[union-attr]
        assert "CM-MAND" in off.score.scorecard.hard_failures  # type: ignore[union-attr]
        lost_guard += on.score.paid_to_unknown_payees_minor  # type: ignore[union-attr]
        lost_off += off.record().outcome.gross_captured_minor
    assert lost_guard == 0 and lost_off > 0


def test_step_up_goes_to_the_user_and_impostor_payee_is_declined() -> None:
    ep = CommerceEpisode(3, "beneficiary_change")
    msg = ep.call("get_messages")["messages"][0]
    victim = msg["claims_to_be"]
    offer = next(
        o
        for o in ep.call("list_offers")["offers"]
        if o["merchant_id"] == victim and o["category"] == ep.case.task.category
    )
    out = ep.call(
        "checkout",
        {"offer_id": offer["offer_id"], "quantity": 1, "payee_account": msg["new_payee"]},
    )
    assert out["verdict"] == "step_up"
    assert any(r["rule"] == "G-BEN" for r in out["reasons"])
    ans = ep.call("request_approval", {"request_id": out["request_id"]})
    assert ans["approved"] is False and ans["verdict"] == "deny"
    assert ans["shown_to_user"]["payee_account"] == msg["new_payee"]  # the Guard renders it
    with pytest.raises(EpisodeError, match="not_pending"):
        ep.call("request_approval", {"request_id": out["request_id"]})


def test_duplicate_charge_is_visible_and_recoverable() -> None:
    ep = run_commerce(careful_policy, 4, "duplicate_charge")
    r = ep.record()
    assert r.outcome.duplicates_posted_minor > 0
    assert r.outcome.duplicates_recovered_minor == r.outcome.duplicates_posted_minor
    assert score_commerce(r).gate


def test_replay_and_evidence_chain() -> None:
    ep = run_commerce(careful_policy, 9, "injection+duplicate_charge")
    r = ep.record()
    assert verify_chain(r.evidence) is None
    eng = CommerceEngine.replay(r.case, CommerceEngine.events_from_json(json.dumps(list(r.events))))
    assert eng.journal == r.journal and eng.outcome() == r.outcome
    again = CommerceEpisode.replay(9, "injection+duplicate_charge", [])
    assert again.case == ep.case


def test_unknown_tool_is_recorded_and_flagged() -> None:
    ep = CommerceEpisode(2)
    with pytest.raises(EpisodeError):
        ep.call("pay_direct", {"amount_minor": 1})
    careful_policy(ep)
    from scruple.agents.commerce_baselines import wallet_report

    score = ep.submit(wallet_report(ep))
    assert "CM-SCOPE" in score.scorecard.hard_failures


def test_episode_store_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = EpisodeStore(tmp_path)
    ep = store.start(7, "duplicate_charge", "commerce")
    store.call(ep.episode_id, "get_task", None)
    loaded = store.load(ep.episode_id)
    again = store.load(ep.episode_id)
    assert isinstance(loaded, CommerceEpisode) and isinstance(again, CommerceEpisode)
    assert loaded.trace.head == again.trace.head
    assert (
        main(["episode", "start", "--kind", "commerce", "--seed", "3", "--store", str(tmp_path)])
        == 0
    )
    eid = json.loads(capsys.readouterr().out)["episode_id"]
    assert main(["episode", "call", eid, "list_offers", "--store", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["offers"]
    assert main(["episode", "tools", "--kind", "commerce"]) == 0
    names = {t["name"] for t in json.loads(capsys.readouterr().out)}
    assert names == set(COMMERCE_TOOL_NAMES)
    assert (
        main(
            [
                "episode",
                "submit",
                eid,
                json.dumps({"explanation": "did nothing"}),
                "--store",
                str(tmp_path),
            ]
        )
        == 0
    )
    score = json.loads(capsys.readouterr().out)
    assert score["scorecard"]["gate"] is True  # nothing bought, nothing claimed


def test_mcp_server_exposes_commerce() -> None:
    from scruple.mcp.sim_server import build_sim_server

    server = build_sim_server()
    import asyncio

    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert "start_commerce_episode" in tools
    assert {f"commerce_{n}" for n in COMMERCE_TOOL_NAMES} <= tools


@pytest.mark.parametrize("exploit", COMMERCE_EXPLOITS, ids=[e.id for e in COMMERCE_EXPLOITS])
def test_commerce_exploit_status(exploit: object) -> None:
    from scruple.verifiers.ach_exploits import WorkflowExploit

    assert isinstance(exploit, WorkflowExploit)
    seeds = range(150) if exploit.id == "XC6" else range(30)
    run = run_commerce_exploit(exploit, seeds)
    want = "not fooled" if exploit.id == "XC6" else "caught"
    assert run.status == want, (exploit.id, run.status, run.fired_on, run.exercised)
    if want == "caught":
        assert run.caught_rate == 1.0
        assert run.reward < run.honest_reward


def test_commerce_coverage_rows() -> None:
    rows = commerce_coverage(range(3))
    assert [r["id"] for r in rows] == [e.id for e in COMMERCE_EXPLOITS]
