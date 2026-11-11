"""`scruple console`: serve the web console on its own port, or seed a demo tenant.

scruple console --db agent-gate.db --data console-data --tenant demo
scruple console demo-seed --db agent-gate.db --data console-data   # SYNTHETIC
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def cmd_console(args: argparse.Namespace) -> int:
    if args.action == "demo-seed":
        return cmd_demo_seed(args)
    import uvicorn

    from scruple.console.app import create_console_app
    from scruple.console.sources import ConsoleSources
    from scruple.logging import configure_logging
    from scruple.service import AgentGate
    from scruple.settings import load_settings

    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    gate = AgentGate.open(args.db or settings.database_dsn())
    tenant = args.tenant or settings.tenant
    for_tenant = getattr(gate.storage, "for_tenant", None)
    if for_tenant is not None and tenant != getattr(gate.storage, "tenant", tenant):
        gate = AgentGate(for_tenant(tenant), gate.clock, gate.config)
    gate.bootstrap_baseline()
    sources = ConsoleSources.build(
        seeds=range(args.seeds),
        episodes=Path(args.episodes) if args.episodes else None,
        reports=Path(args.reports) if args.reports else None,
        replay_seed=args.replay_seed,
        replay_cases=args.replay_cases,
        data=Path(args.data) if args.data else None,
    )
    uvicorn.run(
        create_console_app(gate, sources),
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
    )
    return 0


def cmd_demo_seed(args: argparse.Namespace) -> int:
    from scruple.console.demo_seed import DEMO_TENANT, seed_demo
    from scruple.settings import load_settings

    if not args.data:
        raise SystemExit("demo-seed needs --data DIR (the tenant data folder root)")
    db = args.db or load_settings().database_dsn()
    tenant = args.tenant or DEMO_TENANT
    print("Seeding a SYNTHETIC demo tenant. Nothing below is a real outcome.")
    rep = seed_demo(db, Path(args.data), tenant)
    print(
        f"tenant {rep.tenant}: {rep.decisions} gate decisions, {rep.disputes} synthetic "
        f"disputes, {rep.imported} imported shadow decisions, jobs {rep.jobs}, "
        f"{rep.usage_events} usage events, {rep.pushes} stub billing pushes, "
        f"{rep.eval_runs} eval runs, anchor {'written' if rep.anchored else 'not written'}"
    )
    for note in rep.notes:
        print(f"note: {note}")
    print(
        f"serve it: scruple console --db {db} --data {args.data} --tenant {tenant}\n"
        f"sign in with a key of that tenant: scruple keys create --tenant {tenant} ..."
    )
    return 0


def add_console_parser(sub: Any, default_db: str) -> None:
    sp = sub.add_parser(
        "console", help="serve the web console (dashboard, runs, cases, diffs, approvals)"
    )
    sp.set_defaults(fn=cmd_console)
    sp.add_argument(
        "action",
        nargs="?",
        default="serve",
        choices=["serve", "demo-seed"],
        help="serve (default), or demo-seed: create a SYNTHETIC demo tenant",
    )
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
    sp.add_argument(
        "--data", default=None, help="root of per-tenant data folders (DIR/<tenant>/...)"
    )
    sp.add_argument(
        "--tenant", default=None, help="tenant to serve or seed (default: $SCRUPLE_TENANT)"
    )


__all__ = ["add_console_parser", "cmd_console", "cmd_demo_seed"]
