"""Run every formal check and print what holds, to which depth, and how long it took.

    uv run python -m scruple.formal.run [--depth N] [--json]

For each model: Houdini induction (which invariants hold at every depth under the
abstraction), then bounded model checking of each invariant separately to the depth
given, with cover checks (every command reachable). Exit status 1 if any invariant
has a counterexample. Results are about the abstract models, which the conformance
tests tie to the engines; they are not claims about real networks or banks.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from scruple.formal.ops import Model

DEFAULT_DEPTH = {"dispute": 30, "ach": 12}


@dataclass(frozen=True)
class Target:
    model: Model
    depth: int


def targets(depth: int | None = None) -> list[Target]:
    from scruple.formal import ach, dispute

    return [Target(m, depth or DEFAULT_DEPTH[m.name]) for m in (dispute.MODEL, ach.MODEL)]


def run(depth: int | None = None) -> list[dict[str, Any]]:
    from scruple.formal.bmc import check, induction

    out: list[dict[str, Any]] = []
    for t in targets(depth):
        ind = induction(t.model)
        rep = check(t.model, t.depth)
        out.append(
            {
                "model": t.model.name,
                "depth": t.depth,
                "seconds": rep.seconds,
                "induction_seconds": ind.seconds,
                "uncovered": [c for c, d in rep.covered.items() if d is None],
                "invariants": [
                    {
                        "name": r.invariant,
                        "holds_to_depth": r.holds,
                        "inductive": r.invariant in ind.inductive,
                        "depth": r.depth,
                        "seconds": r.seconds,
                        "counterexample": None
                        if r.counterexample is None
                        else r.counterexample.text(),
                    }
                    for r in rep.results
                ],
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scruple.formal.run")
    ap.add_argument("--depth", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    results = run(args.depth)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for m in results:
            print(
                f"{m['model']}: depth {m['depth']}, {m['seconds']}s "
                f"(induction {m['induction_seconds']}s); uncovered commands: "
                f"{m['uncovered'] or 'none'}"
            )
            for r in m["invariants"]:
                if r["holds_to_depth"]:
                    how = "all depths (inductive)" if r["inductive"] else f"to depth {r['depth']}"
                    print(f"  holds   {r['name']:<24} {how:<24} {r['seconds']}s")
                else:
                    print(f"  FAILS   {r['name']:<24} at depth {r['depth']}")
                    print("    " + r["counterexample"].replace("\n", "\n    "))
    bad = any(not r["holds_to_depth"] for m in results for r in m["invariants"])
    return 1 if bad else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
