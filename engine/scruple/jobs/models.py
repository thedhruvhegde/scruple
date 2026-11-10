"""Job records and the parameters each kind accepts. Inputs are frozen and strict."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.registry.models import ACTOR_PATTERN, RULE_ID_PATTERN

IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
TENANT_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"


class JobKind(StrEnum):
    GATE_EVALUATION = "gate_evaluation"  # shadow plus canary for one candidate version
    BACKTEST = "backtest"  # replay a history against one rule version
    BENCH_SCORE = "bench_score"  # score a built-in agent on the frozen benchmark


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class _Params(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GateEvaluationParams(_Params):
    rule_id: str = Field(pattern=RULE_ID_PATTERN)
    version: int = Field(ge=1, le=2**31 - 1)
    actor: str = Field(pattern=ACTOR_PATTERN)


class SyntheticHistory(_Params):
    """A SYNTHETIC Stripe-shaped history (`scruple backtest synth`)."""

    kind: Literal["synthetic"] = "synthetic"
    n: int = Field(default=2_000, ge=1, le=100_000)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)


class CaseStoreHistory(_Params):
    """The case store the worker was started with (`--case-store`). The path is the
    operator's, never the submitter's: an API caller cannot point a job at a file."""

    kind: Literal["case_store"] = "case_store"


class BacktestParams(_Params):
    """Backtest one registered rule version alone (as shadow mode judges it)."""

    rule_id: str = Field(pattern=RULE_ID_PATTERN)
    version: int = Field(ge=1, le=2**31 - 1)
    history: SyntheticHistory | CaseStoreHistory = Field(discriminator="kind")
    k: int = Field(default=10, ge=1, le=1_000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class BenchScoreParams(_Params):
    """Built-in agents only: a submitted job never names code to import."""

    agent: Literal["evidence", "always_accept", "oracle"]
    limit: int | None = Field(default=None, ge=1, le=1_000_000)


PARAMS: dict[JobKind, type[_Params]] = {
    JobKind.GATE_EVALUATION: GateEvaluationParams,
    JobKind.BACKTEST: BacktestParams,
    JobKind.BENCH_SCORE: BenchScoreParams,
}


def validate_params(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Canonical params for a known kind (raises pydantic.ValidationError, ValueError).
    Unknown kinds pass through unchanged: workers may register their own."""
    try:
        model = PARAMS[JobKind(kind)]
    except ValueError:
        return params
    out: dict[str, Any] = model.model_validate(params).model_dump(mode="json")
    return out


class JobSubmit(BaseModel):
    """What a client submits. `idempotency_key` makes a retry of the same submission
    return the first job instead of queueing a second one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: JobKind
    params: dict[str, Any]
    idempotency_key: str | None = Field(default=None, pattern=IDEMPOTENCY_PATTERN)
    max_attempts: int = Field(default=3, ge=1, le=10)
    timeout_s: int = Field(default=3_600, ge=1, le=86_400)


class Job(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    tenant_id: str
    kind: str
    params: dict[str, Any]
    idempotency_key: str | None
    status: JobStatus
    attempts: int
    max_attempts: int
    timeout_s: int
    run_after: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    result: dict[str, Any] | None
    result_sha256: str | None
    error: str | None
    rule_id: str | None  # set for kinds about one rule version (evidence-pack link)
    version: int | None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def number(self) -> int:
        return int(self.job_id.removeprefix("job-"))


class JobEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    at: datetime
    event: str
    worker: str | None
    detail: dict[str, Any]


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


__all__ = [
    "PARAMS",
    "TERMINAL",
    "BacktestParams",
    "BenchScoreParams",
    "CaseStoreHistory",
    "GateEvaluationParams",
    "Job",
    "JobEvent",
    "JobKind",
    "JobStatus",
    "JobSubmit",
    "SyntheticHistory",
    "canonical",
    "validate_params",
]
