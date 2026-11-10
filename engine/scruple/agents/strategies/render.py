"""Render a strategy as a CLI player brief (markdown), the CLI-brief path.

The brief has the shape of results/real/episodes/BRIEF_PLAYER.md: who the player
is, the objective, the rules of the harness (CLI only, no files, no code), the seeds
and the reply format. The strategy contributes its objective and its method; loop
settings an API runner enforces (a planning turn, a held-back submit) become
instructions, since a terminal player cannot be forced.

The brief holds the seeds and the store path and nothing about the cases.
"""

from __future__ import annotations

from collections.abc import Sequence

from scruple.agents.strategies.base import CONTEXT, Strategy
from scruple.agents.strategies.seeds import format_seeds

DEFAULT_WORKDIR = "the scruple repository root"


def cli_commands(store: str) -> str:
    return "\n".join(
        [
            f"    uv run -q scruple episode start --store {store} --kind dispute --seed SEED",
            "    uv run -q scruple episode tools --kind dispute          (tool schemas)",
            f"    uv run -q scruple episode call --store {store} <episode_id> <tool> '<json args>'",
            f"    uv run -q scruple episode submit --store {store} <episode_id> '<json decision>'",
        ]
    )


def _cli_method(strategy: Strategy) -> list[str]:
    lines: list[str] = []
    if strategy.method:
        lines += ["", "How to work each case:", strategy.method_text()]
    if strategy.loop.planning_turn:
        lines += [
            "",
            "For every seed, write your plan in your reply before you run the first "
            "`episode call` for that episode.",
        ]
    if strategy.loop.critique_holds:
        lines += [
            "",
            "Before every `episode submit`, write a review of your work as described "
            "above and correct your report; only then submit.",
        ]
    return lines


def brief(
    strategy: Strategy,
    seeds: Sequence[int],
    store: str,
    *,
    workdir: str = DEFAULT_WORKDIR,
) -> str:
    """A markdown brief for one player who plays `seeds` into `store`."""
    if not seeds:
        raise ValueError("a brief needs at least one seed")
    ordered = sorted(set(seeds))
    n = len(ordered)
    seed_list = " ".join(str(s) for s in ordered)
    lines = [
        CONTEXT,
        strategy.objective,
        *_cli_method(strategy),
        "",
        "Rules (the evaluation is invalid if broken):",
        f"- Interact ONLY through the CLI below, run from {workdir}:",
        cli_commands(store),
        "  (check `uv run -q scruple episode call --help` for exact flag order.)",
        "- Do NOT read, list or grep any files: not the store directory, not the "
        "repository, nothing except this brief. Do not run python or any other command. "
        "Do not use web search.",
        "- Decide every action yourself from the tool outputs.",
        f"- Seeds to play, one episode each: {seed_list}.",
        f"- Submit a {'truthful ' if strategy.kind == 'method' else ''}decision for each "
        "(the submit_decision schema includes an explanation, an optional stated win "
        "probability, and structured claims).",
        f'Final reply: a list "seed episode_id" for all {n}, nothing else.',
        "",
        f"<!-- scruple strategy {strategy.name}; seeds {format_seeds(ordered)}; "
        f"store {store}; synthetic cases -->",
        "",
    ]
    return "\n".join(lines)


__all__ = ["DEFAULT_WORKDIR", "brief", "cli_commands"]
