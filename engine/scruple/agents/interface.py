"""The agent interface: observe, act, explain.

An agent is any callable `agent(observation, toolbox) -> Decision`. It observes the
whitelisted case features, acts through the toolbox (one action per episode) and
explains itself in the returned `Decision`. Runners for hosted models (Anthropic,
OpenAI, local) drive the same episode through the same tools.
"""

from __future__ import annotations

from collections.abc import Callable

from scruple.agents.env import ActionKind, GroundTruth
from scruple.agents.episode import Episode, EpisodeError
from scruple.agents.models import Decision, EpisodeScore, Observation


class Toolbox:
    """What an in-process agent may touch. It never exposes the episode itself."""

    def __init__(self, episode: Episode, ground_truth: GroundTruth | None = None) -> None:
        self.__episode = episode
        # Only set for reference agents (the oracle) when the caller explicitly asks.
        self.ground_truth = ground_truth

    def act(self, kind: ActionKind | str) -> dict[str, object]:
        return self.__episode.act(ActionKind(kind))


Agent = Callable[[Observation, Toolbox], Decision]
"""Any callable `agent(observation, toolbox) -> Decision`."""


def run_agent(agent: Agent, episode: Episode, *, ground_truth: bool = False) -> EpisodeScore:
    """Run one episode with an in-process agent. Agent exceptions lapse the episode."""
    truth = episode.ground_truth_for_reference() if ground_truth else None
    tools = Toolbox(episode, truth)
    try:
        decision = agent(episode.observe(), tools)
    except Exception:  # any agent failure is scored as a lapse, not raised
        return episode.score or episode.lapse("agent_error")
    if episode.score is not None:
        return episode.score
    try:
        return episode.submit(decision)
    except EpisodeError:
        return episode.lapse("invalid_decision")
