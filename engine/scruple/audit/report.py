"""Markdown evidence reports, written for a model-risk reviewer, not an engineer."""

from __future__ import annotations

from collections.abc import Callable

from scruple.gate.evalue import EValueEvidence
from scruple.gate.models import Decision, DecisionRecord, Outcome, Stage

_VERDICT = {
    Outcome.PROMOTE: "PROMOTED TO CANARY",
    Outcome.HOLD: "HELD (insufficient evidence)",
    Outcome.BLOCK: "BLOCKED",
    Outcome.CANARY_PASSED: "CANARY PASSED (awaiting human approval)",
    Outcome.ROLLBACK: "AUTOMATICALLY ROLLED BACK",
    Outcome.ACTIVATED: "ACTIVATED",
    Outcome.ROLLED_BACK: "ROLLED BACK",
    Outcome.HEALTHY: "HEALTHY",
}

_SCENARIO_NOTES = {
    "fraud": "fraud claims",
    "not_received": "item not received",
    "not_as_described": "item not as described",
    "partial_refund": "partial refund already issued (edge case)",
    "late_evidence": "evidence arrives after the deadline (edge case)",
    "duplicate_dispute": "second dispute on the same transaction (edge case)",
    "currency_mismatch": "dispute and settlement currencies differ (edge case)",
    "high_value": "over $10,000 (edge case)",
}


def usd(minor: float) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}${abs(minor) / 100:,.2f}"


def render_report(rec: DecisionRecord) -> str:
    d = rec.decision
    out: list[str] = []
    w = out.append
    w(f"# Evidence report {d.decision_id}: {_VERDICT[d.outcome]}")
    w("")
    w(f"- **Rule:** `{d.rule_id}` version {d.version} (content hash `{d.content_hash[:16]}`)")
    w(f"- **Stage:** {d.stage.value}")
    w(f"- **Recorded:** {d.created_at.isoformat()} by `{d.actor}`")
    w("- **Data:** synthetic simulator data only. No customer data.")
    w("")
    w("## Decision")
    w("")
    w(d.summary)
    w("")
    for r in d.reasons:
        w(f"- {r}")
    w("")
    _what_changed(d, w)
    if d.shadow is not None:
        _shadow(d, w)
    if d.canary is not None:
        _canary(d, w)
    if d.checks:
        w("## Checks")
        w("")
        w("| Check | Type | Result | Detail |")
        w("|---|---|---|---|")
        for c in d.checks:
            result = "pass" if c.passed else "**FAIL**"
            w(f"| {c.name} | {c.kind} | {result} | {c.detail} |")
        w("")
        w("Hard checks block promotion. Evidence checks hold it until there is more data.")
        w("")
    _who(d, w)
    w("## Limits of this evidence")
    w("")
    w(
        "- Results come from a synthetic simulator. The simulator's fidelity to real "
        "dispute outcomes has not been validated (docs/ASSUMPTIONS.md)."
    )
    w(
        "- Known verifier weaknesses are listed in docs/VERIFIER_FINDINGS.md, including "
        "no limit on repeated submissions (G-1) and dependence on historical data in shadow "
        "replay (G-2)."
    )
    w("- Thresholds are illustrative defaults, not calibrated risk appetite.")
    w("")
    w("## Audit anchor")
    w("")
    w(f"- Decision SHA-256: `{rec.decision_sha256}`")
    w(f"- Audit log entry: seq {rec.audit_seq}, hash `{rec.audit_entry_hash}`")
    w("- Verify with: `agent-gate audit verify`")
    w("")
    return "\n".join(out)


def _what_changed(d: Decision, write: Callable[[str], None]) -> None:
    write("## What changed")
    write("")
    write(f"- **Description (written by the agent):** {d.rule_description}")
    write(f"- **Exact logic:** `{d.rule_logic}`")
    write(f"- **Owner:** `{d.owner}`. **Learned by:** `{d.agent_id}` from {d.episodes} episode(s).")
    write("- Agent-reported metrics, if any, are recorded but never used in this decision.")
    if d.shadow is not None and d.shadow.conflicts:
        write("")
        write("This rule **overrides existing rules** on held-out cases:")
        write("")
        write("| Existing rule | Its action | Cases overridden |")
        write("|---|---|---|")
        for c in d.shadow.conflicts:
            write(f"| `{c.overridden_rule}` | {c.overridden_action} | {c.cases} |")
    write("")


def _shadow(d: Decision, write: Callable[[str], None]) -> None:
    s = d.shadow
    assert s is not None
    write("## What was tested")
    write("")
    write(
        f"- **Shadow replay:** the current policy and the policy with this rule both "
        f"processed the same {s.n_cases:,} held-out historical cases (profile "
        f"`{s.profile}`, seed commitment `{s.eval_seed_commitment}`; the seed is held "
        f"by the gate operator, so the agent cannot regenerate these cases)."
    )
    write(
        f"- The rule decided {s.matched_cases:,} of those cases and changed the decision "
        f"on {s.affected_cases:,}."
    )
    write(
        f"- **Golden regression suite:** {s.golden_cases} hand-written edge cases with the "
        f"actions a policy owner allows."
    )
    write(
        "- **Verifiers:** ledger correctness (journal postings), dispute outcome (ground "
        "truth plus ledger), policy constraints (per case). Each verifier has an "
        "adversarial test suite (`agent-gate redteam`)."
    )
    write(f"- **Baseline policy:** {', '.join(f'`{r}`' for r in s.baseline_rules)}")
    write("")
    write("## Scores")
    write("")
    write(
        "Regret is the money lost per case compared with the best decision in hindsight. "
        "Lower is better."
    )
    write("")
    write("| Metric | Current policy | With this rule |")
    write("|---|---|---|")
    b, c = s.baseline, s.candidate
    write(
        f"| Mean regret per case | {usd(b.mean_regret_usd_minor)} | "
        f"{usd(c.mean_regret_usd_minor)} |"
    )
    write(f"| Cases resolved optimally | {b.accuracy:.1%} | {c.accuracy:.1%} |")
    write(f"| Escalated to humans | {b.escalation_rate:.1%} | {c.escalation_rate:.1%} |")
    write(f"| Ledger violations | {b.ledger_findings} | {c.ledger_findings} |")
    write(f"| Policy violations | {b.policy_findings} | {c.policy_findings} |")
    write("")
    interval = "bootstrap confidence interval" if s.evalue is None else "confidence sequence"
    write(
        f"Paired improvement: **{usd(s.mean_improvement_usd_minor)} per case**, "
        f"{s.confidence:.0%} {interval} "
        f"[{usd(s.ci_low)}, {usd(s.ci_high)}]."
    )
    write("")
    if s.evalue is not None:
        _evalue(s.evalue, write)
    write("### Results by case type")
    write("")
    write("An average can hide a broken edge case, so every case type is checked separately.")
    write("")
    write("| Case type | Cases | Current | With rule | Change | Decisions changed |")
    write("|---|---|---|---|---|---|")
    for sl in s.slices:
        flag = " **WORSE**" if sl.delta > 0 else ""
        write(
            f"| {_SCENARIO_NOTES.get(sl.scenario, sl.scenario)} | {sl.n} | "
            f"{usd(sl.baseline_mean_regret)} | {usd(sl.candidate_mean_regret)} | "
            f"{usd(sl.delta)}{flag} | {sl.changed_cases} |"
        )
    write("")
    if s.new_policy_findings or s.new_ledger_findings or s.golden_regressions:
        write("### Examples of what broke")
        write("")
        for f in (*s.new_policy_findings[:5], *s.new_ledger_findings[:5]):
            write(f"- `{f.case_id}`: {f.kind}: {f.detail}")
        for g in s.golden_regressions:
            write(f"- golden case `{g}` regressed")
        write("")


def _canary(d: Decision, write: Callable[[str], None]) -> None:
    c = d.canary
    assert c is not None
    write("## Canary")
    write("")
    write(
        f"- {c.fraction:.0%} of {c.live_cases:,} simulated live cases (profile "
        f"`{c.profile}`) were routed to the new policy by a deterministic hash split."
    )
    write(
        f"- Cases this rule decides: {c.matched_treatment} on the new policy, "
        f"{c.matched_control} comparable cases on the current policy."
    )
    write(
        f"- Realized mean cost: new policy {usd(c.treatment_mean_cost_usd_minor)}, "
        f"current policy {usd(c.control_mean_cost_usd_minor)}."
    )
    write(f"- Hard violations on canary cases: {len(c.new_hard_findings)}.")
    if c.sequential is not None:
        q = c.sequential
        write(
            f"- **Sequential two-sample test** ({q.method}; arms are matched groups, so "
            f"each case is weighted by its known assignment probability p = "
            f"{q.assignment_probability:g}): decision **{q.decision.replace('_', ' ')}** "
            f"after {q.stopped_at} of {q.n_matched} matched cases, alpha {q.alpha:g}, "
            f"tolerance {usd(q.tolerance_usd_minor)} per case."
        )
        write("")
        write(
            "| Case type | Cases | Treated | Effect per case | Confidence sequence "
            "| e(harm) | e(benefit) |"
        )
        write("|---|---|---|---|---|---|---|")
        for t in q.types:
            write(
                f"| {t.case_type} | {t.n} | {t.treated} | {usd(t.effect_estimate)} | "
                f"[{usd(t.ci_low)}, {usd(t.ci_high)}] | {t.harm_e_value:.3g} | "
                f"{t.benefit_e_value:.3g} |"
            )
    write("")


def _evalue(ev: EValueEvidence, write: Callable[[str], None]) -> None:
    write("### Anytime-valid statistics (e-values)")
    write("")
    write(
        f"Method: {ev.method}. Case types are scenario by amount band, each with a "
        f"declared cost cap. A type is flagged as worse when e-BH over the "
        f"{len(ev.family)} tested type(s) rejects at FDR {ev.alpha:g}, and certified "
        f"better at {ev.alpha_certify:g}. Each type gets an equal share of the error "
        "budget (alpha share); the plan says how many cases each type needs to detect "
        "the minimum detectable regression with power "
        f"{1 - (ev.beta or 0.0):.0%}. Confidence sequences are two-sided, time-uniform "
        f"at {ev.alpha:g} and use their own, more conservative bets, so one can include "
        "zero while the one-sided e-value test already decides."
    )
    write("")
    sample = "sufficient" if ev.sufficient else "**INSUFFICIENT EVIDENCE**"
    write(
        f"- Sample size: plan needs {ev.required_total_cases or 0:,} held-out cases, "
        f"have {ev.n_cases:,}: {sample}."
    )
    write(
        f"- Pooled (information only): confidence sequence for the mean loss change "
        f"[{usd(ev.pooled_ci_low)}, {usd(ev.pooled_ci_high)}], certificate e-value "
        f"{ev.pooled_certify_e_value:.3g}."
    )
    if ev.violations:
        write(f"- Declared-cap violations: {len(ev.violations)} (e.g. {ev.violations[0]}).")
    write("")
    write(
        "| Case type | Cases | Needs | Cap | Change per case | Confidence sequence | "
        "e(worse) | e(better) | Alpha share | Result |"
    )
    write("|---|---|---|---|---|---|---|---|---|---|")
    for t in ev.types:
        flag = "**REGRESSED**" if t.regressed else ("improved" if t.improved else "no call")
        write(
            f"| {t.case_type} | {t.n} | {t.required_n} | {usd(t.cap)} | {usd(t.mean_diff)} | "
            f"[{usd(t.ci_low)}, {usd(t.ci_high)}] | {t.regression_e_value:.3g} | "
            f"{t.improvement_e_value:.3g} | {t.alpha_share:.4g} | {flag} |"
        )
    write("")


def _who(d: Decision, write: Callable[[str], None]) -> None:
    write("## Who can approve or roll back")
    write("")
    approvers = ", ".join(f"`{a}`" for a in d.approvers) or "(none configured)"
    write(f"- **Approvers for activation and rollback:** {approvers}.")
    write(
        f"- The rule's owner (`{d.owner}`) and the agent that learned it (`{d.agent_id}`) "
        "cannot approve it (segregation of duties)."
    )
    if d.outcome is Outcome.CANARY_PASSED:
        write(
            f"- **Next step:** an approver runs `agent-gate approve {d.rule_id} "
            f"{d.version} --approver <id>`."
        )
    if d.stage in (Stage.ACTIVATION, Stage.ROLLBACK) or d.outcome is Outcome.ACTIVATED:
        write(
            f"- **Roll back at any time:** `agent-gate rollback {d.rule_id} "
            f"--actor <approver> --reason <text>`. The previous version is restored "
            "automatically."
        )
    if d.outcome in (Outcome.BLOCK, Outcome.ROLLBACK):
        write(
            "- Nothing went live. A blocked version is terminal; a fix must be "
            "submitted as a new version, which is evaluated from scratch."
        )
    write("")
