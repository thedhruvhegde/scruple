"""Command-line interface. `agent-gate demo` tells the whole story."""

from __future__ import annotations

import argparse
import functools
import sys
from collections.abc import Sequence
from pathlib import Path

from scruple.ci.cli import add_ci_parser
from scruple.registry import InvalidTransitionError
from scruple.service import AgentGate

DEFAULT_DB = "agent-gate.db"


def _gate(args: argparse.Namespace, bootstrap: bool = True) -> AgentGate:
    from scruple.settings import load_settings

    # --db wins; else SCRUPLE_DATABASE_URL (+ password) from settings; else DEFAULT_DB.
    # The command line is the local admin of one tenant ($SCRUPLE_TENANT, "default").
    settings = load_settings()
    g = AgentGate.open(args.db or settings.database_dsn())
    if settings.tenant != g.storage.tenant:
        g = AgentGate(g.storage.for_tenant(settings.tenant), g.clock, g.config)
    if bootstrap:
        g.bootstrap_baseline()
    return g


def cmd_demo(args: argparse.Namespace) -> int:
    from scruple.cli.demo import run_demo

    return run_demo(Path(args.out), print_reports=not args.quiet, stats=args.stats)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from scruple.api.app import create_app
    from scruple.logging import configure_logging
    from scruple.settings import load_settings

    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    if args.demo or settings.demo:
        if args.db:
            raise SystemExit(
                "--db does not apply to --demo: the demo seeds its own database (SCRUPLE_DEMO_DIR)"
            )
        from scruple.demo.host import serve_demo

        return serve_demo(settings.model_copy(update={"demo": True}), args.host, args.port)
    # log_config=None: uvicorn's loggers propagate to our JSON handler; the
    # request-id middleware writes the access line.
    uvicorn.run(
        create_app(_gate(args)), host=args.host, port=args.port, log_config=None, access_log=False
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if args.action != "verify":
        from scruple.audit.cli import dispatch

        return dispatch(args, _gate)
    # Read-only: verifying must never write (no baseline import into the evidence).
    rep = _gate(args, bootstrap=False).verify_integrity(args.expected_head)
    print(
        f"audit chain: {'OK' if rep.chain.ok else 'BROKEN'} "
        f"({rep.chain.entries} entries, head {rep.chain.head_hash[:16]})"
    )
    for p in rep.problems:
        print(f"  PROBLEM: {p}")
    print("INTEGRITY OK" if rep.ok else "INTEGRITY FAILED")
    return 0 if rep.ok else 1


def cmd_rules(args: argparse.Namespace) -> int:
    for r in _gate(args).registry.all_versions():
        print(f"{r.ref:40} {r.status.value:10} {r.spec.describe()}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    from scruple.api.app import RegisterIn

    # Same model as POST /rules: unknown fields anywhere in the file are refused.
    body = RegisterIn.model_validate_json(Path(args.file).read_text())
    rv = _gate(args).propose(body.rule_id, body.spec, body.owner, body.provenance)
    print(f"registered {rv.ref} ({rv.status.value}) hash {rv.content_hash[:16]}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    for rec in _gate(args).evaluate(args.rule_id, args.version):
        d = rec.decision
        print(f"{d.decision_id} {d.stage.value:7} {d.outcome.value:14} {d.summary}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    d = _gate(args).approve(args.rule_id, args.version, args.approver).decision
    print(f"{d.decision_id} {d.summary}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    d = _gate(args).rollback(args.rule_id, args.actor, args.reason).decision
    print(f"{d.decision_id} {d.summary}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    md = _gate(args).report(args.decision_id)
    if md is None:
        print(f"unknown decision {args.decision_id}", file=sys.stderr)
        return 1
    print(md)
    return 0


def cmd_assurance(args: argparse.Namespace) -> int:
    rec = _gate(args).run_assurance()
    print(
        "no active rules"
        if rec is None
        else f"{rec.decision.decision_id} {rec.decision.outcome.value}: {rec.decision.summary}"
    )
    return 0


def cmd_redteam(args: argparse.Namespace) -> int:
    if getattr(args, "suite", "rules") == "exploits":
        return cmd_redteam_exploits(args)
    from scruple.cli.demo import print_redteam

    return 0 if print_redteam() else 1


def cmd_redteam_exploits(args: argparse.Namespace) -> int:
    """Run the dispute-episode exploit library and print the coverage table."""
    import json

    from scruple.verifiers.exploits.coverage import coverage, render, research_summary

    seeds = range(args.start, args.start + args.seeds)
    rows = coverage(seeds)
    if args.json:
        print(
            json.dumps(
                {
                    "seeds": [seeds.start, seeds.stop],
                    "synthetic": True,
                    "rows": [r.model_dump(mode="json") for r in rows],
                    "research": research_summary(rows),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render(rows, seeds))
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    from scruple.version import build_info

    info = build_info()
    print(info.model_dump_json() if args.json else info.line())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scruple", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: object, help_: str, db: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        if db:
            sp.add_argument(
                "--db",
                default=None,
                help="SQLite path or postgresql:// URL "
                f"(default: $SCRUPLE_DATABASE_URL, else {DEFAULT_DB})",
            )
        return sp

    d = add("demo", cmd_demo, "run the end-to-end demo on synthetic data", db=False)
    d.add_argument("--out", default="demo-output", help="directory for reports and demo db")
    d.add_argument("--quiet", action="store_true", help="do not print full reports")
    d.add_argument(
        "--stats",
        choices=("evalue", "bootstrap"),
        default="evalue",
        help="gate statistics: anytime-valid e-values (default) or the bootstrap path",
    )
    s = add("serve", cmd_serve, "run the HTTP API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument(
        "--demo",
        action="store_true",
        help="public read-only demo: seed SYNTHETIC data, no login, refuse every write "
        "(also SCRUPLE_DEMO=1)",
    )
    add("rules", cmd_rules, "list rule versions")
    pr = add("propose", cmd_propose, "register a candidate rule from a JSON file")
    pr.add_argument("file")
    ev = add("evaluate", cmd_evaluate, "run shadow and canary evaluation")
    ev.add_argument("rule_id")
    ev.add_argument("version", type=int)
    ap = add("approve", cmd_approve, "approve a canary-passed rule for activation")
    ap.add_argument("rule_id")
    ap.add_argument("version", type=int)
    ap.add_argument("--approver", required=True)
    rb = add("rollback", cmd_rollback, "roll back the active version of a rule")
    rb.add_argument("rule_id")
    rb.add_argument("--actor", required=True)
    rb.add_argument("--reason", required=True)
    rp = add("report", cmd_report, "print the evidence report for a decision")
    rp.add_argument("decision_id")
    add("assurance", cmd_assurance, "re-check the live policy (auto rollback on failure)")
    rt = add("redteam", cmd_redteam, "run the adversarial verifier suite", db=False)
    rt.add_argument(
        "suite",
        nargs="?",
        default="rules",
        choices=["rules", "exploits"],
        help="rules: attacks on learned rules (default); exploits: dispute-episode exploits",
    )
    rt.add_argument("--seeds", type=int, default=300, help="exploits: number of seeds")
    rt.add_argument("--start", type=int, default=0, help="exploits: first seed")
    rt.add_argument("--json", action="store_true", help="exploits: print JSON")
    au = add("audit", cmd_verify, "verify or anchor the audit trail (hash chain + projections)")
    from scruple.audit.cli import add_audit_args

    add_audit_args(au)
    add_ci_parser(sub)
    v = add("version", cmd_version, "print version and build metadata", db=False)
    v.add_argument("--json", action="store_true", help="print as JSON")
    from scruple.agents.cli import add_episode_parser, add_mcp_parser

    add_episode_parser(sub)
    add_mcp_parser(sub, DEFAULT_DB)
    from scruple.realdata.cli import add_realdata_parser

    add_realdata_parser(sub)
    from scruple.sim.cli import add_sim_parser

    add_sim_parser(sub)
    from scruple.backtest.cli import add_backtest_parser

    add_backtest_parser(sub)
    from scruple.ingest.cli import add_ingest_parser

    add_ingest_parser(sub)
    from scruple.bench.cli import add_bench_parser

    add_bench_parser(sub)
    from scruple.console.cli import add_console_parser

    add_console_parser(sub, DEFAULT_DB)
    from scruple.agents.strategies.cli import add_strategies_parser

    add_strategies_parser(sub)
    from scruple.eval.cli import add_eval_parser

    add_eval_parser(sub)
    __import__("scruple.zoo.cli", fromlist=["add_zoo_parser"]).add_zoo_parser(sub)
    from scruple.auth.cli import add_keys_parser

    add_keys_parser(sub, DEFAULT_DB)
    from scruple.jobs.cli import add_worker_parser

    add_worker_parser(sub, DEFAULT_DB)
    from scruple.storage.retention import add_retention_parser

    add_retention_parser(sub, DEFAULT_DB)
    from scruple.verifiers.meta import add_verifiers_parser

    add_verifiers_parser(sub)
    from scruple.cli.shadow import add_shadow_parser

    add_shadow_parser(sub, DEFAULT_DB)
    from scruple.pilot.cli import add_pilot_parser

    add_pilot_parser(sub)
    from scruple.cli.init import add_init_parser

    add_init_parser(sub)
    from scruple.usage.cli import add_usage_parser

    add_usage_parser(sub, DEFAULT_DB)
    from scruple.billing.cli import add_billing_parser

    add_billing_parser(sub, DEFAULT_DB)
    from scruple.policy.cli import add_policy_parser

    add_policy_parser(sub, DEFAULT_DB)
    from scruple.integrations.cli import add_integrations_parser

    add_integrations_parser(sub, DEFAULT_DB)
    from scruple.tenancy.cli import add_tenant_parser

    add_tenant_parser(sub, DEFAULT_DB)
    return p


@functools.cache
def _parser() -> argparse.ArgumentParser:
    """The parser, built once per process. Building it registers every command
    (over a hundred subparsers, tens of milliseconds), which dominated in-process
    callers such as the episode load test. Parsing never mutates it and every
    default is a constant, so one instance serves every call."""
    return build_parser()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (OSError, ValueError, KeyError, InvalidTransitionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
