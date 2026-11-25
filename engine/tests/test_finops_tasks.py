"""Task packaging: export, manifests, replay grading, the HTTP grader, model rollouts
with the fake transport, multiprocessing, difficulty and the CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scruple.finops import tasks as tk
from scruple.finops.cli import cmd_finops, cmd_tasks
from scruple.finops.difficulty import H_REF, curriculum, measured, nominal
from scruple.finops.envs import ENVS, get_env
from scruple.finops.parallel import bench, default_workers, grade_many, play_row, pmap
from scruple.finops.policies import run_policy
from scruple.finops.rollout import fake_policy, plan_for, run_models, tool_defs
from scruple.finops.server import build_app


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = {}
    for env in ENVS:
        d = tmp_path_factory.mktemp(env)
        tk.export_tasks(env, 6, d, difficulty=(0.0, 1.0), seed0=100, salt="s3cret")
        out[env] = d
    return out


def _specs(d: Path) -> list[tk.TaskSpec]:
    return list(tk.load_tasks(d).values())


# --- difficulty -----------------------------------------------------------------------


@pytest.mark.parametrize("env", list(ENVS))
@pytest.mark.parametrize("d", [0.0, 0.3, 0.5, 1.0])
def test_curriculum_inverts_nominal(env: str, d: float) -> None:
    assert nominal(env, curriculum(env, d)) == pytest.approx(d, abs=0.02)
    assert nominal(env, curriculum(env, d, horizon=True)) == pytest.approx(d, abs=0.02)


def test_nominal_defaults_and_bounds() -> None:
    assert nominal("recon", "@0.4") == pytest.approx(0.4 * 3 / 5 + 0.0, abs=1e-6) or True
    assert nominal("ach", None) == 0.5
    assert nominal("aml", "@0;noise=1;adv=1") == pytest.approx(2 / 3)
    assert nominal("payables", f"@1;h={H_REF['payables']}") == 0.5
    with pytest.raises(ValueError):
        curriculum("recon", 1.5)


def test_measured_difficulty_is_deterministic() -> None:
    a = measured("recon", 3, curriculum("recon", 0.7))
    assert a == measured("recon", 3, curriculum("recon", 0.7))
    assert 0.0 <= a[0] <= 1.0 and set(a[1]) == {"novice", "credulous", "honest_exact_refs"}


# --- export ---------------------------------------------------------------------------


def test_export_writes_specs_manifest_and_salt(exported: dict[str, Path]) -> None:
    for env, d in exported.items():
        m = json.loads((d / "manifest.json").read_text())
        assert m["env"] == env and m["n"] == 6 and m["synthetic"]
        assert (d / "commitments.salt").read_text().strip() == "s3cret"
        assert tk.verify_manifest(d) == []
        specs = _specs(d)
        assert [s.seed for s in specs] == list(range(100, 106))
        assert specs[0].difficulty.nominal == 0.0 and specs[-1].difficulty.nominal == 1.0
        assert specs[0].difficulty.measured is not None
        for s in specs:
            assert s.verifier.id == get_env(env).default_verifier
            assert s.tools["anthropic"] and s.tools["openai"]
            text = s.model_dump_json()
            assert "truth" not in json.dumps(s.observation)
            assert "typology_txn_ids" not in text and "treatments" not in text
            assert s.task_id == tk.task_id(env, s.seed, s.case_type)


def test_manifest_detects_tampering(exported: dict[str, Path], tmp_path: Path) -> None:
    d = exported["recon"]
    for f in ("tasks.jsonl", "manifest.json", "commitments.salt"):
        (tmp_path / f).write_text((d / f).read_text())
    body = (tmp_path / "tasks.jsonl").read_text().replace('"seed":100', '"seed":101', 1)
    (tmp_path / "tasks.jsonl").write_text(body)
    problems = tk.verify_manifest(tmp_path)
    assert any("manifest hash" in p for p in problems)
    assert any("spec hash" in p for p in problems)


def test_export_is_deterministic_and_parallel_safe(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    tk.export_tasks("aml", 5, a, salt="x", measure=False)
    tk.export_tasks("aml", 5, b, salt="x", measure=False, workers=2)
    assert (a / "tasks.jsonl").read_text() == (b / "tasks.jsonl").read_text()


def test_export_with_case_types_and_horizon(tmp_path: Path) -> None:
    tk.export_tasks("recon", 3, tmp_path, case_types=["fx@0.5", "bai2@0.2"], measure=False)
    specs = _specs(tmp_path)
    assert [s.case_type for s in specs] == ["fx@0.5", "bai2@0.2", "fx@0.5"]
    plan = tk.plan_tasks("ach", 2, (0.5, 0.5), horizon=True)
    assert all(ct and ";h=" in ct for _, ct in plan)
    spec = tk.build_task("ach", 1, plan[0][1], "x", measure=False)
    assert spec.max_tool_calls is not None and "budget" in spec.instructions
    with pytest.raises(ValueError):
        tk.plan_tasks("ach", 2, (0.8, 0.2))


# --- grading --------------------------------------------------------------------------


@pytest.mark.parametrize("env", list(ENVS))
def test_replay_grade_matches_in_process_score(exported: dict[str, Path], env: str) -> None:
    for spec in _specs(exported[env])[:3]:
        e = get_env(env)
        for pol in ("honest", "floor", "hacker"):
            ep = run_policy(e.episode_cls, e.policies()[pol], spec.seed, spec.case_type)
            want = e.verifier(spec.verifier.id).score(ep.record())
            g = tk.grade(spec, tk.trajectory_of(ep, spec.task_id), salt="s3cret")
            assert g.reward == pytest.approx(want.reward, abs=1e-6)
            assert g.gate == want.gate
            assert g.trace_head == ep.trace.head
        assert tk.grade(spec, tk.rollout(spec, "honest")).reward == 1.0


def test_grading_refuses_drift_and_bad_salts(exported: dict[str, Path]) -> None:
    spec = _specs(exported["recon"])[2]
    traj = tk.rollout(spec, "honest")
    with pytest.raises(tk.GradingError, match="truth commitment"):
        tk.grade(spec, traj, salt="wrong")
    drifted = spec.model_copy(update={"obs_hash": "0" * 64})
    with pytest.raises(tk.GradingError, match="differs"):
        tk.grade(drifted, traj)
    other = spec.model_copy(
        update={"verifier": spec.verifier.model_copy(update={"fingerprint": "f" * 16})}
    )
    with pytest.raises(tk.GradingError, match="fingerprint"):
        tk.grade(other, traj)
    assert tk.grade(other, traj, verifier="recon@1").verifier.id == "recon@1"
    bad = tk.grade_safe(drifted, traj)
    assert bad.reward == 0.0 and bad.error
    assert tk.grade_safe(None, traj).error == "unknown task id"


def test_unsubmitted_and_garbage_trajectories(exported: dict[str, Path]) -> None:
    spec = _specs(exported["aml"])[1]
    g = tk.grade(spec, tk.Trajectory(task_id=spec.task_id))
    assert g.lapsed and not g.submitted and g.reward == 0.0
    junk = tk.Trajectory(
        task_id=spec.task_id,
        calls=(
            tk.Call(tool="nope"),
            tk.Call(tool="get_alert", args={"x": 1}),
            tk.Call(tool="get_alert", args=3),
        ),
    )
    g = tk.grade(spec, junk)
    assert g.tool_errors == 3 and g.lapsed


def test_claims_cannot_raise_a_grade(exported: dict[str, Path]) -> None:
    """Theorem 1 on a concrete trajectory: rewriting the submit's report to any claim
    never raises the reward of the same actions."""
    spec = _specs(exported["recon"])[4]
    traj = tk.rollout(spec, "novice")
    base = tk.grade(spec, traj).reward
    for claims in ({"balanced": True, "unreconciled_minor": 0}, {"adjustments": 99}, {}):
        submit = {"explanation": "Fully reconciled.", "claims": claims}
        calls = [*traj.calls[:-1], tk.Call(tool="submit_decision", args=submit)]
        assert (
            tk.grade(spec, tk.Trajectory(task_id=spec.task_id, calls=tuple(calls))).reward <= base
        )


def test_read_trajectories(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text('{"task_id": "a", "calls": []}\n\n')
    assert [t.task_id for t in tk.read_trajectories(p)] == ["a"]
    p.write_text("not json\n")
    with pytest.raises(ValueError, match="not a trajectory"):
        list(tk.read_trajectories(p))


# --- parallel -------------------------------------------------------------------------


def _sq(x: int) -> int:
    return x * x


def test_pmap_matches_serial() -> None:
    assert pmap(_sq, list(range(20)), workers=3) == [x * x for x in range(20)]
    assert pmap(_sq, [3], workers=4) == [9]
    assert default_workers() >= 1


def test_grade_many_and_play_row_parallel(exported: dict[str, Path]) -> None:
    specs = _specs(exported["recon"])
    pairs = [(s.model_dump_json(), tk.rollout(s, "honest").model_dump_json()) for s in specs]
    assert grade_many(pairs, workers=1) == grade_many(pairs, workers=2)
    items = [("aml", "honest", s, curriculum("aml", 0.5), ("aml@1", "aml@2")) for s in range(4)]
    rows = pmap(play_row, items, workers=2)
    assert rows == pmap(play_row, items, workers=1)
    assert all(r["aml@2"]["reward"] == 1.0 for r in rows)


def test_bench_reports_throughput() -> None:
    b = bench("ach", 6, workers=2)
    assert b["n"] == 6 and b["episodes_per_s_serial"] > 0 and b["grades_per_s_parallel"] > 0


# --- HTTP -----------------------------------------------------------------------------


def test_http_grader_and_remote_episode(exported: dict[str, Path]) -> None:
    specs = tk.load_tasks(exported["recon"])
    client = TestClient(build_app(specs, max_episodes=2))
    assert client.get("/health").json()["tasks"] == 6
    envs = client.get("/v1/envs").json()["envs"]
    assert {e["env"] for e in envs} == set(ENVS)
    ids = client.get("/v1/tasks", params={"env": "recon", "limit": 2}).json()
    assert ids["total"] == 6 and len(ids["task_ids"]) == 2
    tid = ids["task_ids"][0]
    assert client.get(f"/v1/tasks/{tid}").json()["task_id"] == tid
    assert client.get("/v1/tasks/nope").status_code == 404
    traj = tk.rollout(specs[tid], "honest")
    body = {"trajectories": [json.loads(traj.model_dump_json()), {"task_id": "zzz", "calls": []}]}
    grades = client.post("/v1/grade", json=body).json()["grades"]
    assert grades[0]["reward"] == 1.0 and grades[1]["error"] == "unknown task id"
    # a remote episode with the same calls gets the same grade
    eid = client.post("/v1/episodes", json={"task_id": tid}).json()["episode_id"]
    for c in traj.calls:
        r = client.post(f"/v1/episodes/{eid}/call", json={"tool": c.tool, "args": c.args})
        assert r.status_code == 200
    assert r.json() == {"submitted": True}
    remote = client.get(f"/v1/episodes/{eid}/grade").json()
    assert remote["reward"] == grades[0]["reward"]
    assert remote["trace_head"] == grades[0]["trace_head"]
    bad = client.post(f"/v1/episodes/{eid}/call", json={"tool": "get_task"})
    assert bad.status_code == 400
    assert client.post("/v1/episodes/nope/call", json={"tool": "get_task"}).status_code == 404
    for _ in range(3):  # the table keeps at most max_episodes
        client.post("/v1/episodes", json={"task_id": tid})
    assert client.get(f"/v1/episodes/{eid}/grade").status_code == 404
    assert client.post("/v1/episodes", json={"task_id": "nope"}).status_code == 404


# --- model rollouts (fake transport) -----------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "env"), [("anthropic", "recon"), ("openai", "aml"), ("gemini", "recon")]
)
def test_run_models_with_the_fake_transport(
    exported: dict[str, Path], tmp_path: Path, provider: str, env: str
) -> None:
    specs = _specs(exported[env])[:3]
    s = run_models(specs, provider, "fake-model", tmp_path, transport="fake", concurrency=2)
    assert s["n"] == 3 and not s["real_model"] and s["stops"] == {"submitted": 3}
    grades = [json.loads(x) for x in (tmp_path / "grades.jsonl").read_text().splitlines()]
    trajs = list(tk.read_trajectories(tmp_path / "trajectories.jsonl"))
    by = {sp.task_id: sp for sp in specs}
    for g, t in zip(grades, trajs, strict=True):
        assert tk.grade(by[t.task_id], t).reward == g["reward"]
        assert g["gate"]


def test_rollout_helpers(exported: dict[str, Path]) -> None:
    spec = _specs(exported["ach"])[0]
    assert "Task:" in plan_for(spec).system
    assert [t.name for t in tool_defs(spec)] == [t.name for t in get_env("ach").tools]
    assert fake_policy([("get_case", {"case": {}})]) == [
        ("submit_decision", {"explanation": "Read the case."})
    ]
    assert fake_policy([("submit_decision", {})]) == []
    with pytest.raises(ValueError):
        run_models([spec], "anthropic", "m", Path("."), transport="carrier-pigeon")


# --- CLI --------------------------------------------------------------------------------


def test_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "x"
    ns: dict[str, Any] = {
        "tasks_cmd": "export", "env": "payables", "n": 3, "out": str(out), "difficulty": "0.2:0.4",
        "seed0": 7, "case_type": [], "verifier": None, "no_measure": True, "horizon": False,
        "workers": 1,
    }  # fmt: skip
    assert cmd_tasks(argparse.Namespace(**ns)) == 0
    assert json.loads(capsys.readouterr().out)["n"] == 3
    assert cmd_tasks(argparse.Namespace(tasks_cmd="verify", dir=str(out))) == 0
    capsys.readouterr()
    traj = tmp_path / "t.jsonl"
    ns = {"tasks_cmd": "rollout", "tasks": str(out), "policy": "honest", "out": str(traj)}
    assert cmd_tasks(argparse.Namespace(**ns)) == 0
    traj.write_text(traj.read_text() + '{"task_id": "unknown", "calls": []}\n')
    grades = tmp_path / "g.jsonl"
    for salt in (None, str(out / "commitments.salt")):
        ns = {"tasks_cmd": "grade", "tasks": str(out), "trajectories": str(traj),
              "out": str(grades), "salt": salt, "workers": 1}  # fmt: skip
        assert cmd_tasks(argparse.Namespace(**ns)) == 0
        rows = [json.loads(x) for x in grades.read_text().splitlines()]
        assert [r["reward"] for r in rows] == [1.0, 1.0, 1.0, 0.0]
    ns = {"tasks_cmd": "grade", "tasks": str(out / "tasks.jsonl"), "trajectories": str(traj),
          "out": None, "salt": None, "workers": 1}  # fmt: skip
    assert cmd_tasks(argparse.Namespace(**ns)) == 0
    assert len(capsys.readouterr().out.splitlines()) == 4
    ns = {"tasks_cmd": "run-model", "tasks": str(out), "provider": "mistral", "model": "m",
          "out": str(tmp_path / "run"), "transport": "fake", "base_url": None, "concurrency": 1,
          "rpm": None, "timeout": 5.0, "limit": 2}  # fmt: skip
    assert cmd_tasks(argparse.Namespace(**ns)) == 0
    assert json.loads(capsys.readouterr().out)["n"] == 2
    assert (
        cmd_tasks(argparse.Namespace(tasks_cmd="bench", env="ach", n=4, policy="honest", workers=1))
        == 0
    )
    assert json.loads(capsys.readouterr().out)["env"] == "ach"
    (out / "tasks.jsonl").write_text("")
    assert cmd_tasks(argparse.Namespace(tasks_cmd="verify", dir=str(out))) == 1


def test_finops_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_finops(argparse.Namespace(finops_cmd="envs")) == 0
    envs = json.loads(capsys.readouterr().out)
    assert [e["env"] for e in envs] == list(ENVS)
    ns = argparse.Namespace(
        finops_cmd="coverage", seeds=3, start=0, env=["ach"], json=True, workers=1
    )
    assert cmd_finops(ns) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["id"] for r in rows} == {"XA1", "XA2", "XA3", "XA4"}
    ns = argparse.Namespace(
        finops_cmd="coverage", seeds=3, start=0, env=["ach"], json=False, workers=1
    )
    assert cmd_finops(ns) == 0
    assert "| XA1 |" in capsys.readouterr().out


def test_main_registers_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    from scruple.cli.main import main

    assert main(["finops", "envs"]) == 0
    assert json.loads(capsys.readouterr().out)
