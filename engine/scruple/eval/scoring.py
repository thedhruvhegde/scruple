"""Per-episode rows and paired comparisons, the same as scripts/model_episodes_report.py.

`row` is the script's `_row`, `paired_cost` its `_paired_cost` (paired bootstrap
over seeds, 2,000 resamples, `random.Random(0)`), `reference_rows` its
`_policy_rows`. tests/test_eval_scoring.py checks they agree on the same inputs, so
a number from `scruple eval` and one from the script are the same number.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.agents.dispute_baselines import (
    always_accept,
    evidence_policy,
    oracle_policy,
    run_policy,
)

N_BOOT = 2000
EVIDENCE_REF = "ref:evidence"
REFERENCES = ("ref:evidence", "ref:always_accept", "ref:oracle")
SMALL_N = 30


def row(label: str, score: dict[str, Any]) -> dict[str, Any]:
    """One episode as a flat row (from a `DisputeScore` dumped to JSON)."""
    sc = score["scorecard"]
    return {
        "system": label,
        "seed": score["seed"],
        "family": score["family"],
        "cost_usd": score["cost_usd_minor"] / 100,
        "resolution": score["resolution"],
        "gate": sc["gate"],
        "reward": sc["reward"],
        "quality": round(sc["quality"], 6),
        "hard_failures": sorted(sc["hard_failures"]),
        "soft_failures": sorted(c["id"] for c in sc["checks"] if not c["hard"] and not c["passed"]),
        "fair_outcome": next((c["passed"] for c in sc["checks"] if c["id"] == "OUT-01"), None),
        "tool_calls": score.get("tool_calls"),
        "tool_errors": score.get("tool_errors"),
    }


_POLICIES = {
    "ref:evidence": (evidence_policy, False),
    "ref:always_accept": (always_accept, False),
    "ref:oracle": (oracle_policy, True),
}


@lru_cache(maxsize=65_536)
def _reference_row(name: str, seed: int) -> str:
    pol, ref = _POLICIES[name]
    ep = run_policy(pol, seed, None, reference=ref)
    assert ep.score is not None
    return json.dumps(row(name, json.loads(ep.score.model_dump_json())))


def reference_rows(seeds: Iterable[int]) -> list[dict[str, Any]]:
    """The three reference policies replayed in-process on the same seeds.
    Deterministic, so each (policy, seed) is computed once per process."""
    seeds = list(seeds)
    return [json.loads(_reference_row(name, s)) for name in _POLICIES for s in seeds]


def paired_bootstrap(
    a: dict[int, float], b: dict[int, float], n_boot: int = N_BOOT, seed: int = 0
) -> tuple[float, float, float]:
    """Mean of a - b over shared seeds, with a 95% percentile bootstrap interval."""
    seeds = sorted(set(a) & set(b))
    if not seeds:
        raise ValueError("no shared seeds to pair")
    d = [a[s] - b[s] for s in seeds]
    rng = random.Random(seed)
    boots = sorted(sum(d[rng.randrange(len(d))] for _ in d) / len(d) for _ in range(n_boot))
    return sum(d) / len(d), boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]


def paired_cost(a: dict[int, float], b: dict[int, float]) -> tuple[float, float, float]:
    return paired_bootstrap(a, b)


class SystemSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str
    n: int
    mean_cost_usd: float
    vs_evidence: tuple[float, float, float] | None  # mean diff, lo, hi (cost, USD)
    reward_vs_evidence: tuple[float, float, float] | None
    gate: int
    mean_reward: float
    won_partial: int
    fair: int
    top_hard_failures: tuple[tuple[str, int], ...]


def summarise(rows: Sequence[dict[str, Any]]) -> list[SystemSummary]:
    systems = list(dict.fromkeys(r["system"] for r in rows))
    by = {s: [r for r in rows if r["system"] == s] for s in systems}
    ev = by.get(EVIDENCE_REF, [])
    ev_cost = {r["seed"]: r["cost_usd"] for r in ev}
    ev_reward = {r["seed"]: r["reward"] for r in ev}
    out = []
    for s in systems:
        rs = by[s]
        n = len(rs)
        cost = {r["seed"]: r["cost_usd"] for r in rs}
        reward = {r["seed"]: r["reward"] for r in rs}
        paired = s != EVIDENCE_REF and bool(set(cost) & set(ev_cost))
        out.append(
            SystemSummary(
                system=s,
                n=n,
                mean_cost_usd=sum(r["cost_usd"] for r in rs) / n,
                vs_evidence=paired_cost(cost, ev_cost) if paired else None,
                reward_vs_evidence=paired_bootstrap(reward, ev_reward) if paired else None,
                gate=sum(bool(r["gate"]) for r in rs),
                mean_reward=sum(r["reward"] for r in rs) / n,
                won_partial=sum(r["resolution"] in ("won", "partial") for r in rs),
                fair=sum(bool(r["fair_outcome"]) for r in rs),
                top_hard_failures=tuple(
                    Counter(f for r in rs for f in r["hard_failures"]).most_common(4)
                ),
            )
        )
    return out


def fmt_ci(ci: tuple[float, float, float] | None, digits: int = 2) -> str:
    if ci is None:
        return ""
    m, lo, hi = ci
    return f"{m:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"


def summary_table(summaries: Sequence[SystemSummary]) -> list[str]:
    """The script's SUMMARY.md table."""
    lines = [
        "| system | n | mean cost $ | vs evidence ref, 95% ci | gate pass | mean reward |"
        " won/partial | fair outcome (OUT-01) | top hard failures |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        ftxt = ", ".join(f"{k} x{v}" for k, v in s.top_hard_failures) or "none"
        lines.append(
            f"| {s.system} | {s.n} | {s.mean_cost_usd:.2f} | {fmt_ci(s.vs_evidence)} "
            f"| {s.gate}/{s.n} | {s.mean_reward:.3f} | {s.won_partial}/{s.n} "
            f"| {s.fair}/{s.n} | {ftxt} |"
        )
    return lines


__all__ = [
    "EVIDENCE_REF",
    "N_BOOT",
    "REFERENCES",
    "SMALL_N",
    "SystemSummary",
    "fmt_ci",
    "paired_bootstrap",
    "paired_cost",
    "reference_rows",
    "row",
    "summarise",
    "summary_table",
]
