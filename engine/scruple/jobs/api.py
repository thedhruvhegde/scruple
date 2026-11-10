"""HTTP routes for jobs, mounted by `scruple.api.app` with one line.

    POST /jobs                    submit (Idempotency-Key header or body field)
    GET  /jobs                    list (filters: status, rule_id, version)
    GET  /jobs/{job_id}           poll: status, attempts, result when finished
    GET  /jobs/{job_id}/events    the job's append-only history
    POST /jobs/{job_id}/cancel    cancel (queued: at once; running: at the next heartbeat)

Submitting answers 202 for a new job and 200 when an idempotency key returns the
existing one; 409 if that key was used for a different submission. A gate evaluation
runs as the submitting actor (`X-Actor`), never as an actor named in the body.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from scruple.jobs.models import (
    IDEMPOTENCY_PATTERN,
    Job,
    JobEvent,
    JobKind,
    JobStatus,
    JobSubmit,
)
from scruple.jobs.queue import IdempotencyConflictError, JobQueue, UnknownJobError
from scruple.registry.models import ACTOR_PATTERN
from scruple.storage.base import DEFAULT_TENANT


class JobOut(BaseModel):
    job: Job
    poll_url: str
    created: bool


def _actor(x_actor: Annotated[str, Header(pattern=ACTOR_PATTERN, max_length=128)]) -> str:
    return x_actor


Actor = Annotated[str, Depends(_actor)]


def jobs_router(queue: JobQueue, tenant_id: str = DEFAULT_TENANT) -> APIRouter:
    router = APIRouter(prefix="/jobs", tags=["jobs"])

    def _get(job_id: str) -> Job:
        try:
            return queue.get(job_id, tenant_id)
        except UnknownJobError:
            raise HTTPException(404, "unknown job") from None

    @router.post("", status_code=202)
    def submit(
        body: JobSubmit,
        actor: Actor,
        response: Response,
        idempotency_key: Annotated[
            str | None, Header(pattern=IDEMPOTENCY_PATTERN, max_length=128)
        ] = None,
    ) -> JobOut:
        params: dict[str, Any] = dict(body.params)
        gate_eval = body.kind is JobKind.GATE_EVALUATION
        if gate_eval and params.setdefault("actor", actor) != actor:
            raise HTTPException(403, "a gate evaluation runs as X-Actor")
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
        status: JobStatus | None = None,
        rule_id: Annotated[str | None, Query(max_length=128)] = None,
        version: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
    ) -> list[Job]:
        return queue.list_jobs(tenant_id=tenant_id, status=status, rule_id=rule_id, version=version)

    @router.get("/{job_id}")
    def get_job(job_id: str) -> Job:
        return _get(job_id)

    @router.get("/{job_id}/events")
    def job_events(job_id: str) -> list[JobEvent]:
        return queue.events(_get(job_id).job_id)

    @router.post("/{job_id}/cancel")
    def cancel(job_id: str, actor: Actor) -> Job:
        return queue.cancel(_get(job_id).job_id, tenant_id=tenant_id, actor=actor)

    return router


__all__ = ["JobOut", "jobs_router"]
