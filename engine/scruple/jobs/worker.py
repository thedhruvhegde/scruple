"""`scruple worker`: claim jobs, run them with a timeout, record the outcome.

A worker loops: claim one job, run its handler, heartbeat while it runs, and finish
it. Isolation is per job:

- `process` (the default for `scruple worker`): the handler runs in a fresh child
  process (multiprocessing `spawn`). A timeout or a cancellation terminates the child,
  so the job really stops; a child that dies without answering (killed, out of memory)
  is a failed attempt.
- `thread`: the handler runs in a thread of the worker (tests, and small inline work).
  A thread cannot be killed: on timeout or cancellation the job is finished at once and
  the thread's eventual result is discarded.

The worker heartbeats every `heartbeat_s` (default a third of the lease). The heartbeat
is fenced (`JobQueue.heartbeat`): if it finds the lease gone, another worker has taken
the job after this one stalled, and this worker stops without writing anything.

Handlers are plain functions `(JobContext, params) -> result dict`. Raising
`PermanentJobError` fails the job without a retry (bad input, a sealed benchmark);
anything else is retried with backoff until `max_attempts` is used up.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from multiprocessing.connection import Connection as Pipe
from typing import Any, Literal

from scruple.jobs.models import Job
from scruple.jobs.queue import JobQueue
from scruple.storage.base import DEFAULT_TENANT

log = logging.getLogger("scruple.jobs")

Isolation = Literal["process", "thread"]


class PermanentJobError(Exception):
    """The job cannot succeed by retrying (bad parameters, missing configuration)."""


@dataclass(frozen=True)
class JobContext:
    """What a handler may use. Picklable: it crosses into the child process."""

    database: str  # the gate database (SQLite path or Postgres URL)
    case_store: str | None = None  # operator-configured case store for backtests
    # A case store is one tenant's file (its rows carry no tenant column): only jobs of
    # this tenant may read it.
    case_store_tenant: str = DEFAULT_TENANT
    gate_config_json: str | None = None  # GateConfig JSON; default config when None
    job_id: str = ""
    kind: str = ""
    attempt: int = 0
    tenant: str = DEFAULT_TENANT  # the job's tenant: handlers read and write only its data


Handler = Callable[[JobContext, dict[str, Any]], dict[str, Any]]

# ("ok", result) | ("error", message, permanent)
_Outcome = tuple[Any, ...]


def _child(handler: Handler, ctx: JobContext, params: dict[str, Any], pipe: Pipe) -> None:
    try:
        pipe.send(("ok", handler(ctx, params)))
    except PermanentJobError as exc:
        pipe.send(("error", str(exc), True))
    except BaseException as exc:
        pipe.send(("error", f"{type(exc).__name__}: {exc}", False))
    finally:
        pipe.close()


class _Run:
    def wait(self, seconds: float) -> _Outcome | None:  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class _ProcessRun(_Run):
    def __init__(self, handler: Handler, ctx: JobContext, params: dict[str, Any]) -> None:
        mp = multiprocessing.get_context("spawn")
        self._recv, send = mp.Pipe(duplex=False)
        self._proc = mp.Process(
            target=_child, args=(handler, ctx, params, send), name=f"scruple-{ctx.job_id}"
        )
        self._proc.daemon = True
        self._proc.start()
        send.close()

    def wait(self, seconds: float) -> _Outcome | None:
        if self._recv.poll(seconds):
            try:
                out: _Outcome = self._recv.recv()
            except EOFError:  # the child exited without answering
                self._proc.join(5)
                return ("error", f"job process exited with code {self._proc.exitcode}", False)
            self._proc.join(5)
            return out
        if not self._proc.is_alive():
            self._proc.join()
            return ("error", f"job process exited with code {self._proc.exitcode}", False)
        return None

    def stop(self) -> None:
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(5)
            if self._proc.is_alive():  # pragma: no cover - SIGTERM ignored
                self._proc.kill()
                self._proc.join()
        self._recv.close()


class _ThreadRun(_Run):
    def __init__(self, handler: Handler, ctx: JobContext, params: dict[str, Any]) -> None:
        self._done = threading.Event()
        self._out: _Outcome | None = None

        def target() -> None:
            try:
                self._out = ("ok", handler(ctx, params))
            except PermanentJobError as exc:
                self._out = ("error", str(exc), True)
            except BaseException as exc:
                self._out = ("error", f"{type(exc).__name__}: {exc}", False)
            finally:
                self._done.set()

        threading.Thread(target=target, name=f"scruple-{ctx.job_id}", daemon=True).start()

    def wait(self, seconds: float) -> _Outcome | None:
        return self._out if self._done.wait(seconds) else None

    def stop(self) -> None:
        """Cannot interrupt a thread; its result will be ignored."""


def default_worker_id() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}:{threading.get_ident() % 100_000}"


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        ctx: JobContext,
        *,
        handlers: Mapping[str, Handler] | None = None,
        worker_id: str | None = None,
        lease_s: float = 30.0,
        heartbeat_s: float | None = None,
        poll_s: float = 1.0,
        isolation: Isolation = "process",
    ) -> None:
        if handlers is None:
            from scruple.jobs.handlers import HANDLERS

            handlers = HANDLERS
        if lease_s <= 0 or poll_s < 0:
            raise ValueError("lease_s must be positive and poll_s not negative")
        self.queue = queue
        self.ctx = ctx
        self.handlers = dict(handlers)
        self.worker_id = worker_id or default_worker_id()
        self.lease_s = lease_s
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else lease_s / 3
        if not 0 < self.heartbeat_s < lease_s:
            raise ValueError("heartbeat_s must be positive and shorter than the lease")
        self.poll_s = poll_s
        self.isolation = isolation

    def run_once(self) -> Job | None:
        """Claim and run one job. Returns the job as stored afterwards, or None."""
        job = self.queue.claim(self.worker_id, self.lease_s, frozenset(self.handlers))
        if job is None:
            return None
        self._execute(job)
        return self.queue.get(job.job_id)

    def run(self, *, max_jobs: int | None = None, stop: threading.Event | None = None) -> int:
        """Loop until `stop` is set or `max_jobs` ran. Returns how many ran."""
        ran = 0
        stop = stop or threading.Event()
        while not stop.is_set() and (max_jobs is None or ran < max_jobs):
            if self.run_once() is None:
                stop.wait(self.poll_s)
            else:
                ran += 1
        return ran

    def _execute(self, job: Job) -> None:
        ctx = replace(
            self.ctx,
            job_id=job.job_id,
            kind=job.kind,
            attempt=job.attempts,
            tenant=job.tenant_id,
        )
        handler = self.handlers[job.kind]
        run: _Run = (
            _ProcessRun(handler, ctx, job.params)
            if self.isolation == "process"
            else _ThreadRun(handler, ctx, job.params)
        )
        started = time.monotonic()
        log.info("job %s (%s) attempt %d started", job.job_id, job.kind, job.attempts)
        try:
            while True:
                left = job.timeout_s - (time.monotonic() - started)
                out = run.wait(max(0.0, min(self.heartbeat_s, left)))
                if out is not None:
                    break
                if time.monotonic() - started >= job.timeout_s:
                    run.stop()
                    self.queue.fail(job, self.worker_id, f"timed out after {job.timeout_s}s")
                    return
                state = self.queue.heartbeat(job, self.worker_id, self.lease_s)
                if state is None:
                    run.stop()
                    log.warning("job %s: lease lost; another worker has it", job.job_id)
                    return
                if state:
                    run.stop()
                    self.queue.cancelled(job, self.worker_id)
                    return
        except BaseException:
            run.stop()
            raise
        if out[0] == "ok":
            if not isinstance(out[1], dict):
                self.queue.fail(job, self.worker_id, "handler returned a non-dict result")
                return
            try:
                self.queue.succeed(job, self.worker_id, out[1])
            except (TypeError, ValueError) as exc:  # not JSON: a handler bug, not transient
                self.queue.fail(job, self.worker_id, f"result is not JSON: {exc}", retry=False)
        else:
            self.queue.fail(job, self.worker_id, str(out[1]), retry=not out[2])


__all__ = [
    "Handler",
    "Isolation",
    "JobContext",
    "PermanentJobError",
    "Worker",
    "default_worker_id",
]
