"""Model runners: drive an episode with a hosted or local model over HTTP."""

from scruple.agents.runners.anthropic import AnthropicRunner
from scruple.agents.runners.base import RunnerConfig, RunTrace
from scruple.agents.runners.openai import OpenAIRunner

__all__ = ["AnthropicRunner", "OpenAIRunner", "RunTrace", "RunnerConfig"]
