"""Write a backtest report as JSON, Markdown and (when fpdf2 is installed) PDF.

All three are views of one `BacktestReport`. Every cost line says whether it is
observed (the candidate did what history did; read from the ledger) or estimated (a
counterfactual from the calibrated simulator or a stated assumption). Output is
deterministic: no wall clock, sorted keys, the PDF dated by the latest case in the data.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

from scruple.backtest.engine import BacktestReport


def to_json(r: BacktestReport) -> str:
    return json.dumps(r.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def _m(x: float) -> str:
    return f"{x:,.0f}"


def _e(x: float) -> str:
    return f"{x:.3g}"


def to_markdown(r: BacktestReport) -> str:
    cur = r.currency
    out: list[str] = [f"# Backtest: {r.candidate}", ""]
    if r.synthetic:
        out += [
            "**SYNTHETIC DATA. Not a customer history; no result here is evidence about "
            "real disputes.**",
            "",
        ]
    out += [
        f"- data: {r.dataset}",
        f"- format: {r.format}; currency: {cur} (amounts in minor units)",
        f"- latest case opened: {r.as_of.isoformat() if r.as_of else 'n/a'}",
        "",
        "## Verdict",
        "",
        f"**{r.verdict}**"
        + (
            ""
            if r.verdict == "no change"
            else (
                " (holds at both ends of the estimate bands)"
                if r.robust
                else " (depends on the estimates: the verdict moves within the estimate bands)"
            )
        ),
        "",
    ]
    out += [f"- {t}" for t in r.reasons_text]
    if r.stats is not None:
        s = r.stats
        out += [
            f"- mean difference per case (candidate - history): {_m(s.mean_diff)} "
            f"[{_m(s.ci_low)}, {_m(s.ci_high)}] (time-uniform, alpha {r.config.alpha_certify:g}); "
            f"certificate e-value {_e(s.certify_e_value)}, pooled regression e-value "
            f"{_e(s.pooled_regression_e_value)}",
        ]
    for name, run in (("pessimistic", r.pessimistic), ("optimistic", r.optimistic)):
        if run is not None:
            out.append(
                f"- {name} end of every estimate: {run.outcome}, mean difference "
                f"{_m(run.mean_diff)} [{_m(run.ci_low)}, {_m(run.ci_high)}]"
            )
    out += [
        "",
        "## Cases",
        "",
        f"- input: {r.n_input}; included (resolved chargebacks in {cur}): {r.n_included}",
        f"- observed (candidate did what history did): {r.n_observed}",
        f"- estimated (counterfactual): {r.n_estimated}",
    ]
    out += [f"- excluded, {k}: {v}" for k, v in r.excluded.items()]
    out += [f"- input records skipped, {k}: {v}" for k, v in r.skipped_input.items()]
    out += [f"- cost source {k}: {v} cases" for k, v in r.cost_source.items()]
    out += [
        f"- ledger: merchant net {r.ledger_merchant_net:,} over all postings in {cur}; "
        f"observed costs {'reconcile' if r.ledger_reconciled else 'DO NOT reconcile'} with it",
        "",
        "## Costs",
        "",
        "| | total |",
        "|---|---:|",
        f"| history (observed) | {_m(r.baseline_cost)} |",
        f"| candidate | {_m(r.candidate_cost)} |",
        f"| of which observed | {_m(r.candidate_cost_observed)} |",
        f"| of which estimated | {_m(r.candidate_cost_estimated)} |",
        f"| candidate, estimate band | {_m(r.candidate_cost_low)} to {_m(r.candidate_cost_high)} |",
        "",
        "## By reason",
        "",
        "| reason | n | observed | estimated | history | candidate observed | candidate "
        "estimated | mean diff [CI] | e regress | e improve | verdict | cases needed |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---:|",
    ]
    for x in r.by_reason:
        need = "n/a" if x.plan.cases_needed is None else f"{x.plan.cases_needed:,}"
        if x.plan.cases_needed is not None and not x.plan.sufficient:
            need += " (too few)"
        out.append(
            f"| {x.reason} | {x.n} | {x.n_observed} | {x.n_estimated} | {_m(x.baseline_cost)} | "
            f"{_m(x.candidate_cost_observed)} | {_m(x.candidate_cost_estimated)} | "
            f"{_m(x.mean_diff)} [{_m(x.ci_low)}, {_m(x.ci_high)}] | {_e(x.regression_e_value)} | "
            f"{_e(x.improvement_e_value)} | {x.verdict} | {need} |"
        )
    out += [
        "",
        f"Worse: e-Bonferroni across reasons plus pooled (FWER {r.config.alpha_block:g}). "
        f"Better: per-reason betting e-processes with e-BH (FDR {r.config.alpha_fdr:g}). "
        "`insufficient evidence` means neither was shown.",
        "",
        r.planning_note,
        "",
    ]
    if r.calibration is not None:
        c = r.calibration
        out += [
            "## Calibration (aggregates only)",
            "",
            f"- profile fitted from suppressed aggregates (k = {c.aggregates.k}) of "
            f"{c.aggregates.n_cases} cases",
            f"- divergence from the history: default generator {c.before.overall:.4f}, "
            f"calibrated {c.after.overall:.4f} ({c.comparison_label})",
            "- not fitted (fallback used): " + (", ".join(c.profile.fallbacks) or "none"),
            "",
        ]
    out += ["## Assumptions", ""] + [f"- {a}" for a in r.assumptions] + [""]
    out += [
        "## Reading this report",
        "",
        "- observed: the candidate took the action that was taken; its cost is the "
        "recorded one (ledger postings when the export has them).",
        "- estimated: the candidate took another action. Accepting: the chargeback stands. "
        "Contesting: the calibrated win rate for the reason. Escalating: the historical "
        "outcome plus the escalation cost. Only historically contested cases have "
        "observed contest outcomes (selection bias).",
        "",
    ]
    return "\n".join(out)


def pdf_available() -> bool:
    return importlib.util.find_spec("fpdf") is not None


def to_pdf(r: BacktestReport) -> bytes:
    """The Markdown report as a plain PDF (core fonts, so Latin-1 only)."""
    from fpdf import FPDF

    def t(text: str) -> str:
        return text.encode("latin-1", "replace").decode("latin-1")

    doc = FPDF(format="A4")
    doc.set_creation_date(r.as_of or datetime(1970, 1, 1, tzinfo=UTC))
    doc.set_title(t(f"Backtest: {r.candidate}"))
    doc.set_author("scruple")
    doc.set_auto_page_break(auto=True, margin=15)
    doc.add_page()
    width = doc.w - doc.l_margin - doc.r_margin
    for raw in to_markdown(r).splitlines():
        doc.set_x(doc.l_margin)
        if raw.startswith("# "):
            doc.set_font("Helvetica", style="B", size=14)
            doc.multi_cell(width, 8, t(raw[2:]))
        elif raw.startswith("## "):
            doc.ln(2)
            doc.set_font("Helvetica", style="B", size=11)
            doc.multi_cell(width, 6, t(raw[3:]))
        elif raw.startswith("|"):
            if set(raw) <= set("|-: "):
                continue
            doc.set_font("Courier", size=6)
            doc.multi_cell(width, 3.5, t(raw))
        elif raw:
            doc.set_font("Helvetica", size=8)
            doc.multi_cell(width, 4.5, t(raw.replace("**", "")))
    return bytes(doc.output())


def write_report(r: BacktestReport, out_dir: Path, *, pdf: bool | None = None) -> list[Path]:
    """Write report.json, report.md and (if available, or forced) report.pdf."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [out_dir / "report.json", out_dir / "report.md"]
    files[0].write_text(to_json(r), encoding="utf-8")
    files[1].write_text(to_markdown(r), encoding="utf-8")
    if pdf if pdf is not None else pdf_available():
        path = out_dir / "report.pdf"
        path.write_bytes(to_pdf(r))
        files.append(path)
    return files
