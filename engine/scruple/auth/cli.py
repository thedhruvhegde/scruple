"""`scruple keys create|list|revoke`: manage API keys from the command line.

The command line runs as the local admin principal (`scruple.auth.local_admin`):
whoever can run it can already open the database. A new key is printed once, on
stdout, and never again; only its scrypt hash is stored.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from typing import Any

from scruple.auth.models import DEFAULT_TENANT, Permission, local_admin


def _service(args: argparse.Namespace) -> Any:
    from scruple.auth.config import key_service
    from scruple.settings import load_settings
    from scruple.storage import open_storage

    settings = load_settings()
    storage = open_storage(args.db or settings.database_dsn())
    return key_service(settings, storage), storage


def cmd_create(args: argparse.Namespace) -> int:
    from scruple.clock import SystemClock

    svc, storage = _service(args)
    admin = local_admin(args.tenant)
    expires = SystemClock().now() + timedelta(days=args.expires_days) if args.expires_days else None
    try:
        rec, token = svc.create(
            args.subject,
            [Permission(s) for s in args.scope or ()],
            admin.subject,
            tenant=args.tenant,
            description=args.description,
            expires_at=expires,
        )
    finally:
        storage.close()
    if args.json:
        print(json.dumps({**rec.public(), "api_key": token.get_secret_value()}, sort_keys=True))
    else:
        print(token.get_secret_value())
        print(
            f"created {rec.key_id} for {rec.subject} in tenant {rec.tenant}. "
            "This is the only time the key is shown; store it in a secret manager.",
            file=sys.stderr,
        )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    svc, storage = _service(args)
    try:
        keys = svc.list(args.tenant)
    finally:
        storage.close()
    if args.json:
        print(json.dumps([k.public() for k in keys], sort_keys=True))
        return 0
    for k in keys:
        state = "revoked" if k.revoked_at else "active"
        grants = ",".join([*k.roles, *(s.value for s in k.scopes)])
        used = k.last_used_at.isoformat() if k.last_used_at else "never"
        print(f"{k.key_id}  {state:7}  {k.subject}  {grants}  last used {used}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    from scruple.auth.keys import UnknownKeyError

    svc, storage = _service(args)
    try:
        rec = svc.revoke(args.key_id, local_admin(args.tenant).subject, args.tenant)
    except UnknownKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        storage.close()
    print(f"revoked {rec.key_id} ({rec.subject}) at {rec.revoked_at}")
    return 0


def add_keys_parser(sub: Any, default_db: str) -> None:
    k = sub.add_parser("keys", help="create, list and revoke API keys")
    ks = k.add_subparsers(dest="keys_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            default=None,
            help=f"SQLite path or postgresql:// URL (default: $SCRUPLE_DATABASE_URL, "
            f"else {default_db})",
        )
        p.add_argument("--tenant", default=DEFAULT_TENANT)
        p.add_argument("--json", action="store_true")

    c = ks.add_parser("create", help="create a key (printed once)")
    common(c)
    c.set_defaults(fn=cmd_create)
    c.add_argument("--subject", required=True, help="identity recorded as the audit actor")
    c.add_argument(
        "--scope",
        action="append",
        choices=[p.value for p in Permission],
        help="a permission the key holds (repeatable)",
    )
    c.add_argument("--description", default="")
    c.add_argument("--expires-days", type=int, default=None)

    ls = ks.add_parser("list", help="list a tenant's keys (never the secrets)")
    common(ls)
    ls.set_defaults(fn=cmd_list)

    r = ks.add_parser("revoke", help="revoke a key; takes effect on the next request")
    common(r)
    r.set_defaults(fn=cmd_revoke)
    r.add_argument("key_id")
