"""`scruple suite`: env adapters, the plan (determinism, commitments, tampering), agents,
resumable runs, the release test and the report renderers."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

from scruple.suite.agents import HttpAgent, PythonAgent, ScriptedAgent, parse_agent
from scruple.suite.envs import ENV_NAMES, STRATEGY_ADDENDA, get_adapter, replay
from scruple.suite.plan import build_plan, parse_mix, verify_plan, write_plan
from scruple.suite.report import build_report, release_test, render_html, render_pdf
from scruple.suite.run import run_suite, select
from scruple.suite.runner import Item, ItemRecord, load_item_records, play_item
from scruple.suite.stats import boot_mean, paired_diff, wilson
from scruple.suite.taxonomy import category

# --- adapters ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ENV_NAMES)
def test_every_adapter_builds_scores_and_replays(env: str) -> None:
    a = get_adapter(env)
    ct = a.case_type_at(0.4, random.Random(1))
    refs = a.reference_outcomes(11, ct)
    assert a.honest_ref in refs
    honest = refs[a.honest_ref]
    assert honest.gate and honest.reward > 0.8
    # an episode never submitted is lapsed and scored, never dropped
    ep = replay(a, 11, ct, [])
    assert ep.closed
    out = a.outcome(ep)
    assert not out.submitted
    assert a.obs_hash(a.make(11, ct)) == a.obs_hash(a.make(11, ct))
    assert a.truth_hash(a.make(11, ct), "s1") != a.truth_hash(a.make(11, ct), "s2")


def test_system_prompts_carry_the_strategy_text_and_refuse_unknown_ones() -> None:
    a = get_adapter("aml")
    assert STRATEGY_ADDENDA["incentive-win-rate"] in a.system_prompt("incentive-win-rate")
    assert a.system_prompt("zero-shot").startswith("You are an operations agent")
    with pytest.raises(ValueError, match="no aml text"):
        a.system_prompt("react")
    assert "dispute" in get_adapter("dispute").system_prompt("react").lower()


def test_taxonomy_maps_every_check_family() -> None:
    assert category("SR-01") == "self_report"
    assert category("AML-CLAIM") == "self_report"
    assert category("CM-INJ") == "injection"
    assert category("PAY-OOB") == "injection"
    assert category("EVD-01") == "fabrication"
    assert category("REC-INT") == "integrity"
    assert category("PROC-01") == "process"
    assert category("ACH-SCOPE") == "scope"
    assert category("OUT-01") == "outcome"
    assert category("weird") == "other"


def test_stats_helpers() -> None:
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0.25 < hi < 0.35
    assert wilson(10, 10)[1] == 1.0
    m, lo, hi = boot_mean([1.0, 0.0, 1.0, 1.0])
    assert m == 0.75 and lo <= m <= hi
    d, lo, hi = paired_diff([1.0, 1.0], [0.0, 0.0])
    assert (d, lo, hi) == (1.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        paired_diff([1.0], [])


# --- the plan ---------------------------------------------------------------------------


def test_plan_is_deterministic_and_commitments_depend_only_on_the_salt() -> None:
    p1, _ = build_plan(ENV_NAMES, 60, seed=5, salt="a")
    p2, _ = build_plan(ENV_NAMES, 60, seed=5, salt="a")
    p3, _ = build_plan(ENV_NAMES, 60, seed=5, salt="b")
    p4, _ = build_plan(ENV_NAMES, 60, seed=6, salt="a")
    assert p1["plan_hash"] == p2["plan_hash"]
    assert p1["bank_hash"] == p3["bank_hash"]  # the bank does not depend on the salt
    assert p1["commitments_hash"] != p3["commitments_hash"]
    assert p1["bank_hash"] != p4["bank_hash"]
    ids = [s["scenario_id"] for s in p1["scenarios"]]
    assert len(set(ids)) == 60
    assert sorted(s["index"] for s in p1["scenarios"]) == list(range(60))
    per_env = {e: sum(s["env"] == e for s in p1["scenarios"]) for e in ENV_NAMES}
    assert set(per_env.values()) == {10}
    bands = {
        b: sum(s["band"] == b for s in p1["scenarios"] if s["env"] == "aml")
        for b in ("easy", "medium", "hard")
    }
    assert bands == {"easy": 3, "medium": 4, "hard": 3}
    assert "truth" not in json.dumps(p1["scenarios"][0]).replace("truth_commitment", "")


def test_plan_workers_do_not_change_the_bank() -> None:
    a, _ = build_plan(("aml", "ach"), 12, seed=1, salt="x", workers=1)
    b, _ = build_plan(("aml", "ach"), 12, seed=1, salt="x", workers=2)
    assert a["plan_hash"] == b["plan_hash"]


def test_verify_catches_tampering_and_a_wrong_salt() -> None:
    p, salt = build_plan(("aml", "payables", "dispute"), 9, seed=2, salt="s3cret")
    assert verify_plan(p, salt=salt) == []
    assert verify_plan(p, salt="wrong")[0].startswith("the salt does not match")
    bad = json.loads(json.dumps(p))
    bad["scenarios"][0]["seed"] += 1
    probs = verify_plan(bad)
    assert any("plan_hash" in x for x in probs) and any("obs_hash" in x for x in probs)
    bad2 = json.loads(json.dumps(p))
    bad2["scenarios"][1]["truth_commitment"] = "0" * 64
    assert any("truth commitment" in x for x in verify_plan(bad2, salt=salt))


def test_mix_and_caps_validate() -> None:
    assert parse_mix("easy:1,hard:1") == {"easy": 0.5, "hard": 0.5}
    with pytest.raises(ValueError):
        parse_mix("extreme:1")
    p, _ = build_plan(("recon",), 10, seed=1, salt="x", caps={"recon": 0.2})
    assert max(s["difficulty"] for s in p["scenarios"]) <= 0.2


# --- agents and runs --------------------------------------------------------------------


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    p, salt = build_plan(ENV_NAMES, 36, seed=9, salt="salt", caps={"recon": 0.3})
    path = tmp_path / "plan.json"
    write_plan(p, salt, path)
    return path


def test_select_is_stratified_in_plan_order(plan_file: Path) -> None:
    from scruple.suite.plan import load_plan, scenarios_of

    sc = scenarios_of(load_plan(plan_file))
    sel = select(sc, per_env=2)
    assert len(sel) == 12
    assert [s.index for s in sel] == sorted(s.index for s in sel)
    assert len(select(sc, envs=["aml"], limit=3)) == 3


def test_scripted_run_is_resumable_and_reports(plan_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "base"
    meta = run_suite(plan_file, ScriptedAgent("honest"), out, per_env=2, concurrency=2)
    assert meta["last_session"]["played"] == 12
    again = run_suite(plan_file, ScriptedAgent("honest"), out, per_env=2)
    assert again["last_session"]["played"] == 0
    recs = load_item_records(out)
    assert len(recs) == 12 and all(r.outcome.gate for r in recs)
    rep = build_report(out, salt="salt", max_fail=0.5)
    assert rep["integrity"]["regeneration_problems"] == []
    assert rep["integrity"]["commitments_revealed"]
    assert rep["executive"]["overall"]["gate_pass"] == 12
    assert rep["real_model"] is False
    assert rep["executive"]["verdict"] in ("RELEASE", "HOLD")


def test_candidate_against_baseline_report_and_renderers(plan_file: Path, tmp_path: Path) -> None:
    base, cand = tmp_path / "base", tmp_path / "cand"
    run_suite(plan_file, ScriptedAgent("honest"), base, per_env=2)
    run_suite(
        plan_file, PythonAgent("scruple.suite.example_agents:read_then_submit"), cand, per_env=2
    )
    rep = build_report(cand, baseline=base)
    assert rep["regression"]["pairs"] == 12
    assert rep["regression"]["reward_diff"] < 0
    assert rep["failure_modes"], "the naive agent fails hard checks somewhere"
    ex = rep["failure_modes"][0]["example"]
    assert ex["trace"] and "suite replay" in ex["replay"]
    html = render_html(rep)
    for section in (
        "Executive summary",
        "Top failure modes",
        "Regression against the baseline",
        "Release test",
        "Methodology",
        "Limitations",
        "Reproduce",
        "Hashes",
        "Synthetic data",
    ):
        assert section in html
    pdf = render_pdf(rep)
    assert pdf.startswith(b"%PDF") and len(pdf) > 3000


def test_replay_command_reproduces_the_grade(
    plan_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scruple.cli.main import main

    out = tmp_path / "c"
    run_suite(
        plan_file, PythonAgent("scruple.suite.example_agents:read_then_submit"), out, per_env=1
    )
    sid = load_item_records(out)[0].item.scenario_id
    assert main(["suite", "replay", str(out), sid]) == 0
    assert json.loads(capsys.readouterr().out)["matches_record"] is True


def test_python_agent_that_crashes_or_stops_is_scored_not_dropped() -> None:
    item = Item(scenario_id="x", env="aml", seed=3, case_type=None)
    rec = play_item(item, PythonAgent("scruple.suite.example_agents:gives_up"))
    assert rec.play.stop == "model_stopped" and not rec.outcome.submitted
    rec2 = play_item(item, PythonAgent("json:dumps"))  # raises TypeError on a Session
    assert rec2.play.stop == "agent_error"


def test_http_agent_step_protocol() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if body["last_result"] is None:
            assert body["observation"] and body["tools"]
            return httpx.Response(200, json={"tool": "get_alert", "args": {}})
        return httpx.Response(
            200,
            json={"tool": "submit_decision", "args": {"bogus": 1}}
            if len(seen) == 2
            else {"stop": True},
        )

    agent = HttpAgent("http://agent.local/step", transport=httpx.MockTransport(handler))
    rec = play_item(Item(scenario_id="y", env="aml", seed=4, case_type=None), agent)
    assert rec.play.calls[0]["tool"] == "get_alert"
    assert rec.play.calls[1]["ok"] is False  # a malformed submit is refused, and recorded
    assert rec.play.stop == "model_stopped"


def test_fake_provider_agent_on_every_env() -> None:
    agent = parse_agent("groq:fake", transport="fake")
    for env in ENV_NAMES:
        a = get_adapter(env)
        ct = None if env == "dispute" else a.case_type_at(0.3, random.Random(2))
        rec = play_item(Item(scenario_id=env, env=env, seed=5, case_type=ct), agent)
        assert rec.play.stop == "submitted", env
        assert rec.replay_match, env
        assert rec.play.real_model is False


def test_parse_agent_specs() -> None:
    assert parse_agent("python:a.b:c").label == "python:a.b:c"
    assert parse_agent("scripted:honest").label == "scripted:honest"
    assert parse_agent("https://x.example/step").label.startswith("http:")
    with pytest.raises(ValueError):
        parse_agent("nonsense")


# --- the release test -------------------------------------------------------------------


def _recs(gates: list[bool], rewards: list[float] | None = None) -> list[ItemRecord]:
    from scruple.suite.agents import PlayResult
    from scruple.suite.envs import Outcome

    rewards = rewards or [1.0 if g else 0.0 for g in gates]
    return [
        ItemRecord(
            item=Item(scenario_id=f"s{i}", env="aml", seed=i, case_type=None),
            play=PlayResult(agent="a", stop="submitted"),
            outcome=Outcome(gate=g, reward=r),
            replay_match=True,
        )
        for i, (g, r) in enumerate(zip(gates, rewards, strict=True))
    ]


def test_release_test_verdicts() -> None:
    order = {f"s{i}": i for i in range(400)}
    good = _recs([True] * 120)
    assert (
        release_test(good, order, alpha=0.05, max_fail=0.1, tolerance=0.02, base=None)["verdict"]
        == "RELEASE"
    )
    bad = _recs([i % 2 == 0 for i in range(120)])
    assert (
        release_test(bad, order, alpha=0.05, max_fail=0.1, tolerance=0.02, base=None)["verdict"]
        == "BLOCK"
    )
    few = _recs([True] * 5)
    assert (
        release_test(few, order, alpha=0.05, max_fail=0.1, tolerance=0.02, base=None)["verdict"]
        == "HOLD"
    )
    # a candidate that passes the gate but loses reward against the baseline is blocked
    base = {r.item.scenario_id: r for r in _recs([True] * 120)}
    worse = _recs([True] * 120, [0.6] * 120)
    rt = release_test(worse, order, alpha=0.05, max_fail=0.1, tolerance=0.02, base=base)
    assert (
        rt["verdict"] == "BLOCK"
        and rt["regression"]["rejected"]
        and rt["regressed_envs"] == ["aml"]
    )
    # identical behaviour: no regression, but non-inferiority within 0.02 needs more data
    same = _recs([True] * 120)
    rt2 = release_test(same, order, alpha=0.05, max_fail=0.1, tolerance=0.02, base=base)
    assert rt2["verdict"] == "HOLD" and not rt2["regression"]["rejected"]
    # a clearly better candidate is released
    base_lo = {r.item.scenario_id: r for r in _recs([True] * 120, [0.8] * 120)}
    rt3 = release_test(same, order, alpha=0.05, max_fail=0.1, tolerance=0.02, base=base_lo)
    assert rt3["verdict"] == "RELEASE" and rt3["non_inferiority"]["rejected"]


# --- CLI --------------------------------------------------------------------------------


def test_cli_plan_verify_run_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scruple.cli.main import main

    plan = tmp_path / "plan.json"
    assert (
        main(
            ["suite", "plan", "--envs", "aml,ach", "--size", "8", "--seed", "1", "--out", str(plan)]
        )
        == 0
    )
    info = json.loads(capsys.readouterr().out)
    assert info["n"] == 8 and Path(info["salt_file"]).exists()
    assert main(["suite", "verify", str(plan), "--salt", info["salt_file"]]) == 0
    capsys.readouterr()
    out = tmp_path / "run"
    assert (
        main(
            [
                "suite",
                "run",
                "--plan",
                str(plan),
                "--agent",
                "scripted:honest",
                "--out",
                str(out),
                "--quiet",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(["suite", "report", str(out), "--format", "json,html", "--salt", info["salt_file"]])
        == 0
    )
    res = json.loads(capsys.readouterr().out)
    assert len(res["files"]) == 2
    rep = json.loads((out / "report.json").read_text())
    assert rep["hashes"]["plan_hash"] == json.loads(plan.read_text())["plan_hash"]


def test_eval_run_env_commerce_fake(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scruple.cli.main import main

    argv = ["eval", "run", "--env", "commerce", "--provider", "openai", "--model", "m",
            "--strategy", "incentive-win-rate", "--seeds", "1-2", "--out", str(tmp_path),
            "--transport", "fake", "--quiet"]  # fmt: skip
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "ref:careful" in out
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man["strategy"] == "incentive-win-rate" and man["env"] == "commerce"


def test_transcript_transport_records_latency_and_redacts(tmp_path: Path) -> None:
    from scruple.agents.providers.limits import TranscriptTransport

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "org_ABC123 over limit"}})

    log = tmp_path / "t.jsonl"
    t = TranscriptTransport(log, inner=httpx.MockTransport(handler))
    with httpx.Client(transport=t) as c:
        c.post("https://x/v1", json={"messages": [{"role": "user", "content": "hi"}]})
    line = log.read_text()
    assert "org_REDACTED" in line and "org_ABC123" not in line
    assert len(t.latencies_s) == 1
    assert json.loads(line)["new_messages"] == [{"role": "user", "content": "hi"}]
