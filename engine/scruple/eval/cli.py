"""`scruple eval providers | run | import-store | import-calls | summary | leaderboard`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scruple.agents.providers import Price, RunSettings, make_provider, provider_names
from scruple.agents.providers.registry import PROVIDERS
from scruple.agents.strategies import STRATEGIES, parse_seeds
from scruple.eval.batch import EvalConfig, import_calls, import_store, run_eval, write_summary
from scruple.eval.records import EvalRecord


def _price(args: argparse.Namespace) -> Price | None:
    if args.price_in is None and args.price_out is None:
        return None
    if args.price_in is None or args.price_out is None:
        raise ValueError("give both --price-in and --price-out (USD per million tokens)")
    return Price(input_per_mtok=args.price_in, output_per_mtok=args.price_out)


def _progress(rec: EvalRecord) -> None:
    sc = rec.score["scorecard"]
    print(
        f"seed {rec.seed}: {rec.stop}, {rec.score['resolution']}, "
        f"cost ${rec.row['cost_usd']:.2f}, gate {'pass' if sc['gate'] else 'FAIL'}",
        file=sys.stderr,
    )


def cmd_eval(args: argparse.Namespace) -> int:
    match args.eval_cmd:
        case "providers":
            rows = []
            for name in provider_names():
                cls, url = PROVIDERS[name]
                rows.append(
                    {
                        "name": name,
                        "wire": cls.wire,
                        "base_url": url,
                        "key_env": list(cls.key_env),
                        "key_required": cls.key_required,
                        "seed_field": cls.seed_field,
                        "docs": cls.docs,
                    }
                )
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        case "run":
            if args.transport == "http":  # fail fast on a missing key, before any work
                make_provider(args.provider, base_url=args.base_url)
            cfg = EvalConfig(
                provider=args.provider,
                model=args.model,
                strategy=args.strategy,
                seeds=parse_seeds(args.seeds),
                transport=args.transport,
                base_url=args.base_url,
                concurrency=args.concurrency,
                rpm=args.rpm,
                price=_price(args),
                settings=RunSettings(
                    timeout_s=args.timeout,
                    max_retries=args.max_retries,
                    seed_from_episode=not args.no_seed,
                ),
            )
            out = Path(args.out)
            run_eval(cfg, out, progress=None if args.quiet else _progress)
            print((out / "SUMMARY.md").read_text(), end="")
            return 0
        case "import-store":
            out = Path(args.out)
            import_store(
                Path(args.store), out, model=args.model, strategy=args.strategy, provider=args.label
            )
            print((out / "SUMMARY.md").read_text(), end="")
            return 0
        case "import-calls":
            out = Path(args.out)
            import_calls(
                Path(args.calls), out, model=args.model, strategy=args.strategy, provider=args.label
            )
            print((out / "SUMMARY.md").read_text(), end="")
            return 0
        case "summary":
            out = Path(args.dir)
            write_summary(out)
            print((out / "SUMMARY.md").read_text(), end="")
            return 0
        case "leaderboard":
            from scruple.eval.leaderboard import build_leaderboard

            board = build_leaderboard([Path(d) for d in args.dirs], n_boot=args.n_boot)
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "leaderboard.json").write_text(
                json.dumps(board.model_dump(mode="json"), indent=1, sort_keys=True) + "\n"
            )
            md = board.markdown()
            (out / "LEADERBOARD.md").write_text(md)
            print(md, end="")
            return 0
    raise AssertionError(args.eval_cmd)  # pragma: no cover


def add_eval_parser(sub: Any) -> None:
    p = sub.add_parser("eval", help="evaluate LLM agents on dispute episodes (batch, leaderboard)")
    p.set_defaults(fn=cmd_eval)
    es = p.add_subparsers(dest="eval_cmd", required=True)
    es.add_parser("providers", help="list providers, their key variables and endpoints")

    r = es.add_parser("run", help="play seeds with one provider, model and strategy")
    r.add_argument("--provider", required=True, choices=list(provider_names()))
    r.add_argument("--model", required=True)
    r.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    r.add_argument("--seeds", required=True, help="e.g. 5001-5012 or 1,4,9")
    r.add_argument("--out", required=True, help="run directory (resumable)")
    r.add_argument("--base-url", default=None, help="override the provider endpoint")
    r.add_argument("--concurrency", type=int, default=4, help="episodes at once")
    r.add_argument("--rpm", type=float, default=None, help="max requests per minute, shared")
    r.add_argument("--price-in", type=float, default=None, help="USD per million input tokens")
    r.add_argument("--price-out", type=float, default=None, help="USD per million output tokens")
    r.add_argument("--timeout", type=float, default=120.0, help="seconds per request")
    r.add_argument("--max-retries", type=int, default=4)
    r.add_argument("--no-seed", action="store_true", help="do not send a sampling seed")
    r.add_argument(
        "--transport",
        choices=["http", "fake"],
        default="http",
        help="fake: a scripted model in the provider's wire format, no network",
    )
    r.add_argument("--quiet", action="store_true", help="no per-seed progress on stderr")

    im = es.add_parser("import-store", help="score a store a CLI player wrote (CLI-brief path)")
    im.add_argument("--store", required=True)
    im.add_argument("--model", required=True, help="label for the model that played")
    im.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    im.add_argument("--out", required=True)
    im.add_argument("--label", default="cli-brief", help="provider label (default cli-brief)")

    ic = es.add_parser("import-calls", help="score an MCP client's call log (MCP path)")
    ic.add_argument("--calls", required=True, help='JSONL: {"seed": N, "calls": [...]} per line')
    ic.add_argument("--model", required=True, help="label for the model that played")
    ic.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    ic.add_argument("--out", required=True)
    ic.add_argument("--label", default="mcp", help="provider label (default mcp)")

    sm = es.add_parser("summary", help="re-score a run directory and print its summary")
    sm.add_argument("dir")

    lb = es.add_parser("leaderboard", help="aggregate run directories into a leaderboard")
    lb.add_argument("dirs", nargs="+", help="run directories")
    lb.add_argument("--out", required=True)
    lb.add_argument("--n-boot", type=int, default=2000, help="bootstrap resamples")


__all__ = ["add_eval_parser", "cmd_eval"]
