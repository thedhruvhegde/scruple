"""Scheduled retention: one `retention_apply` job per tenant per day, via the job queue."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scruple.ingest.models import IngestBatch
from scruple.ingest.store import CaseStore
from scruple.jobs.handlers import HANDLERS
from scruple.jobs.models import JobStatus, JobSubmit
from scruple.jobs.queue import JobQueue, queue_for
from scruple.jobs.retention import (
    RETENTION_KIND,
    DailyScheduler,
    day_start,
    idempotency_key,
    retention_apply,
    schedule_retention,
)
from scruple.jobs.worker import JobContext, PermanentJobError, Worker
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage
from scruple.storage.retention import (
    EVENT_PURGED,
    RetentionPolicy,
    retention_for,
    set_policy,
    tenant_gate,
)
from tests.storage.test_retention import BATCH, ROWS, T0, Clock, _shadow

DAY = (T0 + timedelta(days=45)).date()


@pytest.fixture
def db(tmp_path: Path) -> str:
    """A gate database: t1 has one batch at T0 and one at T0+40d, t2 one at T0; t1 keeps
    30 days, t2 keeps 365."""
    path = str(tmp_path / "gate.db")
    s = SQLiteStorage(path)
    clock = Clock()
    gate = AgentGate(s, clock)
    _shadow(s, clock).record(BATCH[:10])
    _shadow(s, clock, "t2").record(BATCH[:5])
    clock.t = T0 + timedelta(days=40)
    _shadow(s, clock).record(BATCH[10:20])
    set_policy(gate, RetentionPolicy(tenant_id="t1", days=30), "ops")
    set_policy(gate, RetentionPolicy(tenant_id="t2", days=365), "ops")
    s.close()
    return path


def worker(queue: JobQueue, db: str, **ctx: Any) -> Worker:
    return Worker(queue, JobContext(database=db, **ctx), isolation="thread", poll_s=0)


def purged(db: str, tenant: str) -> list[dict[str, Any]]:
    s = SQLiteStorage(db)
    try:
        g = tenant_gate(AgentGate(s), tenant)
        return [e.payload for e in g.audit.entries() if e.event_type == EVENT_PURGED]
    finally:
        s.close()


def test_scheduling_is_once_per_tenant_per_day(db: str) -> None:
    s = SQLiteStorage(db)
    q = queue_for(s)
    first = schedule_retention(q, s, DAY)
    assert sorted(j.tenant_id for j, _ in first) == ["t1", "t2"]
    assert all(created for _, created in first)
    assert all(j.idempotency_key == idempotency_key(DAY) for j, _ in first)
    again = schedule_retention(q, s, DAY)  # a second worker, a cron rerun, a restart
    assert [created for _, created in again] == [False, False]
    assert {j.job_id for j, _ in again} == {j.job_id for j, _ in first}
    nxt = schedule_retention(q, s, DAY + timedelta(days=1))
    assert all(created for _, created in nxt)
    assert len(q.list_jobs(tenant_id="t1")) == 2 and len(q.list_jobs(tenant_id="t2")) == 2
    s.close()


def test_scheduled_jobs_purge_each_tenant_by_its_own_policy(db: str) -> None:
    s = SQLiteStorage(db)
    q = queue_for(s)
    schedule_retention(q, s, DAY)
    w = worker(q, db)
    assert w.run(max_jobs=2) == 2
    jobs = {j.tenant_id: j for j in q.list_jobs(tenant_id="t1") + q.list_jobs(tenant_id="t2")}
    assert {t: j.status for t, j in jobs.items()} == {
        "t1": JobStatus.SUCCEEDED,
        "t2": JobStatus.SUCCEEDED,
    }
    r1 = q.get(jobs["t1"].job_id).result
    assert r1 is not None and r1["rows_deleted"] == 1 and r1["day"] == DAY.isoformat()
    cutoff = datetime.fromisoformat(r1["purges"][0]["cutoff"])
    assert cutoff == day_start(DAY) - timedelta(days=30)
    r2 = q.get(jobs["t2"].job_id).result
    assert r2 is not None and r2["rows_deleted"] == 0  # 365 days: nothing expired
    s.close()
    [p] = purged(db, "t1")
    assert p["rows"] == 1 and p["table"] == "imported_shadow_events"
    assert purged(db, "t2") == []


def test_a_retry_or_rerun_deletes_nothing_more(db: str) -> None:
    ctx = JobContext(database=db, tenant="t1")
    params = {"day": DAY.isoformat(), "actor": "retention"}
    first = retention_apply(ctx, params)
    second = retention_apply(ctx, params)  # e.g. the worker died before recording success
    assert first["rows_deleted"] == 1 and second["rows_deleted"] == 0
    assert len(purged(db, "t1")) == 1
    s = SQLiteStorage(db)
    assert len(retention_for(s).purges("t1")) == 1
    assert tenant_gate(AgentGate(s), "t1").audit.verify().ok
    s.close()


def test_the_cutoff_depends_on_the_day_not_on_when_the_job_runs(db: str) -> None:
    early = (T0 + timedelta(days=20)).date()  # T0 batch is 20 days old then: kept
    assert (
        retention_apply(JobContext(database=db, tenant="t1"), {"day": early})["rows_deleted"] == 0
    )


def test_a_future_day_is_refused(db: str) -> None:
    future = (datetime.now(UTC) + timedelta(days=2)).date()
    with pytest.raises(PermanentJobError, match="ahead of the policy"):
        retention_apply(JobContext(database=db, tenant="t1"), {"day": future.isoformat()})
    s = SQLiteStorage(db)
    q = queue_for(s)
    schedule_retention(q, s, future)
    worker(q, db).run(max_jobs=2)
    statuses = {j.status for j in q.list_jobs(tenant_id="t1")}
    assert statuses == {JobStatus.FAILED}
    assert all(j.attempts == 1 for j in q.list_jobs(tenant_id="t1"))  # permanent: no retry
    s.close()
    assert purged(db, "t1") == []


def test_only_the_workers_tenant_store_is_purged(db: str, tmp_path: Path) -> None:
    cs = CaseStore(tmp_path / "cases.db")
    for i, (c, mv) in enumerate(ROWS):
        cs.write_webhook(
            source="stripe",
            event_id=f"evt_{i}",
            event_type="charge.dispute.created",
            event_at=c.opened_at,
            payload_sha256="0" * 64,
            batch=IngestBatch(cases=(c,), movements=mv),
            tenant_id="t2",
        )
    n = len(cs.cases())
    cs.close()
    far = max(c.opened_at for c, _ in ROWS).date() + timedelta(days=400)
    far = min(far, datetime.now(UTC).date())
    store = str(tmp_path / "cases.db")
    t1 = retention_apply(
        JobContext(database=db, tenant="t1", case_store=store, case_store_tenant="t2"),
        {"day": far.isoformat()},
    )
    assert not t1["case_store"]  # another tenant's file: never opened
    assert len(CaseStore(store).cases()) == n
    t2 = retention_apply(
        JobContext(database=db, tenant="t2", case_store=store, case_store_tenant="t2"),
        {"day": far.isoformat()},
    )
    assert t2["case_store"] and {p["store"] for p in t2["purges"]} >= {"case_store"}


def test_the_daily_scheduler_ticks_once_a_day(db: str) -> None:
    s = SQLiteStorage(db)
    q = queue_for(s)
    clock = Clock()
    clock.t = day_start(DAY) + timedelta(hours=1)
    sched = DailyScheduler(q, s, clock)
    sched.tick()
    sched.tick()
    clock.t += timedelta(hours=5)
    sched.tick()
    assert len(q.list_jobs(tenant_id="t1")) == 1
    clock.t += timedelta(days=1)
    sched.tick()
    assert len(q.list_jobs(tenant_id="t1")) == 2
    w = Worker(q, JobContext(database=db), isolation="thread", poll_s=0, tick=sched.tick)
    w.run(max_jobs=4)
    assert {j.status for j in q.list_jobs(tenant_id="t1")} == {JobStatus.SUCCEEDED}
    s.close()


def test_the_http_api_cannot_submit_retention() -> None:
    assert RETENTION_KIND in HANDLERS
    with pytest.raises(ValidationError):
        JobSubmit.model_validate({"kind": RETENTION_KIND, "params": {"day": "2026-01-01"}})


def test_retention_schedule_cli(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    from scruple.cli.main import main

    argv = ["retention", "schedule", "--db", db, "--day", DAY.isoformat()]
    assert main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(r["tenant_id"], r["created"]) for r in out] == [("t1", True), ("t2", True)]
    assert main(argv) == 0
    again = json.loads(capsys.readouterr().out)
    assert [r["created"] for r in again] == [False, False]
    assert [r["job_id"] for r in again] == [r["job_id"] for r in out]


def test_worker_cli_schedules_and_runs(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    from scruple.cli.main import main

    argv = ["worker", "--db", db, "--isolation", "thread", "--poll", "0"]
    assert main([*argv, "--schedule-retention", "--max-jobs", "2"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ran": 2}
    s = SQLiteStorage(db)
    q = queue_for(s)
    today = datetime.now(UTC).date()
    jobs = q.list_jobs(tenant_id="t1")
    assert [j.idempotency_key for j in jobs] == [idempotency_key(today)]
    assert jobs[0].status is JobStatus.SUCCEEDED
    s.close()
    assert purged(db, "t1")[0]["rows"] == 2  # both t1 batches are past 30 days by now


def test_day_param_is_strict() -> None:
    for bad in ({"day": "not a day"}, {"day": "2026-01-01", "extra": 1}, {}):
        with pytest.raises(ValidationError):
            retention_apply(JobContext(database=":memory:"), bad)
    assert isinstance(DAY, date)
