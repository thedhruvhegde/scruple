"""`scruple pilot run`. See `scruple.pilot.run` and docs/pilot/README.md."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from scruple.ingest.errors import IngestError
from scruple.pilot.run import STRIPE_PULL, PilotOptions, run_pilot


def cmd_pilot_run(args: argparse.Namespace) -> int:
    opts = PilotOptions(
        history=args.history,
        candidate=args.candidate,
        out=args.out,
        format=args.format,
        import_config=args.import_config,
        since=args.since,
        until=args.until,
        label=args.label,
        allow_live=args.allow_live,
        k=args.k,
        n_cases=args.n_cases,
        currency=args.currency,
    )
    manifest, rebuilt = run_pilot(opts)
    tag = " [SYNTHETIC DATA]" if manifest.data == "synthetic" else ""
    state = "ran" if rebuilt else "up to date (nothing ran)"
    print(f"pilot run{tag}: {state}; {manifest.history['cases']} cases, {manifest.data}")
    for s in manifest.steps:
        bits = ", ".join(f"{k}={v}" for k, v in sorted(s.summary.items()) if k != "store")
        print(f"  {s.name:<12} {s.status:<8} {s.why}" + (f" ({bits})" if bits else ""))
    print(f"manifest written to {args.out}/manifest.json")
    return 0


def _guard(fn: Any) -> Any:
    def run(args: argparse.Namespace) -> int:
        try:
            return int(fn(args))
        except (IngestError, ValueError) as e:
            print(f"error: {str(e)[:500]}", file=sys.stderr)
            return 2

    return run


def add_pilot_parser(sub: Any) -> None:
    pp = sub.add_parser("pilot", help="pilot pipeline: ingest, backtest, shadow, gate, readout")
    psub = pp.add_subparsers(dest="pilot_cmd", required=True)
    r = psub.add_parser("run", help="every pilot step on one history and one candidate")
    r.add_argument("--history", required=True, help=f"export file, case store, or {STRIPE_PULL}")
    r.add_argument("--candidate", required=True, help="rule .json, agent .py or module[:attr]")
    r.add_argument("--out", required=True, help="output directory (manifest.json and steps)")
    r.add_argument("--format", default=None, choices=["stripe", "adyen", "csv", "jsonl", "store"])
    r.add_argument("--import-config", default=None, help="ImportConfig JSON for csv / jsonl")
    r.add_argument("--since", default=None, help=f"{STRIPE_PULL}: created at or after")
    r.add_argument("--until", default=None, help=f"{STRIPE_PULL}: created before")
    r.add_argument("--label", default=None, help="dataset name on every report")
    r.add_argument("--allow-live", action="store_true", help="accept live-mode data")
    r.add_argument("--k", type=int, default=10, help="small-cell threshold (default 10)")
    r.add_argument("--n-cases", type=int, default=2_000, help="synthetic gate cases")
    r.add_argument("--currency", default=None, help="default: the most common currency")
    r.set_defaults(fn=_guard(cmd_pilot_run))


__all__ = ["add_pilot_parser"]
