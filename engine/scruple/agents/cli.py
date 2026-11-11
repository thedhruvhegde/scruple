"""`scruple episode ...` and `scruple mcp ...` subcommands.

`scruple mcp sim` and `scruple mcp gate --db PATH` run the MCP servers on stdio.

`scruple episode` lets any external agent (a human, or a model in a terminal) play
an episode one command at a time; state lives in a local file store
(`--store`, default `$SCRUPLE_EPISODE_STORE` or `.scruple/episodes`). Every command
prints one JSON document on stdout. Refused calls print `{"error": ...}` and exit 1.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from scruple.agents.ach_tools import ACH_TOOL_NAMES, ach_anthropic_tools
from scruple.agents.aml_tools import AML_TOOL_NAMES, aml_anthropic_tools
from scruple.agents.dispute_tools import DISPUTE_TOOL_NAMES, dispute_anthropic_tools
from scruple.agents.episode import EpisodeError
from scruple.agents.payables_tools import PAYABLES_TOOL_NAMES, payables_anthropic_tools
from scruple.agents.recon_tools import RECON_TOOL_NAMES, recon_anthropic_tools
from scruple.agents.store import KINDS, EpisodeStore, UnknownEpisodeError
from scruple.agents.tools import TOOL_NAMES, anthropic_tools

ALL_TOOL_NAMES = tuple(
    dict.fromkeys(
        (
            *TOOL_NAMES,
            *DISPUTE_TOOL_NAMES,
            *ACH_TOOL_NAMES,
            *RECON_TOOL_NAMES,
            *AML_TOOL_NAMES,
            *PAYABLES_TOOL_NAMES,
        )
    )
)
KIND_TOOLS = {
    "dispute": dispute_anthropic_tools,
    "scenario": dispute_anthropic_tools,  # real-life scenarios play the dispute tools
    "ach": ach_anthropic_tools,
    "recon": recon_anthropic_tools,
    "aml": aml_anthropic_tools,
    "payables": payables_anthropic_tools,
}


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
            ep = store.start(args.seed, args.case_type, args.kind)
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
            _print(KIND_TOOLS.get(args.kind, anthropic_tools)())
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
                    "kind": getattr(ep, "kind", "simple"),
                    "action": None if ep.action is None else str(ep.action),
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
    st.add_argument(
        "--case-type", default=None, help="synthetic case type, or case family for dispute"
    )
    st.add_argument("--kind", choices=KINDS, default="simple", help="episode kind")
    tl = es.add_parser("tools", parents=[common], help="print the tool definitions (JSON schema)")
    tl.add_argument("--kind", choices=KINDS, default="simple")
    c = es.add_parser("call", parents=[common], help="call a tool on an episode")
    c.add_argument("episode_id")
    c.add_argument("tool", choices=ALL_TOOL_NAMES)
    c.add_argument("json", nargs="?", default=None, help="tool arguments as a JSON object")
    sb = es.add_parser(
        "submit", parents=[common], help="submit the decision and print the scorecard"
    )
    sb.add_argument("episode_id")
    sb.add_argument("json", help='decision JSON, e.g. {"explanation": "..."}')
    sh = es.add_parser("show", parents=[common], help="print an episode's call log and score")
    sh.add_argument("episode_id")


def cmd_mcp(args: argparse.Namespace) -> int:  # pragma: no cover - blocks on stdio
    if args.server == "sim":
        from scruple.mcp.sim_server import build_sim_server, run_stdio

        if args.episode_db:  # episodes survive a server restart
            from scruple.agents.store_db import DbEpisodeStore, durable_table
            from scruple.storage import open_storage

            store = DbEpisodeStore.on(open_storage(args.episode_db))
            build_sim_server(durable_table(store)).run("stdio")
        else:
            run_stdio()
    else:
        from scruple.mcp.gate_server import run_stdio as run_gate

        run_gate(args.db)
    return 0


def add_mcp_parser(sub: Any, default_db: str) -> None:
    p = sub.add_parser("mcp", help="run an MCP server on stdio")
    p.set_defaults(fn=cmd_mcp)
    p.add_argument("server", choices=["sim", "gate"])
    p.add_argument("--db", default=default_db, help="SQLite database path (gate only)")
    p.add_argument(
        "--episode-db",
        default=None,
        help="sim only: keep episodes in this database (SQLite path or postgresql:// URL) "
        "instead of server memory",
    )


__all__ = ["add_episode_parser", "add_mcp_parser", "cmd_episode", "cmd_mcp"]
