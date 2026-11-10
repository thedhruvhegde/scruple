"""The graded real-situation task: policies, episodes through the tools and the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, honest_report
from scruple.agents.errors import EpisodeError
from scruple.agents.store import KINDS, EpisodeStore
from scruple.cli.main import main
from scruple.realdata import situations as sit
from scruple.realdata.scenarios import RealScenario, load_real_scenarios

SCENARIOS = load_real_scenarios()
BY_ID = {s.id: s for s in SCENARIOS}


@pytest.fixture(scope="module")
def plays() -> list[sit.Play]:
    return sit.run_all(SCENARIOS)


def test_every_scenario_is_played_by_every_player(plays: list[sit.Play]) -> None:
    players = [sit.ANSWER_KEY, *sit.POLICIES]
    assert len(plays) == len(SCENARIOS) * len(players)
    for sid in BY_ID:
        assert sorted(p.player for p in plays if p.scenario_id == sid) == sorted(players)


def test_answer_key_is_always_right(plays: list[sit.Play]) -> None:
    assert all(p.right for p in plays if p.player == sit.ANSWER_KEY)


def test_run_is_deterministic(plays: list[sit.Play]) -> None:
    again = sit.run_all(SCENARIOS)
    assert [p.model_dump() for p in again] == [p.model_dump() for p in plays]


def test_pinned_policy_results(plays: list[sit.Play]) -> None:
    s = sit.summarise(plays, SCENARIOS)["players"]
    n = len(SCENARIOS)
    assert s[sit.ANSWER_KEY]["right"] == n
    # always-accept is wrong exactly where the answer is to contest (or the refund is moot)
    contest = sum(1 for x in SCENARIOS if x.scenario.expected.action == "represent")
    assert s["always_accept"]["right"] == n - contest
    # the oracle reads truth but concedes valid claims, so it misses the moot-refund cases
    moot = sum(
        1
        for x in SCENARIOS
        if x.scenario.case.refund_in_flight and x.scenario.expected.action == "represent"
    )
    assert s["oracle"]["right"] == n - moot
    assert s["evidence"]["right"] > s["always_accept"]["right"] > s["represent_all"]["right"]
    assert s["oracle"]["reference_run"] and not s["evidence"]["reference_run"]


def test_right_needs_correct_resolution_and_no_extra_fees(plays: list[sit.Play]) -> None:
    for p in plays:
        rs = BY_ID[p.scenario_id]
        if p.right:
            assert p.correct and p.resolution == p.expected_resolution
            assert p.fees_usd_minor <= sit.answer_key_fees(rs)
    # contesting a valid duplicate charge ends "accepted" with the cardholder paid, but is wrong
    gas = next(
        p for p in plays if p.player == "represent_all" and "gas-charged-twice" in p.scenario_id
    )
    assert gas.correct and gas.resolution == "accepted" and not gas.right


def test_summary_slices_and_matrix(plays: list[sit.Play]) -> None:
    s = sit.summarise(plays, SCENARIOS)
    assert set(s["matrix"]) == set(BY_ID)
    ev = s["players"]["evidence"]
    assert sum(v["n"] for k, v in ev["slices"].items() if k.startswith("regime:")) == len(SCENARIOS)
    lo, hi = ev["right_ci95"]
    assert lo <= ev["right_rate"] <= hi
    assert "face value" in s["labels"]["truth"]


@pytest.mark.parametrize("rs", SCENARIOS[:8], ids=[s.id for s in SCENARIOS[:8]])
def test_episode_starts_on_the_scenario_case(rs: RealScenario) -> None:
    ep = sit.ScenarioEpisode(0, rs.id)
    case = ep.call("get_case")["case"]
    c = rs.scenario.case
    assert (case["network"], case["reason_code"], case["amount_minor"]) == (
        c.network.value,
        c.reason_code,
        c.amount_minor,
    )
    obs = ep.observe()
    assert rs.grounding.complaint in obs.task
    # the agent never sees the family, the assumptions or the answer
    for hidden in (c.family.value, rs.scenario.expected.why, *rs.grounding.assumptions):
        assert hidden not in obs.task


def test_episode_needs_a_known_scenario() -> None:
    with pytest.raises(ValueError, match="scenario id"):
        sit.ScenarioEpisode(0, None)
    with pytest.raises(KeyError):
        sit.ScenarioEpisode(0, "real-nope")


def test_grade_episode_refuses_open_episodes() -> None:
    rs = SCENARIOS[0]
    ep = sit.ScenarioEpisode(0, rs.id)
    with pytest.raises(EpisodeError):
        sit.grade_episode(rs, ep, "x")


def test_store_replays_scenario_episodes(tmp_path: Path) -> None:
    assert "scenario" in KINDS
    store = EpisodeStore(tmp_path)
    rs = BY_ID["real-cc-15328101-gas-charged-twice"]
    ep = store.start(0, rs.id, "scenario")
    store.call(ep.episode_id, "get_case", None)
    store.call(ep.episode_id, "accept", {"reason": "duplicate"})
    store.call(ep.episode_id, "submit_decision", {"explanation": "true duplicate"})
    loaded = store.load(ep.episode_id)
    assert isinstance(loaded, sit.ScenarioEpisode) and loaded.closed
    play = sit.grade_episode(rs, loaded, "agent")
    assert play.right and play.first_action == "accept"


def test_prompts_do_not_leak_the_answer() -> None:
    rows = sit.prompt_rows(store="S")
    assert [r["id"] for r in rows] == [s.id for s in SCENARIOS]
    for r in rows:
        rs = BY_ID[r["id"]]
        assert r["task"] == sit.TASK_ID and r["kind"] == "scenario"
        assert f"--case-type {rs.id}" in r["prompt"]
        for hidden in (
            rs.scenario.case.family.value,
            rs.scenario.expected.why,
            *rs.grounding.assumptions,
        ):
            assert hidden not in r["prompt"]


def _play_cli(tmp_path: Path, sid: str, tool: str, args: dict[str, object]) -> str:
    store = str(tmp_path / "store")
    out = tmp_path / f"{sid}.json"
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert (
            main(
                [
                    "episode",
                    "start",
                    "--kind",
                    "scenario",
                    "--case-type",
                    sid,
                    "--seed",
                    "0",
                    "--store",
                    store,
                ]
            )
            == 0
        )
    eid = str(json.loads(buf.getvalue())["episode_id"])
    with contextlib.redirect_stdout(io.StringIO()):
        main(["episode", "call", eid, tool, json.dumps(args), "--store", store])
        assert main(["episode", "submit", eid, '{"explanation": "done"}', "--store", store]) == 0
    out.write_text(eid)
    return eid


def test_cli_prompt_play_grade(tmp_path: Path) -> None:
    store = str(tmp_path / "store")
    prompts = tmp_path / "p.jsonl"
    assert main(["realdata", "scenarios", "prompt", "--out", str(prompts), "--store", store]) == 0
    good = "real-cc-15328101-gas-charged-twice"
    bad = "real-2015-1676438-daughter-bought-tickets"
    e1 = _play_cli(tmp_path, good, "accept", {"reason": "duplicate"})
    e2 = _play_cli(tmp_path, bad, "accept", {"reason": "concede"})
    answers = tmp_path / "a.jsonl"
    answers.write_text(
        json.dumps({"id": good, "episode_id": e1})
        + "\n"
        + json.dumps({"id": bad, "episode_id": e2})
        + "\n"
    )
    card_path = tmp_path / "card.json"
    assert (
        main(
            [
                "realdata",
                "scenarios",
                "grade",
                "--answers",
                str(answers),
                "--prompts",
                str(prompts),
                "--store",
                store,
                "--out",
                str(card_path),
            ]
        )
        == 0
    )
    card = json.loads(card_path.read_text())
    assert card["agent"]["graded"] == 2 and card["agent"]["right"] == 1
    assert card["agent"]["prompted"] == len(SCENARIOS)
    assert (
        card["agent"]["failures"][
            SCENARIOS[0].id if SCENARIOS[0].id not in (good, bad) else SCENARIOS[1].id
        ]
        == "no answer"
    )
    # an episode id given for the wrong scenario does not count
    answers.write_text(json.dumps({"id": bad, "episode_id": e1}) + "\n")
    assert (
        main(
            [
                "realdata",
                "scenarios",
                "grade",
                "--answers",
                str(answers),
                "--prompts",
                str(prompts),
                "--store",
                store,
                "--out",
                str(card_path),
            ]
        )
        == 0
    )
    card = json.loads(card_path.read_text())
    assert card["agent"]["failures"][bad] == "episode is not this scenario"


def test_cli_check_and_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["realdata", "scenarios", "check"]) == 0
    assert "0 problems" in capsys.readouterr().out
    out = tmp_path / "run.json"
    assert main(["realdata", "scenarios", "run", "--out", str(out)]) == 0
    card = json.loads(out.read_text())
    assert card["players"]["answer_key"]["right"] == len(SCENARIOS)
    assert len(card["plays"]) == len(SCENARIOS) * (1 + len(sit.POLICIES))


def test_cli_grade_refuses_unprompted_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = tmp_path / "p.jsonl"
    prompts.write_text(json.dumps({"id": SCENARIOS[0].id}) + "\n")
    answers = tmp_path / "a.jsonl"
    answers.write_text(json.dumps({"id": "real-other", "episode_id": "ep-000000000000"}) + "\n")
    args = ["realdata", "scenarios", "grade", "--answers", str(answers), "--prompts", str(prompts)]
    assert main([*args, "--store", str(tmp_path)]) != 0
    assert "not prompted" in capsys.readouterr().err


def test_policy_helpers_play_through_tools() -> None:
    rs = BY_ID["real-2015-1676438-daughter-bought-tickets"]
    for pol, want in ((evidence_policy, True), (always_accept, False)):
        ep = sit.ScenarioEpisode(0, rs.id)
        pol(ep)
        if not ep.closed:
            ep.submit(honest_report(ep))
        assert sit.grade_episode(rs, ep, "p").right is want
        assert ep.tool_calls > 0
