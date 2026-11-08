"""Promotion engine: shadow replay, paired comparison, regression guards, canary.

The engine is pure: it takes rules and config and returns a verdict. It never
changes status or writes anything. agent_gate.service applies verdicts through the
registry so that every effect is audited in one place.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from agent_gate.hashing import sha256_hex
from agent_gate.registry.models import RuleVersion
from agent_gate.sim.engine import RunResult, Simulator
from agent_gate.sim.generator import generate_cases
from agent_gate.sim.models import DisputeCase
from agent_gate.sim.policy import Policy, PolicyRule
from agent_gate.verifiers.base import Finding, new_findings
from agent_gate.verifiers.costs import realized_costs
from agent_gate.verifiers.ledger import verify_ledger
from agent_gate.verifiers.outcome import OutcomeScores, score_outcomes
from agent_gate.verifiers.policy_constraints import verify_policy

from .config import GateConfig
from .models import (
    ArmStats,
    CanaryMetrics,
    CheckResult,
    ConflictStat,
    Outcome,
    ShadowMetrics,
    SliceStat,
)
from .regression import golden_cases
from .stats import paired_bootstrap_ci


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    summary: str
    reasons: tuple[str, ...]
    checks: tuple[CheckResult, ...]
    shadow: ShadowMetrics | None = None
    canary: CanaryMetrics | None = None


def policy_from(rules: Sequence[RuleVersion]) -> Policy:
    return Policy(rules=tuple(PolicyRule(rule_id=r.rule_id, ref=r.ref, spec=r.spec) for r in rules))


def as_policy_rule(rv: RuleVersion) -> PolicyRule:
    return PolicyRule(rule_id=rv.rule_id, ref=rv.ref, spec=rv.spec)


def seed_commitment(seed: int) -> str:
    return sha256_hex(f"agent-gate-seed:{seed}")[:16]


# --- shadow ------------------------------------------------------------------------


def evaluate_shadow(
    candidate: RuleVersion, active: Sequence[RuleVersion], cfg: GateConfig
) -> Verdict:
    baseline = policy_from(active)
    cand_policy = baseline.with_rule(as_policy_rule(candidate))
    cases = generate_cases(cfg.eval_seed, cfg.n_cases, cfg.profile, prefix="h")
    sim = Simulator(cases)
    b_run, c_run = sim.run(baseline), sim.run(cand_policy)

    b_out, c_out = score_outcomes(cases, b_run), score_outcomes(cases, c_run)
    b_led, c_led = verify_ledger(cases, b_run), verify_ledger(cases, c_run)
    b_pol, c_pol = verify_policy(cases, b_run), verify_policy(cases, c_run)
    new_led, new_pol = new_findings(b_led, c_led), new_findings(b_pol, c_pol)

    ids = [c.case_id for c in cases]
    diffs = [b_out.regret[i] - c_out.regret[i] for i in ids]  # positive = better
    affected = sum(1 for i in ids if b_out.action[i] is not c_out.action[i])
    matched = sum(1 for r in c_run.results if r.decided_by == candidate.ref)
    mean, lo, hi = paired_bootstrap_ci(
        diffs, cfg.bootstrap_resamples, cfg.confidence, cfg.stats_seed
    )
    slices = _slices(cases, b_out, c_out)
    golden_n, golden_regressions = _golden(baseline, cand_policy)
    conflicts = _conflicts(cases, c_run, candidate, baseline)

    worst = max(slices, key=lambda s: s.delta)
    esc_increase = c_out.escalation_rate - b_out.escalation_rate
    checks = (
        _check(
            "no_new_ledger_violations",
            "hard",
            not new_led,
            f"{len(new_led)} new ledger violations (e.g. over-refunds)",
        ),
        _check(
            "no_new_policy_violations",
            "hard",
            not new_pol,
            f"{len(new_pol)} new policy violations: {_kinds(new_pol)}",
        ),
        _check(
            "golden_regression_suite",
            "hard",
            not golden_regressions,
            f"{len(golden_regressions)}/{golden_n} golden cases regressed"
            + (f": {', '.join(golden_regressions)}" if golden_regressions else ""),
        ),
        _check(
            "no_slice_regression",
            "hard",
            worst.delta <= cfg.slice_tolerance_usd_minor,
            f"worst slice '{worst.scenario}' mean regret change "
            f"{_usd(worst.delta)} per case (tolerance {_usd(cfg.slice_tolerance_usd_minor)})",
        ),
        _check(
            "escalation_rate_guard",
            "hard",
            esc_increase <= cfg.max_escalation_rate_increase,
            f"escalation rate {b_out.escalation_rate:.1%} -> {c_out.escalation_rate:.1%}",
        ),
        _check(
            "not_significantly_worse",
            "hard",
            hi >= 0,
            f"mean improvement {_usd(mean)} per case, {cfg.confidence:.0%} CI "
            f"[{_usd(lo)}, {_usd(hi)}]",
        ),
        _check(
            "enough_affected_cases",
            "evidence",
            affected >= cfg.min_affected_cases,
            f"{affected} held-out cases changed decision (minimum {cfg.min_affected_cases})",
        ),
        _check(
            "significant_improvement",
            "evidence",
            lo > cfg.min_mean_improvement_usd_minor,
            f"CI lower bound {_usd(lo)} must exceed "
            f"{_usd(cfg.min_mean_improvement_usd_minor)} per case",
        ),
    )
    metrics = ShadowMetrics(
        n_cases=len(cases),
        eval_seed_commitment=seed_commitment(cfg.eval_seed),
        profile=cfg.profile.name,
        affected_cases=affected,
        matched_cases=matched,
        baseline=_arm(b_out, len(b_led.findings), len(b_pol.findings)),
        candidate=_arm(c_out, len(c_led.findings), len(c_pol.findings)),
        mean_improvement_usd_minor=mean,
        ci_low=lo,
        ci_high=hi,
        confidence=cfg.confidence,
        slices=tuple(slices),
        golden_cases=golden_n,
        golden_regressions=tuple(golden_regressions),
        conflicts=tuple(conflicts),
        new_ledger_findings=tuple(new_led[:10]),
        new_policy_findings=tuple(new_pol[:10]),
        baseline_rules=tuple(baseline.refs()),
    )
    return _verdict(checks, metrics)


def _verdict(checks: tuple[CheckResult, ...], metrics: ShadowMetrics) -> Verdict:
    hard = [c for c in checks if c.kind == "hard" and not c.passed]
    soft = [c for c in checks if c.kind == "evidence" and not c.passed]
    if hard:
        return Verdict(
            Outcome.BLOCK,
            f"Blocked: {len(hard)} hard check(s) failed.",
            tuple(f"{c.name}: {c.detail}" for c in hard),
            checks,
            shadow=metrics,
        )
    if soft:
        return Verdict(
            Outcome.HOLD,
            "Held: not enough evidence that this rule helps.",
            tuple(f"{c.name}: {c.detail}" for c in soft),
            checks,
            shadow=metrics,
        )
    return Verdict(
        Outcome.PROMOTE,
        "Promoted to canary: improves outcomes with no regressions.",
        (
            f"mean regret improves by {_usd(metrics.mean_improvement_usd_minor)} per case "
            f"(CI [{_usd(metrics.ci_low)}, {_usd(metrics.ci_high)}])",
            "all hard checks passed",
        ),
        checks,
        shadow=metrics,
    )


def _slices(cases: Sequence[DisputeCase], b: OutcomeScores, c: OutcomeScores) -> list[SliceStat]:
    groups: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        groups[case.scenario.value].append(case.case_id)
    out = []
    for name in sorted(groups):
        ids = groups[name]
        out.append(
            SliceStat(
                scenario=name,
                n=len(ids),
                baseline_mean_regret=sum(b.regret[i] for i in ids) / len(ids),
                candidate_mean_regret=sum(c.regret[i] for i in ids) / len(ids),
                changed_cases=sum(1 for i in ids if b.action[i] is not c.action[i]),
            )
        )
    return out


def _golden(baseline: Policy, candidate: Policy) -> tuple[int, list[str]]:
    """A golden case regresses if the candidate takes an action the policy owner has
    not allowed for it (and the baseline did not), or adds a hard violation."""
    golden = golden_cases()
    cases = [g.case for g in golden]
    sim = Simulator(cases)
    b_run, c_run = sim.run(baseline), sim.run(candidate)
    b_hard = verify_policy(cases, b_run).keys | verify_ledger(cases, b_run).keys
    c_hard = verify_policy(cases, c_run).keys | verify_ledger(cases, c_run).keys
    b_act = {r.case_id: r.action for r in b_run.results}
    c_act = {r.case_id: r.action for r in c_run.results}
    bad: list[str] = []
    for g in golden:
        i = g.case.case_id
        newly_wrong = c_act[i] not in g.allowed and b_act[i] in g.allowed
        new_hard = {k for k in c_hard if k[0] == i} - b_hard
        if newly_wrong or new_hard:
            bad.append(i)
    return len(golden), bad


def _conflicts(
    cases: Sequence[DisputeCase], run: RunResult, candidate: RuleVersion, baseline: Policy
) -> list[ConflictStat]:
    """Where the candidate decided a case that an existing rule would have decided
    differently: shows which safeguards the candidate overrides."""
    by_case = {c.case_id: c for c in cases}
    counts: Counter[tuple[str, str]] = Counter()
    for r in run.results:
        if r.decided_by != candidate.ref:
            continue
        action, ref = baseline.decide(by_case[r.case_id].view())
        if ref != "default" and action is not candidate.spec.action:
            counts[(ref, action.value)] += 1
    return [
        ConflictStat(overridden_rule=ref, overridden_action=a, cases=n)
        for (ref, a), n in sorted(counts.items())
    ]


# --- canary --------------------------------------------------------------------------


def in_canary(case_id: str, content_hash: str, fraction: float) -> bool:
    bucket = int(sha256_hex(f"{case_id}|{content_hash}")[:8], 16) % 10_000
    return bucket < int(fraction * 10_000)


def evaluate_canary(
    candidate: RuleVersion, active: Sequence[RuleVersion], cfg: GateConfig
) -> Verdict:
    """Route a deterministic slice of 'live' traffic to the candidate policy.

    Uses realized cost only (no hindsight oracle): in production we would only see
    what money moved. Assumption: outcomes resolve immediately in the simulator.
    """
    baseline = policy_from(active)
    cand_policy = baseline.with_rule(as_policy_rule(candidate))
    cases = generate_cases(cfg.canary_seed, cfg.canary_n_cases, cfg.canary_profile, prefix="l")
    treat = {
        c.case_id
        for c in cases
        if in_canary(c.case_id, candidate.content_hash, cfg.canary_fraction)
    }
    run = Simulator(cases).run_routed(lambda c: cand_policy if c.case_id in treat else baseline)
    costs = realized_costs(run)

    # Cases the candidate *would* decide (same precedence in both arms), so the
    # control group is comparable with the treatment group.
    matches = {c.case_id for c in cases if cand_policy.decide(c.view())[1] == candidate.ref}
    decided = {r.case_id for r in run.results if r.decided_by == candidate.ref}
    t_ids = sorted(decided)
    ctrl_ids = sorted(i for i in matches - treat)
    t_mean = sum(costs[i] for i in t_ids) / len(t_ids) if t_ids else 0.0
    c_mean = sum(costs[i] for i in ctrl_ids) / len(ctrl_ids) if ctrl_ids else 0.0

    hard: list[Finding] = [
        f
        for f in (*verify_ledger(cases, run).findings, *verify_policy(cases, run).findings)
        if f.case_id in decided
    ]
    enough = len(t_ids) >= cfg.canary_min_matched and len(ctrl_ids) >= cfg.canary_min_matched
    within = t_mean <= c_mean + cfg.canary_tolerance_usd_minor
    checks = (
        _check(
            "canary_no_hard_violations",
            "hard",
            not hard,
            f"{len(hard)} ledger/policy violations on canary cases",
        ),
        _check(
            "canary_cost_within_tolerance",
            "hard",
            within or not enough,
            f"treatment mean cost {_usd(t_mean)} vs control {_usd(c_mean)} "
            f"(tolerance {_usd(cfg.canary_tolerance_usd_minor)})",
        ),
        _check(
            "canary_enough_traffic",
            "evidence",
            enough,
            f"{len(t_ids)} treatment / {len(ctrl_ids)} control matched cases "
            f"(minimum {cfg.canary_min_matched} each)",
        ),
    )
    metrics = CanaryMetrics(
        live_cases=len(cases),
        fraction=cfg.canary_fraction,
        profile=cfg.canary_profile.name,
        treatment_cases=len(treat),
        matched_treatment=len(t_ids),
        matched_control=len(ctrl_ids),
        treatment_mean_cost_usd_minor=t_mean,
        control_mean_cost_usd_minor=c_mean,
        new_hard_findings=tuple(hard[:10]),
    )
    failed_hard = [c for c in checks if c.kind == "hard" and not c.passed]
    if failed_hard:
        return Verdict(
            Outcome.ROLLBACK,
            "Canary regression: automatically rolled back.",
            tuple(f"{c.name}: {c.detail}" for c in failed_hard),
            checks,
            canary=metrics,
        )
    if not enough:
        return Verdict(
            Outcome.HOLD,
            "Canary inconclusive: not enough live traffic yet.",
            tuple(f"{c.name}: {c.detail}" for c in checks if not c.passed),
            checks,
            canary=metrics,
        )
    return Verdict(
        Outcome.CANARY_PASSED,
        "Canary passed: ready for human approval.",
        ("live outcomes within tolerance of control", "no hard violations"),
        checks,
        canary=metrics,
    )


# --- helpers -------------------------------------------------------------------------


def _arm(o: OutcomeScores, ledger: int, policy: int) -> ArmStats:
    return ArmStats(
        mean_regret_usd_minor=o.mean_regret,
        accuracy=o.accuracy,
        escalation_rate=o.escalation_rate,
        ledger_findings=ledger,
        policy_findings=policy,
    )


def _check(name: str, kind: str, passed: bool, detail: str) -> CheckResult:
    return CheckResult.model_validate(
        {"name": name, "kind": kind, "passed": passed, "detail": detail}
    )


def _kinds(findings: Sequence[Finding]) -> str:
    c = Counter(f.kind for f in findings)
    return ", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "none"


def _usd(minor: float) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}${abs(minor) / 100:,.2f}"
