"""`scruple usage sync|report`: record and total a tenant's usage from the command line.

The command line is the local admin of one tenant (`--tenant`, else
`$SCRUPLE_TENANT`, else `default`). `report` syncs first, so it always includes every
decision already in the database.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any


def _open(args: argparse.Namespace) -> tuple[Any, Any]:
    from scruple.service import AgentGate
    from scruple.settings import load_settings
    from scruple.storage import open_storage
    from scruple.usage.meter import usage_store_for

    settings = load_settings()
    storage = open_storage(args.db or settings.database_dsn())
    gate = AgentGate(storage.for_tenant(args.tenant or settings.tenant))
    store = usage_store_for(gate.storage, gate.clock)
    if store is None:  # pragma: no cover - every backend here is SQL
        storage.close()
        raise ValueError("this database keeps no usage events")
    return gate, store


def _day(s: str | None) -> date | None:
    if s is None:
        return None
    if len(s) == 7:  # YYYY-MM
        s += "-01"
    return date.fromisoformat(s)


def cmd_sync(args: argparse.Namespace) -> int:
    gate, store = _open(args)
    try:
        n = store.sync(gate)
    finally:
        gate.storage.close()
    print(f"recorded {n} new usage events for tenant {gate.storage.tenant}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    gate, store = _open(args)
    try:
        store.sync(gate)
        rows = store.aggregate(gate.storage.tenant, args.period, _day(args.start), _day(args.end))
    finally:
        gate.storage.close()
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in rows], sort_keys=True))
        return 0
    print(f"usage for tenant {gate.storage.tenant}, per {args.period} (UTC)")
    if not rows:
        print("  no usage recorded")
    for r in rows:
        print(
            f"  {r.period_start.isoformat()}  {r.meter.value:11} {r.quantity:>12,}  "
            f"({r.events} events)"
        )
    return 0


def add_usage_parser(sub: Any, default_db: str) -> None:
    u = sub.add_parser("usage", help="usage metering: gate runs, cases replayed, rollouts")
    us = u.add_subparsers(dest="usage_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            default=None,
            help=f"SQLite path or postgresql:// URL (default: $SCRUPLE_DATABASE_URL, "
            f"else {default_db})",
        )
        p.add_argument("--tenant", default=None, help="default: $SCRUPLE_TENANT, else default")

    s = us.add_parser("sync", help="record usage for decisions not yet metered (idempotent)")
    common(s)
    s.set_defaults(fn=cmd_sync)
    r = us.add_parser("report", help="usage totals per period")
    common(r)
    r.set_defaults(fn=cmd_report)
    r.add_argument("--period", choices=["day", "month"], default="month")
    r.add_argument("--start", default=None, help="YYYY-MM or YYYY-MM-DD (inclusive)")
    r.add_argument("--end", default=None, help="YYYY-MM or YYYY-MM-DD (exclusive)")
    r.add_argument("--json", action="store_true")


__all__ = ["add_usage_parser"]
