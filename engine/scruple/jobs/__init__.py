"""A durable job queue for long work (gate evaluation, backtests, bench scoring).

Jobs live in the gate database (`scruple.storage.jobs`): submit through the API
(`POST /jobs`) or `JobQueue.submit`, run them with `scruple worker`, poll
`GET /jobs/{id}`. See `queue` for leases, retries and fencing, and `worker` for
timeouts, cancellation and process isolation.
"""

from __future__ import annotations

from scruple.jobs.models import (
    Job,
    JobEvent,
    JobKind,
    JobStatus,
    JobSubmit,
)
from scruple.jobs.queue import (
    Backoff,
    IdempotencyConflictError,
    JobError,
    JobQueue,
    UnknownJobError,
    open_queue,
    queue_for,
)
from scruple.jobs.worker import JobContext, PermanentJobError, Worker

__all__ = [
    "Backoff",
    "IdempotencyConflictError",
    "Job",
    "JobContext",
    "JobError",
    "JobEvent",
    "JobKind",
    "JobQueue",
    "JobStatus",
    "JobSubmit",
    "PermanentJobError",
    "UnknownJobError",
    "Worker",
    "open_queue",
    "queue_for",
]
