"""`scruple tenant list|show|create|quota|offboard`: tenant administration.

Like `scruple keys` and `scruple retention`, the command line is the local admin:
whoever can run it can already open the database. Every change is audited in the
affected tenant's own chain, with `--actor` (default `local-admin`) as the actor.

`offboard` is a dry run unless `--yes` is given: it prints what it would revoke and
purge and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from scruple.auth.models import local_admin


def _open(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    from scruple.auth.config import key_service
    from scruple.service import AgentGate
    from scruple.settings import load_settings
    from scruple.storage import open_storage
    from scruple.tenancy.directory import TenantDirectory

    settings = load_settings()
    gate = AgentGate(open_storage(args.db or settings.database_dsn()))
    return gate, TenantDirectory(gate.storage, gate.clock), key_service(settings, gate.storage)


def _summary(gate: Any, keys: Any, directory: Any, tenant: str) -> dict[str, Any]:
    from scruple.storage.retention import retention_for, tenant_gate
    from scruple.tenancy.directory import gate_runs_today

    rec = directory.get(tenant)
    tg = tenant_gate(gate, tenant)
    now = gate.clock.now()
    all_keys = keys.list(tenant)
    last = tg.storage.last_audit()
    policy = next(
        (p for p in retention_for(gate.storage).policies() if p.tenant_id == tenant), None
    )
    return {
        **rec.model_dump(mode="json"),
        "keys_active": sum(1 for k in all_keys if k.active(now)),
        "keys_inactive": sum(1 for k in all_keys if not k.active(now)),
        "rules": len({v.rule_id for v in tg.storage.list_rule_versions()}),
        "decisions": tg.storage.count_decisions(),
        "audit_entries": last.seq if last is not None else 0,
        "imported_shadow_rows": directory.imported_shadow_rows(tenant),
        "retention_days": policy.days if policy is not None else None,
        "gate_runs_today": gate_runs_today(tg, now),
    }


def _print(obj: Any) -> None:
    print(json.dumps(obj, sort_keys=True, indent=1))


def cmd_list(args: argparse.Namespace) -> int:
    gate, directory, _ = _open(args)
    try:
        recs = directory.list()
    finally:
        gate.storage.close()
    if args.json:
        print(json.dumps([r.model_dump(mode="json") for r in recs], sort_keys=True))
        return 0
    if not recs:
        print("no tenants")
    for r in recs:
        how = "created" if r.explicit else "implicit"
        quota = r.quota.gate_runs_per_day
        print(
            f"{r.tenant}  {r.status:10}  {how:8}  "
            f"gate runs/day {quota if quota is not None else 'unlimited'}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from scruple.tenancy.directory import TenancyError

    gate, directory, keys = _open(args)
    try:
        if not directory.exists(args.tenant):
            raise TenancyError(f"no tenant {args.tenant}")
        _print(_summary(gate, keys, directory, args.tenant))
    finally:
        gate.storage.close()
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    gate, directory, _ = _open(args)
    try:
        rec = directory.create(args.tenant, args.actor)
    finally:
        gate.storage.close()
    _print(rec.model_dump(mode="json"))
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    from scruple.tenancy.directory import Quota

    if args.clear == (args.gate_runs_per_day is not None):
        print("error: give --gate-runs-per-day N or --clear", file=sys.stderr)
        return 2
    gate, directory, _ = _open(args)
    try:
        rec = directory.set_quota(
            args.tenant, Quota(gate_runs_per_day=args.gate_runs_per_day), args.actor
        )
    finally:
        gate.storage.close()
    _print(rec.model_dump(mode="json"))
    return 0


def cmd_offboard(args: argparse.Namespace) -> int:
    from scruple.tenancy.offboard import offboard, plan_offboarding

    gate, _, keys = _open(args)
    store = None
    try:
        if args.case_store:
            from scruple.ingest.store import CaseStore

            store = CaseStore(args.case_store, create=False)
        if not args.yes:
            plan = plan_offboarding(gate, keys, args.tenant, store)
            _print({"dry_run": True, **plan.model_dump(mode="json")})
            print(
                f"dry run: nothing changed. Run again with --yes to offboard {args.tenant}.",
                file=sys.stderr,
            )
            return 0
        res = offboard(gate, keys, args.tenant, args.actor, store)
    finally:
        if store is not None:
            store.close()
        gate.storage.close()
    _print({"dry_run": False, **res.model_dump(mode="json")})
    rows = sum(int(p["rows"]) for p in res.purges)
    print(
        f"offboarded {res.tenant}: {len(res.keys_revoked)} keys revoked, {rows} rows purged "
        f"in {len(res.purges)} tables; audit chain head seq {res.audit_head_seq}.",
        file=sys.stderr,
    )
    return 0


def add_tenant_parser(sub: Any, default_db: str) -> None:
    t = sub.add_parser("tenant", help="list, show, create, set quotas for and offboard tenants")
    ts = t.add_subparsers(dest="tenant_cmd", required=True)

    def common(p: argparse.ArgumentParser, *, tenant: bool = True, actor: bool = False) -> None:
        p.add_argument(
            "--db",
            default=None,
            help=f"SQLite path or postgresql:// URL (default: $SCRUPLE_DATABASE_URL, "
            f"else {default_db})",
        )
        if tenant:
            p.add_argument("tenant", help="tenant id")
        if actor:
            p.add_argument("--actor", default=local_admin().subject, help="who, for the audit log")

    ls = ts.add_parser("list", help="every tenant the database knows")
    common(ls, tenant=False)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)

    sh = ts.add_parser("show", help="one tenant: status, quota, keys, data held")
    common(sh)
    sh.set_defaults(fn=cmd_show)

    c = ts.add_parser("create", help="record a tenant explicitly (audited)")
    common(c, actor=True)
    c.set_defaults(fn=cmd_create)

    q = ts.add_parser("quota", help="set or clear a tenant's daily gate-run quota (audited)")
    common(q, actor=True)
    q.add_argument("--gate-runs-per-day", type=int, default=None)
    q.add_argument("--clear", action="store_true", help="remove the limit")
    q.set_defaults(fn=cmd_quota)

    o = ts.add_parser(
        "offboard",
        help="revoke a tenant's keys and purge its case data (dry run without --yes)",
    )
    common(o, actor=True)
    o.add_argument("--case-store", default=None, help="the tenant's case store file")
    o.add_argument("--yes", action="store_true", help="do it; without this, a dry run")
    o.set_defaults(fn=cmd_offboard)


__all__ = ["add_tenant_parser"]
