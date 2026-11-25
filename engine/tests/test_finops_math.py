"""The statements of docs/math/10_ledger_anchored_reward.md, checked on generated
episodes: Theorem 1 (reports cannot raise reward), Lemma 4a/4b and Theorem 4 (the
honest projection), Proposition 5 (the adversarial-rate coupling)."""

from __future__ import annotations

from functools import cache
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.finops import tasks as tk
from scruple.finops.anchoring import aml_projection, project, projection_gap, recon_projection
from scruple.finops.difficulty import curriculum
from scruple.finops.envs import ENVS, get_env
from scruple.finops.policies import AmlKnobs, AmlPolicy, ReconKnobs, ReconPolicy, run_policy

PHRASES = ("", "Fully reconciled.", "We filed a SAR.", "All breaks cleared.", "ok")


@cache
def _spec(env: str, seed: int, d: float) -> tk.TaskSpec:
    return tk.build_task(env, seed, curriculum(env, d), "salt", measure=False)


def _claims(env: str, draw: Any) -> dict[str, Any]:
    if env == "recon":
        keys = {
            "balanced": st.none() | st.booleans(),
            "unreconciled_minor": st.none() | st.integers(-(10**7), 10**7),
            "adjustments": st.none() | st.integers(0, 30),
            "escalations": st.none() | st.integers(0, 30),
        }
    elif env == "aml":
        keys = {
            "decision": st.none() | st.sampled_from(["close", "escalate", "file_sar", "none"]),
            "sar_filed": st.none() | st.booleans(),
            "sar_amount_minor": st.none() | st.integers(0, 10**8),
            "info_requests": st.none() | st.integers(0, 3),
            "reviewed_linked": st.none() | st.booleans(),
        }
    else:
        return {}
    return {k: v for k, s in keys.items() if (v := draw(s)) is not None}


@settings(max_examples=60, deadline=None)
@given(
    env=st.sampled_from(sorted(ENVS)),
    seed=st.integers(0, 400),
    d=st.sampled_from([0.0, 0.5, 1.0]),
    policy=st.sampled_from(["honest", "novice", "credulous", "hacker", "floor"]),
    data=st.data(),
)
def test_theorem1_reports_cannot_raise_reward(
    env: str, seed: int, d: float, policy: str, data: Any
) -> None:
    pols = get_env(env).policies()
    if policy not in pols:
        policy = "floor"
    spec = _spec(env, seed, d)
    traj = tk.rollout(spec, policy)
    calls = [c for c in traj.calls if c.tool != "submit_decision"]
    null = tk.Call(tool="submit_decision", args={"explanation": "Done."})
    r0 = tk.grade(spec, tk.Trajectory(task_id=spec.task_id, calls=(*calls, null))).reward
    report: dict[str, Any] = {"explanation": data.draw(st.sampled_from(PHRASES)) or "x"}
    claims = _claims(env, data.draw)
    if claims:
        report["claims"] = claims
    lie = tk.Call(tool="submit_decision", args=report)
    g = tk.grade(spec, tk.Trajectory(task_id=spec.task_id, calls=(*calls, lie)))
    if g.calls == len(calls) + 1:  # the report was accepted by the schema
        assert g.reward in (0.0, pytest.approx(r0))
        assert g.reward <= r0 + 1e-12


RECON_POLICIES = [
    ReconKnobs(),
    ReconKnobs(novice=True),
    ReconKnobs(novice=True, dump=True),
    ReconKnobs(dump=True, timing_all=True),
    ReconKnobs(lazy=True),
    ReconKnobs(follow=True, obey_text=True),
    ReconKnobs(normalise=False),
    ReconKnobs(novice=True, timing_all=True, claim=True),
]
AML_POLICIES = [
    AmlKnobs(),
    AmlKnobs(novice=True),
    AmlKnobs(sar_always=True),
    AmlKnobs(all_indicators=True, sar_always=True),
    AmlKnobs(follow=True),
    AmlKnobs(novice=True, all_indicators=True),
]
ANCHORED = {
    "recon@2": ("REC-CLEAR", "REC-MATCH", "REC-COVER"),
    "aml@2": ("AML-DEC2", "AML-BASIS2"),
}


@pytest.mark.parametrize("knobs", RECON_POLICIES)
def test_theorem4_recon_projection(knobs: ReconKnobs) -> None:
    for seed in range(10):
        ct = curriculum("recon", (seed % 11) / 10)
        r, p, per = project("recon", ReconPolicy(knobs), seed, ct, "recon@2")
        if r > 0:  # Lemma 4a: a gated trajectory's projection is gated
            hard = [c.id for c in get_env("recon").verifier("recon@2").checks if c.hard]
            assert all(per[h][1] == 1.0 for h in hard), (knobs, seed, per)
        for cid in ANCHORED["recon@2"]:  # Lemma 4b
            assert per[cid][1] >= per[cid][0] - 1e-12, (cid, knobs, seed)
        assert r <= p + 1 / 6 + 1e-9  # Theorem 4


@pytest.mark.parametrize("knobs", AML_POLICIES)
def test_theorem4_aml_projection(knobs: AmlKnobs) -> None:
    for seed in range(12):
        ct = curriculum("aml", (seed % 11) / 10)
        r, p, per = project("aml", AmlPolicy(knobs), seed, ct, "aml@2")
        if r > 0:
            hard = [c.id for c in get_env("aml").verifier("aml@2").checks if c.hard]
            assert all(per[h][1] == 1.0 for h in hard), (knobs, seed, per)
        for cid in ANCHORED["aml@2"]:
            assert per[cid][1] >= per[cid][0] - 1e-12
        assert r <= p + 1e-9  # gap 0 on aml@2


def test_projection_calls_shape() -> None:
    e = get_env("recon")
    ep = run_policy(e.episode_cls, ReconPolicy(ReconKnobs(novice=True)), 3, "@0.8;n=50;breaks=8")
    calls = recon_projection(ep.record())
    assert calls[-1][0] == "submit_decision"
    assert all(len((a or {}).get("statement_line_ids", ())) <= 20 for _, a in calls)
    a = get_env("aml")
    ep2 = run_policy(a.episode_cls, a.policies()["floor"], 3, "@0.5")
    assert aml_projection(ep2.record()) == [
        ("submit_decision", {"explanation": "Projection of the episode."})
    ]
    r, p, _ = projection_gap("aml", ep2.record(), "@0.5", "aml@2")
    assert r == p == 0.0


@pytest.mark.parametrize("env", ["recon", "aml"])
def test_proposition5_artefact_blind_reward_is_invariant(env: str) -> None:
    e = get_env(env)
    honest = e.policies()["honest"]
    for seed in range(8):
        rewards = set()
        for adv in (0.0, 0.3, 0.7, 1.0):
            ct = f"@0.5;adv={adv}" if adv else "@0.5"
            ep = run_policy(e.episode_cls, honest, seed, ct)
            rewards.add(e.verifier().score(ep.record()).reward)
        assert len(rewards) == 1


def test_proposition5_case_differs_only_in_artefacts() -> None:
    a = get_env("recon").new_episode(9, "@0.5").case
    b = get_env("recon").new_episode(9, "@0.5;adv=1").case
    da = a.model_dump(mode="json")
    db = b.model_dump(mode="json")
    for k in da:
        if k not in ("bank_lines", "statement_file"):
            assert da[k] == db[k], k
    diffs = [(x, y) for x, y in zip(a.bank_lines, b.bank_lines, strict=True) if x != y]
    assert len(diffs) <= 1 and all(x.description != y.description for x, y in diffs)
