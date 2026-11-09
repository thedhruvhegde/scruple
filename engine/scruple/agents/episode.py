"""An episode: one simulated dispute, observed, acted on and explained by an agent.

Lifecycle: `observe()` -> read tools -> one action -> `submit(decision)` ->
`EpisodeScore`. Money moves when the action is taken; the score is computed from the
journal by the verifiers. Once submitted (or lapsed) the episode is closed and
rejects every further call, reads included.

`call(name, args)` is the single entry point every runner, adapter, MCP server and
the CLI use. Each call is logged, so an episode can be rebuilt exactly by replaying
its log (`Episode.replay`), which is how the CLI persists episodes between commands.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.agents.env import ActionKind, CaseEnvironment, GroundTruth, SimEnvironment
from scruple.agents.errors import EpisodeClosedError, EpisodeError, ToolArgumentError
from scruple.agents.models import Decision, EpisodeScore, Observation
from scruple.agents.scoring import score_episode
from scruple.agents.tools import (
    TOOL_BY_NAME,
    TOOL_NAMES,
    GetEvidenceArgs,
    RepresentArgs,
    RequestInfoArgs,
    Scope,
    ToolSpec,
)
from scruple.hashing import content_hash

__all__ = [
    "CallRecord",
    "Episode",
    "EpisodeClosedError",
    "EpisodeError",
    "ToolArgumentError",
    "make_episode_id",
]


def make_episode_id(seed: int, case_type: str | None, nonce: int = 0) -> str:
    """Opaque and deterministic. Does not reveal the case type."""
    digest = content_hash({"seed": seed, "case_type": case_type, "nonce": nonce})
    return f"ep-{digest[:12]}"


class CallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str
    args: Any
    ok: bool
    error: str | None = None


class Episode:
    def __init__(
        self,
        seed: int,
        case_type: str | None = None,
        *,
        env: CaseEnvironment | None = None,
        nonce: int = 0,
    ) -> None:
        self.seed = seed
        self.case_type = case_type
        self.nonce = nonce
        self.env: CaseEnvironment = env or SimEnvironment()
        self.episode_id = make_episode_id(seed, case_type, nonce)
        self._case = self.env.load(seed, case_type)
        self._action: ActionKind | None = None
        self._cited: tuple[str, ...] = ()
        self._score: EpisodeScore | None = None
        self._flags: list[str] = []
        self.log: list[CallRecord] = []
        self.tool_calls = 0
        self.tool_errors = 0
        self.info_requests = 0

    @classmethod
    def replay(
        cls,
        seed: int,
        case_type: str | None,
        calls: Sequence[tuple[str, Any]],
        *,
        nonce: int = 0,
        env: CaseEnvironment | None = None,
    ) -> Episode:
        """Rebuild an episode by replaying its call log (errors replay as errors)."""
        ep = cls(seed, case_type, nonce=nonce, env=env)
        for tool, args in calls:
            with contextlib.suppress(EpisodeError):
                ep.call(tool, args)
        return ep

    # --- state ---------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._score is not None

    @property
    def action(self) -> ActionKind | None:
        return self._action

    @property
    def score(self) -> EpisodeScore | None:
        return self._score

    def _require_open(self) -> None:
        if self._score is not None:
            raise EpisodeClosedError("episode is closed: the decision was already submitted")

    # --- observe, act, explain ----------------------------------------------------------

    def observe(self) -> Observation:
        return Observation(
            episode_id=self.episode_id,
            fields=self._case.observable(),
            tools=self.tool_names(),
        )

    def tool_names(self) -> tuple[str, ...]:
        return TOOL_NAMES

    def call(self, name: str, args: Any = None) -> dict[str, Any]:
        """Run one tool. Raises `EpisodeError` (safe to show the agent) on refusal."""
        try:
            self._require_open()
            self.tool_calls += 1
            spec = TOOL_BY_NAME.get(name)
            if spec is None:
                raise EpisodeError(f"unknown tool {name!r}; tools are {', '.join(TOOL_NAMES)}")
            if args is not None and not isinstance(args, Mapping):
                raise ToolArgumentError(f"arguments for {name} must be a JSON object")
            result = self._dispatch(spec, spec.parse(args))
        except EpisodeError as exc:
            if not isinstance(exc, EpisodeClosedError):
                self.tool_errors += 1
            self.log.append(CallRecord(tool=name, args=args, ok=False, error=str(exc)))
            raise
        self.log.append(CallRecord(tool=name, args=args, ok=True))
        return result

    def act(self, kind: ActionKind, evidence_ids: Sequence[str] = ()) -> dict[str, Any]:
        self._require_open()
        if self._action is not None:
            raise EpisodeError(f"an action was already taken ({self._action.value})")
        cited = tuple(evidence_ids)
        if kind is ActionKind.REPRESENT and cited:
            known = {d.evidence_id for d in self._case.evidence()}
            unknown = [e for e in cited if e not in known]
            if unknown:
                self._flags.append("unknown_evidence_probe")
                raise EpisodeError(f"unknown evidence id(s): {', '.join(unknown)}")
            if len(set(cited)) != len(cited):
                raise EpisodeError("evidence ids must be unique")
        self._case.resolve(kind)
        self._action = kind
        self._cited = cited
        return {"ok": True, "action": kind.value, "status": "resolved on the ledger"}

    def submit(self, decision: Decision) -> EpisodeScore:
        self._require_open()
        if self._action is None:
            if decision.action is None:
                raise EpisodeError("take an action first, or include one in the decision")
            self.act(decision.action, decision.evidence_ids)
        return self._close(decision)

    def lapse(self, reason: str) -> EpisodeScore:
        """Close without a decision (turn limit, budget, refusal). The response
        deadline passes, so liability is accepted by default."""
        self._require_open()
        self._flags += ["lapsed", f"lapsed:{reason}"]
        if self._action is None:
            self._case.resolve(ActionKind.ACCEPT_LIABILITY)
        return self._close(Decision(explanation=f"lapsed: {reason}"))

    def ground_truth_for_reference(self) -> GroundTruth:
        """Hidden labels, for reference agents (the oracle baseline) only."""
        return self._case.ground_truth()

    # --- internals ------------------------------------------------------------------

    def _dispatch(self, spec: ToolSpec, args: BaseModel) -> dict[str, Any]:
        if spec.scope is Scope.INFO and self._action is not None:
            raise EpisodeError(f"{spec.name} is only allowed before the action is taken")
        match spec.name:
            case "get_case":
                return {
                    "episode_id": self.episode_id,
                    "fields": self._case.observable(),
                    "action_taken": None if self._action is None else self._action.value,
                    "synthetic": True,
                }
            case "list_evidence":
                return {
                    "evidence": [
                        {"evidence_id": d.evidence_id, "kind": d.kind, "title": d.title}
                        for d in self._case.evidence()
                    ]
                }
            case "get_evidence":
                assert isinstance(args, GetEvidenceArgs)
                for d in self._case.evidence():
                    if d.evidence_id == args.evidence_id:
                        return d.model_dump(mode="json")
                self._flags.append("unknown_evidence_probe")
                raise EpisodeError(f"unknown evidence id {args.evidence_id!r}")
            case "get_ledger_view":
                return self._case.ledger_view().model_dump(mode="json")
            case "request_info":
                assert isinstance(args, RequestInfoArgs)
                self.info_requests += 1
                return self._case.request_info(args.question).model_dump(mode="json")
            case "submit_decision":
                assert isinstance(args, Decision)
                return self.submit(args).model_dump(mode="json")
        assert spec.action is not None
        cited = args.evidence_ids if isinstance(args, RepresentArgs) else ()
        return self.act(spec.action, cited)

    def _close(self, decision: Decision) -> EpisodeScore:
        self._score = score_episode(
            episode_id=self.episode_id,
            seed=self.seed,
            case=self._case,
            action=self._action,
            decision=decision,
            flags=self._flags,
            tool_calls=self.tool_calls,
            tool_errors=self.tool_errors,
            info_requests=self.info_requests,
        )
        return self._score
