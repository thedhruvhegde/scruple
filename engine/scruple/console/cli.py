"""`scruple console`: serve the web console on its own port."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def cmd_console(args: argparse.Namespace) -> int:
    import uvicorn

    from scruple.console.app import create_console_app
    from scruple.console.sources import ConsoleSources
    from scruple.logging import configure_logging
    from scruple.service import AgentGate
    from scruple.settings import load_settings

    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    gate = AgentGate.open(args.db or settings.database_dsn())
    gate.bootstrap_baseline()
    sources = ConsoleSources.build(
        seeds=range(args.seeds),
        episodes=Path(args.episodes) if args.episodes else None,
        reports=Path(args.reports) if args.reports else None,
        replay_seed=args.replay_seed,
        replay_cases=args.replay_cases,
    )
    uvicorn.run(
        create_console_app(gate, sources),
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
    )
    return 0


def add_console_parser(sub: Any, default_db: str) -> None:
    sp = sub.add_parser("console", help="serve the web console (runs, cases, diffs, approvals)")
    sp.set_defaults(fn=cmd_console)
    sp.add_argument(
        "--db",
        default=None,
        help=f"SQLite path or postgresql:// URL (default: $SCRUPLE_DATABASE_URL, else "
        f"{default_db})",
    )
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8090)
    sp.add_argument("--seeds", type=int, default=24, help="seeds per reference agent version")
    sp.add_argument("--episodes", default=None, help="episode store directory to show as a version")
    sp.add_argument("--reports", default=None, help="directory of backtest report JSON files")
    sp.add_argument(
        "--replay-seed", type=int, default=4_242, help="public replay set for rule diffs"
    )
    sp.add_argument("--replay-cases", type=int, default=1_000, help="cases in the rule diff replay")


__all__ = ["add_console_parser", "cmd_console"]
