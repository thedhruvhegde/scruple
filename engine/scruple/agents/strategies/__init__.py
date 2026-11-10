"""Strategies for LLM agents on dispute episodes: prompts, objectives, loop settings.

Each strategy renders as an API plan (system prompt plus `LoopConfig`, for
scruple.agents.providers) and as a CLI player brief (markdown, for an agent that
plays through `scruple episode`). See docs/EVALUATING_AGENTS.md.
"""

from scruple.agents.strategies.base import ApiPlan, LoopConfig, Strategy, StrategyKind
from scruple.agents.strategies.library import STRATEGIES, UnknownStrategyError, get_strategy
from scruple.agents.strategies.render import brief
from scruple.agents.strategies.seeds import format_seeds, parse_seeds

__all__ = [
    "STRATEGIES",
    "ApiPlan",
    "LoopConfig",
    "Strategy",
    "StrategyKind",
    "UnknownStrategyError",
    "brief",
    "format_seeds",
    "get_strategy",
    "parse_seeds",
]
