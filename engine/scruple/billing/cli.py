"""`scruple billing plan|push`: send usage to Stripe metered billing (test mode first).

    scruple billing plan --config billing.toml          # what would be sent; sends nothing
    SCRUPLE_STRIPE_BILLING_KEY=sk_test_... scruple billing push --config billing.toml

Each configured tenant's decisions are metered first (`scruple usage sync`), then the
per-day totals not yet pushed are sent (`scruple.billing.push`). A live key is
refused unless the config says `mode = "live"` and `--allow-live` is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

KEY_ENV = "SCRUPLE_STRIPE_BILLING_KEY"


def _prepare(args: argparse.Namespace) -> tuple[Any, Any, Any, Any, list[str]]:
    from scruple.billing.push import BillingConfig, ledger_for
    from scruple.service import AgentGate
    from scruple.settings import load_settings
    from scruple.storage import open_storage
    from scruple.usage.meter import usage_store_for

    config = BillingConfig.load(args.config)
    settings = load_settings()
    storage = open_storage(args.db or settings.database_dsn())
    tenants = [args.tenant] if args.tenant else sorted(config.customers)
    ledger = ledger_for(storage)
    usage = usage_store_for(storage)
    if ledger is None or usage is None:  # pragma: no cover - every backend here is SQL
        storage.close()
        raise ValueError("this database keeps no usage or billing ledger")
    for t in tenants:
        usage.sync(AgentGate(storage.for_tenant(t)))
    return config, storage, usage, ledger, tenants


def _print(report: Any, as_json: bool, dry: bool) -> None:
    def row(p: Any) -> dict[str, Any]:
        return {
            "tenant": p.tenant,
            "meter": p.meter.value,
            "day": p.day.isoformat(),
            "value": p.delta,
            "cumulative": p.cumulative,
            "event_name": p.event_name,
            "customer": p.customer,
            "identifier": p.identifier,
            "timestamp": p.timestamp,
        }

    if as_json:
        out = {
            "dry_run": dry,
            "planned": [row(p) for p in report.planned],
            "sent": [row(p) for p in report.sent],
            "failed": [{**row(p), "error": e} for p, e in report.failed],
            "stale": [
                {"tenant": t, "meter": m.value, "day": d.isoformat(), "value": v}
                for t, m, d, v in report.stale
            ],
        }
        print(json.dumps(out, sort_keys=True))
        return
    items = report.planned if dry else report.sent
    verb = "would send" if dry else "sent"
    for p in items:
        print(f"{verb} {p.tenant} {p.day} {p.meter.value}={p.delta} -> {p.event_name} {p.customer}")
    for p, e in report.failed:
        print(f"FAILED {p.tenant} {p.day} {p.meter.value}={p.delta}: {e}", file=sys.stderr)
    for t, m, d, v in report.stale:
        print(f"STALE {t} {d} {m.value}={v}: older than the lookback; not sent", file=sys.stderr)
    if not items and not report.failed:
        print("nothing to send")


def cmd_plan(args: argparse.Namespace) -> int:
    from scruple.billing.push import plan
    from scruple.clock import SystemClock

    config, storage, usage, ledger, tenants = _prepare(args)
    try:
        report = plan(config, usage, ledger, SystemClock().now(), tenants)
    finally:
        storage.close()
    _print(report, args.json, dry=True)
    return 0


def cmd_push(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    from scruple.billing.push import push
    from scruple.billing.stripe import StripeBillingClient
    from scruple.clock import SystemClock

    key = os.environ.get(KEY_ENV, "")
    if not key:
        print(f"error: set {KEY_ENV} (an sk_test_ key) to push", file=sys.stderr)
        return 2
    config, storage, usage, ledger, tenants = _prepare(args)
    allow_live = bool(args.allow_live and config.mode == "live")
    try:
        with StripeBillingClient(key, transport=transport, allow_live=allow_live) as client:
            report = push(config, client, usage, ledger, SystemClock(), tenants)
    finally:
        storage.close()
    _print(report, args.json, dry=False)
    return 0 if report.ok else 1


def add_billing_parser(sub: Any, default_db: str) -> None:
    b = sub.add_parser("billing", help="push usage to Stripe metered billing (test mode)")
    bs = b.add_subparsers(dest="billing_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", required=True, help="billing TOML file ([billing] table)")
        p.add_argument(
            "--db",
            default=None,
            help=f"SQLite path or postgresql:// URL (default: $SCRUPLE_DATABASE_URL, "
            f"else {default_db})",
        )
        p.add_argument("--tenant", default=None, help="one tenant (default: every configured)")
        p.add_argument("--json", action="store_true")

    p = bs.add_parser("plan", help="show what would be sent; sends nothing")
    common(p)
    p.set_defaults(fn=cmd_plan)
    q = bs.add_parser("push", help=f"send unpushed usage (key from ${KEY_ENV})")
    common(q)
    q.add_argument("--allow-live", action="store_true", help='with mode = "live" only')
    q.set_defaults(fn=cmd_push)


__all__ = ["KEY_ENV", "add_billing_parser", "cmd_plan", "cmd_push"]
