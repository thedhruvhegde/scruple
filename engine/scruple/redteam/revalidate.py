"""Re-evaluate the escapes one search run returned under the current verifiers.

`python -m scruple.redteam.revalidate SEARCH.json OUT.json`: for every escape (and
every top candidate) of each environment in SEARCH.json, the held-out comparison with
the honest policy (same seeds as the search), with the code on the path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from scruple.redteam.genome import Genome
from scruple.redteam.search import _kind, compare, eval_one


def main(argv: list[str]) -> int:
    src, dst = Path(argv[0]), Path(argv[1])
    out: list[dict[str, Any]] = []
    for res in json.loads(src.read_text()):
        env = res["env"]
        seeds = tuple(range(*res["holdout_seeds"]))
        h = eval_one((env, {"base": "honest"}, seeds))
        for t in res["top"]:
            g = Genome.from_json(t["genome"])
            c = compare(env, eval_one((env, g.to_json(), seeds)), h)
            out.append(
                {
                    "env": env,
                    "genome": g.describe(),
                    "before": _kind_of(t["holdout"]),
                    "after": _kind(c) or "no",
                    "after_detail": c.model_dump(mode="json"),
                }
            )
            print(env, g.describe(), out[-1]["before"], "->", out[-1]["after"], flush=True)
    dst.write_text(json.dumps(out, indent=1) + "\n")
    return 0


def _kind_of(h: dict[str, Any] | None) -> str:
    if h is None:
        return "-"
    from scruple.redteam.search import Comparison

    return _kind(Comparison.model_validate(h)) or "no"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
