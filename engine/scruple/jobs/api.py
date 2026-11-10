"""HTTP routes for jobs, mounted by `scruple.api.app` with one line.

    POST /jobs                    submit (Idempotency-Key header or body field)
    GET  /jobs                    list (filters: status, rule_id, version)
    GET  /jobs/{job_id}           poll: status, attempts, result when finished
    GET  /jobs/{job_id}/events    the job's append-only history
    POST /jobs/{job_id}/cancel    cancel (queued: at once; running: at the next heartbeat)

Submitting answers 202 for a new job and 200 when an idempotency key returns the
existing one; 409 if that key was used for a different submission. A gate evaluation
runs as the authenticated caller, never as an actor named in the body.

Authentication and tenancy are the HTTP API's (`scruple.auth`): submitting and
cancelling need `propose` (a job evaluates or replays rules and writes to the audit
log, like `POST /rules/{id}/versions/{v}/evaluate`), reading needs `read`. Every route
sees only the caller's tenant's jobs; another tenant's job answers 404.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from scruple.auth import Permission, Principal, require
from scruple.jobs.models import (
    IDEMPOTENCY_PATTERN,
    Job,
    JobEvent,
    JobKind,
    JobStatus,
    JobSubmit,
)
from scruple.jobs.queue import IdempotencyConflictError, JobQueue, UnknownJobError


class JobOut(BaseModel):
    job: Job
    poll_url: str
    created: bool


Reader = Annotated[Principal, require(Permission.READ)]
Proposer = Annotated[Principal, require(Permission.PROPOSE)]


def jobs_router(queue: JobQueue) -> APIRouter:
    router = APIRouter(prefix="/jobs", tags=["jobs"])

    def _get(job_id: str, p: Principal) -> Job:
        try:
            return queue.get(job_id, p.tenant)
        except UnknownJobError:
            raise HTTPException(404, "unknown job") from None

    @router.post("", status_code=202)
    def submit(
        body: JobSubmit,
        caller: Proposer,
        response: Response,
        idempotency_key: Annotated[
            str | None, Header(pattern=IDEMPOTENCY_PATTERN, max_length=128)
        ] = None,
    ) -> JobOut:
        actor, tenant_id = caller.subject, caller.tenant
        params: dict[str, Any] = dict(body.params)
        gate_eval = body.kind is JobKind.GATE_EVALUATION
        if gate_eval and params.setdefault("actor", actor) != actor:
            raise HTTPException(403, "a gate evaluation runs as the authenticated caller")
        key = body.idempotency_key or idempotency_key
        if body.idempotency_key and idempotency_key and body.idempotency_key != idempotency_key:
            raise HTTPException(400, "Idempotency-Key header and body disagree")
        spec = body.model_copy(update={"params": params, "idempotency_key": key})
        try:
            job, created = queue.submit(spec, tenant_id=tenant_id, actor=actor)
        except ValidationError as exc:  # the kind's own params model refused them
            errors = exc.errors(include_url=False, include_context=False, include_input=False)
            raise RequestValidationError(
                [{**e, "loc": ("body", "params", *e["loc"])} for e in errors]
            ) from None
        except IdempotencyConflictError as exc:
            raise HTTPException(409, str(exc)) from None
        if not created:
            response.status_code = 200
        return JobOut(job=job, poll_url=f"/jobs/{job.job_id}", created=created)

    @router.get("")
    def list_jobs(
        caller: Reader,
        status: JobStatus | None = None,
        rule_id: Annotated[str | None, Query(max_length=128)] = None,
        version: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
    ) -> list[Job]:
        return queue.list_jobs(
            tenant_id=caller.tenant, status=status, rule_id=rule_id, version=version
        )

    @router.get("/{job_id}")
    def get_job(job_id: str, caller: Reader) -> Job:
        return _get(job_id, caller)

    @router.get("/{job_id}/events")
    def job_events(job_id: str, caller: Reader) -> list[JobEvent]:
        return queue.events(_get(job_id, caller).job_id)

    @router.post("/{job_id}/cancel")
    def cancel(job_id: str, caller: Proposer) -> Job:
        job = _get(job_id, caller).job_id
        return queue.cancel(job, tenant_id=caller.tenant, actor=caller.subject)

    return router


__all__ = ["JobOut", "jobs_router"]
