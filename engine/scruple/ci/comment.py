"""Render a scorecard as a GitHub-flavoured markdown pull request comment.

Pure and deterministic: the same scorecard always renders to the same bytes
(snapshot-tested). The hidden marker lets the poster update its own comment
instead of adding a new one on every push.
"""

from __future__ import annotations

from collections.abc import Callable

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
    if card.history is not None:
        _history(w, card)
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
    footer = (
        "Synthetic simulator data only."
        if card.history is None
        else "Synthetic held-out cases, plus the history above (labelled by source)."
    )
    w(
        f"{footer} Thresholds are illustrative defaults. "
        f"Config `{card.config_sha256[:16]}`, audit head `{card.audit_head[:16]}`."
    )
    return "\n".join(out) + "\n"


def _money(minor: float, currency: str) -> str:
    return f"{minor / 100:,.2f} {currency}"


def _history(w: Callable[[str], None], card: Scorecard) -> None:
    h = card.history
    assert h is not None
    syn = card.synthetic_verdict.value.upper() if card.synthetic_verdict else "?"
    tag = " (SYNTHETIC data)" if h.synthetic else ""
    w("### Sources")
    w("")
    w("| Source | Verdict | Cases |")
    w("|---|---|---|")
    stats_n = card.statistics.n_cases if card.statistics else 0
    w(f"| synthetic held-out cases | **{syn}** | {stats_n:,} |")
    w(
        f"| partner history{tag} | **{h.verdict.upper()}** | {h.n_included:,} "
        f"({h.n_observed:,} observed, {h.n_estimated:,} estimated) |"
    )
    w("")
    w(f"### Partner history: {h.verdict.upper()}{tag}")
    w("")
    w(f"Dataset: {_cell(h.dataset)}. Content hash `{h.history_sha256[:16]}`.")
    w("")
    w(f"- Backtest verdict **{h.backtest_verdict}**: {_cell(h.why)}.")
    w(
        f"- Cost, history (observed): {_money(h.baseline_cost, h.currency)}; candidate: "
        f"{_money(h.candidate_cost, h.currency)}, of which observed "
        f"{_money(h.candidate_cost_observed, h.currency)} and estimated "
        f"{_money(h.candidate_cost_estimated, h.currency)} (estimate band "
        f"{_money(h.candidate_cost_low, h.currency)} to "
        f"{_money(h.candidate_cost_high, h.currency)})"
    )
    if h.mean_diff is not None and h.ci_low is not None and h.ci_high is not None:
        w(
            f"- Mean change per case {_money(h.mean_diff, h.currency)}, anytime-valid "
            f"interval [{_money(h.ci_low, h.currency)}, {_money(h.ci_high, h.currency)}]"
        )
    w(f"- Method: {_cell(h.method)}")
    w(f"- Estimated costs from: {_cell(h.profile)}")
    if h.rule_fields_defaulted:
        shown = ", ".join(f"{k} ({v})" for k, v in h.rule_fields_defaulted.items())
        w(f"- Rule fields not in the export, default used (cases): {_cell(shown)}")
    w("")
    if h.by_reason:
        w("| Reason | Cases | Observed | Estimated | Verdict | Cases needed |")
        w("|---|---:|---:|---:|---|---:|")
        for r in h.by_reason:
            need = "-" if r.verdict == "no change" else f"{r.cases_needed or 0:,}"
            if r.verdict != "no change" and not r.sufficient:
                need += " (insufficient)"
            w(
                f"| `{_cell(r.reason)}` | {r.n:,} | {r.n_observed:,} | {r.n_estimated:,} | "
                f"{r.verdict} | {need} |"
            )
        w("")
