"""A minimal HTTP grader and remote environment for the finance-operations suite.

`build_app(tasks)` serves an exported task set (`scruple tasks serve --tasks DIR`):

- `GET  /health`
- `GET  /v1/envs`: environments, knobs, verifiers (with fingerprints)
- `GET  /v1/tasks?env=...&limit=...`: task ids; `GET /v1/tasks/{task_id}`: one spec
- `POST /v1/grade`: `{"trajectories": [{"task_id", "calls": [...]}, ...]}` -> one grade
  per trajectory, in order (replayed through the engine; claims are never read)
- `POST /v1/episodes`: `{"task_id"}` -> `{"episode_id", "observation"}`, a live episode
  a remote agent plays tool call by tool call
- `POST /v1/episodes/{id}/call`: `{"tool", "args"}` -> the tool result (or
  `{"error"}` with status 400 when the episode refused the call)
- `GET  /v1/episodes/{id}/grade`: the grade of the calls made so far (lapsed if not
  submitted), computed by replay as `/v1/grade` would; an agent gets reward, gate and
  pass/fail per check id only (round 6: explanations name the hidden truth), unless
  the server runs with `full_grades`

Live episodes are held in memory (at most `max_episodes`, oldest dropped). Their
grades come from replaying the recorded calls, so a remote episode and an uploaded
trajectory with the same calls get the same grade. There is no authentication: run
it behind your own gateway. Everything is synthetic.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from scruple.agents.errors import EpisodeError
from scruple.finops.envs import ENVS, SUITE_VERSION, get_env
from scruple.finops.tasks import Call, Grade, TaskSpec, Trajectory, case_salt, grade_safe
from scruple.version import package_version

MAX_BATCH = 1_000


class GradeRequest(BaseModel):
    trajectories: list[Trajectory] = Field(max_length=MAX_BATCH)


class StartRequest(BaseModel):
    task_id: str


class CallRequest(BaseModel):
    tool: str
    args: dict[str, Any] | None = None


class _Live:
    def __init__(self, spec: TaskSpec, salt: str | None) -> None:
        self.spec = spec
        key = case_salt(salt) if spec.keyed and salt is not None else None
        if spec.keyed and key is None:
            raise HTTPException(409, f"{spec.task_id} is keyed; start the server with its salt")
        self.episode = get_env(spec.env).new_episode(spec.seed, spec.case_type, salt=key)
        self.calls: list[Call] = []


def agent_grade(g: Grade) -> dict[str, Any]:
    """The live-episode grade an agent may read: reward, gate, pass/fail per check id.
    No explanations (they name the expected decision, the family, the breaks)."""
    return {
        "task_id": g.task_id,
        "reward": g.reward,
        "gate": g.gate,
        "submitted": g.submitted,
        "checks": [{"id": c.id, "passed": c.passed} for c in g.checks],
        "error": g.error,
    }


def build_app(
    tasks: dict[str, TaskSpec],
    *,
    max_episodes: int = 10_000,
    salt: str | None = None,
    full_grades: bool = False,
) -> FastAPI:
    """`salt` (the export's commitments salt) is needed to serve keyed tasks;
    `full_grades` gives live episodes the full grade (operators only, round 6)."""
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
    def grade(req: GradeRequest) -> dict[str, Any]:
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
        x.calls.append(Call(tool=req.tool, args=req.args))
        try:
            result = x.episode.call(req.tool, req.args)
        except EpisodeError as exc:
            raise HTTPException(400, str(exc)) from None
        if req.tool == "submit_decision":
            return {"submitted": True}  # the scorecard names hidden labels: GET .../grade
        return {"result": result}

    @app.get("/v1/episodes/{episode_id}/grade")
    def episode_grade(episode_id: str) -> dict[str, Any]:
        x = episode_of(episode_id)
        g = grade_safe(x.spec, Trajectory(task_id=x.spec.task_id, calls=tuple(x.calls)), salt=salt)
        return g.model_dump(mode="json") if full_grades else agent_grade(g)

    return app


def serve(
    tasks: dict[str, TaskSpec],
    host: str = "127.0.0.1",
    port: int = 8077,
    *,
    salt: str | None = None,
    full_grades: bool = False,
) -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(build_app(tasks, salt=salt, full_grades=full_grades), host=host, port=port)


__all__ = ["build_app", "serve"]
