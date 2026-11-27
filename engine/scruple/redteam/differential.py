"""Differential testing of the verifiers against their independent second versions.

`corpus(env, seeds, n_random)` lists the genomes played for an environment: every
base policy, every hacker knob alone, the hacker families, and `n_random` random
genomes (seeded, the same generator the search uses). `run(...)` plays them, scores
each record with the environment's verifier and with every independent checker in
`scruple.verifiers.nversion.SHADOWS` for that environment, and returns per-check
agreement counts and every disagreeing episode.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.finops.parallel import pmap
from scruple.redteam.envs import adapter, play
from scruple.redteam.genome import Genome, random_op
from scruple.verifiers.nversion import SHADOWS


class Disagreement(BaseModel):
    model_config = ConfigDict(frozen=True)

    env: str
    check: str
    genome: str
    seed: int
    existing: bool
    independent: bool
    existing_why: str
    independent_why: tuple[str, ...]


def corpus(env: str, n_random: int, rng_seed: int = 0) -> list[Genome]:
    a = adapter(env)
    out = [Genome(b) for b in a.space.bases if b != "knobs"]
    out += [Genome("knobs", (k,)) for k in a.space.knobs]
    rng = random.Random(rng_seed)
    for _ in range(n_random):
        b = rng.choice(a.space.bases)
        ks = tuple(sorted({rng.choice(a.space.knobs) for _ in range(2)})) if b == "knobs" else ()
        ops = tuple(random_op(a.space, rng) for _ in range(rng.randint(1, 3)))
        out.append(Genome(b, ks, ops))
    return list(dict.fromkeys(out))


def _one(item: tuple[str, dict[str, Any], tuple[int, ...]]) -> tuple[Counter[str], list[Any]]:
    env, gj, seeds = item
    a = adapter(env)
    g = Genome.from_json(gj)
    shadows = {cid: fn for cid, (e, fn) in SHADOWS.items() if e == env}
    agree: Counter[str] = Counter()
    diffs: list[Any] = []
    for s in seeds:
        r = play(env, g, s)
        card = a.score(r)
        # the claim shadows are of the v2 claim checks (text reading included); v3
        # renamed and narrowed those (`*-CLAIM3`), so they are compared with v2's card
        by = {c.id: c for c in _v2_card(env, r).checks} | {c.id: c for c in card.checks}
        for cid, fn in shadows.items():
            ok, why = fn(r)
            ex = by[cid]
            agree[f"{cid}|n"] += 1
            agree[f"{cid}|fail_existing"] += int(not ex.passed)
            agree[f"{cid}|fail_independent"] += int(not ok)
            if ok == ex.passed:
                agree[f"{cid}|agree"] += 1
            else:
                diffs.append(
                    Disagreement(
                        env=env,
                        check=cid,
                        genome=g.describe(),
                        seed=s,
                        existing=ex.passed,
                        independent=ok,
                        existing_why=ex.explanation if hasattr(ex, "explanation") else str(ex),
                        independent_why=why,
                    ).model_dump(mode="json")
                )
    return agree, diffs


def _v2_card(env: str, r: Any) -> Any:
    if env == "dispute":
        from scruple.verifiers.dispute import DISPUTE_CHECKS_V2, score_record

        return score_record(r, DISPUTE_CHECKS_V2)
    if env == "commerce":
        from scruple.verifiers.commerce import score_commerce

        return score_commerce(r)
    from scruple.finops.verifiers import get_verifier

    return get_verifier(f"{env}@2").score(r)


def run(
    env: str, seeds: Sequence[int], n_random: int = 40, workers: int = 1, rng_seed: int = 0
) -> dict[str, Any]:
    gs = corpus(env, n_random, rng_seed)
    items = [(env, g.to_json(), tuple(seeds)) for g in gs]
    agree: Counter[str] = Counter()
    diffs: list[Any] = []
    for c, d in pmap(_one, items, workers=workers, chunksize=1):
        agree.update(c)
        diffs += d
    checks = sorted({k.split("|")[0] for k in agree})
    table = {
        cid: {
            "episodes": agree[f"{cid}|n"],
            "agree": agree[f"{cid}|agree"],
            "existing_fails": agree[f"{cid}|fail_existing"],
            "independent_fails": agree[f"{cid}|fail_independent"],
        }
        for cid in checks
    }
    return {"env": env, "genomes": len(gs), "seeds": len(seeds), "table": table, "diffs": diffs}


__all__ = ["Disagreement", "corpus", "run"]
