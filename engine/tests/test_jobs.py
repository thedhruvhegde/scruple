"""The durable job queue and `scruple worker`: retries, leases, crashes, cancellation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import Role, key_permissions
from scruple.jobs import (
    Backoff,
    IdempotencyConflictError,
    JobContext,
    JobKind,
    JobQueue,
    JobStatus,
    JobSubmit,
    PermanentJobError,
    Worker,
    open_queue,
)
from scruple.jobs.handlers import HANDLERS
from scruple.registry import RuleStatus
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import ImmutableHistoryError, SQLiteStorage
from tests.ci_support import PROV
from tests.test_gate_shadow import CFG

ROOT = Path(__file__).resolve().parents[1]
# The API tests use the development-only X-Actor trust; tests/test_auth_*.py cover
# real authentication.
DEV_AUTH = Authenticator(dev_actor_header=True)
READER = {"X-Actor": "auditor"}  # default caller for reads; writes set their own


class ManualClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 3, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


# --- handlers used by the tests (module level: they cross into child processes) ----


def ok(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"echo": params, "job": ctx.job_id, "attempt": ctx.attempt}


def flaky(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    if ctx.attempt < int(params["succeed_on"]):
        raise RuntimeError(f"transient failure on attempt {ctx.attempt}")
    return {"attempt": ctx.attempt}


def permanent(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    raise PermanentJobError("bad input")


def sleepy(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    time.sleep(float(params["seconds"]))
    return {"slept": params["seconds"]}


def crash_once(ctx: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    """Kills its own process on the first attempt (an OOM kill, a segfault)."""
    marker = Path(params["marker"])
    if not marker.exists():
        marker.write_text("crashed")
        os._exit(9)
    return {"recovered": True, "attempt": ctx.attempt}


TEST_HANDLERS = {"ok": ok, "flaky": flaky, "permanent": permanent, "sleepy": sleepy}


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def queue(tmp_path: Path, clock: ManualClock) -> Iterator[JobQueue]:
    q = open_queue(str(tmp_path / "gate.db"), clock=clock, backoff=Backoff(2.0, 60.0))
    yield q
    q.close()


def _worker(q: JobQueue, tmp_path: Path, **kw: Any) -> Worker:
    kw.setdefault("handlers", TEST_HANDLERS)
    kw.setdefault("isolation", "thread")
    kw.setdefault("lease_s", 30.0)
    kw.setdefault("heartbeat_s", 0.05)
    return Worker(q, JobContext(database=str(tmp_path / "gate.db")), **kw)


# --- the queue -----------------------------------------------------------------------


def test_submit_claim_succeed(queue: JobQueue, tmp_path: Path) -> None:
    job, created = queue.submit_raw("ok", {"x": 1})
    assert created and job.status is JobStatus.QUEUED and job.job_id == "job-00000001"
    done = _worker(queue, tmp_path).run_once()
    assert done is not None and done.status is JobStatus.SUCCEEDED
    assert done.result == {"echo": {"x": 1}, "job": job.job_id, "attempt": 1}
    assert done.result_sha256 and done.lease_owner is None and done.finished_at
    assert [e.event for e in queue.events(job.job_id)] == ["submitted", "claimed", "succeeded"]
    assert _worker(queue, tmp_path).run_once() is None


def test_idempotency_key_returns_the_first_job(queue: JobQueue) -> None:
    a, c1 = queue.submit_raw("ok", {"x": 1}, idempotency_key="k-1")
    b, c2 = queue.submit_raw("ok", {"x": 1}, idempotency_key="k-1")
    assert c1 and not c2 and a.job_id == b.job_id
    with pytest.raises(IdempotencyConflictError):
        queue.submit_raw("ok", {"x": 2}, idempotency_key="k-1")
    other, c3 = queue.submit_raw("ok", {"x": 1}, idempotency_key="k-1", tenant_id="t2")
    assert c3 and other.job_id != a.job_id  # keys are per tenant
    assert len(queue.list_jobs()) == 2


def test_known_kinds_validate_params(queue: JobQueue) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        queue.submit(JobSubmit(kind=JobKind.BENCH_SCORE, params={"agent": "module:evil"}))
    job, _ = queue.submit(
        JobSubmit(
            kind=JobKind.GATE_EVALUATION,
            params={"rule_id": "r-1", "version": 2, "actor": "ops"},
        )
    )
    assert (job.rule_id, job.version) == ("r-1", 2)


def test_retries_back_off_then_succeed(queue: JobQueue, clock: ManualClock, tmp_path: Path) -> None:
    job, _ = queue.submit_raw("flaky", {"succeed_on": 3}, max_attempts=3)
    w = _worker(queue, tmp_path)
    first = w.run_once()
    assert first is not None and first.status is JobStatus.QUEUED and first.attempts == 1
    assert first.run_after == clock.t + timedelta(seconds=2)
    assert w.run_once() is None  # not due yet
    clock.advance(2)
    second = w.run_once()
    assert second is not None and second.run_after == clock.t + timedelta(seconds=4)
    clock.advance(4)
    third = w.run_once()
    assert third is not None and third.status is JobStatus.SUCCEEDED
    assert third.result == {"attempt": 3}
    events = [e.event for e in queue.events(job.job_id)]
    assert events.count("retry_scheduled") == 2 and events[-1] == "succeeded"


def test_attempts_run_out(queue: JobQueue, clock: ManualClock, tmp_path: Path) -> None:
    queue.submit_raw("flaky", {"succeed_on": 9}, max_attempts=2)
    w = _worker(queue, tmp_path)
    w.run_once()
    clock.advance(10)
    last = w.run_once()
    assert last is not None and last.status is JobStatus.FAILED and last.attempts == 2
    assert last.error is not None and "attempt 2" in last.error


def test_permanent_errors_do_not_retry(queue: JobQueue, tmp_path: Path) -> None:
    queue.submit_raw("permanent", {}, max_attempts=5)
    done = _worker(queue, tmp_path).run_once()
    assert done is not None and done.status is JobStatus.FAILED and done.attempts == 1


def test_timeout_fails_the_attempt(queue: JobQueue, tmp_path: Path) -> None:
    queue.submit_raw("sleepy", {"seconds": 5}, timeout_s=1, max_attempts=1)
    t0 = time.monotonic()
    done = _worker(queue, tmp_path).run_once()
    assert time.monotonic() - t0 < 4
    assert done is not None and done.status is JobStatus.FAILED
    assert done.error == "timed out after 1s"


@pytest.mark.parametrize("isolation", ["thread", "process"])
def test_cancel_a_running_job(queue: JobQueue, tmp_path: Path, isolation: str) -> None:
    job, _ = queue.submit_raw("sleepy", {"seconds": 30})
    w = _worker(queue, tmp_path, isolation=isolation)
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(w.run_once()))
    t.start()
    deadline = time.monotonic() + 20
    while queue.get(job.job_id).status is not JobStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert queue.cancel(job.job_id, actor="ops").cancel_requested
    t.join(20)
    assert out and out[0].status is JobStatus.CANCELLED
    assert "cancel_requested" in [e.event for e in queue.events(job.job_id)]


def test_cancel_a_queued_job_and_a_finished_one(queue: JobQueue, tmp_path: Path) -> None:
    a, _ = queue.submit_raw("ok", {})
    assert queue.cancel(a.job_id).status is JobStatus.CANCELLED
    assert _worker(queue, tmp_path).run_once() is None
    b, _ = queue.submit_raw("ok", {"n": 2})
    _worker(queue, tmp_path).run_once()
    assert queue.cancel(b.job_id).status is JobStatus.SUCCEEDED


def test_process_isolation_survives_a_crashing_handler(queue: JobQueue, tmp_path: Path) -> None:
    queue.submit_raw("crash_once", {"marker": str(tmp_path / "m")}, max_attempts=2)
    w = _worker(queue, tmp_path, isolation="process", handlers={"crash_once": crash_once})
    first = w.run_once()
    assert first is not None and first.status is JobStatus.QUEUED
    assert first.error is not None and "exited with code 9" in first.error
    queue.clock.advance(5)  # type: ignore[attr-defined]
    second = w.run_once()
    assert second is not None and second.status is JobStatus.SUCCEEDED
    assert second.result == {"recovered": True, "attempt": 2}


# --- leases: a dead worker's job is taken over, and the dead worker is fenced off ----


def test_expired_lease_is_reclaimed_and_the_stale_worker_is_fenced(
    queue: JobQueue, clock: ManualClock, tmp_path: Path
) -> None:
    job, _ = queue.submit_raw("ok", {"x": 1})
    stale = queue.claim("dead-worker", lease_s=10)  # then it crashes: no heartbeat
    assert stale is not None and stale.status is JobStatus.RUNNING
    assert queue.claim("other", lease_s=10) is None  # the lease still holds
    clock.advance(11)
    done = _worker(queue, tmp_path, worker_id="rescuer").run_once()
    assert done is not None and done.status is JobStatus.SUCCEEDED and done.attempts == 2
    assert not queue.succeed(stale, "dead-worker", {"late": True})  # fenced
    assert queue.heartbeat(stale, "dead-worker", 10) is None
    assert queue.get(job.job_id).result == {"echo": {"x": 1}, "job": job.job_id, "attempt": 2}
    claims = [e for e in queue.events(job.job_id) if e.event == "claimed"]
    assert [c.detail["after_lease_expiry"] for c in claims] == [False, True]


def test_a_dead_worker_on_the_last_attempt_fails_the_job(
    queue: JobQueue, clock: ManualClock
) -> None:
    job, _ = queue.submit_raw("ok", {}, max_attempts=1)
    assert queue.claim("dead", lease_s=5) is not None
    clock.advance(6)
    assert queue.claim("next", lease_s=5) is None
    failed = queue.get(job.job_id)
    assert failed.status is JobStatus.FAILED and failed.error is not None


def test_worker_process_killed_mid_job_is_recovered(tmp_path: Path) -> None:
    """Fault injection: the worker process itself dies holding a job (os._exit in the
    handler, thread isolation, so nothing finishes it). A new worker takes over once
    the lease expires, and the job runs to completion exactly once."""
    db = str(tmp_path / "gate.db")
    q = open_queue(db)
    job, _ = q.submit_raw("crash_once", {"marker": str(tmp_path / "m")})
    script = (
        "from scruple.jobs import JobContext, Worker, open_queue\n"
        "from tests.test_jobs import crash_once\n"
        f"q = open_queue({db!r})\n"
        f"Worker(q, JobContext(database={db!r}), handlers={{'crash_once': crash_once}},"
        " isolation='thread', lease_s=1.0, heartbeat_s=0.2, worker_id='doomed').run_once()\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert out.returncode == 9, out.stderr
    held = q.get(job.job_id)
    assert held.status is JobStatus.RUNNING and held.lease_owner == "doomed"
    w = Worker(
        q,
        JobContext(database=db),
        handlers={"crash_once": crash_once},
        isolation="thread",
        lease_s=5.0,
        heartbeat_s=0.2,
        worker_id="rescuer",
    )
    deadline = time.monotonic() + 15
    done = None
    while done is None:
        assert time.monotonic() < deadline
        done = w.run_once()
        time.sleep(0.1)
    assert done.status is JobStatus.SUCCEEDED and done.attempts == 2
    assert done.result == {"recovered": True, "attempt": 2}
    q.close()


# --- concurrency: two workers never take the same job --------------------------------


def _claim_all(make: Any, n_workers: int) -> list[str]:
    claimed: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(n_workers, timeout=60)

    def run(i: int) -> None:
        q = make()
        start.wait()
        while (job := q.claim(f"w{i}", lease_s=600)) is not None:
            with lock:
                claimed.append(job.job_id)
            q.succeed(job, f"w{i}", {"by": i})
        q.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    return claimed


def test_sqlite_workers_never_share_a_job(tmp_path: Path) -> None:
    db = str(tmp_path / "gate.db")
    q = open_queue(db)
    ids = {q.submit_raw("ok", {"i": i})[0].job_id for i in range(60)}
    claimed = _claim_all(lambda: open_queue(db), 4)
    assert sorted(claimed) == sorted(ids)
    assert all(j.status is JobStatus.SUCCEEDED for j in q.list_jobs(limit=1000))
    q.close()


@pytest.fixture
def pg_schema(postgres_url: str) -> Iterator[str]:
    import psycopg

    schema = f"j_{uuid.uuid4().hex[:16]}"
    yield schema
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.mark.postgres
def test_postgres_workers_never_share_a_job(postgres_url: str, pg_schema: str) -> None:
    q = open_queue(postgres_url, schema=pg_schema)
    ids = {q.submit_raw("ok", {"i": i})[0].job_id for i in range(120)}
    claimed = _claim_all(lambda: open_queue(postgres_url, schema=pg_schema), 6)
    assert len(claimed) == len(set(claimed)) == 120 and set(claimed) == ids
    owners = {e.worker for j in ids for e in q.events(j) if e.event == "claimed"}
    assert len(owners) > 1  # the work really was shared out
    q.close()


@pytest.mark.postgres
def test_postgres_queue_retries_leases_and_cancels(postgres_url: str, pg_schema: str) -> None:
    clock = ManualClock()
    q = open_queue(postgres_url, schema=pg_schema, clock=clock)
    a, _ = q.submit_raw("ok", {}, idempotency_key="same")
    assert q.submit_raw("ok", {}, idempotency_key="same")[0].job_id == a.job_id
    stale = q.claim("dead", lease_s=5)
    assert stale is not None
    clock.advance(6)
    again = q.claim("alive", lease_s=5)
    assert again is not None and again.attempts == 2
    assert not q.succeed(stale, "dead", {}) and q.succeed(again, "alive", {"ok": 1})
    b, _ = q.submit_raw("ok", {"b": 1})
    assert q.cancel(b.job_id).status is JobStatus.CANCELLED
    with pytest.raises(ImmutableHistoryError):
        q._c.execute("DELETE FROM job_events")
    q.close()


@settings(max_examples=20, deadline=None)
@given(st.lists(st.sampled_from(["ok", "fail", "crash", "cancel"]), min_size=1, max_size=12))
def test_every_job_ends_in_exactly_one_terminal_state(script: list[str]) -> None:
    """Whatever happens to each attempt, a job finishes once and its history agrees."""
    clock = ManualClock()
    q = open_queue(":memory:", clock=clock, backoff=Backoff(1.0, 1.0))
    for i, step in enumerate(script):
        job, _ = q.submit_raw("x", {"i": i}, max_attempts=2)
        for _attempt in range(3):
            claimed = q.claim("w", lease_s=5)
            if claimed is None:
                break
            if step == "ok":
                q.succeed(claimed, "w", {})
            elif step == "fail":
                q.fail(claimed, "w", "boom")
            elif step == "cancel":
                q.cancel(claimed.job_id)
                assert q.heartbeat(claimed, "w", 5) is True
                q.cancelled(claimed, "w")
            clock.advance(10)  # crashes simply let the lease run out
        final = q.get(job.job_id)
        assert final.terminal, (step, final)
        terminal = [
            e.event for e in q.events(job.job_id) if e.event in {"succeeded", "failed", "cancelled"}
        ]
        assert terminal == [final.status.value]
    q.close()


# --- the built-in kinds, the audit link and the API ----------------------------------


def _gate(db: str) -> AgentGate:
    g = AgentGate(SQLiteStorage(db), config=CFG)
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    return g


def _version(g: AgentGate) -> int:
    return g.registry.versions(GOOD_RULE_ID)[-1].version


def test_gate_evaluation_job_links_into_the_audit_log(tmp_path: Path) -> None:
    db = str(tmp_path / "gate.db")
    g = _gate(db)
    v = _version(g)
    q = open_queue(db)
    job, _ = q.submit(
        JobSubmit(
            kind=JobKind.GATE_EVALUATION,
            params={"rule_id": GOOD_RULE_ID, "version": v, "actor": "ops"},
        )
    )
    ctx = JobContext(database=db, gate_config_json=CFG.model_dump_json())
    done = Worker(q, ctx, isolation="thread", heartbeat_s=0.2).run_once()
    assert done is not None and done.status is JobStatus.SUCCEEDED, done
    assert done.result is not None and done.result["decisions"]
    assert g.registry.get(GOOD_RULE_ID, v).status is not RuleStatus.CANDIDATE
    links = [e for e in g.audit.entries() if e.event_type == "job.succeeded"]
    assert [e.payload["job_id"] for e in links] == [job.job_id]
    assert links[0].seq == done.result["audit_seq"]
    assert g.verify_integrity().ok

    # A retry after a crash that happened past the commit resumes, and links once.
    from scruple.jobs.handlers import gate_evaluation

    again = gate_evaluation(
        JobContext(database=db, job_id=job.job_id, kind="gate_evaluation", attempt=2),
        dict(job.params),
    )
    assert again["resumed"] and again["decisions"] == done.result["decisions"]
    assert again["audit_seq"] == links[0].seq
    assert len([e for e in g.audit.entries() if e.event_type == "job.succeeded"]) == 1

    from scruple.ci.evidence import build_evidence_pack

    pack = build_evidence_pack(g, GOOD_RULE_ID, v)
    assert job.job_id in {
        e.payload.get("job_id") for e in pack.audit.entries if e.event_type == "job.succeeded"
    }
    q.close()


def test_backtest_job_on_a_synthetic_history(tmp_path: Path) -> None:
    db = str(tmp_path / "gate.db")
    g = _gate(db)
    q = open_queue(db)
    params = {
        "rule_id": GOOD_RULE_ID,
        "version": _version(g),
        "history": {"kind": "synthetic", "n": 300, "seed": 3},
    }
    q.submit(JobSubmit(kind=JobKind.BACKTEST, params=params))
    done = Worker(q, JobContext(database=db), isolation="thread").run_once()
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED, done.error
    assert done.result is not None and done.result["synthetic"] is True
    assert done.result["report"]["n_included"] > 0
    q.submit(
        JobSubmit(
            kind=JobKind.BACKTEST,
            params={**params, "history": {"kind": "case_store"}},
            max_attempts=3,
        )
    )
    failed = Worker(q, JobContext(database=db), isolation="thread").run_once()
    assert failed is not None and failed.status is JobStatus.FAILED and failed.attempts == 1
    q.close()


def test_bench_score_without_the_salt_fails_permanently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCRUPLE_HELDOUT_SALT", raising=False)
    q = open_queue(str(tmp_path / "gate.db"))
    q.submit(JobSubmit(kind=JobKind.BENCH_SCORE, params={"agent": "always_accept", "limit": 10}))
    done = Worker(q, JobContext(database=str(tmp_path / "gate.db")), isolation="thread").run_once()
    assert done is not None and done.status is JobStatus.FAILED and done.attempts == 1
    assert set(HANDLERS) == {k.value for k in JobKind}
    q.close()


def test_api_submit_poll_cancel(tmp_path: Path) -> None:
    db = str(tmp_path / "gate.db")
    g = _gate(db)
    c = TestClient(create_app(g, auth=DEV_AUTH), headers=READER)
    hdr = {"X-Actor": "ops", "Idempotency-Key": "eval-1"}
    body: dict[str, Any] = {
        "kind": "gate_evaluation",
        "params": {"rule_id": GOOD_RULE_ID, "version": 2},
    }
    r = c.post("/jobs", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["params"]["actor"] == "ops" and job["status"] == "queued"
    again = c.post("/jobs", json=body, headers=hdr)
    assert again.status_code == 200 and again.json()["job"]["job_id"] == job["job_id"]
    other = {**body, "params": {**body["params"], "version": 1}}
    assert c.post("/jobs", json=other, headers=hdr).status_code == 409
    spoof = {**body, "params": {**body["params"], "actor": "root"}}
    assert c.post("/jobs", json=spoof, headers={"X-Actor": "ops"}).status_code == 403
    bad = {"kind": "bench_score", "params": {"agent": "os:system"}}
    assert c.post("/jobs", json=bad, headers={"X-Actor": "ops"}).status_code == 422
    assert c.get(f"/jobs/{job['job_id']}").json()["status"] == "queued"
    assert c.get("/jobs/job-99999999").status_code == 404
    listed = c.get("/jobs", params={"rule_id": GOOD_RULE_ID, "version": 2}).json()
    assert [j["job_id"] for j in listed] == [job["job_id"]]
    cancelled = c.post(f"/jobs/{job['job_id']}/cancel", headers={"X-Actor": "ops"})
    assert cancelled.json()["status"] == "cancelled"
    events = c.get(f"/jobs/{job['job_id']}/events").json()
    assert [e["event"] for e in events] == ["submitted", "cancelled"]


def test_api_job_runs_in_a_separate_worker_and_survives_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "gate.db")
    g = _gate(db)
    c = TestClient(create_app(g, auth=DEV_AUTH), headers=READER)
    params = {
        "rule_id": GOOD_RULE_ID,
        "version": _version(g),
        "history": {"kind": "synthetic", "n": 200, "seed": 1},
    }
    r = c.post("/jobs", json={"kind": "backtest", "params": params}, headers={"X-Actor": "ops"})
    job_id = r.json()["job"]["job_id"]
    g.storage.close()
    out = subprocess.run(
        [sys.executable, "-m", "scruple.cli.main", "worker", "--db", db, "--once"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert out.returncode == 0, out.stderr
    c2 = TestClient(
        create_app(AgentGate(SQLiteStorage(db), config=CFG), auth=DEV_AUTH), headers=READER
    )
    polled = c2.get(f"/jobs/{job_id}").json()
    assert polled["status"] == "succeeded" and polled["result"]["synthetic"] is True
    assert json.loads(json.dumps(polled["result"]["report"]))["n_included"] > 0


def test_jobs_are_tenant_scoped_and_run_in_their_own_tenant(tmp_path: Path) -> None:
    """A job is submitted, seen and run in the caller's tenant only, by real keys."""
    db = str(tmp_path / "gate.db")
    g = _gate(db)  # tenant "default": the good rule proposed as a candidate
    gb = AgentGate(g.storage.for_tenant("bank-b"), config=CFG)
    gb.bootstrap_baseline()
    gb.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    keys = ApiKeyService(g.storage, params=ScryptParams(n=1024), permissions_for=key_permissions)

    def key(subject: str, role: Role, tenant: str) -> dict[str, str]:
        _, token = keys.create(subject, (), "local-admin", roles=[role], tenant=tenant)
        return {"X-API-Key": token.get_secret_value()}

    c = TestClient(create_app(g, auth=Authenticator(keys)))
    b_agent = key("agent-b", Role.PROPOSER, "bank-b")
    body = {"kind": "gate_evaluation", "params": {"rule_id": GOOD_RULE_ID, "version": 1}}
    r = c.post("/jobs", json=body, headers=b_agent)
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["tenant_id"] == "bank-b" and job["params"]["actor"] == "agent-b"
    assert (
        c.post("/jobs", json=body, headers=key("aud-b", Role.AUDITOR, "bank-b")).status_code == 403
    )
    a_reader = key("aud-a", Role.AUDITOR, "default")
    assert c.get(f"/jobs/{job['job_id']}", headers=a_reader).status_code == 404
    assert c.get("/jobs", headers=a_reader).json() == []
    a_agent = key("agent-a", Role.PROPOSER, "default")
    assert c.post(f"/jobs/{job['job_id']}/cancel", headers=a_agent).status_code == 404

    head_a = g.audit.head()
    done = Worker(open_queue(db), JobContext(database=db), isolation="thread").run_once()
    assert done is not None and done.status is JobStatus.SUCCEEDED, done
    assert g.registry.get(GOOD_RULE_ID, 1).status is RuleStatus.CANDIDATE  # untouched
    assert g.audit.head() == head_a
    assert gb.registry.get(GOOD_RULE_ID, 1).status is not RuleStatus.CANDIDATE
    assert gb.audit.verify().ok and g.audit.verify().ok
