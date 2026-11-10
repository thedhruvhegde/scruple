"""`scruple strategies list | show <name> | brief <name> --seeds ... --store ...`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scruple.agents.strategies.library import STRATEGIES, get_strategy
from scruple.agents.strategies.render import DEFAULT_WORKDIR, brief
from scruple.agents.strategies.seeds import parse_seeds


def cmd_strategies(args: argparse.Namespace) -> int:
    match args.strategies_cmd:
        case "list":
            if args.json:
                rows = [
                    {
                        "name": s.name,
                        "kind": s.kind,
                        "title": s.title,
                        "summary": s.summary,
                        "measures": s.measures,
                    }
                    for s in STRATEGIES.values()
                ]
                print(json.dumps(rows, indent=2, sort_keys=True))
                return 0
            width = max(len(n) for n in STRATEGIES)
            for s in STRATEGIES.values():
                print(f"{s.name:{width}}  {s.kind:9}  {s.summary}")
            return 0
        case "show":
            plan = get_strategy(args.name).api_plan()
            if args.json:
                print(plan.model_dump_json(indent=2))
            else:
                print(plan.system)
                print()
                print("loop: " + json.dumps(plan.loop.model_dump(mode="json"), sort_keys=True))
            return 0
        case "brief":
            text = brief(
                get_strategy(args.name),
                parse_seeds(args.seeds),
                args.store,
                workdir=args.workdir,
            )
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(text)
            else:
                print(text, end="")
            return 0
    raise AssertionError(args.strategies_cmd)  # pragma: no cover


def add_strategies_parser(sub: Any) -> None:
    p = sub.add_parser("strategies", help="list agent strategies and render their briefs")
    p.set_defaults(fn=cmd_strategies)
    ss = p.add_subparsers(dest="strategies_cmd", required=True)
    ls = ss.add_parser("list", help="list strategies")
    ls.add_argument("--json", action="store_true")
    sh = ss.add_parser("show", help="print the API system prompt and loop config")
    sh.add_argument("name", choices=list(STRATEGIES))
    sh.add_argument("--json", action="store_true")
    br = ss.add_parser("brief", help="render a CLI player brief (markdown)")
    br.add_argument("name", choices=list(STRATEGIES))
    br.add_argument("--seeds", required=True, help="e.g. 5001-5012 or 1,4,9")
    br.add_argument("--store", required=True, help="episode store the player writes to")
    br.add_argument("--workdir", default=DEFAULT_WORKDIR, help="where the player runs the CLI")
    br.add_argument("--out", default=None, help="write the brief here instead of stdout")


__all__ = ["add_strategies_parser", "cmd_strategies"]
