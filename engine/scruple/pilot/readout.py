"""`scruple pilot readout`: docs/pilot/READOUT_TEMPLATE.md filled from a pilot run.

    scruple pilot readout --run pilot/ --criteria criteria.json --out readout/

Reads only the files `scruple pilot run` wrote (manifest.json and the step outputs)
and a pre-registered criteria file in the shape of docs/pilot/SUCCESS_CRITERIA.md.
Writes readout.md, readout.pdf and readout.json. Deterministic: no wall clock, no
absolute paths, the PDF creation date fixed. Every criterion gets one verdict, pass,
fail or "not enough data", computed by the rule below; nothing is blended. Fields
only a person can fill (names, signatures, deletion dates, the outcome model stated
in week 1) stay as `[...]` placeholders.

Criteria (`metric`, then what passes):

- `planted_blocked` (H1): each plant is its own pilot run (a defective candidate on
  the same history). Pass when every plant's gate failed the check (exit code not 0:
  block, or hold with fail_on_hold) and, where the plant names a reason code, the
  history backtest of that plant reads "worse" in that code. A plant that could not
  be gated fails.
- `new_hard_violations` (H2): action findings of the candidate above production's in
  shadow mode on the imported cases; pass when at most `max`.
- `self_report_gap` (H3): the partner's reported win rate per reason code against
  the win rate among contested cases in the history (calibration aggregates,
  suppressed below k), with a 95% Wilson interval. Pass when every reason code with
  at least `min_cases` contested cases has a reported figure and a measured rate;
  not enough data when no reported figures were supplied or no code reaches
  `min_cases`. A large or small gap is a finding, not a failure.
- `identical_reruns` (H4): reruns of the same pilot run (other directories). Pass
  when at least `required` reruns exist and every one has the same scorecard sha256
  and audit head as the main run; not enough data when fewer were supplied.
- `improvement_every_reason` (H5, optional): the backtest's per-reason verdict.
  Fail if any listed code is "worse" or "no change"; pass when every one is
  "better"; not enough data when a code is short of its planned sample size.
"""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.pilot.run import MANIFEST, PilotManifest

Verdict = Literal["pass", "fail", "not enough data"]
_FROZEN = ConfigDict(frozen=True, extra="forbid")
Z95 = 1.959963984540054


class Plant(BaseModel):
    model_config = _FROZEN

    id: str = Field(min_length=1, max_length=64)
    run: str  # pilot run directory, relative to the criteria file
    defect: str = ""
    reason: str | None = None  # reason code it should hurt (Stripe reason, as on the report)


class _Base(BaseModel):
    model_config = _FROZEN

    id: str = Field(min_length=1, max_length=16)
    hypothesis: str = Field(default="", max_length=500)


class PlantedBlocked(_Base):
    metric: Literal["planted_blocked"]
    plants: tuple[Plant, ...] = Field(min_length=1)


class NewHardViolations(_Base):
    metric: Literal["new_hard_violations"]
    max: int = Field(default=0, ge=0)


class SelfReportGap(_Base):
    metric: Literal["self_report_gap"]
    min_cases: int = Field(default=30, ge=1)
    reported: dict[str, float] = {}  # reason -> partner-reported win rate in [0, 1]
    reported_metric: str = "win rate (partner dashboard)"


class IdenticalReruns(_Base):
    metric: Literal["identical_reruns"]
    reruns: tuple[str, ...] = ()
    required: int = Field(default=3, ge=1)


class ImprovementEveryReason(_Base):
    metric: Literal["improvement_every_reason"]
    reasons: tuple[str, ...] | None = None  # None: every reason the candidate changes


Criterion = Annotated[
    PlantedBlocked | NewHardViolations | SelfReportGap | IdenticalReruns | ImprovementEveryReason,
    Field(discriminator="metric"),
]


class Criteria(BaseModel):
    model_config = _FROZEN

    partner: str = "[PARTNER]"
    period: str = "[start] to [end]"
    signed: str = "[date]"
    prepared_by: str = "[names, roles]"
    criteria: tuple[Criterion, ...] = Field(min_length=1)


class Result(BaseModel):
    model_config = _FROZEN

    id: str
    metric: str
    bar: str
    result: str
    verdict: Verdict
    source: str


class Readout(BaseModel):
    model_config = _FROZEN

    kind: Literal["scruple.pilot.readout"] = "scruple.pilot.readout"
    data: str
    criteria_sha256: str
    manifest_sha256: str
    results: tuple[Result, ...]
    markdown: str

    def ids(self, verdict: Verdict) -> list[str]:
        return [r.id for r in self.results if r.verdict == verdict]


def load_criteria(path: Path) -> Criteria:
    text = path.read_text(encoding="utf-8")
    raw = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
    return Criteria.model_validate(raw)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _RunView:
    def __init__(self, run: Path) -> None:
        self.dir = run
        self.manifest = PilotManifest.model_validate_json((run / MANIFEST).read_text("utf-8"))

    def json(self, rel: str) -> Any:
        p = self.dir / rel
        return json.loads(p.read_text("utf-8")) if p.is_file() else None

    def backtest(self) -> Any:
        return self.json("03-backtest/report.json")

    def scorecard(self) -> Any:
        return self.json("05-gate/scorecard.json")

    def scorecard_sha(self) -> str | None:
        return self.manifest.step("gate").outputs.get("05-gate/scorecard.json")


def _wilson(k: int, n: int) -> tuple[float, float]:
    p = k / n
    den = 1 + Z95**2 / n
    mid = (p + Z95**2 / (2 * n)) / den
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def _planted(c: PlantedBlocked, base: Path) -> Result:
    bits, ok = [], True
    for p in c.plants:
        v = _RunView(base / p.run)
        g = v.manifest.step("gate")
        if g.status != "ok":
            ok = False
            bits.append(f"{p.id}: not gated ({g.why})")
            continue
        blocked = int(g.summary["exit_code"]) != 0
        where = "check"
        if p.reason is not None:
            rows = {r["reason"]: r for r in (v.backtest() or {}).get("by_reason", [])}
            flagged = rows.get(p.reason, {}).get("verdict") == "worse"
            where = f"{p.reason} {'worse' if flagged else 'not flagged'}"
            blocked = blocked and flagged
        ok = ok and blocked
        bits.append(f"{p.id}: {g.summary['verdict']} ({where})")
    n_ok = sum(1 for b in bits if "not gated" not in b and "not flagged" not in b)
    return Result(
        id=c.id,
        metric=c.metric,
        bar=f"all {len(c.plants)} blocked, each in its reason code",
        result=f"{n_ok} of {len(c.plants)}: " + "; ".join(bits),
        verdict="pass" if ok else "fail",
        source="section 5",
    )


def _violations(c: NewHardViolations, run: _RunView) -> Result:
    s = run.manifest.step("shadow").summary
    n = int(s["candidate_new_hard_findings"])
    return Result(
        id=c.id,
        metric=c.metric,
        bar=f"at most {c.max} new hard violations",
        result=f"{n} new in shadow mode over {s['decisions']} imported decisions",
        verdict="pass" if n <= c.max else "fail",
        source="sections 4, 7",
    )


def _gap(c: SelfReportGap, run: _RunView) -> tuple[Result, list[str]]:
    cal = run.json("02-calibration/calibration.json") or {}
    wins: dict[str, Any] = cal.get("aggregates", {}).get("win_by_reason", {})
    lines, missing, measured = [], [], 0
    for reason in sorted(wins):
        w = wins[reason]
        k, n = int(w["successes"]), int(w["trials"])
        if n < c.min_cases:
            continue
        measured += 1
        lo, hi = _wilson(k, n)
        rep = c.reported.get(reason)
        if rep is None:
            missing.append(reason)
            lines.append(f"{reason}: history {k / n:.3f} [{lo:.3f}, {hi:.3f}] n={n}; no figure")
            continue
        lines.append(
            f"{reason}: reported {rep:.3f}, history {k / n:.3f} [{lo:.3f}, {hi:.3f}] n={n}, "
            f"gap {rep - k / n:+.3f}"
        )
    verdict: Verdict
    if not c.reported or measured == 0:
        verdict = "not enough data"
        text = (
            "no partner-reported figures supplied"
            if not c.reported
            else f"no reason code has {c.min_cases} contested cases"
        )
    else:
        verdict = "fail" if missing else "pass"
        text = f"{measured} reason codes measured" + (
            f"; no reported figure for {', '.join(missing)}" if missing else ""
        )
    return Result(
        id=c.id,
        metric=c.metric,
        bar=f"gap with a 95% interval for every reason code with at least {c.min_cases} cases",
        result=text,
        verdict=verdict,
        source="section 4",
    ), lines


def _reruns(c: IdenticalReruns, run: _RunView, base: Path) -> Result:
    ref = (run.scorecard_sha(), run.manifest.step("gate").summary.get("audit_head"))
    same = 0
    for d in c.reruns:
        v = _RunView(base / d)
        if (v.scorecard_sha(), v.manifest.step("gate").summary.get("audit_head")) == ref:
            same += 1
    verdict: Verdict
    if ref[0] is None:
        verdict, text = "not enough data", "the main run was not gated"
    elif len(c.reruns) < c.required:
        verdict = "not enough data"
        text = f"{len(c.reruns)} reruns supplied, {c.required} registered"
    else:
        verdict = "pass" if same == len(c.reruns) else "fail"
        text = f"{same} of {len(c.reruns)} identical (scorecard sha256 {ref[0][:12]})"
    return Result(
        id=c.id,
        metric=c.metric,
        bar=f"identical on {c.required} reruns",
        result=text,
        verdict=verdict,
        source="section 6",
    )


def _improvement(c: ImprovementEveryReason, run: _RunView) -> Result:
    rows = {r["reason"]: r for r in (run.backtest() or {}).get("by_reason", [])}
    names = list(c.reasons) if c.reasons is not None else [
        k for k, r in sorted(rows.items()) if r["verdict"] != "no change"
    ]  # fmt: skip
    verdict: Verdict = "pass"
    bits = []
    for name in names:
        r = rows.get(name)
        if r is None:
            bits.append(f"{name}: no cases")
            verdict = "fail"
            continue
        bits.append(f"{name}: {r['verdict']} (e = {r['improvement_e_value']:.3g}, n={r['n']})")
        if r["verdict"] in ("worse", "no change"):
            verdict = "fail"
        elif r["verdict"] != "better" and verdict == "pass":
            verdict = "not enough data" if not r["plan"]["sufficient"] else "fail"
    if not names:
        verdict, bits = "fail", ["the candidate changes no reason code"]
    return Result(
        id=c.id,
        metric=c.metric,
        bar="'better' (e-BH, FDR 0.1) in every reason code",
        result="; ".join(bits),
        verdict=verdict,
        source="section 4",
    )


def _money(v: float | None, ccy: str) -> str:
    return "-" if v is None else f"{v / 100:,.2f} {ccy}"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]
    return out


def build_readout(run_dir: Path, criteria_path: Path) -> Readout:
    run = _RunView(run_dir)
    crit = load_criteria(criteria_path)
    base = criteria_path.parent
    m = run.manifest
    results: list[Result] = []
    gap_lines: list[str] = []
    for c in crit.criteria:
        if isinstance(c, PlantedBlocked):
            results.append(_planted(c, base))
        elif isinstance(c, NewHardViolations):
            results.append(_violations(c, run))
        elif isinstance(c, SelfReportGap):
            res, gap_lines = _gap(c, run)
            results.append(res)
        elif isinstance(c, IdenticalReruns):
            results.append(_reruns(c, run, base))
        else:
            results.append(_improvement(c, run))
    synthetic = m.data == "synthetic"
    tag = " (SYNTHETIC DATA)" if synthetic else ""
    md: list[str] = [f"# Pilot readout: {crit.partner}, card disputes{tag}", ""]
    if synthetic:
        md += [
            "Every number below comes from a SYNTHETIC history. It shows what the readout",
            "looks like; it is not a result about any partner.",
            "",
        ]
    md += _table(
        ["", ""],
        [
            ["partner", crit.partner],
            ["period", crit.period],
            ["scruple version", m.scruple_version],
            ["criteria document", f"signed {crit.signed}, sha256 {_sha(criteria_path)}"],
            ["pilot run", f"manifest sha256 {_sha(run_dir / MANIFEST)}, run key {m.run_key[:16]}"],
            ["data", f"{m.data}: {m.history['label']}"],
            ["candidate", f"{m.candidate['name']} (sha256 {m.candidate['sha256'][:16]})"],
            ["prepared by / reviewed by", crit.prepared_by],
        ],
    )
    passed = [r.id for r in results if r.verdict == "pass"]
    failed = [r.id for r in results if r.verdict == "fail"]
    short = [r.id for r in results if r.verdict == "not enough data"]
    md += [
        "",
        "## 1. Summary",
        "",
        f"- Criteria passed: {', '.join(passed) or 'none'}. Failed: {', '.join(failed) or 'none'}."
        f" Not enough data: {', '.join(short) or 'none'}.",
        "- Most important finding: `[one number and the section it comes from]`.",
        "- Next step: `[recommendation, or no recommendation]`.",
        "",
        "## 2. Results against the pre-registered criteria",
        "",
    ]
    md += _table(
        ["id", "pass bar (as registered)", "result", "verdict", "source"],
        [[r.id, r.bar, r.result, r.verdict, r.source] for r in results],
    )
    md += ["", *_data_section(run), *_backtest_section(run, gap_lines)]
    md += [*_gate_section(run), *_ci_section(results), *_shadow_section(run)]
    md += _evidence_section(run)
    md += _limits_section(run, results)
    text = "\n".join(md) + "\n"
    return Readout(
        data=m.data,
        criteria_sha256=_sha(criteria_path),
        manifest_sha256=_sha(run_dir / MANIFEST),
        results=tuple(results),
        markdown=text,
    )


def _data_section(run: _RunView) -> list[str]:
    ing = run.json("01-ingest/ingest.json")
    ver = run.json("01-ingest/verify.json")
    out = ["## 3. Data received", ""]
    if ing is None or ver is None:
        return [*out, f"Ingest step: {run.manifest.step('ingest').why}.", ""]
    errors: dict[str, int] = {}
    for e in ing.get("errors", []):
        errors[e["message"]] = errors.get(e["message"], 0) + 1
    rows = [
        ["source", f"{ing['source']}; label {ing['label']}; input sha256 {ing['input_sha256']}"],
        ["records read", str(ing["rows_read"])],
        [
            "cases stored",
            f"{ing['cases']} ({ing['cases_new']} new, {ing['cases_updated']} updated)",
        ],
        ["records rejected", f"{ing['rows_failed']}" + (f": {errors}" if errors else "")],
        ["reason-code mix", json.dumps(ver["by_reason"]["cells"], sort_keys=True)],
        ["resolved share", json.dumps(ver["by_state"]["cells"], sort_keys=True)],
        [
            "PII controls applied",
            f"{sum(ing['redactions'].values())} redactions, {ing['pans_tokenised']} card "
            f"tokens, {ing['fields_encrypted']} encrypted fields",
        ],
        ["store check", f"{ver['raw_pans_found']} card numbers in the store (must be 0)"],
    ]
    return [*out, *_table(["field", "value"], rows), ""]


def _backtest_section(run: _RunView, gap_lines: list[str]) -> list[str]:
    b = run.backtest()
    out = ["## 4. Backtest on history", ""]
    if b is None:
        return [*out, "No backtest output.", ""]
    ccy = b["currency"]
    out += [
        f"Verdict: **{b['verdict']}** (robust to the estimate band: {b['robust']}); "
        f"{b['n_included']} cases ({b['n_observed']} observed, {b['n_estimated']} estimated). "
        f"Dataset: {b['dataset']}.",
        "",
    ]
    rows = [
        [
            r["reason"],
            str(r["n"]),
            str(r["n_observed"]),
            str(r["n_estimated"]),
            _money(r["baseline_cost"], ccy),
            _money(r["candidate_cost_observed"], ccy),
            f"{_money(r['candidate_cost_estimated'], ccy)} ({_money(r['candidate_cost_low'], ccy)}"
            f" to {_money(r['candidate_cost_high'], ccy)})",
            r["verdict"],
            str(r["plan"]["cases_needed"]),
        ]
        for r in b["by_reason"]
    ]
    out += _table(
        [
            "reason code",
            "cases",
            "observed",
            "estimated",
            "cost, history",
            "candidate, observed part",
            "candidate, estimated part (band)",
            "verdict",
            "cases needed",
        ],
        rows,
    )
    defaulted = b.get("rule_fields_defaulted") or {}
    out += [
        "",
        f"- Rule fields the export did not carry (cases that used a default): "
        f"{json.dumps(defaulted, sort_keys=True) if defaulted else 'none'}.",
        "- Outcome model used for changed decisions: `[stated in week 1]`. These costs are "
        "model-based, not measured.",
        f"- Ledger reconciled with observed costs: {b['ledger_reconciled']}.",
    ]
    if gap_lines:
        out += ["- Self-report gap (H3), win rate among contested cases, 95% Wilson interval:"]
        out += [f"  - {line}" for line in gap_lines]
    return [*out, ""]


def _gate_section(run: _RunView) -> list[str]:
    s = run.scorecard()
    out = ["## 5. Gate results", ""]
    if s is None:
        return [*out, f"Not run: {run.manifest.step('gate').why}.", ""]
    h = s.get("history") or {}
    failed = [f"{c['name']}: {c.get('detail', '')}" for c in s["checks"] if not c["passed"]]
    rows = [
        ["candidate", f"{s['rule_id']} v{s['version']}, content hash {s['content_hash']}"],
        ["logic", s["rule_logic"]],
        ["verdict and exit code", f"{s['verdict']}, {s['exit_code']}"],
        ["data", s["data"]],
        [
            "sources",
            f"synthetic {s.get('synthetic_verdict')}; partner history {h.get('verdict')} "
            f"({h.get('backtest_verdict')}: {h.get('why')}), {h.get('n_observed')} observed / "
            f"{h.get('n_estimated')} estimated, history content hash {h.get('history_sha256')}",
        ],
        ["failed checks", "; ".join(failed) or "none"],
        ["regressions", "; ".join(f"{r['kind']}: {r['name']}" for r in s["regressions"]) or "none"],
        ["config and audit", f"config sha256 {s['config_sha256']}, audit head {s['audit_head']}"],
    ]
    return [*out, *_table(["field", "value"], rows), ""]


def _ci_section(results: list[Result]) -> list[str]:
    h4 = [r for r in results if r.metric == "identical_reruns"]
    repro = f"{h4[0].result} ({h4[0].verdict})" if h4 else "`[not registered]`"
    rows = [
        ["repository and workflow", "`[repo]`, `.github/workflows/scruple.yml`"],
        ["required check", "`scruple/gate`"],
        ["pull requests gated", "`[n]`; verdicts: `[pass / hold / block counts]`"],
        ["reproducibility (H4)", repro],
    ]
    return [
        "## 6. Gate in the partner's CI",
        "",
        *_table(["field", "value"], rows),
        "",
        "The gate in section 5 used synthetic held-out cases and the partner history, "
        "shown as separate sources.",
        "",
    ]


def _shadow_section(run: _RunView) -> list[str]:
    sh = run.json("04-shadow/shadow.json")
    out = ["## 7. Shadow mode (replayed over the imported history)", ""]
    if sh is None:
        return [*out, "No shadow output.", ""]
    rows = []
    for mt in sh["metrics"]:
        band = "" if mt["low"] is None else f" ({mt['low']:,.0f} to {mt['high']:,.0f})"
        val = "-" if mt["value"] is None else f"{mt['value']:,.4g}"
        rows.append([mt["name"], mt["basis"], f"{val}{band} over {mt['n']}"])
    for f in sh["findings"]:
        cand = "-" if f["candidate"] is None else str(f["candidate"])
        value = f"production {f['production']}, candidate {cand}"
        rows.append([f"finding {f['check']}", f["basis"], value])
    out += [f"Data: {sh['data']}. Estimated costs from: {sh['profile']}.", ""]
    out += _table(["metric", "basis", "value"], rows)
    out += [
        "",
        f"Outcome status: {sh['resolved']} of {sh['decisions']} replayed cases resolved; "
        "a live shadow window is reported separately.",
        "",
    ]
    return out


def _evidence_section(run: _RunView) -> list[str]:
    st = run.manifest.step("evidence")
    out = ["## 8. Evidence packs", ""]
    if st.status != "ok":
        return [*out, f"Not written: {st.why}.", ""]
    rows = [[rel, sha] for rel, sha in sorted(st.outputs.items())]
    out += _table(["file", "sha256"], rows)
    out += ["", f"Pack hash: {st.summary['pack_sha256']}.", ""]
    return out


def _limits_section(run: _RunView, results: list[Result]) -> list[str]:
    b = run.backtest() or {}
    short = [r["reason"] for r in b.get("by_reason", []) if not r["plan"]["sufficient"]]
    lines = [
        "## 9. Limits of this pilot",
        "",
        f"- Data: {run.manifest.data}. Backtest costs on changed decisions are model-based "
        "(calibrated profile); costs on unchanged decisions are observed.",
        f"- Reason codes below the planned sample size: {', '.join(short) or 'none'}.",
        "- Shadow results here are replayed over history; open live-shadow cases: `[n]`.",
        "- Known verifier weaknesses that apply: docs/VERIFIER_FINDINGS.md G-1 to G-7.",
        "- Policy constraints used, and who wrote them: `[list]`.",
        "- Anything planned in SCOPE.md that was not finished: `[list]`.",
    ]
    if any(r.verdict == "not enough data" for r in results):
        lines.append("- Criteria marked not enough data are not passes.")
    lines += ["", "## 10. Data deletion", ""]
    lines += _table(
        ["item", "deleted on", "by", "how"],
        [
            ["imported cases and ingest reports", "", "", ""],
            ["pilot database (SQLite file or Postgres schema)", "", "", ""],
            ["tokenisation and encryption keys held by scruple", "", "", ""],
            ["copies in backups or scratch locations", "", "", ""],
        ],
    )
    return [*lines, "", "Signed: `[scruple]`, `[date]`. Acknowledged: `[partner]`, `[date]`."]


def render_pdf(readout: Readout) -> bytes:
    """The Markdown readout as a plain PDF (core fonts, Latin-1; fixed creation date)."""
    from fpdf import FPDF

    def t(text: str) -> str:
        return text.encode("latin-1", "replace").decode("latin-1")

    doc = FPDF(format="A4")
    doc.set_creation_date(datetime(1970, 1, 1, tzinfo=UTC))
    doc.set_title("Pilot readout")
    doc.set_author("scruple")
    doc.set_auto_page_break(auto=True, margin=15)
    doc.add_page()
    width = doc.w - doc.l_margin - doc.r_margin
    for raw in readout.markdown.splitlines():
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
            doc.multi_cell(width, 4.5, t(raw.replace("**", "").replace("`", "")))
    return bytes(doc.output())


def write_readout(readout: Readout, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    md, pdf, js = out / "readout.md", out / "readout.pdf", out / "readout.json"
    md.write_text(readout.markdown, encoding="utf-8")
    pdf.write_bytes(render_pdf(readout))
    js.write_text(
        json.dumps(readout.model_dump(mode="json", exclude={"markdown"}), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return [md, pdf, js]


__all__ = [
    "Criteria",
    "Readout",
    "Result",
    "build_readout",
    "load_criteria",
    "render_pdf",
    "write_readout",
]
