"""The durable job queue: submit, claim, heartbeat, finish, retry, cancel.

Every state change is one short transaction and is fenced: a worker may only finish,
fail or extend a job while it still holds the lease it was given (`lease_owner` and the
attempt number it claimed). A worker that stalls past its lease, or crashes, simply
loses the job: the next `claim` takes it again (a new attempt), and anything the stale
worker tries to write afterwards matches no row and is dropped.

Claiming:

- PostgreSQL: `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`. Each
  claimer locks the row it picks and skips rows other claimers hold, so two workers
  never take the same job and never wait on each other. The queue runs on its own
  connection without the gate's global advisory write lock.
- SQLite: the same statement without the lock clause, inside `BEGIN IMMEDIATE`, which
  serialises claimers on the database file.

A claimable job is a queued job whose `run_after` has passed, or a running job whose
lease has expired (its worker died). Attempts are counted at claim time, so a crash
counts as an attempt; once `max_attempts` is used up the job fails instead of running
again. Retries back off exponentially (`backoff`), without jitter, so schedules are
reproducible under an injected clock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from scruple.clock import Clock, SystemClock
from scruple.jobs.models import (
    TERMINAL,
    Job,
    JobEvent,
    JobStatus,
    JobSubmit,
    canonical,
    validate_params,
)
from scruple.storage.base import DEFAULT_TENANT
from scruple.storage.sql import Connection, Row

Dialect = Literal["sqlite", "postgres"]
LEASE_LOST = "lease expired: the worker stopped heartbeating"

_JOB_COLS = (
    "id, tenant_id, kind, params_json, idempotency_key, status, attempts, max_attempts, "
    "timeout_s, run_after, lease_owner, lease_expires_at, cancel_requested, created_at, "
    "updated_at, finished_at, result_json, result_sha256, error, rule_id, version"
)


class JobError(Exception):
    """A queue operation was refused."""


class UnknownJobError(JobError, KeyError):
    pass


class IdempotencyConflictError(JobError):
    """The idempotency key was already used for a different submission."""


def ts(at: datetime) -> str:
    """Canonical timestamp text: UTC, microseconds, so text order is time order."""
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def job_id(n: int) -> str:
    return f"job-{n:08d}"


def _number(job: str) -> int:
    digits = job.removeprefix("job-")
    if not (job.startswith("job-") and digits.isdigit() and len(digits) <= 15):
        raise UnknownJobError(f"malformed job id {job[:40]!r}")
    return int(digits)


@dataclass(frozen=True)
class Backoff:
    """Delay before attempt n+1 after attempt n failed: base * 2**(n-1), capped."""

    base_s: float = 2.0
    cap_s: float = 300.0

    def delay(self, attempts: int) -> timedelta:
        return timedelta(seconds=min(self.cap_s, self.base_s * 2 ** max(0, attempts - 1)))


def _decode(row: Row) -> Job:
    def when(v: object) -> datetime | None:
        return None if v is None else datetime.fromisoformat(str(v))

    return Job(
        job_id=job_id(int(row["id"])),
        tenant_id=row["tenant_id"],
        kind=row["kind"],
        params=json.loads(row["params_json"]),
        idempotency_key=row["idempotency_key"],
        status=JobStatus(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        timeout_s=int(row["timeout_s"]),
        run_after=datetime.fromisoformat(row["run_after"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=when(row["lease_expires_at"]),
        cancel_requested=bool(row["cancel_requested"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        finished_at=when(row["finished_at"]),
        result=None if row["result_json"] is None else json.loads(row["result_json"]),
        result_sha256=row["result_sha256"],
        error=row["error"],
        rule_id=row["rule_id"],
        version=None if row["version"] is None else int(row["version"]),
    )


class JobQueue:
    """The queue over one backend connection. Thread safe (the connection locks)."""

    def __init__(
        self,
        conn: Connection,
        dialect: Dialect,
        *,
        clock: Clock | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        self._c = conn
        self.dialect = dialect
        self.clock = clock or SystemClock()
        self.backoff = backoff or Backoff()

    def close(self) -> None:
        self._c.close()

    @contextmanager
    def _tx(self) -> Iterator[None]:
        with self._c.transaction():
            yield

    def _event(self, tenant: str, n: int, event: str, worker: str | None, **detail: Any) -> None:
        self._c.execute(
            "INSERT INTO job_events (tenant_id, job_id, at, event, worker, detail_json) "
            "VALUES (?,?,?,?,?,?)",
            (tenant, n, ts(self.clock.now()), event, worker, canonical(detail)),
        )

    def _get(self, n: int) -> Job | None:
        rows = self._c.query(f"SELECT {_JOB_COLS} FROM jobs WHERE id=?", (n,))  # noqa: S608
        return _decode(rows[0]) if rows else None

    # --- submit ----------------------------------------------------------------------

    def submit(
        self, spec: JobSubmit, *, tenant_id: str = DEFAULT_TENANT, actor: str = "system"
    ) -> tuple[Job, bool]:
        """Queue a job. Returns (job, created). With an idempotency key already used
        for the same submission, returns the first job and created=False; used for a
        different one, raises `IdempotencyConflictError`."""
        params = validate_params(spec.kind.value, spec.params)
        return self.submit_raw(
            spec.kind.value,
            params,
            tenant_id=tenant_id,
            actor=actor,
            idempotency_key=spec.idempotency_key,
            max_attempts=spec.max_attempts,
            timeout_s=spec.timeout_s,
        )

    def submit_raw(
        self,
        kind: str,
        params: dict[str, Any],
        *,
        tenant_id: str = DEFAULT_TENANT,
        actor: str = "system",
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        timeout_s: int = 3_600,
    ) -> tuple[Job, bool]:
        """`submit` for any kind (custom worker handlers); params are taken as given."""
        if max_attempts < 1 or timeout_s < 1:
            raise JobError("max_attempts and timeout_s must be positive")
        now = ts(self.clock.now())
        body = canonical(params)
        rule_id = params.get("rule_id") if isinstance(params.get("rule_id"), str) else None
        version = params.get("version") if isinstance(params.get("version"), int) else None
        with self._tx():
            rows = self._c.query(
                "INSERT INTO jobs (tenant_id, kind, params_json, idempotency_key, status, "
                "attempts, max_attempts, timeout_s, run_after, cancel_requested, created_at, "
                "updated_at, rule_id, version) VALUES (?,?,?,?,'queued',0,?,?,?,0,?,?,?,?) "
                "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id",
                (
                    tenant_id,
                    kind,
                    body,
                    idempotency_key,
                    max_attempts,
                    timeout_s,
                    now,
                    now,
                    now,
                    rule_id,
                    version,
                ),
            )
            if rows:
                n = int(rows[0]["id"])
                self._event(tenant_id, n, "submitted", None, actor=actor, kind=kind)
                job = self._get(n)
                assert job is not None
                return job, True
        existing = self._c.query(
            f"SELECT {_JOB_COLS} FROM jobs WHERE tenant_id=? AND idempotency_key=?",  # noqa: S608
            (tenant_id, idempotency_key),
        )
        job = _decode(existing[0])
        if job.kind != kind or canonical(job.params) != body:
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} was used for a different job ({job.job_id})"
            )
        return job, False

    # --- reads -----------------------------------------------------------------------

    def get(self, job: str, tenant_id: str | None = None) -> Job:
        found = self._get(_number(job))
        if found is None or (tenant_id is not None and found.tenant_id != tenant_id):
            raise UnknownJobError(f"unknown job {job!r}")
        return found

    def list_jobs(
        self,
        *,
        tenant_id: str | None = None,
        status: JobStatus | None = None,
        rule_id: str | None = None,
        version: int | None = None,
        limit: int = 100,
    ) -> list[Job]:
        where, args = [], []
        for col, val in (
            ("tenant_id", tenant_id),
            ("status", status.value if status else None),
            ("rule_id", rule_id),
            ("version", version),
        ):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        sql = f"SELECT {_JOB_COLS} FROM jobs"  # noqa: S608
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._c.query(sql + f" ORDER BY id LIMIT {int(limit)}", tuple(args))
        return [_decode(r) for r in rows]

    def events(self, job: str) -> list[JobEvent]:
        rows = self._c.query(
            "SELECT job_id, at, event, worker, detail_json FROM job_events WHERE job_id=? "
            "ORDER BY id",
            (_number(job),),
        )
        return [
            JobEvent(
                job_id=job_id(int(r["job_id"])),
                at=datetime.fromisoformat(r["at"]),
                event=r["event"],
                worker=r["worker"],
                detail=json.loads(r["detail_json"]),
            )
            for r in rows
        ]

    # --- the worker's side -----------------------------------------------------------

    def claim(self, worker: str, lease_s: float, kinds: frozenset[str] | None = None) -> Job | None:
        """Take the next runnable job (or one whose worker's lease expired), or None."""
        while True:
            now = self.clock.now()
            lock = " FOR UPDATE SKIP LOCKED" if self.dialect == "postgres" else ""
            kind_sql, kind_args = "", tuple[str, ...]()
            if kinds is not None:
                if not kinds:
                    return None
                kind_sql = f" AND kind IN ({','.join('?' * len(kinds))})"
                kind_args = tuple(sorted(kinds))
            with self._tx():
                rows = self._c.query(
                    "UPDATE jobs SET status='running', attempts=attempts+1, lease_owner=?, "  # noqa: S608
                    "lease_expires_at=?, updated_at=?, "
                    # SET reads the old row: a running row here is one whose worker died.
                    f"error=CASE WHEN status='running' THEN '{LEASE_LOST}' ELSE error END "
                    "WHERE id = ("
                    " SELECT id FROM jobs WHERE ((status='queued' AND run_after<=?)"
                    " OR (status='running' AND lease_expires_at<?))"
                    f"{kind_sql} ORDER BY run_after, id LIMIT 1{lock}"
                    f") RETURNING {_JOB_COLS}",
                    (
                        worker,
                        ts(now + timedelta(seconds=lease_s)),
                        ts(now),
                        ts(now),
                        ts(now),
                        *kind_args,
                    ),
                )
                if not rows:
                    return None
                job = _decode(rows[0])
                n = job.number
                reclaimed = job.error == LEASE_LOST
                if job.cancel_requested:
                    self._finish(job, JobStatus.CANCELLED, worker, error="cancelled")
                    continue
                if job.attempts > job.max_attempts:
                    # Its last worker died holding it: nothing is left to retry with.
                    self._finish(
                        job,
                        JobStatus.FAILED,
                        worker,
                        error=f"lease expired; {job.max_attempts} attempts used",
                    )
                    continue
                self._event(
                    job.tenant_id,
                    n,
                    "claimed",
                    worker,
                    attempt=job.attempts,
                    after_lease_expiry=reclaimed,
                )
                return job

    def _finish(
        self,
        job: Job,
        status: JobStatus,
        worker: str,
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Fenced terminal write (inside a transaction): only the current lease holder."""
        now = ts(self.clock.now())
        body = None if result is None else canonical(result)
        sha = None if body is None else hashlib.sha256(body.encode()).hexdigest()
        rows = self._c.query(
            "UPDATE jobs SET status=?, finished_at=?, updated_at=?, lease_owner=NULL, "
            "lease_expires_at=NULL, error=?, result_json=?, result_sha256=? "
            "WHERE id=? AND status='running' AND lease_owner=? AND attempts=? RETURNING id",
            (status.value, now, now, error, body, sha, job.number, worker, job.attempts),
        )
        if rows:
            detail: dict[str, Any] = {"attempt": job.attempts}
            if error is not None:
                detail["error"] = error[:500]
            if sha is not None:
                detail["result_sha256"] = sha
            self._event(job.tenant_id, job.number, status.value, worker, **detail)
        return bool(rows)

    def heartbeat(self, job: Job, worker: str, lease_s: float) -> bool | None:
        """Extend the lease. Returns cancel_requested, or None if the lease was lost."""
        now = self.clock.now()
        with self._tx():
            rows = self._c.query(
                "UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE id=? "
                "AND status='running' AND lease_owner=? AND attempts=? RETURNING cancel_requested",
                (ts(now + timedelta(seconds=lease_s)), ts(now), job.number, worker, job.attempts),
            )
        return bool(rows[0]["cancel_requested"]) if rows else None

    def succeed(self, job: Job, worker: str, result: dict[str, Any]) -> bool:
        with self._tx():
            return self._finish(job, JobStatus.SUCCEEDED, worker, result=result)

    def cancelled(self, job: Job, worker: str) -> bool:
        with self._tx():
            return self._finish(job, JobStatus.CANCELLED, worker, error="cancelled")

    def fail(self, job: Job, worker: str, error: str, *, retry: bool = True) -> bool:
        """Record a failed attempt: requeue with backoff while attempts remain."""
        with self._tx():
            if not retry or job.attempts >= job.max_attempts:
                return self._finish(job, JobStatus.FAILED, worker, error=error[:2_000])
            now = self.clock.now()
            after = now + self.backoff.delay(job.attempts)
            rows = self._c.query(
                "UPDATE jobs SET status='queued', run_after=?, updated_at=?, lease_owner=NULL, "
                "lease_expires_at=NULL, error=? WHERE id=? AND status='running' "
                "AND lease_owner=? AND attempts=? RETURNING id",
                (ts(after), ts(now), error[:2_000], job.number, worker, job.attempts),
            )
            if rows:
                self._event(
                    job.tenant_id,
                    job.number,
                    "retry_scheduled",
                    worker,
                    attempt=job.attempts,
                    run_after=ts(after),
                    error=error[:500],
                )
            return bool(rows)

    # --- cancellation ----------------------------------------------------------------

    def cancel(self, job: str, *, tenant_id: str | None = None, actor: str = "system") -> Job:
        """Cancel a queued job now; ask a running one's worker to stop (it checks on
        every heartbeat). A finished job is returned unchanged."""
        current = self.get(job, tenant_id)
        if current.status in TERMINAL:
            return current
        now = ts(self.clock.now())
        with self._tx():
            done = self._c.query(
                "UPDATE jobs SET status='cancelled', finished_at=?, updated_at=?, "
                "error='cancelled' WHERE id=? AND status='queued' RETURNING id",
                (now, now, current.number),
            )
            if done:
                self._event(current.tenant_id, current.number, "cancelled", None, actor=actor)
            else:
                asked = self._c.query(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=? "
                    "AND status='running' RETURNING id",
                    (now, current.number),
                )
                if asked:
                    self._event(
                        current.tenant_id, current.number, "cancel_requested", None, actor=actor
                    )
        return self.get(job)


def open_queue(
    location: str,
    *,
    clock: Clock | None = None,
    backoff: Backoff | None = None,
    schema: str | None = None,
) -> JobQueue:
    """A queue on its own connection to the gate database (SQLite path or Postgres URL)."""
    from scruple.storage import POSTGRES_PREFIXES, SQLITE_PREFIX

    if location.startswith(POSTGRES_PREFIXES):
        from scruple.storage.postgres import PostgresConnection

        return JobQueue(
            PostgresConnection(location, schema, create_schema=True, serialise_writes=False),
            "postgres",
            clock=clock,
            backoff=backoff,
        )
    from scruple.storage.sqlite import SQLiteConnection

    if location.startswith(SQLITE_PREFIX):
        location = location[len(SQLITE_PREFIX) :] or ":memory:"
    return JobQueue(SQLiteConnection(location), "sqlite", clock=clock, backoff=backoff)


def queue_for(storage: object, *, clock: Clock | None = None) -> JobQueue:
    """A queue on the same database as a gate `Storage`.

    SQLite shares the storage's connection (its writers serialise on the file anyway,
    and an in-memory database has no second connection); Postgres opens a second
    connection that does not take the gate's advisory write lock."""
    from scruple.storage.sqlite import SQLiteStorage

    if isinstance(storage, SQLiteStorage):
        return JobQueue(storage.connection, "sqlite", clock=clock)
    from scruple.storage.postgres import PostgresStorage

    if isinstance(storage, PostgresStorage):
        return open_queue(storage.dsn, clock=clock, schema=storage.schema)
    raise JobError(f"no job queue for {type(storage).__name__}")


__all__ = [
    "Backoff",
    "IdempotencyConflictError",
    "JobError",
    "JobQueue",
    "UnknownJobError",
    "job_id",
    "open_queue",
    "queue_for",
    "ts",
]
