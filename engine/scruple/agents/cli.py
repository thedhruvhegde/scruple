"""`scruple episode ...` subcommands.

`scruple episode` lets any external agent (a human, or a model in a terminal) play
an episode one command at a time; state lives in a local file store
(`--store`, default `$SCRUPLE_EPISODE_STORE` or `.scruple/episodes`). Every command
prints one JSON document on stdout. Refused calls print `{"error": ...}` and exit 1.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from scruple.agents.episode import EpisodeError
from scruple.agents.store import EpisodeStore, UnknownEpisodeError
from scruple.agents.tools import TOOL_NAMES, anthropic_tools


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _args(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"arguments are not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object")
    return parsed


def _call(store: EpisodeStore, episode_id: str, tool: str, raw: str | None) -> int:
    try:
        _print(store.call(episode_id, tool, _args(raw)))
    except EpisodeError as exc:
        _print({"error": str(exc)})
        return 1
    except UnknownEpisodeError as exc:
        _print({"error": str(exc.args[0])})
        return 1
    return 0


def cmd_episode(args: argparse.Namespace) -> int:
    store = EpisodeStore(args.store)
    match args.episode_cmd:
        case "start":
            ep = store.start(args.seed, args.case_type)
            _print(
                {
                    "episode_id": ep.episode_id,
                    "observation": ep.observe().model_dump(mode="json"),
                    "next": "scruple episode call <episode_id> <tool> '<json args>', then "
                    "scruple episode submit <episode_id> '<json decision>'",
                }
            )
            return 0
        case "tools":
            _print(anthropic_tools())
            return 0
        case "call":
            return _call(store, args.episode_id, args.tool, args.json)
        case "submit":
            return _call(store, args.episode_id, "submit_decision", args.json)
        case "show":
            try:
                ep = store.load(args.episode_id)
            except UnknownEpisodeError as exc:
                _print({"error": str(exc.args[0])})
                return 1
            _print(
                {
                    "episode_id": ep.episode_id,
                    "closed": ep.closed,
                    "action": None if ep.action is None else ep.action.value,
                    "calls": store.log(args.episode_id),
                    "score": None if ep.score is None else ep.score.model_dump(mode="json"),
                }
            )
            return 0
    raise AssertionError(args.episode_cmd)  # pragma: no cover


def add_episode_parser(sub: Any) -> None:
    p = sub.add_parser("episode", help="play one simulated dispute from the terminal")
    p.set_defaults(fn=cmd_episode)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=None, help="episode store directory")
    es = p.add_subparsers(dest="episode_cmd", required=True)
    st = es.add_parser("start", parents=[common], help="start an episode and print its observation")
    st.add_argument("--seed", type=int, required=True)
    st.add_argument("--case-type", default=None, help="synthetic case type (optional)")
    es.add_parser("tools", parents=[common], help="print the tool definitions (JSON schema)")
    c = es.add_parser("call", parents=[common], help="call a tool on an episode")
    c.add_argument("episode_id")
    c.add_argument("tool", choices=TOOL_NAMES)
    c.add_argument("json", nargs="?", default=None, help="tool arguments as a JSON object")
    sb = es.add_parser(
        "submit", parents=[common], help="submit the decision and print the scorecard"
    )
    sb.add_argument("episode_id")
    sb.add_argument("json", help='decision JSON, e.g. {"explanation": "..."}')
    sh = es.add_parser("show", parents=[common], help="print an episode's call log and score")
    sh.add_argument("episode_id")


__all__ = ["add_episode_parser", "cmd_episode"]
