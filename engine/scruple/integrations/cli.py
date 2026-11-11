"""`scruple integrations ...`: the same operations as `/integrations`, as the local admin.

    scruple integrations settings show
    scruple integrations settings set integrations.yaml
    scruple integrations dispatch
    scruple integrations webhooks add https://hooks.example.com/scruple [--event decision.recorded]
    scruple integrations webhooks list | rotate ID | disable ID | enable ID
    scruple integrations deliveries [--status failed] [--endpoint ID]
    scruple integrations replay DELIVERY_ID

The tenant is `$SCRUPLE_TENANT` (default "default"); the database is `--db` or
`$SCRUPLE_DATABASE_URL`.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _gate(args: argparse.Namespace) -> Any:
    from scruple.service import AgentGate
    from scruple.settings import load_settings

    settings = load_settings()
    g = AgentGate.open(args.db or settings.database_dsn())
    if settings.tenant != g.storage.tenant:
        g = AgentGate(g.storage.for_tenant(settings.tenant), g.clock, g.config)
    return g


def _queue(g: Any) -> Any:
    from scruple.jobs.queue import queue_for

    return queue_for(g.storage, clock=g.clock)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_settings(args: argparse.Namespace) -> int:
    from scruple.integrations.config import IntegrationSettings
    from scruple.integrations.ops import set_settings
    from scruple.integrations.store import IntegrationStore

    g = _gate(args)
    try:
        store = IntegrationStore(g.storage)
        if args.action == "show":
            _print(store.settings().model_dump(mode="json"))
            return 0
        import yaml

        with open(args.file, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        body = IntegrationSettings.model_validate(doc or {})
        n = set_settings(g, body, args.actor)
        print(f"integration settings #{n} in force for tenant {g.storage.tenant}")
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        g.storage.close()


def cmd_dispatch(args: argparse.Namespace) -> int:
    from scruple.integrations.dispatch import dispatch

    g = _gate(args)
    try:
        _print(dispatch(g, _queue(g)).model_dump(mode="json"))
    finally:
        g.storage.close()
    return 0


def cmd_webhooks(args: argparse.Namespace) -> int:
    from scruple.integrations.events import EVENT_TYPES
    from scruple.integrations.runtime import Runtime, SealError
    from scruple.integrations.store import IntegrationStore
    from scruple.integrations.webhooks import (
        EndpointIn,
        WebhookError,
        create_endpoint,
        rotate_secret,
        set_active,
    )

    g = _gate(args)
    try:
        if args.action == "list":
            _print([e.public() for e in IntegrationStore(g.storage).endpoints()])
        elif args.action == "add":
            body = EndpointIn(
                url=args.target, events=tuple(args.event or EVENT_TYPES), description=args.note
            )
            ep, secret = create_endpoint(g, Runtime.from_env(), body, args.actor)
            print(secret)
            print(
                f"registered {ep.endpoint_id}; the secret above is shown only once", file=sys.stderr
            )
        elif args.action == "rotate":
            print(rotate_secret(g, Runtime.from_env(), args.target, args.actor))
        else:
            _print(set_active(g, args.target, args.action == "enable", args.actor).public())
        return 0
    except (WebhookError, SealError, KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        g.storage.close()


def cmd_deliveries(args: argparse.Namespace) -> int:
    from scruple.integrations.store import IntegrationStore

    g = _gate(args)
    try:
        rows = IntegrationStore(g.storage).deliveries(args.endpoint, args.status, args.limit)
        _print([d.model_dump(mode="json") for d in rows])
    finally:
        g.storage.close()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from scruple.integrations.webhooks import replay

    g = _gate(args)
    try:
        n = replay(g, _queue(g), args.delivery_id, args.actor)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        g.storage.close()
    print(f"replay {n} of {args.delivery_id} queued")
    return 0


def add_integrations_parser(sub: Any, default_db: str) -> None:
    p = sub.add_parser("integrations", help="webhooks, tickets and Slack approvals")
    ps = p.add_subparsers(dest="integrations_cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument(
            "--db",
            default=None,
            help="SQLite path or postgresql:// URL "
            f"(default: $SCRUPLE_DATABASE_URL, else {default_db})",
        )
        sp.add_argument("--actor", default="local-admin", help="recorded in the audit log")
        return sp

    s = common(ps.add_parser("settings", help="show or set the tenant's integration settings"))
    s.add_argument("action", choices=["show", "set"])
    s.add_argument("file", nargs="?", help="YAML or JSON settings (for set)")
    s.set_defaults(fn=cmd_settings)

    d = common(ps.add_parser("dispatch", help="turn new gate events into jobs now"))
    d.set_defaults(fn=cmd_dispatch)

    w = common(ps.add_parser("webhooks", help="manage outbound webhook endpoints"))
    w.add_argument("action", choices=["list", "add", "rotate", "disable", "enable"])
    w.add_argument("target", nargs="?", help="URL (add) or endpoint id")
    w.add_argument("--event", action="append", help="subscribe to this event (repeatable)")
    w.add_argument("--note", default="", help="description")
    w.set_defaults(fn=cmd_webhooks)

    dl = common(ps.add_parser("deliveries", help="the webhook delivery log"))
    dl.add_argument("--status", choices=["pending", "delivered", "failed"], default=None)
    dl.add_argument("--endpoint", default=None)
    dl.add_argument("--limit", type=int, default=100)
    dl.set_defaults(fn=cmd_deliveries)

    r = common(ps.add_parser("replay", help="queue a webhook delivery again"))
    r.add_argument("delivery_id")
    r.set_defaults(fn=cmd_replay)


__all__ = ["add_integrations_parser"]
