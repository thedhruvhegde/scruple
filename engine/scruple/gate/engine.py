"""Promotion engine: shadow replay, paired comparison, regression guards, canary.

The engine is pure: it takes rules and config and returns a verdict. It never
changes status or writes anything. scruple.service applies verdicts through the
registry so that every effect is audited in one place.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from scruple.hashing import sha256_hex
from scruple.registry.models import RuleVersion
from scruple.sim.engine import RunResult, Simulator
from scruple.sim.generator import generate_cases
from scruple.sim.models import DisputeCase
from scruple.sim.policy import Policy, PolicyRule
from scruple.verifiers.base import Finding, new_findings
from scruple.verifiers.costs import realized_costs
from scruple.verifiers.ledger import verify_ledger
from scruple.verifiers.outcome import OutcomeScores, score_outcomes
from scruple.verifiers.policy_constraints import verify_policy

from .config import GateConfig
from .evalue import EValueEvidence, evaluate_evalue
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
from .sequential import SequentialCanaryResult, describe, observations, sequential_checks
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
    ev: EValueEvidence | None = None
    if cfg.stats in ("evalue", "evalue_vr"):
        ev = evaluate_evalue(cases, b_out, c_out, cfg)
        # pooled time-uniform CS, flipped to "improvement" (positive = better)
        mean, lo, hi = -ev.pooled_mean_diff, -ev.pooled_ci_high, -ev.pooled_ci_low
        if ev.violations:
            mean = sum(diffs) / len(diffs) if diffs else 0.0
    else:
        mean, lo, hi = paired_bootstrap_ci(
            diffs, cfg.bootstrap_resamples, cfg.confidence, cfg.stats_seed
        )
    slices = _slices(cases, b_out, c_out)
    golden_n, golden_regressions = _golden(baseline, cand_policy)
    conflicts = _conflicts(cases, c_run, candidate, baseline)

    worst = max(slices, key=lambda s: s.delta)
    esc_increase = c_out.escalation_rate - b_out.escalation_rate
    checks: tuple[CheckResult, ...] = (
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
    if ev is not None:
        replaced = {"no_slice_regression", "not_significantly_worse", "significant_improvement"}
        checks = tuple(c for c in checks if c.name not in replaced) + _evalue_checks(ev)
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
        evalue=ev,
    )
    return _verdict(checks, metrics)


def _evalue_checks(ev: EValueEvidence) -> tuple[CheckResult, ...]:
    """The stratified checks that replace the slice and bootstrap checks."""
    flagged = [t for t in ev.types if t.regressed]
    worst = max(ev.types, key=lambda t: t.regression_e_value, default=None)
    if flagged:
        reg_detail = "; ".join(
            f"'{t.case_type}' mean loss change {_usd(t.mean_diff)} per case "
            f"(tolerance {_usd(t.tolerance)}, e-value {t.regression_e_value:.3g})"
            for t in flagged
        )
    elif worst is not None:
        reg_detail = (
            f"no case type flagged; largest regression e-value {worst.regression_e_value:.3g} "
            f"('{worst.case_type}', flags at {worst.e_threshold:.3g} alone)"
        )
    else:
        reg_detail = "no case type has a changed decision"
    if ev.stratified is not None:
        st = ev.stratified
        reg_detail += (
            f"; stratified betting over the family at {st.alpha:g}: e-value {st.e_value:.3g} "
            f"(flags at {st.threshold:.3g})" + (", REGRESSED" if st.rejected else "")
        )
    best = max(ev.types, key=lambda t: t.improvement_e_value, default=None)
    if ev.improved:
        imp_detail = "certified better: " + ", ".join(
            f"'{t.case_type}' {_usd(t.mean_diff)} per case (e-value {t.improvement_e_value:.3g})"
            for t in ev.types
            if t.improved
        )
    elif best is not None:
        imp_detail = (
            f"no case type certified better; largest improvement e-value "
            f"{best.improvement_e_value:.3g} ('{best.case_type}')"
        )
    else:
        imp_detail = "no case type has a changed decision"
    k = len(ev.family)
    short = [t for t in ev.types if t.case_type in ev.insufficient]
    if short:
        size_detail = (
            f"insufficient evidence: held-out set has {ev.n_cases} cases, the plan needs "
            f"{ev.required_total_cases}; "
            + "; ".join(
                f"'{t.case_type}' has {t.n} cases, needs {t.required_n} to detect "
                f"{_usd(t.tolerance + (t.min_detectable_regression or 0.0))} per case"
                for t in short
            )
        )
    else:
        size_detail = (
            f"every tested case type has enough held-out cases ({ev.plan_method}; "
            f"plan needs {ev.required_total_cases or 0} held-out cases, have {ev.n_cases})"
        )
    off = [t.case_type for t in ev.types if t.sd_within_assumption is False]
    return (
        _check(
            "evalue_declared_caps",
            "hard",
            not ev.violations,
            "every loss change within its declared cap"
            if not ev.violations
            else f"{len(ev.violations)} violation(s): {ev.violations[0]}",
        ),
        _check(
            "evalue_no_type_regression",
            "hard",
            not ev.blocks,
            f"e-BH over {k} case type(s) at FDR "
            f"{ev.alpha - (ev.stratified.alpha if ev.stratified else 0.0):g}: {reg_detail}",
        ),
        _check(
            "evalue_sample_size",
            "evidence",
            ev.sufficient,
            size_detail,
        ),
        _check(
            "evalue_improvement_certified",
            "evidence",
            bool(ev.improved),
            f"e-BH over {k} case type(s) at {ev.alpha_certify:g}: {imp_detail}",
        ),
        _check(
            "evalue_planning_assumption",
            "info",
            not off,
            "observed per-case sd within the planning assumption for every tested type"
            if not off
            else "observed sd above the planning assumption (power not guaranteed) for: "
            + ", ".join(off),
        ),
    )


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
    if soft and any(c.name == "evalue_sample_size" for c in soft):
        return Verdict(
            Outcome.HOLD,
            "Insufficient evidence: the held-out set is too small for the e-value gate.",
            tuple(f"{c.name}: {c.detail}" for c in soft),
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
    if metrics.evalue is not None:
        ev = metrics.evalue
        reasons: tuple[str, ...] = (
            f"certified better (e-BH at {ev.alpha_certify:g}): {', '.join(ev.improved)}",
            f"no case type regressed (e-BH over {len(ev.family)} types at FDR {ev.alpha:g})",
            "all hard checks passed",
        )
    else:
        reasons = (
            f"mean regret improves by {_usd(metrics.mean_improvement_usd_minor)} per case "
            f"(CI [{_usd(metrics.ci_low)}, {_usd(metrics.ci_high)}])",
            "all hard checks passed",
        )
    return Verdict(
        Outcome.PROMOTE,
        "Promoted to canary: improves outcomes with no regressions.",
        reasons,
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
    seq: SequentialCanaryResult | None = None
    cost_checks: tuple[CheckResult, ...]
    if cfg.stats in ("evalue", "evalue_vr"):
        p = int(cfg.canary_fraction * 10_000) / 10_000  # exactly what in_canary uses
        cost_checks, seq = sequential_checks(
            observations(cases, matches, treat, costs, cfg),
            p,
            cfg.evalue_alpha,
            cfg.canary_tolerance_usd_minor,
            centred=cfg.stats == "evalue_vr",
        )
    else:
        cost_checks = (
            _check(
                "canary_cost_within_tolerance",
                "hard",
                within or not enough,
                f"treatment mean cost {_usd(t_mean)} vs control {_usd(c_mean)} "
                f"(tolerance {_usd(cfg.canary_tolerance_usd_minor)})",
            ),
        )
    checks = (
        _check(
            "canary_no_hard_violations",
            "hard",
            not hard,
            f"{len(hard)} ledger/policy violations on canary cases",
        ),
        *cost_checks,
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
        sequential=seq,
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
    if seq is not None:
        early = seq.decision == "benefit"
        return Verdict(
            Outcome.CANARY_PASSED,
            "Canary passed early: candidate certified cheaper than control."
            if early
            else "Canary passed: ready for human approval.",
            (describe(seq), "no hard violations"),
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
