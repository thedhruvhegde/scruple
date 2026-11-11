"""`scruple policy validate|show|diff|apply|history`.

    scruple policy validate policy.yaml        # exit 0 valid, 1 invalid (reasons on stderr)
    scruple policy show policy.yaml            # canonical form plus its sha256
    scruple policy show --current              # the tenant's policy in force
    scruple policy diff old.yaml new.toml      # every changed leaf, by path
    scruple policy diff @current new.yaml      # in force vs a file
    scruple policy apply policy.yaml --actor model-risk@demo.bank
    scruple policy history

`apply`, `--current` and `@current` work on the tenant `$SCRUPLE_TENANT` (default "default") of
the database given by `--db` or `$SCRUPLE_DATABASE_URL`, as the local admin.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from scruple.policy.files import PolicyFileError, dump_policy, load_policy
from scruple.policy.models import Policy, diff

CURRENT = "@current"


def _gate(args: argparse.Namespace) -> Any:
    from scruple.service import AgentGate
    from scruple.settings import load_settings

    settings = load_settings()
    g = AgentGate.open(args.db or settings.database_dsn())
    if settings.tenant != g.storage.tenant:
        g = AgentGate(g.storage.for_tenant(settings.tenant), g.clock, g.config)
    return g


def _current(args: argparse.Namespace) -> Policy:
    from scruple.policy.apply import current_policy

    g = _gate(args)
    try:
        rec = current_policy(g)
    finally:
        g.storage.close()
    if rec is None:
        raise PolicyFileError(f"tenant {g.storage.tenant!r} has no policy in force")
    return rec.policy


def _source(args: argparse.Namespace, value: str | None) -> Policy:
    if value is None or value == CURRENT:
        return _current(args)
    return load_policy(value)


def cmd_validate(args: argparse.Namespace) -> int:
    bad = 0
    for path in args.files:
        try:
            p = load_policy(path)
        except PolicyFileError as exc:
            print(str(exc), file=sys.stderr)
            bad += 1
            continue
        print(f"{path}: valid policy {p.name} v{p.policy_version} sha256 {p.policy_sha256}")
    return 1 if bad else 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        p = _source(args, CURRENT if args.current or args.file is None else args.file)
    except PolicyFileError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.format == "json":
        body = json.loads(dump_policy(p, "json"))
        print(json.dumps({"policy": body, "policy_sha256": p.policy_sha256}, indent=2))
    else:
        print(f"# policy_sha256: {p.policy_sha256}")
        print(dump_policy(p, "yaml"), end="")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Exit 0 when equal, 1 when they differ, 2 when either side cannot be read."""
    try:
        old, new = _source(args, args.old), _source(args, args.new)
    except PolicyFileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    changes = diff(old, new)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "old_sha256": old.policy_sha256,
                    "new_sha256": new.policy_sha256,
                    "changes": [c.model_dump(mode="json") for c in changes],
                },
                indent=2,
            )
        )
    else:
        print(f"--- {old.policy_sha256}")
        print(f"+++ {new.policy_sha256}")
        for c in changes:
            print(c.line())
        if not changes:
            print("(no changes)")
    return 1 if changes else 0


def cmd_apply(args: argparse.Namespace) -> int:
    from scruple.gate.approval import ApprovalError
    from scruple.policy.apply import apply_policy

    try:
        p = load_policy(args.file)
    except PolicyFileError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    g = _gate(args)
    try:
        rec, changed = apply_policy(g, p, args.actor)
    except ApprovalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        g.storage.close()
    state = "applied" if changed else "already in force"
    print(f"policy {p.name} #{rec.number} {state} for tenant {rec.tenant}: {rec.policy_sha256}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    from scruple.policy.store import PolicyStore

    g = _gate(args)
    try:
        rows = PolicyStore(g.storage).history()
    finally:
        g.storage.close()
    for r in rows:
        print(f"#{r.number} {r.created_at.isoformat()} {r.actor} {r.policy.name} {r.policy_sha256}")
    return 0


def add_policy_parser(sub: Any, default_db: str) -> None:
    p = sub.add_parser("policy", help="validate, show, diff and apply tenant policy files")
    ps = p.add_subparsers(dest="policy_cmd", required=True)

    def db(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--db",
            default=None,
            help="SQLite path or postgresql:// URL "
            f"(default: $SCRUPLE_DATABASE_URL, else {default_db})",
        )

    v = ps.add_parser("validate", help="check policy files (exit 1 if any is invalid)")
    v.add_argument("files", nargs="+")
    v.set_defaults(fn=cmd_validate)

    s = ps.add_parser("show", help="print a policy in canonical form with its hash")
    s.add_argument("file", nargs="?")
    s.add_argument("--current", action="store_true", help="the tenant's policy in force")
    s.add_argument("--format", choices=["yaml", "json"], default="yaml")
    db(s)
    s.set_defaults(fn=cmd_show)

    d = ps.add_parser("diff", help=f"compare two policies (a file or {CURRENT})")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--format", choices=["text", "json"], default="text")
    db(d)
    d.set_defaults(fn=cmd_diff)

    a = ps.add_parser("apply", help="put a policy in force for the tenant (audited)")
    a.add_argument("file")
    a.add_argument(
        "--actor",
        default="local-admin",
        help="who applies it; a policy with an approval section needs a gate operator",
    )
    db(a)
    a.set_defaults(fn=cmd_apply)

    h = ps.add_parser("history", help="every policy the tenant has had in force")
    db(h)
    h.set_defaults(fn=cmd_history)


__all__ = ["add_policy_parser"]
