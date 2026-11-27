"""Artefacts, the suite's policies and the versioned verifiers."""

from __future__ import annotations

from typing import Any

import pytest

from scruple.agents.aml_episode import AmlEpisode
from scruple.agents.recon_episode import ReconEpisode
from scruple.finops.artefacts import MEMO_INJECTION, aml_perturb, memo_injected, recon_inbox
from scruple.finops.envs import ENVS, anthropic_schema, get_env, openai_schema
from scruple.finops.knobs import Knobs
from scruple.finops.policies import (
    AmlKnobs,
    AmlPolicy,
    ReconKnobs,
    ReconPolicy,
    iter_variants,
    knob_grid,
    run_policy,
    toggle,
)
from scruple.finops.verifiers import VERIFIERS, gated, get_verifier
from scruple.sim.aml.generator import generate_aml_case
from scruple.sim.aml.models import TYPOLOGIES
from scruple.sim.recon.generator import generate_recon_case

SEEDS = range(12)


def _play(env: str, policy: str, seed: int, ct: str | None) -> Any:
    e = get_env(env)
    return run_policy(e.episode_cls, e.policies()[policy], seed, ct)


def _reward(env: str, policy: str, seed: int, ct: str | None, vid: str | None = None) -> float:
    ep = _play(env, policy, seed, ct)
    return get_env(env).verifier(vid).score(ep.record()).reward


# --- artefacts ----------------------------------------------------------------------


def test_recon_inbox_is_seeded_and_provenance_tagged() -> None:
    case = generate_recon_case(7, 0.8)
    a = recon_inbox(case, Knobs(adv=1))
    assert a == recon_inbox(case, Knobs(adv=1))
    assert recon_inbox(case, Knobs()) == ()
    assert {m.provenance for m in a} <= {"external_unverified", "internal_unverified"}
    assert any(m.kind == "plug" for m in a)
    assert any(not m.adversarial for m in a)  # a message is not itself a signal
    v = a[0].view()
    assert "kind" not in v and "adversarial" not in v
    assert [m.artefact_id for m in a] == [f"M-{i:02d}" for i in range(1, len(a) + 1)]


def test_aml_perturb_keeps_the_case_valid_and_the_truth() -> None:
    for seed in SEEDS:
        base = generate_aml_case(seed, None, 0.5)
        noisy = aml_perturb(base, Knobs(noise=1.0, adv=1.0))
        assert len(noisy.transfers) == len(base.transfers) + 40
        assert noisy.truth.family == base.truth.family
        assert len(noisy.alert.linked_txn_ids) == len(base.alert.linked_txn_ids)
        assert memo_injected(noisy)
        linked = {t.txn_id: t for t in noisy.transfers}
        assert any(linked[x].memo == MEMO_INJECTION for x in noisy.alert.linked_txn_ids)
        old = {t.txn_id: t.amount_minor for t in base.transfers}
        new = {t.txn_id: t.amount_minor for t in noisy.transfers}
        assert sorted(old[x] for x in base.alert.linked_txn_ids) == sorted(
            new[x] for x in noisy.alert.linked_txn_ids
        )
    assert aml_perturb(base, Knobs()) is base


def test_aml_notes_tool() -> None:
    ep = AmlEpisode(3, "@0.5;adv=1")
    notes = ep.call("get_case_notes")["notes"]
    assert notes and all(n["provenance"] == "internal_unverified" for n in notes)
    assert AmlEpisode(3, "@0.5").call("get_case_notes")["notes"] == []


def test_recon_inbox_tool() -> None:
    assert ReconEpisode(3, "@0.5").call("get_inbox")["messages"] == []
    assert ReconEpisode(3, "@0.5;adv=1").call("get_inbox")["messages"]


# --- policies -------------------------------------------------------------------------


@pytest.mark.parametrize("env", list(ENVS))
@pytest.mark.parametrize("seed", range(6))
def test_honest_and_oracle_are_perfect_and_floor_is_low(env: str, seed: int) -> None:
    ct = "@0.9;n=30;breaks=8;noise=0.5;adv=1" if env == "recon" else "@0.9"
    if env == "aml":
        ct = "@0.9;noise=1;adv=1"
    assert _reward(env, "honest", seed, ct) == 1.0
    assert _reward(env, "oracle", seed, ct) == 1.0
    if env in ("recon", "aml"):
        assert _reward(env, "floor", seed, ct) == 0.0


def test_exact_reference_policy_breaks_under_noise() -> None:
    rs = [_reward("recon", "honest_exact_refs", s, "@0.5;noise=1") for s in SEEDS]
    assert sum(rs) / len(rs) < 0.95


def test_credulous_recon_is_caught_by_rec_apply() -> None:
    fired = 0
    for s in range(30):
        ep = _play("recon", "credulous", s, "@0.8;adv=1")
        card = get_verifier("recon@2").score(ep.record())
        fired += any(c.id == "REC-APPLY" and not c.passed for c in card.checks)
        v1 = get_verifier("recon@1").score(ep.record())
        assert all(c.passed for c in v1.checks if c.id == "REC-EVID")
    assert fired > 0


def test_credulous_aml_obeys_the_notes() -> None:
    closed_typology = 0
    for s in range(30):
        ep = _play("aml", "credulous", s, "@0.5;adv=1")
        if ep.case.truth.family in TYPOLOGIES and ep.engine.outcome().decision == "close":
            closed_typology += 1
    assert closed_typology > 0


def test_peek_without_a_licence_fails_scope() -> None:
    ep = run_policy(ReconEpisode, ReconPolicy(ReconKnobs(peek=True)), 1, "@0.5")
    card = get_verifier("recon@2").score(ep.record())
    assert not card.gate and any(c.id == "REC-SCOPE" and not c.passed for c in card.checks)
    ep = run_policy(AmlEpisode, AmlPolicy(AmlKnobs(peek=True)), 1, "@0.5")
    assert not get_verifier("aml@2").score(ep.record()).gate


@pytest.mark.parametrize(
    "knobs",
    [
        ReconKnobs(novice=True, dump=True),
        ReconKnobs(plug=True, novice=True),
        ReconKnobs(timing_all=True),
        ReconKnobs(claim=True, lazy=True),
        ReconKnobs(follow=True, obey_text=True, normalise=False),
    ],
)
def test_recon_knob_policies_run(knobs: ReconKnobs) -> None:
    for s in range(4):
        ep = run_policy(ReconEpisode, ReconPolicy(knobs), s, "@0.7;adv=1;noise=0.3")
        assert ep.closed
        assert 0.0 <= get_verifier("recon@2").score(ep.record()).reward <= 1.0


@pytest.mark.parametrize(
    "knobs",
    [
        AmlKnobs(sar_always=True, inflate=True),
        AmlKnobs(skip_review=True),
        AmlKnobs(skip_review=True, sar_always=True),
        AmlKnobs(network=True, sar_always=True),
        AmlKnobs(all_indicators=True, sar_always=True),
        AmlKnobs(novice=True, claim=True),
    ],
)
def test_aml_knob_policies_run(knobs: AmlKnobs) -> None:
    for s in range(6):
        ep = run_policy(AmlEpisode, AmlPolicy(knobs), s, "@0.7;adv=1")
        assert ep.closed


def test_aml_exploits_fail_hard_checks() -> None:
    for knobs, check in [
        (AmlKnobs(sar_always=True, inflate=True), "AML-SAR2"),
        (AmlKnobs(sar_always=True, network=True), "AML-SAR2"),
        (AmlKnobs(skip_review=True), "AML-PROC"),
    ]:
        ep = run_policy(AmlEpisode, AmlPolicy(knobs), 2, "@0.5")
        card = get_verifier("aml@2").score(ep.record())
        assert any(c.id == check and not c.passed for c in card.checks), knobs


def test_policy_names_grid_and_toggle() -> None:
    assert ReconPolicy(ReconKnobs(), "honest").name == "honest"
    assert ReconPolicy(ReconKnobs()).name.startswith("recon[")
    assert AmlPolicy(AmlKnobs()).name.startswith("aml[")
    assert ReconKnobs(plug=True, claim=True).exploits() == ("plug", "claim")
    assert AmlKnobs(inflate=True).exploits() == ("inflate",)
    assert len(knob_grid(ReconKnobs, {"plug": (False, True), "claim": (False, True)})) == 4
    assert len(list(iter_variants(ReconPolicy(), {"plug": (False, True)}))) == 2
    assert len(list(iter_variants(AmlPolicy(), {"inflate": (False, True)}))) == 2
    p = toggle(ReconPolicy(), plug=True)
    assert isinstance(p, ReconPolicy) and p.knobs.plug
    q = toggle(AmlPolicy(), inflate=True)
    assert isinstance(q, AmlPolicy) and q.knobs.inflate
    with pytest.raises(TypeError):
        toggle(get_env("ach").policies()["honest"], plug=True)


def test_kind_baselines_are_wrapped() -> None:
    pols = get_env("payables").policies()
    assert {"honest", "oracle", "floor", "hacker", "pay_everything"} <= set(pols)
    assert pols["hacker"].name == "trust_the_email"


# --- verifiers ------------------------------------------------------------------------


def test_verifier_registry() -> None:
    assert set(VERIFIERS) == {
        "recon@1", "recon@2", "aml@1", "aml@2", "ach@1", "ach@2", "payables@1", "payables@2",
        "recon@3", "aml@3", "ach@3", "payables@3", "commerce@2", "commerce@3", "dispute@2",
        "dispute@3",
    }  # fmt: skip
    for v in VERIFIERS.values():
        assert len(v.fingerprint) == 16
        assert v.fingerprint == v.fingerprint  # cached
        assert v.table()
        if v.id.endswith(("@2", "@3")) and v.env not in ("dispute",):
            assert all(c.weight == 0 for c in v.checks if c.hard)
    with pytest.raises(ValueError):
        get_verifier("nope@1")
    with pytest.raises(ValueError):
        get_env("recon").verifier("aml@2")
    with pytest.raises(ValueError):
        get_env("nope")
    ids = [c.id for c in get_verifier("recon@2").checks]
    assert "REC-APPLY" in ids and "REC-TIE2" in ids and "REC-TIE" not in ids
    ids = [c.id for c in get_verifier("aml@2").checks]
    assert {"AML-SAR2", "AML-DEC2", "AML-BASIS2"} <= set(ids)
    assert gated(()) == ()


def test_rec_tie2_refuses_a_tie_through_a_stray_entry() -> None:
    ep = run_policy(ReconEpisode, ReconPolicy(ReconKnobs(novice=True, dump=True)), 0, "@0.6")
    card = get_verifier("recon@2").score(ep.record())
    tie2 = next(c for c in card.checks if c.id == "REC-TIE2")
    if ep.engine.outcome().difference_minor == 0 and ep.engine.outcome().adjustments:
        assert not tie2.passed and "ties out only through" in tie2.explanation


def test_aml_sar2_refuses_unread_citations() -> None:
    ep = AmlEpisode(5, "@0.5")
    alert = ep.call("get_alert")["alert"]
    ep.call("get_account", {"account_id": alert["subject_id"]})
    ep.call("get_transactions", {"txn_ids": alert["linked_txn_ids"]})
    sid = alert["subject_id"]
    own = [t for t in ep.case.transfers if sid in (t.src, t.dst)]
    extra = [t for t in own if t.txn_id not in alert["linked_txn_ids"]][:2]
    cited = list(alert["linked_txn_ids"]) + [t.txn_id for t in extra]
    total = sum(t.amount_minor for t in ep.case.transfers if t.txn_id in cited)
    ep.call(
        "file_sar",
        {"indicators": ["fan_in"], "txn_ids": cited, "amount_minor": total, "narrative": "x"},
    )
    ep.call("submit_decision", {"explanation": "filed"})
    v1 = get_verifier("aml@1").score(ep.record())
    v2 = get_verifier("aml@2").score(ep.record())
    assert next(c for c in v1.checks if c.id == "AML-SAR").passed
    sar2 = next(c for c in v2.checks if c.id == "AML-SAR2")
    assert not sar2.passed and "never read" in sar2.explanation


def test_aml_undecided_scores_zero_in_v2() -> None:
    ep = AmlEpisode(5, "@0.5")
    ep.call("submit_decision", {"explanation": "nothing"})
    assert get_verifier("aml@1").score(ep.record()).reward >= 0.5
    assert get_verifier("aml@2").score(ep.record()).reward == 0.0


def test_schemas_for_both_apis() -> None:
    for e in ENVS.values():
        a, o = e.anthropic_tools(), e.openai_tools()
        assert [t["name"] for t in a] == [t["function"]["name"] for t in o]
        assert a == anthropic_schema(e.tools) and o == openai_schema(e.tools)
        assert e.task and e.knobs
        assert e.verifier().id == e.default_verifier
