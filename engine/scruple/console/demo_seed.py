"""`scruple console demo-seed`: a demo tenant with SYNTHETIC data, so the dashboard has
something to show.

Everything is produced by the engine itself on simulated or synthetic inputs; nothing
is typed in as a result. Over twelve weeks ending today:

- a tenant policy (e-value gate, a two-of-three approval quorum, a four-stage canary
  ramp) is put in force;
- two simulated agents propose rules. The gate shadows each on 12,000 synthetic
  held-out cases: the good rule passes, ramps through the canary, collects two
  approvals and goes live; the harmful ones are blocked; the weak ones are held. A
  rollback drill retires the good rule and it comes back as v2, which is ramping
  when the seed ends;
- a case store of synthetic Stripe-shaped disputes is ingested through the normal
  pipeline (the store marks itself synthetic);
- backtest and gate-evaluation jobs run through the job queue and a worker; one job
  fails on purpose (an unknown rule version) and one is left queued;
- production's decisions on the synthetic disputes are recorded in imported shadow
  mode for the v2 candidate, week by week, then their outcomes;
- usage is metered from the decisions, and pushed to an in-process stub of Stripe's
  meter-events endpoint in test mode (never to Stripe);
- two eval runs play the fake model transport (a harness check, not a real model);
  one is given a demo token price so its cost column is filled;
- the audit head is anchored in a local git repository (never pushed).

Dates are relative to the day the seed runs; durations are real compute times measured
while it runs. `tenant.json` in the tenant's data folder marks it synthetic, and every
dashboard page prints that banner. The seed refuses a tenant that already has history:
history is append-only, so seed a fresh tenant or a fresh database.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from scruple.domain import Action, RuleSpec
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID, C
from scruple.storage import open_storage

DEMO_TENANT = "demo"
OPERATOR = "risk-ops@demo.example"
ALICE = "alice@demo.example"
BOB = "bob@demo.example"
CAROL = "carol@demo.example"  # a named approver who does not act in the seed
AGENT_1 = "dispute-agent-01"
AGENT_2 = "dispute-agent-02"
OWNER = "payments-ops"
DAYS = 84
HELD_OUT = 12_000  # the gate's plan promotes the good rule at this size (e-value gate)
DISPUTES = 480
LABEL = (
    "Synthetic demo tenant seeded by scruple console demo-seed: simulated agents, a "
    "synthetic held-out set, synthetic Stripe-shaped disputes, a stub billing endpoint "
    "and a fake model transport."
)

FRAUD_SMALL = RuleSpec(
    description="Accept fraud claims under $25 without contesting.",
    conditions=(C("reason_code", "eq", "fraud"), C("amount_usd_minor", "lt", 2_500)),
    action=Action.ACCEPT,
    priority=25,
)
FRAUD_ALL = RuleSpec(
    description="Contest every fraud claim.",
    conditions=(C("reason_code", "eq", "fraud"),),
    action=Action.CONTEST,
    priority=28,
)
NAD_ESCALATE = RuleSpec(
    description="Send not-as-described claims above $500 to an analyst.",
    conditions=(C("reason_code", "eq", "not_as_described"), C("amount_usd_minor", "gt", 50_000)),
    action=Action.ESCALATE,
    priority=35,
)
NAD_ACCEPT = RuleSpec(
    description="Accept not-as-described claims under $40.",
    conditions=(C("reason_code", "eq", "not_as_described"), C("amount_usd_minor", "lt", 4_000)),
    action=Action.ACCEPT,
    priority=55,
)
GOOD_V2 = GOOD_RULE.model_copy(
    update={
        "description": (
            "Contest 'item not received' disputes up to $10,000 when the carrier confirmed "
            "delivery (re-proposed after the rollback drill)."
        ),
        # Above $10,000 the baseline escalates first anyway (priority 0); the bound
        # makes the content new without changing what the rule does.
        "conditions": (*GOOD_RULE.conditions, C("amount_usd_minor", "le", 1_000_000)),
    }
)


class DemoClock:
    """Wall-clock durations placed on a demo timeline: `now()` is the last `at(...)`
    plus the real time elapsed since, so a job that takes 2.3 s records 2.3 s."""

    def __init__(self, start: datetime) -> None:
        self._base = start
        self._mark = time.monotonic()

    def at(self, t: datetime) -> None:
        current = self.now()
        self._base = max(t, current)
        self._mark = time.monotonic()

    def now(self) -> datetime:
        return self._base + timedelta(seconds=time.monotonic() - self._mark)


@dataclass
class SeedReport:
    tenant: str
    database: str
    folder: Path
    decisions: int = 0
    jobs: dict[str, int] = field(default_factory=dict)
    disputes: int = 0
    imported: int = 0
    usage_events: int = 0
    pushes: int = 0
    eval_runs: int = 0
    anchored: bool = False
    notes: list[str] = field(default_factory=list)


def _prov(agent: str, n: int, notes: str, **claims: float) -> Provenance:
    return Provenance(
        agent_id=agent,
        episode_ids=tuple(f"ep-demo-{i:04d}" for i in range(n)),
        notes=notes,
        claimed_metrics=claims,
    )


def _stub_stripe() -> httpx.MockTransport:
    """Answers like Stripe's meter-events endpoint in test mode. A stub: nothing leaves
    the process."""

    def handle(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        payload = {k[8:-1]: v for k, v in form.items() if k.startswith("payload[")}
        return httpx.Response(
            200,
            json={
                "object": "billing.meter_event",
                "created": int(form.get("timestamp", "0")),
                "event_name": form.get("event_name", ""),
                "identifier": form.get("identifier", ""),
                "livemode": False,
                "payload": payload,
                "timestamp": int(form.get("timestamp", "0")),
            },
        )

    return httpx.MockTransport(handle)


def seed_demo(
    db: str,
    data: Path,
    tenant: str = DEMO_TENANT,
    *,
    today: datetime | None = None,
    held_out: int = HELD_OUT,
    disputes: int = DISPUTES,
    say: Callable[[str], None] = print,
) -> SeedReport:
    from scruple.gate.approval import ApprovalPolicy, NamedApprover
    from scruple.gate.config import GateConfig
    from scruple.policy.apply import apply_policy, configured
    from scruple.policy.models import Policy

    end = (today or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    start = end - timedelta(days=DAYS)
    clock = DemoClock(start)

    def day(n: float) -> None:
        clock.at(start + timedelta(days=n))

    storage: Any = open_storage(db)
    if hasattr(storage, "for_tenant"):
        storage = storage.for_tenant(tenant)
    if storage.last_audit() is not None:
        storage.close()
        raise ValueError(
            f"tenant {tenant!r} already has history in {db}; history is append-only, so "
            "seed a new tenant (--tenant) or a new database"
        )
    folder = data / tenant
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tenant.json").write_text(
        json.dumps({"synthetic": True, "label": LABEL}, indent=2) + "\n", encoding="utf-8"
    )
    rep = SeedReport(tenant, db, folder)
    base = GateConfig(stats="evalue", n_cases=held_out, approvers=(OPERATOR,))
    gate = AgentGate(storage, clock, base)

    # --- week 0: baseline and policy -------------------------------------------------
    day(0)
    gate.bootstrap_baseline(OPERATOR)
    policy = Policy.model_validate(
        {
            "name": "demo-disputes",
            "description": "SYNTHETIC demo policy for the dashboard (scruple console demo-seed).",
            "stats": {"provider": "evalue", "alpha": 0.05},
            "approval": ApprovalPolicy(
                approvers=(
                    NamedApprover(name=ALICE, roles=("model-risk",)),
                    NamedApprover(name=BOB, roles=("ops-lead",)),
                    NamedApprover(name=CAROL, roles=("model-risk",)),
                ),
                required=2,
                required_role="model-risk",
            ).model_dump(mode="json"),
            "canary": {"stages": [5, 10, 25, 50]},
        }
    )
    apply_policy(gate, policy, OPERATOR)
    gate = configured(gate)
    say(f"policy {policy.name} in force for tenant {tenant}")

    # --- the case store: synthetic Stripe-shaped disputes ----------------------------
    cases_db = folder / "cases.db"
    rep.disputes = _ingest_disputes(cases_db, start - timedelta(days=30), DAYS + 30, disputes)
    say(f"ingested {rep.disputes} synthetic Stripe-shaped disputes into {cases_db}")

    from scruple.jobs.models import JobStatus
    from scruple.jobs.queue import queue_for
    from scruple.jobs.worker import JobContext, Worker

    queue = queue_for(gate.storage, clock=clock)
    worker = Worker(
        queue,
        JobContext(
            database=db,
            case_store=str(cases_db),
            case_store_tenant=tenant,
            gate_config_json=gate.config.model_dump_json(),
        ),
        worker_id="demo-worker-1",
        isolation="thread",
        lease_s=600.0,
    )

    def job(kind: str, params: dict[str, Any], key: str, *, run: bool = True) -> None:
        from scruple.jobs.models import JobKind, JobSubmit

        queue.submit(
            JobSubmit(kind=JobKind(kind), params=params, idempotency_key=key, max_attempts=1),
            tenant_id=tenant,
            actor=OPERATOR,
        )
        if run:
            worker.run_once()

    def backtest(rule: str, version: int, history: dict[str, Any], key: str) -> None:
        job("backtest", {"rule_id": rule, "version": version, "history": history}, key)

    # --- weeks 1 to 3: the good rule goes live; the bad one is blocked ----------------
    day(3)
    gate.propose(
        GOOD_RULE_ID,
        GOOD_RULE,
        OWNER,
        _prov(AGENT_1, 37, "Disputes with delivery confirmation were usually invalid."),
    )
    gate.evaluate(GOOD_RULE_ID, 1, AGENT_1)
    day(4)
    backtest(GOOD_RULE_ID, 1, {"kind": "case_store"}, "bt-good-v1")
    gate.start_canary_ramp(GOOD_RULE_ID, 1, actor=OPERATOR)
    for i in range(4):
        day(5 + 2 * i)
        gate.advance_canary(GOOD_RULE_ID, 1, actor=OPERATOR)
    day(10)
    gate.propose(
        BAD_RULE_ID,
        BAD_RULE,
        OWNER,
        _prov(AGENT_1, 41, "This always works; make it an override.", win_rate=0.97),
    )
    gate.evaluate(BAD_RULE_ID, 1, AGENT_1)
    day(11)
    backtest(BAD_RULE_ID, 1, {"kind": "synthetic", "n": 600, "seed": 11}, "bt-bad-v1")
    day(13)
    gate.request_approval(GOOD_RULE_ID, 1, AGENT_1)
    day(14)
    gate.grant_approval(GOOD_RULE_ID, 1, ALICE, reason="evidence pack reviewed; slices clean")
    day(15)
    gate.grant_approval(GOOD_RULE_ID, 1, BOB, reason="ops ready for the change")
    say("good rule activated after the ramp and two model-risk approvals; bad rule blocked")

    # --- weeks 3 to 8: a second agent; assurance; the rollback drill ------------------
    day(20)
    gate.run_assurance(OPERATOR)
    day(24)
    gate.propose(
        "fraud-small-accept",
        FRAUD_SMALL,
        OWNER,
        _prov(AGENT_2, 22, "Small fraud claims are rarely worth contesting."),
    )
    gate.evaluate("fraud-small-accept", 1, AGENT_2)
    day(27)
    gate.run_assurance(OPERATOR)
    day(31)
    gate.propose(
        "nad-escalate-high",
        NAD_ESCALATE,
        OWNER,
        _prov(AGENT_2, 18, "Large not-as-described claims need a person."),
    )
    gate.evaluate("nad-escalate-high", 1, AGENT_2)
    day(34)
    gate.run_assurance(OPERATOR)
    day(38)
    gate.propose(
        "fraud-contest-all",
        FRAUD_ALL,
        OWNER,
        _prov(AGENT_2, 64, "Contesting fraud wins often enough.", win_rate=0.9),
    )
    gate.evaluate("fraud-contest-all", 1, AGENT_2)
    day(39)
    backtest("fraud-contest-all", 1, {"kind": "case_store"}, "bt-fraud-all-v1")
    day(41)
    gate.run_assurance(OPERATOR)
    day(45)
    gate.propose(
        "nad-accept-small",
        NAD_ACCEPT,
        OWNER,
        _prov(AGENT_2, 15, "Small claims cost more to fight."),
    )
    gate.evaluate("nad-accept-small", 1, AGENT_2)
    day(48)
    gate.run_assurance(OPERATOR)
    day(52)
    gate.rollback(GOOD_RULE_ID, OPERATOR, "quarterly rollback drill")
    say("rollback drill: good rule retired, baseline restored")

    # --- weeks 8 to 12: v2 ramps; imported shadow on the disputes ---------------------
    day(56)
    gate.propose(
        GOOD_RULE_ID,
        GOOD_V2,
        OWNER,
        _prov(AGENT_1, 52, "Re-proposed after the rollback drill."),
    )
    gate.evaluate(GOOD_RULE_ID, 2, AGENT_1)
    day(57)
    backtest(GOOD_RULE_ID, 2, {"kind": "case_store"}, "bt-good-v2")
    gate.start_canary_ramp(GOOD_RULE_ID, 2, actor=OPERATOR)
    for i in range(2):
        day(60 + 4 * i)
        gate.advance_canary(GOOD_RULE_ID, 2, actor=OPERATOR)
    rep.imported = _imported_shadow(gate, tenant, cases_db, GOOD_RULE_ID, 2, day, start)
    say(f"imported shadow: {rep.imported} synthetic decisions for {GOOD_RULE_ID}@v2")
    day(69)
    gate.run_assurance(OPERATOR)
    day(76)
    gate.run_assurance(OPERATOR)
    day(78)
    job(
        "backtest",
        {"rule_id": "no-such-rule", "version": 1, "history": {"kind": "case_store"}},
        "bt-missing",
    )

    # --- eval runs (fake transport) --------------------------------------------------
    rep.eval_runs = _eval_runs(folder)
    say(f"eval runs: {rep.eval_runs} (fake model transport)")

    # --- anchor, usage, billing ------------------------------------------------------
    day(80)
    rep.anchored = _anchor(gate, tenant, folder, rep)
    day(DAYS - 0.5)
    gate.propose(
        "inr-nodelivery-accept",
        RuleSpec(
            description="Accept item-not-received claims without delivery confirmation.",
            conditions=(
                C("reason_code", "eq", "not_received"),
                C("delivery_confirmed", "eq", False),
            ),
            action=Action.ACCEPT,
            priority=54,
        ),
        OWNER,
        _prov(AGENT_2, 12, "No delivery proof, no case."),
    )
    clock.at(datetime.now(UTC))  # the worker's own gate reads the wall clock
    job(
        "gate_evaluation",
        {"rule_id": "inr-nodelivery-accept", "version": 1, "actor": AGENT_2},
        "eval-inr-nodelivery",
    )
    job("bench_score", {"agent": "evidence", "limit": 50}, "bench-evidence", run=False)
    rep.usage_events, rep.pushes = _usage_and_billing(gate, tenant, clock)
    say(f"usage: {rep.usage_events} events; {rep.pushes} pushes to the stub billing endpoint")

    rep.decisions = len(gate.decisions())
    rep.jobs = {
        s.value: len(queue.list_jobs(tenant_id=tenant, status=s, limit=1_000)) for s in JobStatus
    }
    gate.storage.close()
    return rep


def _ingest_disputes(path: Path, start: datetime, span_days: int, n: int) -> int:
    from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
    from scruple.ingest.pipeline import IngestOptions, ingest
    from scruple.ingest.store import CaseStore

    objs = stripe_disputes(DEFAULT_PROFILE, n, 20_261, start=start, span_days=span_days)
    with CaseStore(path) as store:
        report = ingest(
            json.dumps(objs),
            IngestOptions(source="stripe", label="SYNTHETIC Stripe-shaped disputes (demo seed)"),
            store,
        )
    return report.cases


def _imported_shadow(
    gate: AgentGate,
    tenant: str,
    cases_db: Path,
    rule_id: str,
    version: int,
    day: Callable[[float], None],
    start: datetime,
) -> int:
    """Production's decisions on the stored disputes, batch by batch, then outcomes.
    The recorder is told the data is synthetic, so its summaries say so."""
    from scruple.backtest.candidate import RuleCandidate, RuleEntry
    from scruple.backtest.history import load_store
    from scruple.calibration.facts import contested, is_inquiry_level
    from scruple.gate.shadow_imported import (
        ImportedDecision,
        ImportedOutcome,
        ImportedShadowRecorder,
    )
    from scruple.ingest.models import CaseState
    from scruple.sim.models import Stage
    from scruple.storage.imported_shadow import DurableImportedShadow, repository_for

    history = load_store(str(cases_db))
    moves: dict[str, list[Any]] = {}
    for m in history.movements:
        moves.setdefault(m.case_external_id, []).append(m)

    rv = gate.registry.get(rule_id, version)

    def fresh() -> ImportedShadowRecorder:
        # As `ShadowRecorder.imported()` builds it (the rule alone), told the data is
        # synthetic so every stored summary says so.
        candidate = RuleCandidate((RuleEntry(rule_id=rv.rule_id, spec=rv.spec),), None, rv.ref)
        return ImportedShadowRecorder(candidate, synthetic=True)

    shadow = DurableImportedShadow(
        repository_for(gate.storage), rule_id, version, fresh, tenant_id=tenant, clock=gate.clock
    )
    cases = sorted(history.cases, key=lambda h: h.case.opened_at)[-160:]
    batches = [cases[i : i + 40] for i in range(0, len(cases), 40)]
    n = 0
    for b, batch in enumerate(batches):
        day(61 + 5 * b)
        decisions = []
        for hc in batch:
            c = hc.case
            stage = Stage.INQUIRY if is_inquiry_level(c) else Stage.CHARGEBACK
            at_open = c.model_copy(
                update={"stage": stage, "state": CaseState.OPEN, "evidence_submitted": False}
            )
            decisions.append(
                ImportedDecision(
                    case=at_open,
                    action=Action.CONTEST if contested(c) else Action.ACCEPT,
                    decided_at=c.opened_at + timedelta(hours=2),
                    movements=tuple(
                        m for m in moves.get(c.external_id, []) if m.occurred_at <= c.opened_at
                    ),
                )
            )
        shadow.record(decisions, actor="production")
        n += len(decisions)
        day(63 + 5 * b)
        shadow.observe(
            [
                ImportedOutcome(case=hc.case, movements=tuple(moves.get(hc.case.external_id, [])))
                for hc in batch
            ],
            actor="production",
        )
    return n


def _eval_runs(folder: Path) -> int:
    from scruple.agents.providers.runner import Price
    from scruple.eval.batch import EvalConfig, run_eval

    runs = [
        (AGENT_1, "anthropic", "checklist-first", Price(input_per_mtok=1.0, output_per_mtok=4.0)),
        (AGENT_2, "openai", "react", None),
    ]
    done = 0
    for agent, provider, strategy, price in runs:
        out = folder / "evals" / agent / f"{provider}-{strategy}"
        shutil.rmtree(out, ignore_errors=True)
        run_eval(
            EvalConfig(
                provider=provider,
                model="demo-fake-model",
                strategy=strategy,
                seeds=tuple(range(6)),
                transport="fake",
                concurrency=1,
                price=price,
            ),
            out,
        )
        done += 1
    return done


def _anchor(gate: AgentGate, tenant: str, folder: Path, rep: SeedReport) -> bool:
    from scruple.audit.anchor import anchor_git, statement_for

    if shutil.which("git") is None:
        rep.notes.append("git not found: no audit anchor written")
        return False
    repo = folder / "anchor-repo"
    repo.mkdir(exist_ok=True)
    try:
        if not (repo / ".git").exists():
            subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
        for k, v in (("user.name", "scruple demo seed"), ("user.email", "demo-seed@demo.example")):
            subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)  # noqa: S603, S607
        st = statement_for(gate.audit.entries(), tenant)
        a = anchor_git(st, repo)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        rep.notes.append(f"no audit anchor written: {exc}")
        return False
    out = folder / "anchors" / f"anchor-{tenant}-{st.entries}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(a.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    return True


def _usage_and_billing(gate: AgentGate, tenant: str, clock: DemoClock) -> tuple[int, int]:
    from scruple.billing.push import BillingConfig, ledger_for, push
    from scruple.billing.stripe import StripeBillingClient
    from scruple.usage.meter import Meter, usage_store_for

    usage = usage_store_for(gate.storage, clock)
    ledger = ledger_for(gate.storage)
    if usage is None or ledger is None:
        return 0, 0
    n = usage.sync(gate)
    config = BillingConfig(
        customers={tenant: "cus_demo0000000001"},
        meters={
            Meter.GATE_RUN: "scruple_gate_runs",
            Meter.GATE_CASES: "scruple_gate_cases",
            Meter.ROLLOUT: "scruple_rollouts",
        },
    )
    client = StripeBillingClient("sk_test_demostubnotakey", transport=_stub_stripe())
    try:
        report = push(config, client, usage, ledger, clock, tenants=[tenant])
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return n, len(report.sent)


__all__ = ["DEMO_TENANT", "DemoClock", "SeedReport", "seed_demo"]
