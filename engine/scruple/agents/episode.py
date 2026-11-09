"""An episode: one simulated dispute, observed, acted on and explained by an agent.

Lifecycle: `observe()` -> one action (`act`) -> `submit(decision)` -> `EpisodeScore`.
Money moves when the action is taken; the score is computed from the journal by
the verifiers. Once submitted (or lapsed) the episode is closed and rejects every
further call.
"""

from __future__ import annotations

from scruple.agents.env import ActionKind, CaseEnvironment, GroundTruth, SimEnvironment
from scruple.agents.models import Decision, EpisodeScore, Observation
from scruple.agents.scoring import score_episode
from scruple.hashing import content_hash


class EpisodeError(Exception):
    """A call the episode refuses. The message is safe to show to the agent."""


class EpisodeClosedError(EpisodeError):
    pass


def make_episode_id(seed: int, case_type: str | None, nonce: int = 0) -> str:
    """Opaque and deterministic. Does not reveal the case type."""
    digest = content_hash({"seed": seed, "case_type": case_type, "nonce": nonce})
    return f"ep-{digest[:12]}"


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
        self.env: CaseEnvironment = env or SimEnvironment()
        self.episode_id = make_episode_id(seed, case_type, nonce)
        self._case = self.env.load(seed, case_type)
        self._action: ActionKind | None = None
        self._score: EpisodeScore | None = None
        self._flags: list[str] = []
        self.tool_calls = 0
        self.tool_errors = 0
        self.info_requests = 0

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
        return (*(k.value for k in ActionKind), "submit_decision")

    def act(self, kind: ActionKind) -> dict[str, object]:
        self._require_open()
        if self._action is not None:
            raise EpisodeError(f"an action was already taken ({self._action.value})")
        self._case.resolve(kind)
        self._action = kind
        return {"ok": True, "action": kind.value, "status": "resolved on the ledger"}

    def submit(self, decision: Decision) -> EpisodeScore:
        self._require_open()
        if self._action is None:
            if decision.action is None:
                raise EpisodeError("take an action first, or include one in the decision")
            self._take_from_decision(decision)
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

    def _take_from_decision(self, decision: Decision) -> None:
        assert decision.action is not None
        self.act(decision.action)

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
