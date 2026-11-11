"""The built-in job kinds: gate evaluation, backtest, bench score, and the scheduled
`retention_apply` (scruple.jobs.retention; not submittable over HTTP).

Each handler opens what it needs from the `JobContext` (it may run in a fresh child
process), does the work that used to run inline in a request or CLI call, and returns
a JSON-able result. Handlers are written to be safe to run again after a crash:

- gate evaluation commits shadow, canary and their audit entries in one transaction
  (`AgentGate.evaluate`), so an attempt either evaluated the version or did nothing.
  A retry that finds the version already evaluated returns those decisions
  (`resumed: true`) instead of failing.
- backtest and bench score are pure: re-running gives the same result.

Jobs about one rule version (gate evaluation, backtest) are linked into the audit log
(`job.succeeded`, naming the job and its result hash), so they appear in that version's
evidence pack audit segment; the link is written once per job even across retries.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from scruple.jobs.models import (
    BacktestParams,
    BenchScoreParams,
    CaseStoreHistory,
    GateEvaluationParams,
    JobKind,
    canonical,
)
from scruple.jobs.worker import Handler, JobContext, PermanentJobError
from scruple.storage.base import DEFAULT_TENANT


def _gate(ctx: JobContext) -> Any:
    from scruple.gate.config import GateConfig
    from scruple.service import AgentGate
    from scruple.storage import open_storage

    cfg = GateConfig.model_validate_json(ctx.gate_config_json) if ctx.gate_config_json else None
    storage = open_storage(ctx.database)
    # The job's tenant only: its registry, decisions and audit chain.
    for_tenant = getattr(storage, "for_tenant", None)
    if for_tenant is not None:
        storage = for_tenant(ctx.tenant)
    elif ctx.tenant != DEFAULT_TENANT:  # pragma: no cover - every SQL backend has it
        raise PermanentJobError(f"this database cannot serve tenant {ctx.tenant!r}")
    return AgentGate(storage, config=cfg)


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def gate_evaluation(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    from scruple.registry import RuleNotFoundError, RuleStatus

    p = GateEvaluationParams.model_validate(params)
    gate = _gate(ctx)
    try:
        try:
            rv = gate.registry.get(p.rule_id, p.version)
        except RuleNotFoundError as exc:
            raise PermanentJobError(str(exc)) from None
        resumed = rv.status is not RuleStatus.CANDIDATE
        if resumed:
            records = [r for r in gate.decisions(p.rule_id) if r.decision.version == p.version]
            if not records:
                raise PermanentJobError(f"{rv.ref} is {rv.status.value}, not candidate")
        else:
            records = gate.evaluate(p.rule_id, p.version, p.actor)
        decisions = [
            {
                "decision_id": r.decision.decision_id,
                "stage": r.decision.stage.value,
                "outcome": r.decision.outcome.value,
                "summary": r.decision.summary,
                "report_url": f"/decisions/{r.decision.decision_id}/report",
            }
            for r in records
        ]
        result: dict[str, Any] = {
            "rule_id": p.rule_id,
            "version": p.version,
            "status": gate.registry.get(p.rule_id, p.version).status.value,
            "decisions": decisions,
            "resumed": resumed,
            "synthetic": True,
        }
        link = gate.record_job(ctx.job_id, ctx.kind, p.rule_id, p.version, _sha(decisions), p.actor)
        result["audit_seq"] = link.seq
        return result
    finally:
        gate.storage.close()


def backtest(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    from scruple.backtest.candidate import RuleCandidate, RuleEntry
    from scruple.backtest.engine import BacktestConfig, run_backtest
    from scruple.backtest.history import load_store, load_stripe
    from scruple.registry import RuleNotFoundError

    p = BacktestParams.model_validate(params)
    gate = _gate(ctx)
    try:
        try:
            rv = gate.registry.get(p.rule_id, p.version)
        except RuleNotFoundError as exc:
            raise PermanentJobError(str(exc)) from None
        if isinstance(p.history, CaseStoreHistory):
            if not ctx.case_store:
                raise PermanentJobError("this worker has no --case-store configured")
            if ctx.tenant != ctx.case_store_tenant:
                raise PermanentJobError(
                    f"this worker's case store belongs to tenant {ctx.case_store_tenant!r}"
                )
            history = load_store(ctx.case_store)
        else:
            from scruple.calibration import DEFAULT_PROFILE, stripe_disputes

            objs = stripe_disputes(DEFAULT_PROFILE, p.history.n, p.history.seed)
            history = load_stripe(
                json.dumps(objs),
                label=f"SYNTHETIC Stripe-shaped history n={p.history.n} seed={p.history.seed}",
            )
        candidate = RuleCandidate(
            (RuleEntry(rule_id=rv.rule_id, spec=rv.spec),), default=None, name=rv.ref
        )
        report = run_backtest(history, candidate, config=BacktestConfig(k=p.k, currency=p.currency))
        body: dict[str, Any] = report.model_dump(mode="json")
        result = {
            "rule_id": p.rule_id,
            "version": p.version,
            "synthetic": bool(report.synthetic),
            "verdict": report.verdict,
            "report": body,
        }
        link = gate.record_job(ctx.job_id, ctx.kind, p.rule_id, p.version, _sha(body), "worker")
        result["audit_seq"] = link.seq
        return result
    finally:
        gate.storage.close()


def bench_score(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    from scruple.bench.cli import DEFAULT_BENCH
    from scruple.bench.core import BenchFile, score_agent
    from scruple.bench.spec import space_from

    p = BenchScoreParams.model_validate(params)
    bench = BenchFile.load(str(DEFAULT_BENCH))
    try:
        space = space_from(None)
    except Exception as exc:
        raise PermanentJobError(f"benchmark is sealed: {exc}") from None
    try:
        run, _ = score_agent(bench, p.agent, space, limit=p.limit)
    except (ValueError, PermissionError) as exc:  # wrong salt, or sealed (no salt)
        raise PermanentJobError(f"{type(exc).__name__}: {exc}") from None
    out: dict[str, Any] = {"synthetic": True, "run": run.model_dump(mode="json")}
    return out


def retention_apply(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    """Scheduled retention for the job's tenant (scruple.jobs.retention)."""
    from scruple.jobs.retention import retention_apply as run

    return run(ctx, params)


def webhook_deliver(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    """One outbound webhook delivery (scruple.integrations.webhooks)."""
    from scruple.integrations.jobs import webhook_deliver as run

    return run(ctx, params)


def ticket_sync(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    """One Jira or Linear change-ticket update (scruple.integrations.tickets)."""
    from scruple.integrations.jobs import ticket_sync as run

    return run(ctx, params)


def slack_notify(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    """One Slack approval-request message (scruple.integrations.slack)."""
    from scruple.integrations.jobs import slack_notify as run

    return run(ctx, params)


HANDLERS: dict[str, Handler] = {
    JobKind.GATE_EVALUATION.value: gate_evaluation,
    JobKind.BACKTEST.value: backtest,
    JobKind.BENCH_SCORE.value: bench_score,
    "retention_apply": retention_apply,
    "webhook_deliver": webhook_deliver,
    "ticket_sync": ticket_sync,
    "slack_notify": slack_notify,
}

__all__ = [
    "HANDLERS",
    "backtest",
    "bench_score",
    "gate_evaluation",
    "retention_apply",
    "slack_notify",
    "ticket_sync",
    "webhook_deliver",
]
