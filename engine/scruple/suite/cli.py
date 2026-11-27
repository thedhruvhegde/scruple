"""`scruple suite plan | verify | run | report | replay`: the client test-suite deliverable."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scruple.suite.envs import ENV_NAMES


def _envs(text: str | None) -> list[str] | None:
    if not text:
        return None
    out = [e.strip() for e in text.split(",") if e.strip()]
    if out == ["all"]:
        return list(ENV_NAMES)
    for e in out:
        if e not in ENV_NAMES:
            raise ValueError(f"unknown environment {e!r}; choose from {', '.join(ENV_NAMES)}")
    return out


def _salt(path: str | None) -> str | None:
    return None if path is None else Path(path).read_text().strip()


def cmd_suite(args: argparse.Namespace) -> int:
    match args.suite_cmd:
        case "plan":
            from scruple.suite.plan import build_plan, parse_caps, write_plan

            plan, salt = build_plan(
                _envs(args.envs) or list(ENV_NAMES),
                args.size,
                seed=args.seed,
                mix=args.difficulty_mix,
                caps=parse_caps(args.cap),
                salt=_salt(args.salt),
                workers=args.workers,
            )
            sp = write_plan(
                plan, salt, Path(args.out), Path(args.salt_out) if args.salt_out else None
            )
            info = {k: plan[k] for k in ("n", "plan_hash", "bank_hash", "commitments_hash")}
            info |= {"stats": plan["stats"], "plan": args.out, "salt_file": str(sp)}
            print(json.dumps(info, indent=2, sort_keys=True))
            return 0
        case "verify":
            from scruple.suite.plan import load_plan, verify_plan

            plan = load_plan(Path(args.plan))
            probs = verify_plan(plan, salt=_salt(args.salt), limit=args.limit)
            print(
                json.dumps(
                    {"plan_hash": plan["plan_hash"], "n": plan["n"], "problems": probs},
                    indent=2,
                )
            )
            return 1 if probs else 0
        case "run":
            from scruple.agents.providers import RunSettings
            from scruple.suite.agents import parse_agent
            from scruple.suite.run import run_suite

            agent = parse_agent(
                args.agent,
                rpm=args.rpm,
                settings=RunSettings(
                    timeout_s=args.timeout, max_retries=args.max_retries, backoff_s=4.0
                ),
                base_url=args.base_url,
                paced=not args.no_pacing,
                transport=args.transport,
            )

            def progress(rec: Any) -> None:
                if args.quiet:
                    return
                o = rec.outcome
                print(
                    f"{rec.item.scenario_id} {rec.item.env}: {rec.play.stop}, "
                    f"gate {'pass' if o.gate else 'FAIL ' + ','.join(o.hard_failures)}, "
                    f"reward {o.reward:.3f}",
                    file=sys.stderr,
                )

            meta = run_suite(
                Path(args.plan),
                agent,
                Path(args.out),
                strategy=args.strategy,
                envs=_envs(args.envs),
                per_env=args.per_env,
                limit=args.limit,
                concurrency=args.concurrency,
                progress=progress,
            )
            print(
                json.dumps({k: meta[k] for k in ("agent", "plan_hash", "last_session")}, indent=2)
            )
            return 0
        case "report":
            from scruple.suite.report import build_report, write_report

            rep = build_report(
                Path(args.dir),
                baseline=Path(args.baseline) if args.baseline else None,
                salt=_salt(args.salt),
                alpha=args.alpha,
                max_fail=args.max_fail,
                tolerance=args.tolerance,
                title=args.title,
            )
            fmts = [f.strip() for f in args.format.split(",") if f.strip()]
            paths = write_report(rep, Path(args.out or args.dir), fmts)
            print(
                json.dumps(
                    {"verdict": rep["executive"]["verdict"], "files": [str(p) for p in paths]},
                    indent=2,
                )
            )
            return 0
        case "replay":
            from scruple.suite.runner import ItemRecord, grade_calls

            rec = ItemRecord.model_validate_json(
                (Path(args.dir) / "items" / f"{args.scenario}.json").read_text()
            )
            outcome, head = grade_calls(rec.item, rec.play.calls)
            print(
                json.dumps(
                    {
                        "scenario_id": rec.item.scenario_id,
                        "env": rec.item.env,
                        "calls": list(rec.play.calls),
                        "outcome": outcome.model_dump(mode="json"),
                        "trace_head": head,
                        "matches_record": outcome == rec.outcome,
                    },
                    indent=1,
                    default=str,
                )
            )
            return 0
    raise AssertionError(args.suite_cmd)  # pragma: no cover


def add_suite_parser(sub: Any) -> None:
    p = sub.add_parser(
        "suite", help="client test suite: plan a scenario bank, run an agent, report"
    )
    p.set_defaults(fn=cmd_suite)
    ss = p.add_subparsers(dest="suite_cmd", required=True)

    pl = ss.add_parser("plan", help="generate a versioned, hash-committed scenario bank")
    pl.add_argument("--envs", default="all", help=f"comma list or all ({', '.join(ENV_NAMES)})")
    pl.add_argument("--size", type=int, required=True, help="number of scenarios")
    pl.add_argument(
        "--difficulty-mix",
        default="easy:0.3,medium:0.4,hard:0.3",
        help="band weights, e.g. easy:0.3,medium:0.4,hard:0.3",
    )
    pl.add_argument("--seed", type=int, default=0, help="plan seed (same seed, same bank)")
    pl.add_argument("--cap", default=None, help="scale an env's difficulty, e.g. recon=0.3")
    pl.add_argument(
        "--salt", default=None, help="file holding the commitment salt (default: random)"
    )
    pl.add_argument("--salt-out", default=None, help="where to write the salt (default: OUT.salt)")
    pl.add_argument("--workers", type=int, default=1)
    pl.add_argument("--out", required=True)

    v = ss.add_parser("verify", help="regenerate a plan's cases and check its hashes")
    v.add_argument("plan")
    v.add_argument("--salt", default=None, help="salt file: also check every truth commitment")
    v.add_argument("--limit", type=int, default=None, help="check only the first N scenarios")

    r = ss.add_parser("run", help="run an agent through a plan (resumable)")
    r.add_argument("--plan", required=True)
    r.add_argument(
        "--agent",
        required=True,
        help="provider:model | python:module:fn | scripted:policy | http(s)://endpoint",
    )
    r.add_argument("--out", required=True)
    r.add_argument("--strategy", default="zero-shot")
    r.add_argument("--envs", default=None)
    r.add_argument("--per-env", type=int, default=None, help="first K scenarios per env")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--concurrency", type=int, default=4, help="local agents only")
    r.add_argument("--rpm", type=float, default=None, help="API agents: requests per minute")
    r.add_argument("--timeout", type=float, default=180.0)
    r.add_argument("--max-retries", type=int, default=10)
    r.add_argument("--base-url", default=None)
    r.add_argument("--no-pacing", action="store_true", help="API agents: no provider-limit rules")
    r.add_argument("--transport", choices=["http", "fake"], default="http")
    r.add_argument("--quiet", action="store_true")

    rp = ss.add_parser("report", help="the client report for a run directory")
    rp.add_argument("dir")
    rp.add_argument("--baseline", default=None, help="a run directory on the same plan")
    rp.add_argument("--format", default="html,pdf,json")
    rp.add_argument("--out", default=None, help="output directory (default: the run directory)")
    rp.add_argument("--salt", default=None, help="plan salt file: reveal and check commitments")
    rp.add_argument("--alpha", type=float, default=0.05)
    rp.add_argument(
        "--max-fail", type=float, default=0.10, help="largest acceptable gate failure rate"
    )
    rp.add_argument("--tolerance", type=float, default=0.02, help="reward regression tolerance")
    rp.add_argument("--title", default=None)

    rr = ss.add_parser("replay", help="replay one scenario's calls and print the graded trace")
    rr.add_argument("dir")
    rr.add_argument("scenario")


__all__ = ["add_suite_parser", "cmd_suite"]
