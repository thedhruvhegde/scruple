"""Render a scorecard as a GitHub-flavoured markdown pull request comment.

Pure and deterministic: the same scorecard always renders to the same bytes
(snapshot-tested). The hidden marker lets the poster update its own comment
instead of adding a new one on every push.
"""

from __future__ import annotations

from scruple.ci.scorecard import Scorecard, Verdict

MARKER = "<!-- scruple-gate -->"

_HEADLINE = {
    Verdict.PASS: "scruple gate: PASS",
    Verdict.HOLD: "scruple gate: HOLD (not enough evidence)",
    Verdict.BLOCK: "scruple gate: BLOCK",
}


def usd(minor: float) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}${abs(minor) / 100:,.2f}"


def _cell(text: str) -> str:
    """Make arbitrary text safe inside a markdown table cell."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def render_comment(card: Scorecard) -> str:
    out: list[str] = []
    w = out.append
    w(MARKER)
    w(f"## {_HEADLINE[card.verdict]}")
    w("")
    w(
        f"Candidate `{card.rule_id}` version {card.version} "
        f"(content hash `{card.content_hash[:16]}`)."
    )
    w("")
    w(f"> {_cell(card.rule_description)}")
    w("")
    w(f"Logic: `{card.rule_logic}`")
    w("")
    w("| Stage | Outcome | Decision | Summary |")
    w("|---|---|---|---|")
    for d in card.decisions:
        w(f"| {d.stage.value} | **{d.outcome.value}** | `{d.decision_id}` | {_cell(d.summary)} |")
    w("")
    w("### Regressions")
    w("")
    if card.regressions:
        w("| Kind | Name | Detail |")
        w("|---|---|---|")
        for r in card.regressions:
            w(f"| {r.kind} | `{_cell(r.name)}` | {_cell(r.detail)} |")
    else:
        w("None found.")
    w("")
    if card.statistics is not None:
        s = card.statistics
        w("### Statistics")
        w("")
        w(
            f"- Mean regret improvement: **{usd(s.mean_improvement_usd_minor)}** per case, "
            f"{s.confidence:.0%} interval [{usd(s.ci_low)}, {usd(s.ci_high)}]"
        )
        w(
            f"- {s.n_cases:,} held-out cases, {s.affected_cases:,} changed decision; "
            f"method `{s.method}`"
        )
        w("")
    if card.evalue is not None:
        ev = card.evalue
        sample = "sufficient" if ev.sufficient else "**insufficient evidence**"
        w("### E-value statistics")
        w("")
        w(
            f"- e-BH over {len(ev.family)} tested case type(s): false-block FDR "
            f"{ev.alpha:g}, certificate {ev.alpha_certify:g}; sample size {sample} "
            f"(plan needs {ev.required_total_cases or 0:,} held-out cases, have "
            f"{ev.n_cases:,})"
        )
        w("")
        w(
            "| Case type | Cases | Needs | Change | Confidence sequence | e(worse) | "
            "e(better) | Alpha share | Result |"
        )
        w("|---|---:|---:|---:|---|---:|---:|---:|---|")
        for t in ev.types:
            flag = "**regressed**" if t.regressed else ("improved" if t.improved else "-")
            w(
                f"| `{_cell(t.case_type)}` | {t.n:,} | {t.required_n or 0:,} | "
                f"{usd(t.mean_diff)} | [{usd(t.ci_low)}, {usd(t.ci_high)}] | "
                f"{t.regression_e_value:.3g} | {t.improvement_e_value:.3g} | "
                f"{t.alpha_share:.4g} | {flag} |"
            )
        w("")
    if card.slices:
        w("<details><summary>Per-scenario regret (minor units per case)</summary>")
        w("")
        w("| Scenario | Cases | Baseline | Candidate | Change | Changed decisions |")
        w("|---|---:|---:|---:|---:|---:|")
        for sl in card.slices:
            flag = " **worse**" if sl.delta > 0 else ""
            w(
                f"| {sl.scenario} | {sl.n:,} | {sl.baseline_mean_regret:,.1f} | "
                f"{sl.candidate_mean_regret:,.1f} | {sl.delta:+,.1f}{flag} | "
                f"{sl.changed_cases:,} |"
            )
        w("")
        w("</details>")
        w("")
    if card.canary is not None:
        c = card.canary
        w("### Canary")
        w("")
        w(
            f"- {c.fraction:.0%} of simulated live traffic: treatment mean cost "
            f"{usd(c.treatment_mean_cost_usd_minor)} vs control "
            f"{usd(c.control_mean_cost_usd_minor)} "
            f"({c.matched_treatment:,} / {c.matched_control:,} matched cases)"
        )
        if card.canary_sequential is not None:
            q = card.canary_sequential
            w(
                f"- Sequential two-sample test (p = {q.assignment_probability:g}, alpha "
                f"{q.alpha:g}): **{q.decision.replace('_', ' ')}** after {q.stopped_at:,} "
                f"of {q.n_matched:,} matched cases"
            )
        w("")
    failed = [c for c in card.checks if not c.passed]
    passed = len(card.checks) - len(failed)
    w(f"<details><summary>Checks ({passed}/{len(card.checks)} passed)</summary>")
    w("")
    w("| Check | Type | Result | Detail |")
    w("|---|---|---|---|")
    for ch in card.checks:
        result = "pass" if ch.passed else "**FAIL**"
        w(f"| {ch.name} | {ch.kind} | {result} | {_cell(ch.detail)} |")
    w("")
    w("</details>")
    w("")
    w(
        "Synthetic simulator data only. Thresholds are illustrative defaults. "
        f"Config `{card.config_sha256[:16]}`, audit head `{card.audit_head[:16]}`."
    )
    return "\n".join(out) + "\n"
