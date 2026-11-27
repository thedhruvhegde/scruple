"""A minimal HTTP grader and remote environment for the finance-operations suite.

`build_app(tasks)` serves an exported task set (`scruple tasks serve --tasks DIR`):

- `GET  /health`
- `GET  /v1/envs`: environments, knobs, verifiers (with fingerprints)
- `GET  /v1/tasks?env=...&limit=...`: task ids; `GET /v1/tasks/{task_id}`: one spec
- `POST /v1/grade` (**operator only**): `{"trajectories": [{"task_id", "calls": [...]},
  ...]}` -> one full grade per trajectory, in order (replayed through the engine;
  claims are never read). Needs `Authorization: Bearer <operator token>`; a server
  started without an operator token refuses it (403).
- `POST /v1/episodes`: `{"task_id"}` -> `{"episode_id", "observation"}`, a live episode
  a remote agent plays tool call by tool call
- `POST /v1/episodes/{id}/call`: `{"tool", "args"}` -> the tool result (or
  `{"error"}` with status 400 when the episode refused the call)
- `GET  /v1/episodes/{id}/grade`: the episode's grade. An agent gets reward, gate and
  pass/fail per check id only, no explanation (explanations name the hidden truth);
  the operator token (or `full_grades`) gets the full grade.

Round 7 (the second blind review found the keyed server a truth oracle: `/v1/grade`
was open and returned explanations naming the truth, and every live episode of a task
was the same case, so an agent could read the answer and replay it, reward 1.000 on
aml): `/v1/grade` is operator-only; every live episode draws a fresh salt, so its case
is a new case of the task's kind, seed and knobs, never the exported one or another
live episode's; and an episode is graded once: asking for the grade closes it (lapsed
if never submitted), so a grade cannot be read mid-episode and acted on. Repeated
requests return the same grade.

Live episodes are held in memory (at most `max_episodes`, oldest dropped). Everything is
synthetic.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from scruple.agents.errors import EpisodeError
from scruple.agents.keys import new_salt
from scruple.finops.envs import ENVS, SUITE_VERSION, get_env
from scruple.finops.tasks import (
    Call,
    Grade,
    GradingError,
    TaskSpec,
    Trajectory,
    case_salt,
    grade_episode,
    grade_safe,
)
from scruple.version import package_version

MAX_BATCH = 1_000
OPERATOR_TOKEN_ENV = "SCRUPLE_OPERATOR_TOKEN"  # noqa: S105 - the variable name, not a secret


class GradeRequest(BaseModel):
    trajectories: list[Trajectory] = Field(max_length=MAX_BATCH)


class StartRequest(BaseModel):
    task_id: str


class CallRequest(BaseModel):
    tool: str
    args: dict[str, Any] | None = None


class _Live:
    """A live episode: a fresh case of the task (kind, seed, knobs) under its own salt."""

    def __init__(self, spec: TaskSpec, salt: str | None) -> None:
        self.spec = spec
        if spec.keyed and salt is None:
            raise HTTPException(409, f"{spec.task_id} is keyed; start the server with its salt")
        base = case_salt(salt) if salt is not None else "unkeyed"
        self.key = f"{base}|live|{new_salt()}"
        self.episode = get_env(spec.env).new_episode(spec.seed, spec.case_type, salt=self.key)
        self.calls: list[Call] = []
        self.grade: Grade | None = None


def agent_grade(g: Grade) -> dict[str, Any]:
    """The grade an agent may read: reward, gate, pass/fail per check id. No
    explanations or scores per check (they name the expected decision, the family,
    the breaks, the truth dispositions)."""
    return {
        "task_id": g.task_id,
        "reward": g.reward,
        "gate": g.gate,
        "submitted": g.submitted,
        "checks": [{"id": c.id, "passed": c.passed} for c in g.checks],
        "error": g.error,
    }


def operator_token_from_env() -> str | None:
    tok = os.environ.get(OPERATOR_TOKEN_ENV, "").strip()
    return tok or None


def build_app(
    tasks: dict[str, TaskSpec],
    *,
    max_episodes: int = 10_000,
    salt: str | None = None,
    full_grades: bool = False,
    operator_token: str | None = None,
) -> FastAPI:
    """`salt` (the export's commitments salt) is needed to serve keyed tasks;
    `operator_token` unlocks `/v1/grade` and full grades (none: both refused to
    everyone); `full_grades` gives every live-episode grade in full (operators only)."""
    app = FastAPI(title="scruple finops grader", version=package_version())
    live: OrderedDict[str, _Live] = OrderedDict()

    def spec_of(tid: str) -> TaskSpec:
        s = tasks.get(tid)
        if s is None:
            raise HTTPException(404, f"unknown task {tid!r}")
        return s

    def episode_of(eid: str) -> _Live:
        x = live.get(eid)
        if x is None:
            raise HTTPException(404, f"unknown episode {eid!r}")
        return x

    def is_operator(authorization: str | None) -> bool:
        if operator_token is None or not authorization:
            return False
        scheme, _, tok = authorization.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(tok.strip(), operator_token)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "tasks": len(tasks), "suite_version": SUITE_VERSION}

    @app.get("/v1/envs")
    def envs() -> dict[str, Any]:
        return {
            "suite_version": SUITE_VERSION,
            "envs": [
                {
                    "env": e.name,
                    "title": e.title,
                    "knobs": sorted(e.knobs),
                    "default_verifier": e.default_verifier,
                    "verifiers": [
                        {"id": v.id, "version": v.version, "fingerprint": v.fingerprint}
                        for v in e.verifiers()
                    ],
                    "tools": [t.name for t in e.tools],
                }
                for e in ENVS.values()
            ],
        }

    @app.get("/v1/tasks")
    def list_tasks(env: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        ids = [t for t, s in tasks.items() if env is None or s.env == env]
        return {"total": len(ids), "task_ids": ids[offset : offset + max(0, min(limit, 1000))]}

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        return spec_of(task_id).model_dump(mode="json")

    @app.post("/v1/grade")
    def grade(
        req: GradeRequest, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        if not is_operator(authorization):
            raise HTTPException(
                403,
                "grading uploaded trajectories is for operators: send the operator token "
                f"(Authorization: Bearer ...; the server reads it from {OPERATOR_TOKEN_ENV} "
                "or --operator-token)",
            )
        out: list[Grade] = [
            grade_safe(tasks.get(t.task_id), t, salt=salt) for t in req.trajectories
        ]
        return {"grades": [g.model_dump(mode="json") for g in out]}

    @app.post("/v1/episodes")
    def start(req: StartRequest) -> dict[str, Any]:
        spec = spec_of(req.task_id)
        eid = "rep-" + secrets.token_hex(8)
        live[eid] = _Live(spec, salt)
        while len(live) > max_episodes:
            live.popitem(last=False)
        return {
            "episode_id": eid,
            "task_id": spec.task_id,
            "observation": live[eid].episode.observe().model_dump(mode="json"),
        }

    @app.post("/v1/episodes/{episode_id}/call")
    def call(episode_id: str, req: CallRequest) -> dict[str, Any]:
        x = episode_of(episode_id)
        if x.grade is not None:
            raise HTTPException(409, "the episode was graded and is closed")
        x.calls.append(Call(tool=req.tool, args=req.args))
        try:
            result = x.episode.call(req.tool, req.args)
        except EpisodeError as exc:
            raise HTTPException(400, str(exc)) from None
        if req.tool == "submit_decision":
            return {"submitted": True}  # the scorecard names hidden labels: GET .../grade
        return {"result": result}

    @app.get("/v1/episodes/{episode_id}/grade")
    def episode_grade(
        episode_id: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        x = episode_of(episode_id)
        if x.grade is None:  # graded once: this closes the episode (lapsed if open)
            try:
                x.grade = grade_episode(x.spec, x.episode)
            except GradingError as exc:
                raise HTTPException(409, str(exc)) from None
        g = x.grade
        return (
            g.model_dump(mode="json")
            if full_grades or is_operator(authorization)
            else agent_grade(g)
        )

    return app


def serve(
    tasks: dict[str, TaskSpec],
    host: str = "127.0.0.1",
    port: int = 8077,
    *,
    salt: str | None = None,
    full_grades: bool = False,
    operator_token: str | None = None,
) -> None:  # pragma: no cover
    import uvicorn

    app = build_app(
        tasks,
        salt=salt,
        full_grades=full_grades,
        operator_token=operator_token or operator_token_from_env(),
    )
    uvicorn.run(app, host=host, port=port)


__all__ = ["OPERATOR_TOKEN_ENV", "agent_grade", "build_app", "operator_token_from_env", "serve"]
