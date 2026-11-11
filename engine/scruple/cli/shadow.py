"""`scruple shadow imported ...`: shadow mode on imported (real) cases from a terminal.

The same store as the API (`/shadow/rules/{id}/versions/{v}/imported...`): accepted
batches are appended to the gate database and replayed, so `record` in one process
and `summary` in another give the answer the API gives. The command line is the
local admin of one tenant ($SCRUPLE_TENANT, else "default"), as for every other
command. Batch files use the API's request bodies:

    scruple shadow imported record  RULE VERSION decisions.json --actor ops
    scruple shadow imported observe RULE VERSION outcomes.json --actor ops
    scruple shadow imported summary RULE VERSION [--json]
    scruple shadow imported history RULE VERSION [--json]

`-` reads the batch from stdin. A refused batch (a duplicate case, a decision timed
before the case was seen, ...) changes nothing and exits 2. Metrics are labelled
observed or estimated, as in the API; nothing here invents ground truth.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scruple.gate.shadow_imported import (
    ImportedDecisionBatch,
    ImportedOutcomeBatch,
    ImportedShadowSummary,
)


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text()


def _shadow(args: argparse.Namespace, *, write: bool) -> Any:
    from scruple.cli.main import _gate
    from scruple.storage.imported_shadow import imported_shadows

    # Reads never write (no baseline import); writes need the rule registered anyway.
    g = _gate(args, bootstrap=write)
    return imported_shadows(g, g.storage.tenant)(args.rule_id, args.version)


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.4g}"


def print_summary(s: ImportedShadowSummary) -> None:
    print(f"candidate {s.candidate}  data: {s.data}  cost profile: {s.profile}")
    print(f"decisions {s.decisions}  resolved {s.resolved}  open {s.open}")
    if s.currency is not None:
        print(f"cost metrics in {s.currency}")
    for m in s.metrics:
        band = f" [{_fmt(m.low)}, {_fmt(m.high)}]" if m.low is not None else ""
        note = f"  ({m.note})" if m.note else ""
        print(f"  {m.name:34} {m.basis:9} {_fmt(m.value):>10}{band}  n={m.n}{note}")
    for f in s.findings:
        cand = "-" if f.candidate is None else str(f.candidate)
        print(
            f"  finding {f.check} ({f.kind}, {f.basis}): production {f.production}, "
            f"candidate {cand}"
        )


def cmd_shadow_imported(args: argparse.Namespace) -> int:
    action = args.shadow_cmd
    if action == "record":
        batch = ImportedDecisionBatch.model_validate_json(_read(args.file))
        res: Any = _shadow(args, write=True).record(batch.decisions, args.actor)
        print(json.dumps(res.model_dump(mode="json"), sort_keys=True))
        return 0
    if action == "observe":
        outcomes = ImportedOutcomeBatch.model_validate_json(_read(args.file))
        res = _shadow(args, write=True).observe(outcomes.outcomes, args.actor)
        print(json.dumps(res.model_dump(mode="json"), sort_keys=True))
        return 0
    shadow = _shadow(args, write=False)
    if action == "summary":
        summary = shadow.summary()
        if args.json:
            print(json.dumps(summary.model_dump(mode="json"), sort_keys=True, indent=1))
        else:
            print_summary(summary)
        return 0
    rows = shadow.history()
    if args.json:
        out = [
            {"seq": r.seq, "at": r.at.isoformat(), "summary": json.loads(r.body_json)} for r in rows
        ]
        print(json.dumps(out, sort_keys=True, indent=1))
        return 0
    if not rows:
        print("no batches recorded")
    for r in rows:
        s = ImportedShadowSummary.model_validate_json(r.body_json)
        print(
            f"{r.seq:6} {r.at.isoformat()}  decisions {s.decisions}  resolved {s.resolved}"
            f"  open {s.open}"
        )
    return 0


def add_shadow_parser(sub: Any, default_db: str) -> None:
    p = sub.add_parser("shadow", help="shadow mode on imported cases (the persisted store)")
    kinds = p.add_subparsers(dest="shadow_kind", required=True)
    imp = kinds.add_parser("imported", help="imported (real) cases: record, observe, read")
    cmds = imp.add_subparsers(dest="shadow_cmd", required=True)
    for name, help_ in (
        ("record", "record a batch of production decisions (ImportedDecisionBatch JSON)"),
        ("observe", "record a batch of outcomes (ImportedOutcomeBatch JSON)"),
        ("summary", "print the current summary"),
        ("history", "print the summary after every stored batch, oldest first"),
    ):
        sp = cmds.add_parser(name, help=help_)
        sp.set_defaults(fn=cmd_shadow_imported)
        sp.add_argument("rule_id")
        sp.add_argument("version", type=int)
        sp.add_argument(
            "--db",
            default=None,
            help=f"SQLite path or postgresql:// URL (default: $SCRUPLE_DATABASE_URL, "
            f"else {default_db})",
        )
        if name in ("record", "observe"):
            sp.add_argument("file", help="the batch as JSON, or - for stdin")
            sp.add_argument("--actor", required=True, help="who sends it, for the store")
        else:
            sp.add_argument("--json", action="store_true", help="print JSON")


__all__ = ["add_shadow_parser", "cmd_shadow_imported", "print_summary"]
