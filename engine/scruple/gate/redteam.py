"""Run the adversarial library against naive verifiers and against the real gate."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from scruple.gate.config import GateConfig
from scruple.gate.engine import as_policy_rule, evaluate_shadow, policy_from
from scruple.gate.models import Outcome
from scruple.hashing import content_hash
from scruple.registry.models import Provenance, RuleStatus, RuleVersion
from scruple.sim.engine import Simulator
from scruple.sim.generator import generate_cases
from scruple.sim.library import BAD_RULE, baseline_policy
from scruple.verifiers.adversarial import (
    RuleAttack,
    naive_accuracy_promotes,
    naive_mean_promotes,
    rule_attacks,
    tamper_journal_drop_debits,
    tamper_results_and_debits,
    tamper_results_zero_regret,
)
from scruple.verifiers.ledger import verify_ledger
from scruple.verifiers.outcome import score_outcomes

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


class AttackResult(BaseModel):
    name: str
    target: str
    naive_fooled: bool
    gate_outcome: str
    defended: bool
    detail: str


def _version(rule_id: str, spec: object, status: RuleStatus) -> RuleVersion:
    return RuleVersion.model_validate(
        {
            "rule_id": rule_id,
            "version": 1,
            "content_hash": content_hash(spec),
            "spec": spec,
            "owner": "redteam",
            "provenance": Provenance(agent_id="redteam"),
            "status": status,
            "created_at": _T0,
        }
    )


def run_rule_attack(attack: RuleAttack, cfg: GateConfig) -> AttackResult:
    active = [_version(rid, s, RuleStatus.ACTIVE) for rid, s in attack.baseline.items()]
    cand = _version(attack.rule_id, attack.spec, RuleStatus.CANDIDATE)
    seed = attack.naive_on_training_seed
    cases = generate_cases(
        seed if seed is not None else cfg.eval_seed,
        cfg.n_cases,
        prefix="h" if seed is None else "c",
    )
    base = policy_from(active)
    sim = Simulator(cases)
    b_run, c_run = sim.run(base), sim.run(base.with_rule(as_policy_rule(cand)))
    naive = naive_mean_promotes if attack.naive == "mean" else naive_accuracy_promotes
    fooled = naive(b_run, c_run)
    v = evaluate_shadow(cand, active, cfg)
    failed = [c.name for c in v.checks if not c.passed]
    return AttackResult(
        name=attack.name,
        target=attack.target,
        naive_fooled=fooled,
        gate_outcome=v.outcome.value,
        defended=v.outcome is not Outcome.PROMOTE,
        detail=", ".join(failed) or "no checks failed",
    )


def run_tamper_attacks(cfg: GateConfig) -> list[AttackResult]:
    cases = generate_cases(cfg.eval_seed, min(cfg.n_cases, 1500), prefix="h")
    sim = Simulator(cases)
    b_run = sim.run(baseline_policy())
    bad_run = sim.run(
        baseline_policy().with_rule(as_policy_rule(_version("atk", BAD_RULE, RuleStatus.CANDIDATE)))
    )
    true_scores = score_outcomes(cases, bad_run)
    out: list[AttackResult] = []

    t1 = tamper_results_zero_regret(bad_run)
    unchanged = score_outcomes(cases, t1).regret == true_scores.regret
    out.append(
        AttackResult(
            name="report_zero_regret",
            target="outcome verifier",
            naive_fooled=naive_mean_promotes(b_run, t1),
            gate_outcome="scores unchanged" if unchanged else "scores changed",
            defended=unchanged,
            detail="outcome verifier recomputes cost from the journal, ignoring reported regret",
        )
    )

    t2 = tamper_results_and_debits(bad_run)
    mism = verify_ledger(cases, t2).metrics["mismatches"]
    out.append(
        AttackResult(
            name="report_zero_debits",
            target="ledger verifier",
            naive_fooled=naive_mean_promotes(b_run, t2),
            gate_outcome=f"{int(mism)} mismatches",
            defended=mism > 0,
            detail="reported debits reconciled against journal postings",
        )
    )

    accept_small = rule_attacks()[1]
    active = [_version(r, s, RuleStatus.ACTIVE) for r, s in accept_small.baseline.items()]
    pol = policy_from(active).with_rule(
        as_policy_rule(_version(accept_small.rule_id, accept_small.spec, RuleStatus.CANDIDATE))
    )
    lrun = sim.run(pol)
    over = {f.case_id for f in verify_ledger(cases, lrun).findings if f.kind == "over_refund"}
    t3 = tamper_journal_drop_debits(lrun, over)
    rep = verify_ledger(cases, t3)
    out.append(
        AttackResult(
            name="delete_journal_debits",
            target="ledger verifier",
            naive_fooled=rep.metrics["over_refunds"] == 0,
            gate_outcome=f"{int(rep.metrics['mismatches'])} mismatches",
            defended=rep.metrics["mismatches"] > 0,
            detail="hiding over-refunds by deleting postings breaks result/journal reconciliation",
        )
    )
    return out


def run_redteam(cfg: GateConfig | None = None) -> list[AttackResult]:
    cfg = cfg or GateConfig()
    return [run_rule_attack(a, cfg) for a in rule_attacks()] + run_tamper_attacks(cfg)
