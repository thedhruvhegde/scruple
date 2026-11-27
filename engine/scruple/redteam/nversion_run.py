"""Run the N-version differential harness at scale and write the table.

`python -m scruple.redteam.nversion_run OUT.json [--seeds N] [--random K] [--workers W]
[--searched search.json]`: every base policy and knob of each environment, K random
genomes, and the genomes a search returned, on N seeds each (seeds 70,000+).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from scruple.finops.parallel import pmap
from scruple.redteam.differential import _one, corpus
from scruple.redteam.envs import ENV_NAMES
from scruple.redteam.genome import Genome


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("--seeds", type=int, default=40)
    p.add_argument("--random", type=int, default=40)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--searched", default=None)
    p.add_argument("--env", default="all")
    a = p.parse_args(argv)
    found: dict[str, list[Genome]] = {}
    if a.searched:
        for res in json.loads(Path(a.searched).read_text()):
            found[res["env"]] = [Genome.from_json(t["genome"]) for t in res["top"]]
    envs = list(ENV_NAMES) if a.env == "all" else a.env.split(",")
    out: dict[str, Any] = {}
    seeds = tuple(range(70_000, 70_000 + a.seeds))
    for env in envs:
        gs = list(dict.fromkeys([*corpus(env, a.random, rng_seed=3), *found.get(env, [])]))
        agree: Counter[str] = Counter()
        diffs: list[Any] = []
        for c, d in pmap(_one, [(env, g.to_json(), seeds) for g in gs], workers=a.workers):
            agree.update(c)
            diffs += d
        checks = sorted({k.split("|")[0] for k in agree})
        out[env] = {
            "genomes": len(gs),
            "seeds": len(seeds),
            "table": {
                cid: {
                    "episodes": agree[f"{cid}|n"],
                    "agree": agree[f"{cid}|agree"],
                    "existing_fails": agree[f"{cid}|fail_existing"],
                    "independent_fails": agree[f"{cid}|fail_independent"],
                }
                for cid in checks
            },
            "diffs": diffs[:200],
            "n_diffs": len(diffs),
        }
        print(env, json.dumps(out[env]["table"]), "diffs", len(diffs), flush=True)
    out["faults"] = faults(seeds)
    print("faults", json.dumps(out["faults"]), flush=True)
    Path(a.out).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return 0


def _shift_posting(r: Any, k: int) -> Any:
    """Fault injection: move one minor unit in the k-th non-opening entry's first
    posting, in the journal and in the trace's copy alike (the hash chain is left
    stale, so the integrity check also fires; the ledger checks must too)."""
    from scruple.verifiers.trace import EventKind

    entries = list(r.journal)
    idx = [i for i, e in enumerate(entries) if getattr(e, "kind", "") != "opening"]
    if not idx:
        return None
    i = idx[k % len(idx)]
    e = entries[i]
    p0 = e.postings[0].model_copy(update={"amount_minor": e.postings[0].amount_minor + 1})
    entries[i] = e.model_copy(update={"postings": (p0, *e.postings[1:])})
    trace = list(r.trace)
    n = -1
    for j, ev in enumerate(trace):
        if ev.kind is EventKind.POSTING:
            n += 1
            if n == i:
                d = json.loads(json.dumps(ev.data))
                d["postings"][0]["amount_minor"] += 1
                trace[j] = ev.model_copy(update={"data": d})
    return r.model_copy(update={"journal": tuple(entries), "trace": tuple(trace)})


def faults(seeds: tuple[int, ...]) -> dict[str, Any]:
    """Both versions on records the agents cannot produce: shifted postings (ledger
    checks) and Guard-off commerce episodes (the mandate check)."""
    from scruple.redteam.banks import play_ct
    from scruple.redteam.envs import adapter, play
    from scruple.verifiers.nversion import SHADOWS

    res: dict[str, Any] = {}
    for cid, (env, fn) in SHADOWS.items():
        if cid not in ("REC-BAL", "ACH-LED", "PAY-LED", "CM-LED"):
            continue
        a = adapter(env)
        agree = n = fail_x = fail_i = 0
        for s in seeds:
            r = play(env, Genome(a.honest), s)
            for k in range(3):
                t = _shift_posting(r, k)
                if t is None:
                    continue
                ex = next(c for c in a.score(t).checks if c.id == cid).passed
                ok = fn(t)[0]
                n += 1
                agree += int(ex == ok)
                fail_x += int(not ex)
                fail_i += int(not ok)
        res[cid] = {"episodes": n, "agree": agree, "existing_fails": fail_x, "independent_fails": fail_i}
    a = adapter("commerce")
    fn = SHADOWS["CM-MAND"][1]
    agree = n = fail_x = fail_i = 0
    for s in seeds:
        for ct in ("injection+noguard", "beneficiary_change+noguard", "overspend+noguard"):
            for b in ("obedient", "honest", "split_purchase"):
                r = play_ct("commerce", Genome(b), s, ct)
                ex = next(c for c in a.score(r).checks if c.id == "CM-MAND").passed
                ok = fn(r)[0]
                n += 1
                agree += int(ex == ok)
                fail_x += int(not ex)
                fail_i += int(not ok)
    res["CM-MAND (Guard off)"] = {
        "episodes": n,
        "agree": agree,
        "existing_fails": fail_x,
        "independent_fails": fail_i,
    }
    return res


if __name__ == "__main__":
    raise SystemExit(main())
