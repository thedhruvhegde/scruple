"""Play benchmark episodes with any agent and reduce each to one compact row (174).

An agent is named by a string:

- a built-in reference policy from `scruple.agents.dispute_baselines`: `evidence`,
  `always_accept`, or `oracle` (a declared reference run: reads ground truth);
- `package.module:callable`: a Python callable `policy(episode) -> None` that plays a
  `DisputeEpisode` through `episode.call(tool, args)`. An optional `report` attribute
  on the callable (`report(episode) -> SelfReport`) replaces the honest report;
- `cli:<command>`: an external program speaking JSON lines on stdin/stdout. Per case
  it receives `{"type": "episode", "episode_id", "observation", "tools"}`, answers with
  `{"tool": name, "args": {...}}` lines and gets `{"type": "result", "ok", "result" or
  "error"}` back, until it calls `submit_decision` (reply `{"type": "closed"}`). At
  the end it receives `{"type": "done"}`. A CLI agent sees only what the tools return:
  no seed, no ground truth, no score.

Episodes run in blocks of `BLOCK` consecutive cases. The batch checks (consistency,
calibration, batch self-report) run per block, so their verdicts do not depend on how
many worker processes there are. Everything is synthetic.
"""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from multiprocessing import get_context
from typing import IO, Any

from pydantic import BaseModel, ConfigDict

from scruple.agents.dispute_baselines import (
    DisputePolicy,
    always_accept,
    evidence_policy,
    honest_report,
    oracle_policy,
    run_policy,
)
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.dispute_tools import DISPUTE_TOOL_NAMES
from scruple.agents.errors import EpisodeError
from scruple.bench.spec import BenchCase, case_digest, case_of, tier_of
from scruple.verifiers.dispute import BATCH_CHECKS, score_record
from scruple.verifiers.record import EpisodeRecord

BLOCK = 500
MAX_CALLS = 200  # per episode for a CLI agent; then the episode lapses

BUILTIN: dict[str, DisputePolicy] = {
    "evidence": evidence_policy,
    "always_accept": always_accept,
    "oracle": oracle_policy,
}
REFERENCE_RUNS = frozenset({"oracle"})  # may read ground truth (declared)

Extra = Callable[[EpisodeRecord], dict[str, float]]


class BenchAgentError(RuntimeError):
    """The agent could not be loaded or broke the protocol."""


class CaseRow(BaseModel):
    """One episode, reduced. Money in USD minor units (the merchant's cost)."""

    model_config = ConfigDict(frozen=True)

    index: int
    family: str
    tier: str
    digest: str  # case digest, checked against the manifest
    reward: float
    quality: float
    gate: bool
    hard_failures: tuple[str, ...]
    failed: tuple[str, ...]
    cost: int
    baseline_cost: int | None  # the evidence policy on the same case and issuer draws
    correct: bool
    won: bool
    lapsed: bool
    tool_calls: int
    amount_minor: int
    extra: dict[str, float] = {}


class BlockResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: int
    rows: tuple[CaseRow, ...]
    batch: dict[str, bool]  # batch check id -> passed on this block


# --- agents -----------------------------------------------------------------------------


class CliAgent:
    """An external agent process, started on first use (one per worker process)."""

    def __init__(self, command: str) -> None:
        self.command = command
        self._proc: subprocess.Popen[str] | None = None

    def __getstate__(self) -> dict[str, Any]:
        return {"command": self.command, "_proc": None}

    def _start(self) -> subprocess.Popen[str]:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(  # noqa: S603 - the operator names the agent
                shlex.split(self.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        return self._proc

    @staticmethod
    def _send(stream: IO[str] | None, msg: dict[str, Any]) -> None:
        assert stream is not None
        stream.write(json.dumps(msg, default=str, sort_keys=True) + "\n")
        stream.flush()

    def _recv(self, proc: subprocess.Popen[str]) -> dict[str, Any]:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line:
            raise BenchAgentError(f"agent {self.command!r} exited or closed its output")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchAgentError(f"agent sent invalid JSON: {line[:200]!r}") from exc
        if not isinstance(msg, dict) or not isinstance(msg.get("tool"), str):
            raise BenchAgentError(f"agent message must be {{'tool': ..., 'args': ...}}: {msg!r}")
        return msg

    def __call__(self, ep: DisputeEpisode) -> None:
        proc = self._start()
        self._send(
            proc.stdin,
            {
                "type": "episode",
                "episode_id": ep.episode_id,
                "observation": ep.observe().model_dump(mode="json"),
                "tools": list(DISPUTE_TOOL_NAMES),
            },
        )
        for _ in range(MAX_CALLS):
            msg = self._recv(proc)
            tool, args = msg["tool"], msg.get("args")
            try:
                result = ep.call(tool, args)
            except EpisodeError as exc:
                self._send(proc.stdin, {"type": "result", "ok": False, "error": str(exc)})
                continue
            if ep.closed:
                self._send(proc.stdin, {"type": "closed"})
                return
            self._send(proc.stdin, {"type": "result", "ok": True, "result": result})
        ep.lapse("call limit")
        self._send(proc.stdin, {"type": "closed", "lapsed": True})

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._send(self._proc.stdin, {"type": "done"})
                assert self._proc.stdin is not None
                self._proc.stdin.close()
                self._proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - misbehaving agent
                self._proc.kill()
        self._proc = None


@dataclass(frozen=True)
class Agent:
    name: str
    policy: DisputePolicy
    reference: bool = False


def load_agent(spec: str) -> Agent:
    """Resolve `evidence`, `module:callable` or `cli:<command>`."""
    if spec in BUILTIN:
        return Agent(spec, BUILTIN[spec], spec in REFERENCE_RUNS)
    if spec.startswith("cli:"):
        cmd = spec[4:].strip()
        if not cmd:
            raise BenchAgentError("cli: needs a command")
        return Agent(spec, CliAgent(cmd))
    mod_name, sep, attr = spec.partition(":")
    if not sep or not mod_name or not attr:
        raise BenchAgentError(
            f"unknown agent {spec!r}: use one of {', '.join(BUILTIN)}, module:callable "
            "or cli:<command>"
        )
    try:
        obj: Any = importlib.import_module(mod_name)
        for part in attr.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError) as exc:
        raise BenchAgentError(f"cannot load agent {spec!r}: {exc}") from exc
    if not callable(obj):
        raise BenchAgentError(f"agent {spec!r} is not callable")
    return Agent(spec, obj)


# --- episodes ---------------------------------------------------------------------------


def play_case(agent: Agent, bc: BenchCase) -> EpisodeRecord:
    report = getattr(agent.policy, "report", honest_report)

    def guarded(ep: DisputeEpisode) -> None:
        try:
            agent.policy(ep)
        except EpisodeError:
            if not ep.closed:
                ep.lapse("agent error")

    ep = run_policy(
        guarded, bc.seed, None, report=report, reference=agent.reference, id_prefix=bc.id_prefix
    )
    return ep.record()


def row_of(bc: BenchCase, rec: EpisodeRecord, extra: Extra | None = None) -> CaseRow:
    card = score_record(rec)
    case = case_of(bc)
    return CaseRow(
        index=bc.index,
        family=case.truth.family.value,
        tier=tier_of(case),
        digest=case_digest(case),
        reward=card.reward,
        quality=card.quality,
        gate=card.gate,
        hard_failures=card.hard_failures,
        failed=card.failed,
        cost=rec.cost_usd_minor,
        baseline_cost=None if rec.baseline is None else rec.baseline.cost_usd_minor,
        correct=rec.outcome is not None and rec.outcome.correct,
        won=rec.merchant_won,
        lapsed=rec.lapsed,
        tool_calls=len(rec.tool_calls()),
        amount_minor=rec.case.amount_minor,
        extra={} if extra is None else extra(rec),
    )


def run_block(agent: Agent, cases: Sequence[BenchCase], extra: Extra | None = None) -> BlockResult:
    records = [play_case(agent, bc) for bc in cases]
    if isinstance(agent.policy, CliAgent):
        agent.policy.close()
    batch = {b.id: b.run_batch(records).passed for b in BATCH_CHECKS}
    rows = tuple(row_of(bc, r, extra) for bc, r in zip(cases, records, strict=True))
    return BlockResult(start=cases[0].index, rows=rows, batch=batch)


def blocks(cases: Sequence[BenchCase], size: int = BLOCK) -> list[list[BenchCase]]:
    """Consecutive blocks aligned on benchmark positions (index // size)."""
    out: dict[int, list[BenchCase]] = {}
    for c in cases:
        out.setdefault(c.index // size, []).append(c)
    return [out[k] for k in sorted(out)]


def run_agent(
    agent: Agent,
    cases: Sequence[BenchCase],
    *,
    jobs: int = 1,
    extra: Extra | None = None,
    block: int = BLOCK,
) -> list[BlockResult]:
    """Play every case; results in benchmark order. `jobs > 1` forks worker processes."""
    parts = blocks(cases, block)
    if jobs <= 1 or len(parts) <= 1:
        return [run_block(agent, p, extra) for p in parts]
    with ProcessPoolExecutor(max_workers=jobs, mp_context=get_context("fork")) as pool:
        futures = [pool.submit(run_block, agent, p, extra) for p in parts]
        return [f.result() for f in futures]


__all__ = [
    "BLOCK",
    "BUILTIN",
    "MAX_CALLS",
    "Agent",
    "BenchAgentError",
    "BlockResult",
    "CaseRow",
    "CliAgent",
    "Extra",
    "blocks",
    "load_agent",
    "play_case",
    "row_of",
    "run_agent",
    "run_block",
]
