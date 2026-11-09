"""Score a hosted or local model on synthetic dispute episodes.

Run one of:
  ANTHROPIC_API_KEY=... uv run python examples/model_agent.py anthropic [model]
  OPENAI_API_KEY=...    uv run python examples/model_agent.py openai <model>
  uv run python examples/model_agent.py local <model> [base_url]

Makes live API calls (costs money for hosted models). Nothing here runs in tests.
"""

from __future__ import annotations

import sys

import scruple
from scruple.agents.runners import AnthropicRunner, LocalRunner, OpenAIRunner, RunnerConfig
from scruple.agents.runners.anthropic import default_model


def main(argv: list[str]) -> None:
    kind = argv[0] if argv else "anthropic"
    if kind == "anthropic":
        runner = AnthropicRunner(RunnerConfig(model=argv[1] if len(argv) > 1 else default_model()))
    elif kind == "openai":
        runner = OpenAIRunner(RunnerConfig(model=argv[1]))
    else:
        runner = LocalRunner(
            RunnerConfig(model=argv[1]), base_url=argv[2] if len(argv) > 2 else None
        )
    card = scruple.evaluate(runner, n=16, seed=0)
    print(card.summary())


if __name__ == "__main__":
    main(sys.argv[1:])
