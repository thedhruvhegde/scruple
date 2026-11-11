"""`scruple pilot run`: the whole pilot pipeline in one command, with a manifest.

    scruple pilot run --history <export file | cases.db | stripe-pull> \\
        --candidate <rule.json | agent.py | module:attr> --out pilot/

Steps, each writing into its own directory under `--out`:

1. `01-ingest`: the export goes through the ingest pipeline into a fresh case store
   (validation, redaction, card tokenisation and field encryption with the keys in
   the environment, docs/INTEGRATIONS.md), then the suppression-safe store summary.
   `stripe-pull` first pages the Stripe API with `STRIPE_API_KEY` (test mode).
   A case store given directly is used as it is (the step says so).
2. `02-calibration`: aggregates with small-cell suppression, the profile fitted from
   them only, and the divergence report (docs/BACKTEST.md).
3. `03-backtest`: the candidate replayed over the history with that profile
   (report.json, report.md, report.pdf).
4. `04-shadow`: shadow mode on the imported cases (`scruple.pilot.shadow`): every
   case as production decided it on the day it opened, then its outcome.
5. `05-gate`: the CI gate on synthetic held-out cases plus the history, as
   `scruple ci run --history` does. Rule candidates only: the gate judges one rule;
   an agent or a multi-rule file is backtested and shadowed, and this step is skipped
   with the reason.
6. `06-evidence`: the evidence pack (JSON and PDF) of the gated version.

`manifest.json` lists every step: status, why, inputs and outputs with sha256, and a
summary. Paths inside it are relative to `--out`; no wall clock is read, so the same
inputs give the same bytes. Idempotent: when the manifest's run key (history,
candidate and options, by content hash) matches and every output still has its
hash, the run is a no-op; otherwise every step is rebuilt from scratch.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scruple.backtest.candidate import load_candidate
from scruple.backtest.engine import BacktestConfig, run_backtest
from scruple.backtest.history import History, load_store
from scruple.backtest.report import write_report
from scruple.calibration import CalibrationConfig, calibrate
from scruple.ingest.errors import IngestError
from scruple.ingest.keys import cipher_from_env, tokenizer_from_env
from scruple.ingest.pipeline import IngestOptions, ingest
from scruple.ingest.store import CaseStore
from scruple.ingest.tabular import ImportConfig
from scruple.ingest.verify import summarise
from scruple.pilot.shadow import replay_shadow
from scruple.version import package_version

STRIPE_PULL = "stripe-pull"
MANIFEST = "manifest.json"
Status = Literal["ok", "skipped"]
HistoryFormat = Literal["stripe", "adyen", "csv", "jsonl", "store"]


class PilotOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    history: str  # a file, or "stripe-pull"
    candidate: str
    out: str
    format: HistoryFormat | None = None  # None: inferred from the suffix
    import_config: str | None = None  # csv / jsonl
    since: str | None = None  # stripe-pull window
    until: str | None = None
    label: str | None = Field(default=None, max_length=200)
    allow_live: bool = False
    k: int = Field(default=10, ge=2)
    n_cases: int = Field(default=2_000, ge=100, le=100_000)  # synthetic gate cases
    currency: str | None = None


class Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: Status
    why: str
    inputs: dict[str, str] = {}  # name -> sha256
    outputs: dict[str, str] = {}  # path relative to --out -> sha256
    summary: dict[str, Any] = {}


class PilotManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["scruple.pilot.run"] = "scruple.pilot.run"
    schema_version: int = 1
    scruple_version: str
    data: str  # "synthetic" or "real (imported)"
    run_key: str
    history: dict[str, Any]
    candidate: dict[str, Any]
    options: dict[str, Any]
    steps: tuple[Step, ...]

    def step(self, name: str) -> Step:
        return next(s for s in self.steps if s.name == name)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _infer_format(path: Path, given: HistoryFormat | None) -> HistoryFormat:
    if given is not None:
        return given
    suffix = path.suffix.lower()
    if suffix in (".db", ".sqlite", ".sqlite3"):
        return "store"
    if suffix == ".csv":
        return "csv"
    if suffix in (".json", ".jsonl"):
        return "stripe"
    raise IngestError(f"cannot tell the format of {path.name}; pass --format")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _outputs(out: Path, paths: list[Path]) -> dict[str, str]:
    return {p.relative_to(out).as_posix(): sha256_file(p) for p in sorted(paths)}


def _gate_candidate(doc: Any) -> tuple[dict[str, Any] | None, str]:
    """The `scruple propose` body the CI gate needs, or None and why not."""
    if not isinstance(doc, dict):
        return None, "the candidate is not a JSON object"
    if "rules" in doc:
        return None, "a multi-rule file: the CI gate judges one rule at a time"
    if "spec" in doc:
        body = dict(doc)
    elif "conditions" in doc:
        body = {"rule_id": "pilot-candidate", "spec": doc}
    else:
        return None, "not a rule"
    body.setdefault("rule_id", "pilot-candidate")
    body.setdefault("owner", "pilot")
    body.setdefault(
        "provenance",
        {"agent_id": "pilot-candidate", "notes": "wrapped by scruple pilot run"},
    )
    return body, "rule candidate"


class _Run:
    def __init__(self, opts: PilotOptions, env: Mapping[str, str] | None) -> None:
        self.opts = opts
        self.out = Path(opts.out)
        self.env = env
        self.steps: list[Step] = []

    def d(self, name: str) -> Path:
        p = self.out / name
        p.mkdir(parents=True, exist_ok=True)
        return p


def _pull(r: _Run, transport: httpx.BaseTransport | None) -> Path:
    from scruple.ingest.stripe.pull import client_from_env, parse_instant, pull_disputes

    o = r.opts
    if not (o.since and o.until):
        raise IngestError("--history stripe-pull needs --since and --until")
    target = r.d("00-pull") / "stripe-pull.jsonl"
    with client_from_env(r.env, allow_live=o.allow_live, transport=transport) as client:
        rep = pull_disputes(
            client,
            parse_instant(o.since),
            parse_instant(o.until),
            target,
            allow_live=o.allow_live,
        )
    if not rep.complete:  # pragma: no cover - pull_disputes runs to completion here
        raise IngestError("the Stripe pull did not complete; rerun to resume")
    return target


def _ingest(r: _Run, src: Path, fmt: HistoryFormat) -> tuple[History, Step]:
    o = r.opts
    label = o.label or f"pilot history ({fmt})"
    if fmt == "store":
        hist = load_store(str(src), label=label)
        return hist, Step(
            name="ingest",
            status="skipped",
            why="the history is already a case store; used as given",
            inputs={"history": sha256_file(src)},
            summary={"cases": len(hist.cases), "synthetic": hist.synthetic},
        )
    cfg = None
    if fmt in ("csv", "jsonl"):
        if not o.import_config:
            raise IngestError(f"{fmt} history needs --import-config (an ImportConfig JSON)")
        cfg = ImportConfig.model_validate_json(Path(o.import_config).read_text("utf-8"))
    d = r.d("01-ingest")
    store_path = d / "cases.db"
    for p in d.glob("cases.db*"):
        p.unlink()
    data = src.read_bytes()
    opts = IngestOptions(source=fmt, label=label, import_config=cfg, allow_live=o.allow_live)
    with CaseStore(str(store_path)) as store:
        report = ingest(
            data,
            opts,
            store,
            tokenizer=tokenizer_from_env(r.env),
            cipher=cipher_from_env(r.env),
        )
        summary = summarise(store, k=o.k, cipher=cipher_from_env(r.env))
    rep_path, ver_path = d / "ingest.json", d / "verify.json"
    _write_json(rep_path, report.model_dump(mode="json"))
    _write_json(ver_path, summary.model_dump(mode="json"))
    hist = load_store(str(store_path), label=label)
    inputs = {"history": report.input_sha256}
    if o.import_config:
        inputs["import_config"] = sha256_file(Path(o.import_config))
    return hist, Step(
        name="ingest",
        status="ok",
        why=f"{fmt} export through the ingest pipeline into a fresh case store",
        inputs=inputs,
        outputs=_outputs(r.out, [rep_path, ver_path]),
        summary={
            "rows_read": report.rows_read,
            "rows_failed": report.rows_failed,
            "cases": report.cases,
            "movements": report.movements,
            "redactions": sum(report.redactions.values()),
            "pans_tokenised": report.pans_tokenised,
            "fields_encrypted": report.fields_encrypted,
            "raw_pans_found": summary.raw_pans_found,
            "synthetic": report.synthetic,
            "store": "01-ingest/cases.db (not hashed: SQLite file)",
        },
    )


def run_pilot(
    opts: PilotOptions,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[PilotManifest, bool]:
    """Run (or reuse) the pilot pipeline. Returns the manifest and whether it was
    rebuilt (False: an up-to-date manifest was found and nothing ran)."""
    r = _Run(opts, env)
    r.out.mkdir(parents=True, exist_ok=True)
    fmt: HistoryFormat = "stripe"
    if opts.history == STRIPE_PULL:
        src = _pull(r, transport)
    else:
        src = Path(opts.history)
        fmt = _infer_format(src, opts.format)
    cand_spec = opts.candidate
    cand_path = Path(cand_spec)
    cand_sha = (
        sha256_file(cand_path)
        if cand_path.is_file()
        else _sha_text(f"module:{cand_spec}")  # a module name: its import path only
    )
    keyed = opts.model_dump(mode="json", exclude={"out", "history", "candidate"})
    hist_sha = sha256_file(src)
    cfg_sha = sha256_file(Path(opts.import_config)) if opts.import_config else None
    run_key = _sha_text(
        json.dumps(
            {
                "history": hist_sha,
                "format": fmt,
                "candidate": cand_sha,
                "options": keyed,
                "import_config": cfg_sha,
                "version": package_version(),
            },
            sort_keys=True,
        )
    )
    old = _up_to_date(r.out, run_key)
    if old is not None:
        return old, False
    for sub in ("01-ingest", "02-calibration", "03-backtest", "04-shadow", "05-gate"):
        shutil.rmtree(r.out / sub, ignore_errors=True)
    shutil.rmtree(r.out / "06-evidence", ignore_errors=True)

    hist, ingest_step = _ingest(r, src, fmt)
    steps = [ingest_step]

    # 2. calibration from aggregates only
    cal = calibrate([hc.case for hc in hist.cases], hist.movements, CalibrationConfig(k=opts.k))
    cd = r.d("02-calibration")
    _write_json(cd / "calibration.json", cal.model_dump(mode="json"))
    (cd / "profile.json").write_text(cal.profile.model_dump_json(indent=2) + "\n", "utf-8")
    steps.append(
        Step(
            name="calibration",
            status="ok",
            why="aggregates with small-cell suppression; profile fitted from them only",
            outputs=_outputs(r.out, [cd / "calibration.json", cd / "profile.json"]),
            summary={
                "k": opts.k,
                "divergence_default": round(cal.before.overall, 6),
                "divergence_calibrated": round(cal.after.overall, 6),
                "fallbacks": len(cal.profile.fallbacks),
            },
        )
    )

    # 3. backtest with the calibrated profile
    candidate = load_candidate(cand_spec)
    bcfg = BacktestConfig(k=opts.k, currency=opts.currency)
    report = run_backtest(hist, candidate, config=bcfg, profile=cal.profile)
    files = write_report(report, r.d("03-backtest"))
    steps.append(
        Step(
            name="backtest",
            status="ok",
            why="the candidate replayed over the history, no look-ahead (docs/BACKTEST.md)",
            inputs={"candidate": cand_sha},
            outputs=_outputs(r.out, files),
            summary={
                "verdict": report.verdict,
                "robust": report.robust,
                "n_included": report.n_included,
                "n_observed": report.n_observed,
                "n_estimated": report.n_estimated,
                "baseline_cost": report.baseline_cost,
                "candidate_cost": report.candidate_cost,
                "ledger_reconciled": report.ledger_reconciled,
            },
        )
    )

    # 4. shadow mode over the imported history
    shadow = replay_shadow(hist, candidate, cal.profile)
    sp = r.d("04-shadow") / "shadow.json"
    _write_json(sp, shadow.model_dump(mode="json"))
    # action checks (the candidate's own action is checked): findings above production's
    new_violations = sum(
        max(0, f.candidate - f.production) for f in shadow.findings if f.candidate is not None
    )
    steps.append(
        Step(
            name="shadow",
            status="ok",
            why="each case as production decided it when it opened, then its outcome",
            outputs=_outputs(r.out, [sp]),
            summary={
                "data": shadow.data,
                "decisions": shadow.decisions,
                "agreement": shadow.metric("agreement").value,
                "changed_decisions": shadow.metric("changed_decisions").value,
                "resolved": shadow.resolved,
                "candidate_new_hard_findings": new_violations,
            },
        )
    )

    # 5 and 6. the CI gate on history, and the evidence pack
    steps += _gate_and_evidence(r, cand_path, hist)

    manifest = PilotManifest(
        scruple_version=package_version(),
        data="synthetic" if hist.synthetic else "real (imported)",
        run_key=run_key,
        history={
            "source": STRIPE_PULL if opts.history == STRIPE_PULL else "file",
            "format": fmt,
            "label": hist.label,
            "sha256": hist_sha,
            "cases": len(hist.cases),
        },
        candidate={
            "spec": cand_path.name if cand_path.is_file() else cand_spec,
            "name": candidate.name,
            "sha256": cand_sha,
        },
        options=keyed,
        steps=tuple(steps),
    )
    (r.out / MANIFEST).write_text(manifest.to_json(), encoding="utf-8")
    return manifest, True


def _gate_and_evidence(r: _Run, cand_path: Path, hist: History) -> list[Step]:
    from scruple.ci.config import CandidateFile, CandidateSection, CIConfig, CISection
    from scruple.ci.evidence import build_evidence_pack
    from scruple.ci.evidence_pdf import render_pdf
    from scruple.ci.runner import ci_clock, run_candidate
    from scruple.gate.config import GateConfig
    from scruple.service import AgentGate

    body, why = (None, "an agent candidate: the CI gate judges rules")
    if cand_path.is_file() and cand_path.suffix == ".json":
        body, why = _gate_candidate(json.loads(cand_path.read_text("utf-8")))
    if body is None:
        skip = f"{why}; backtest and shadow cover it"
        return [
            Step(name="gate", status="skipped", why=skip),
            Step(name="evidence", status="skipped", why="no gated version"),
        ]
    gd = r.d("05-gate")
    db = gd / "gate.db"
    for p in gd.glob("gate.db*"):
        p.unlink()
    cfg = CIConfig(
        candidate=CandidateSection(file=str(cand_path)),
        gate=GateConfig(n_cases=r.opts.n_cases),
        ci=CISection(db=str(db)),
    )
    # Fixed clock (the CI clock of in-memory runs): a pilot run is a replay, and the
    # same inputs must give the same audit head and pack bytes.
    gate = AgentGate.open(str(db), ci_clock(str(db), fixed=True), cfg.gate)
    run = run_candidate(cfg, CandidateFile.model_validate(body), gate=gate, history=hist)
    card = run.scorecard
    sc = gd / "scorecard.json"
    sc.write_text(card.to_json(), encoding="utf-8")
    gate_step = Step(
        name="gate",
        status="ok",
        why="synthetic held-out cases and the partner history, kept apart",
        outputs=_outputs(r.out, [sc]),
        summary={
            "verdict": card.verdict.value,
            "exit_code": card.exit_code,
            "synthetic_verdict": None
            if card.synthetic_verdict is None
            else card.synthetic_verdict.value,
            "history_verdict": None if card.history is None else card.history.verdict,
            "rule_id": card.rule_id,
            "version": card.version,
            "audit_head": card.audit_head,
            "gate_db": "05-gate/gate.db (not hashed: SQLite file)",
            "clock": "fixed CI clock (a replay: reproducible audit head)",
        },
    )
    pack = build_evidence_pack(run.gate, card.rule_id, card.version, card.history, True)
    ed = r.d("06-evidence")
    stem = ed / f"evidence-{card.rule_id}-v{card.version}"
    stem.with_suffix(".json").write_text(pack.to_json(), encoding="utf-8")
    stem.with_suffix(".pdf").write_bytes(render_pdf(pack))
    ev_step = Step(
        name="evidence",
        status="ok",
        why="evidence pack of the gated version (JSON and PDF, audit proof included)",
        outputs=_outputs(r.out, [stem.with_suffix(".json"), stem.with_suffix(".pdf")]),
        summary={"pack_sha256": pack.pack_sha256},
    )
    return [gate_step, ev_step]


def _up_to_date(out: Path, run_key: str) -> PilotManifest | None:
    path = out / MANIFEST
    if not path.exists():
        return None
    try:
        m = PilotManifest.model_validate_json(path.read_text("utf-8"))
    except ValueError:
        return None
    if m.run_key != run_key:
        return None
    for s in m.steps:
        for rel, sha in s.outputs.items():
            p = out / rel
            if not p.is_file() or sha256_file(p) != sha:
                return None
    return m


__all__ = ["MANIFEST", "STRIPE_PULL", "PilotManifest", "PilotOptions", "Step", "run_pilot"]
