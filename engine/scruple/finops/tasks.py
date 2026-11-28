"""Portable task specs, replay grading and manifests.

Export (`export_tasks`): `tasks.jsonl` with one `TaskSpec` per line, `manifest.json`
(suite and verifier versions, the generation parameters, every task id with its spec
hash, the file's sha256) and `commitments.salt` (private: the salt of each task's
truth commitment; keep it away from the policy being trained). A task spec holds the
seed and case type (which regenerate the episode exactly), the instructions, both
tool schemas, the initial observation, the verifier id, version and fingerprint, the
difficulty, a hash of everything observable in the case (`obs_hash`) and a salted
commitment to its hidden truth (`truth_commitment`). Nothing hidden is in the spec.

Grading (`grade`): a trajectory is `{"task_id": ..., "calls": [{"tool": ..., "args":
{...}}, ...]}`, the tool calls the agent made, in order. The grader regenerates the
episode from the spec, checks `obs_hash` (and, given the salt, the truth commitment)
so generator drift is refused, replays every call through the episode (refused calls
stay refused), lapses the episode if it was never submitted, and scores the record
with the named verifier. What the agent or the lab says about the run is never read:
only the calls, replayed through the engine. Same spec and calls, same grade.
"""

from __future__ import annotations

import contextlib
import json
import random
import secrets
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.agents.keys import restoring_id
from scruple.finops.difficulty import curriculum, measured, nominal
from scruple.finops.envs import SUITE_VERSION, get_env
from scruple.finops.knobs import parse_knobs
from scruple.hashing import sha256_hex
from scruple.version import package_version

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class VerifierRef(BaseModel):
    model_config = _FROZEN
    id: str
    version: str
    fingerprint: str


class Difficulty(BaseModel):
    model_config = _FROZEN
    nominal: float
    measured: float | None = None
    panel: dict[str, float] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    model_config = _FROZEN

    task_id: str
    env: str
    suite_version: str
    seed: int
    case_type: str | None
    knobs: dict[str, Any]
    difficulty: Difficulty
    instructions: str
    observation: dict[str, Any]
    tools: dict[str, list[dict[str, Any]]]  # {"anthropic": [...], "openai": [...]}
    verifier: VerifierRef
    obs_hash: str
    truth_commitment: str
    max_tool_calls: int | None = None
    synthetic: bool = True
    # round 6: a keyed task's case is generated from HMAC(case salt, seed), so the
    # public seed does not rebuild it; regenerating (grading, serving) needs the salt
    keyed: bool = False

    def spec_hash(self) -> str:
        return sha256_hex(self.model_dump_json())


class Call(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    tool: str
    args: Any = None


class Trajectory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    task_id: str
    calls: tuple[Call, ...] = ()


class CheckRow(BaseModel):
    model_config = _FROZEN
    id: str
    hard: bool
    passed: bool
    score: float
    explanation: str = ""


class Grade(BaseModel):
    model_config = _FROZEN

    task_id: str
    env: str
    verifier: VerifierRef
    reward: float
    gate: bool
    hard_failures: tuple[str, ...]
    checks: tuple[CheckRow, ...]
    submitted: bool
    lapsed: bool
    calls: int
    tool_errors: int
    trace_head: str
    error: str | None = None


# --- hashes ------------------------------------------------------------------------


def _truth(ep: Any) -> Any:
    return ep.case.truth.model_dump(mode="json")


def obs_hash(ep: Any) -> str:
    """Everything observable about the case: the case without its truth, plus any
    artefacts the episode serves."""
    raw = ep.case.model_dump(mode="json")
    raw.pop("truth", None)
    extra = [a.model_dump(mode="json") for a in getattr(ep, "inbox", ())] + [
        a.model_dump(mode="json") for a in getattr(ep, "notes", ())
    ]
    for a in extra:
        a.pop("kind", None)
        a.pop("adversarial", None)
    return sha256_hex(json.dumps({"case": raw, "artefacts": extra}, sort_keys=True))


def truth_commitment(ep: Any, salt: str) -> str:
    return sha256_hex(salt + "|" + json.dumps(_truth(ep), sort_keys=True))


def case_salt(salt: str) -> str:
    """The case key of a keyed export, derived from its (private) commitments salt."""
    return "case|" + salt


def _episode(spec_env: str, seed: int, case_type: str | None, keyed: bool, salt: str | None) -> Any:
    if keyed and salt is None:
        raise GradingError("a keyed task is regenerated only with its export salt")
    return get_env(spec_env).new_episode(
        seed, case_type, salt=case_salt(salt) if keyed and salt is not None else None
    )


def task_id(env: str, seed: int, case_type: str | None) -> str:
    return f"fo-{env}-" + sha256_hex(f"{SUITE_VERSION}|{env}|{seed}|{case_type}")[:12]


# --- export ------------------------------------------------------------------------


def build_task(
    env: str,
    seed: int,
    case_type: str | None,
    salt: str,
    *,
    verifier: str | None = None,
    measure: bool = True,
    keyed: bool = False,
) -> TaskSpec:
    e = get_env(env)
    ver = e.verifier(verifier)
    # the spec's observation shows an episode id: keyed by the export salt, so the export
    # is deterministic and the id says nothing about the seed (round 6)
    with restoring_id("ep-" + sha256_hex(f"{salt}|{task_id(env, seed, case_type)}")[:12]):
        ep = _episode(env, seed, case_type, keyed, salt)
    k = parse_knobs(case_type, env)
    diff = Difficulty(nominal=nominal(env, case_type))
    if measure:
        m, panel = measured(env, seed, case_type)
        diff = Difficulty(nominal=diff.nominal, measured=m, panel=panel)
    instructions = e.task
    if k.h is not None:
        instructions += (
            f" You have a budget of {k.h} tool calls (submit_decision is free); when it is "
            "spent the episode ends as it stands."
        )
    return TaskSpec(
        task_id=task_id(env, seed, case_type),
        env=env,
        suite_version=SUITE_VERSION,
        seed=seed,
        case_type=case_type,
        knobs=k.as_dict(),
        difficulty=diff,
        instructions=instructions,
        observation=ep.observe().model_dump(mode="json"),
        tools={"anthropic": e.anthropic_tools(), "openai": e.openai_tools()},
        verifier=VerifierRef(id=ver.id, version=ver.version, fingerprint=ver.fingerprint),
        obs_hash=obs_hash(ep),
        truth_commitment=truth_commitment(ep, salt),
        max_tool_calls=k.h,
        keyed=keyed,
    )


def plan_tasks(
    env: str,
    n: int,
    difficulty: tuple[float, float],
    *,
    seed0: int = 0,
    case_types: Sequence[str] | None = None,
    horizon: bool = False,
) -> list[tuple[int, str | None]]:
    """(seed, case type) for `n` tasks: dev seeds `seed0..seed0+n-1`; difficulty
    spread evenly over `[lo, hi]` along the curriculum (deterministic), unless explicit
    case types are given (cycled)."""
    lo, hi = difficulty
    if not 0.0 <= lo <= hi <= 1.0:
        raise ValueError("difficulty range must satisfy 0 <= lo <= hi <= 1")
    out: list[tuple[int, str | None]] = []
    for i in range(n):
        if case_types:
            ct: str | None = case_types[i % len(case_types)]
        else:
            d = lo if n == 1 or lo == hi else lo + (hi - lo) * i / (n - 1)
            ct = curriculum(env, round(d, 4), horizon=horizon)
        out.append((seed0 + i, ct))
    return out


def _build_one(item: tuple[str, int, str | None, str, str | None, bool, bool]) -> str:
    env, seed, ct, salt, ver, measure, keyed = item
    spec = build_task(env, seed, ct, salt, verifier=ver, measure=measure, keyed=keyed)
    return spec.model_dump_json()


def export_tasks(
    env: str,
    n: int,
    out: Path,
    *,
    difficulty: tuple[float, float] = (0.0, 1.0),
    seed0: int = 0,
    case_types: Sequence[str] | None = None,
    verifier: str | None = None,
    measure: bool = True,
    horizon: bool = False,
    workers: int = 1,
    salt: str | None = None,
    keyed: bool = False,
) -> dict[str, Any]:
    from scruple.finops.parallel import pmap

    out.mkdir(parents=True, exist_ok=True)
    salt = salt or secrets.token_hex(16)
    plan = plan_tasks(env, n, difficulty, seed0=seed0, case_types=case_types, horizon=horizon)
    items = [(env, s, ct, salt, verifier, measure, keyed) for s, ct in plan]
    lines = pmap(_build_one, items, workers=workers)
    path = out / "tasks.jsonl"
    body = "".join(line + "\n" for line in lines)
    path.write_text(body)
    specs = [TaskSpec.model_validate_json(line) for line in lines]
    ver = specs[0].verifier if specs else None
    manifest = {
        "suite": "scruple-finops",
        "suite_version": SUITE_VERSION,
        "scruple_version": package_version(),
        "env": env,
        "n": len(specs),
        "seeds": [seed0, seed0 + n - 1] if n else [],
        "seed_space": "dev (public; not the sealed benchmark)",
        "keyed": keyed,
        "difficulty_range": list(difficulty),
        "horizon": horizon,
        "case_types": list(case_types) if case_types else None,
        "verifier": None if ver is None else ver.model_dump(),
        "files": {"tasks.jsonl": sha256_hex(body)},
        "tasks": {s.task_id: s.spec_hash() for s in specs},
        "synthetic": True,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    (out / "commitments.salt").write_text(salt + "\n")
    return manifest


def load_tasks(path: Path) -> dict[str, TaskSpec]:
    """Tasks from a `tasks.jsonl` file or an export directory."""
    p = path / "tasks.jsonl" if path.is_dir() else path
    specs = (TaskSpec.model_validate_json(x) for x in p.read_text().splitlines() if x.strip())
    return {s.task_id: s for s in specs}


def verify_manifest(directory: Path) -> list[str]:
    """Problems with an export directory (empty when the files match the manifest)."""
    m = json.loads((directory / "manifest.json").read_text())
    body = (directory / "tasks.jsonl").read_text()
    problems = []
    if sha256_hex(body) != m["files"]["tasks.jsonl"]:
        problems.append("tasks.jsonl does not match the manifest hash")
    specs = load_tasks(directory)
    if set(specs) != set(m["tasks"]):
        problems.append("task ids differ from the manifest")
    for tid, s in specs.items():
        if m["tasks"].get(tid) != s.spec_hash():
            problems.append(f"{tid}: spec hash differs")
    return problems


# --- grading ------------------------------------------------------------------------


class GradingError(ValueError):
    pass


def replay(spec: TaskSpec, calls: Iterable[Call], *, salt: str | None = None) -> Any:
    """The episode the calls produce, closed (lapsed if never submitted)."""
    from scruple.agents.errors import EpisodeError

    ep = _episode(spec.env, spec.seed, spec.case_type, spec.keyed, salt)
    if obs_hash(ep) != spec.obs_hash:
        raise GradingError(f"{spec.task_id}: the regenerated case differs from the spec")
    if salt is not None and truth_commitment(ep, salt) != spec.truth_commitment:
        raise GradingError(f"{spec.task_id}: the truth commitment does not match")
    for c in calls:
        if ep.closed:
            break
        # refused calls are part of the trajectory; the trace records them
        with contextlib.suppress(EpisodeError):
            ep.call(c.tool, c.args)
    if not ep.closed:
        ep.lapse("not_submitted")
    return ep


def _verifier_for(spec: TaskSpec, verifier: str | None) -> Any:
    e = get_env(spec.env)
    ver = e.verifier(verifier or spec.verifier.id)
    if verifier is None and ver.fingerprint != spec.verifier.fingerprint:
        raise GradingError(
            f"{spec.task_id}: verifier {ver.id} fingerprint {ver.fingerprint} differs from "
            f"the spec's {spec.verifier.fingerprint}; grade with the matching scruple version "
            "or pass a verifier explicitly"
        )
    return ver


def grade(
    spec: TaskSpec, traj: Trajectory, *, salt: str | None = None, verifier: str | None = None
) -> Grade:
    ver = _verifier_for(spec, verifier)
    ep = replay(spec, traj.calls, salt=salt)
    return _grade_closed(spec, ep, ver)


def grade_episode(spec: TaskSpec, ep: Any, *, verifier: str | None = None) -> Grade:
    """The grade of an in-process episode of `spec` (a live episode of the task server,
    played on its own per-episode case): lapsed first if it was never submitted."""
    ver = _verifier_for(spec, verifier)
    if not ep.closed:
        ep.lapse("not_submitted")
    return _grade_closed(spec, ep, ver)


def _grade_closed(spec: TaskSpec, ep: Any, ver: Any) -> Grade:
    rec = ep.record()
    card = ver.score(rec)
    return Grade(
        task_id=spec.task_id,
        env=spec.env,
        verifier=VerifierRef(id=ver.id, version=ver.version, fingerprint=ver.fingerprint),
        reward=round(card.reward, 6),
        gate=card.gate,
        hard_failures=tuple(c.id for c in card.checks if c.hard and not c.passed),
        checks=tuple(
            CheckRow(
                id=c.id,
                hard=c.hard,
                passed=c.passed,
                score=round(c.score, 6),
                explanation=c.explanation,
            )
            for c in card.checks
        ),
        submitted=not rec.lapsed,
        lapsed=rec.lapsed,
        calls=ep.tool_calls,
        tool_errors=ep.tool_errors,
        trace_head=ep.trace.head,
    )


def grade_safe(spec: TaskSpec | None, traj: Trajectory, **kw: Any) -> Grade:
    """`grade`, with an unknown task or a refused grading as a zero-reward row."""
    if spec is None:
        return _error_row(traj.task_id, "", "unknown task id")
    try:
        return grade(spec, traj, **kw)
    except GradingError as exc:
        return _error_row(traj.task_id, spec.env, str(exc))


def _error_row(tid: str, env: str, why: str) -> Grade:
    return Grade(
        task_id=tid,
        env=env,
        verifier=VerifierRef(id="", version="", fingerprint=""),
        reward=0.0,
        gate=False,
        hard_failures=(),
        checks=(),
        submitted=False,
        lapsed=True,
        calls=0,
        tool_errors=0,
        trace_head="",
        error=why,
    )


def read_trajectories(path: Path) -> Iterator[Trajectory]:
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            yield Trajectory.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"{path}:{n}: not a trajectory ({exc})") from exc


def trajectory_of(ep: Any, tid: str) -> Trajectory:
    """The trajectory a closed in-process episode played (from its trace)."""
    from scruple.verifiers.trace import EventKind

    calls = [
        Call(tool=str(e.tool), args=e.args)
        for e in ep.trace
        if e.kind is EventKind.TOOL_CALL and e.actor.value == "agent"
    ]
    return Trajectory(task_id=tid, calls=tuple(calls))


def rollout(spec: TaskSpec, policy: str, *, salt: str | None = None) -> Trajectory:
    """Play a scripted policy on a task and return its trajectory (a keyed task needs
    its export salt)."""
    from scruple.finops.policies import run_policy

    e = get_env(spec.env)
    if spec.keyed and salt is None:
        raise GradingError("a keyed task is regenerated only with its export salt")
    key = case_salt(salt) if spec.keyed and salt is not None else None
    ep = run_policy(e.episode_cls, e.policies()[policy], spec.seed, spec.case_type, salt=key)
    return trajectory_of(ep, spec.task_id)


def sample_case_types(env: str, n: int, rng: random.Random) -> list[str]:
    return [curriculum(env, round(rng.random(), 3)) for _ in range(n)]


__all__ = [
    "Call",
    "Grade",
    "GradingError",
    "TaskSpec",
    "Trajectory",
    "build_task",
    "export_tasks",
    "grade",
    "grade_episode",
    "grade_safe",
    "load_tasks",
    "obs_hash",
    "plan_tasks",
    "read_trajectories",
    "replay",
    "rollout",
    "task_id",
    "trajectory_of",
    "truth_commitment",
    "verify_manifest",
]
