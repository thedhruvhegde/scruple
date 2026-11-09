"""Agent-facing side of scruple: episodes, tools, runners and baselines.

Every number an episode produces comes from the synthetic simulator.
"""

from scruple.agents.env import ActionKind, GroundTruth, SimEnvironment
from scruple.agents.episode import Episode, EpisodeClosedError, EpisodeError
from scruple.agents.interface import Agent, Toolbox, run_agent
from scruple.agents.models import Decision, EpisodeScore, Observation

__all__ = [
    "ActionKind",
    "Agent",
    "Decision",
    "Episode",
    "EpisodeClosedError",
    "EpisodeError",
    "EpisodeScore",
    "GroundTruth",
    "Observation",
    "SimEnvironment",
    "Toolbox",
    "run_agent",
]
