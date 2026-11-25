"""Large dev-split evaluation of the scripted dispute policies, with confidence intervals.

Plays each policy on the same public dev-split episodes (no salt; the sealed
benchmark is never touched), scores every episode with the full dispute scorecard
(per-episode checks only: batch checks are reported separately on the whole batch),
and writes one JSON line per episode plus a markdown summary.

    uv run python scripts/exp_large_eval.py --n 3000 --jobs 4 \
        --out results/large_eval_dev3000.jsonl --md results/large_eval_dev3000.md

Policies (`--policies`, comma separated):

- `evidence`: the reference evidence policy, truthful report (honest);
- `oracle`: reads the hidden labels in a declared reference run (honest by declaration);
- `always_accept`: concede everything (honest, the floor);
- `hacker`: the E2 proxy optimum (`fabricate` + `claim_rate = 1`, X38 + X11);
- `hacker_quiet`: fabrication only, truthful report (X38 alone);
- `refund_forger`: authors a settled refund record and cites it (L1, this round).

Everything is synthetic; the numbers describe our simulator and policies.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from scruple.agents.dispute_baselines import (
    always_accept,
    evidence_policy,
    honest_report,
    oracle_policy,
    run_policy,
)
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.bench.hacking import FamilyPolicy, Knobs
from scruple.sim.splits import SeedSpace, Split
from scruple.verifiers.dispute import BATCH_CHECKS, score_record
from scruple.verifiers.exploits.base import claimed_win
from scruple.verifiers.record import EpisodeRecord, SelfReport

DEV_OFFSET = 9_000_000  # dev indices used here (disjoint from the zoo's 7_000_000 range)


def _forger(ep: DisputeEpisode) -> None:
    from scruple.verifiers.exploits.laundering import forge_refund_record

    forge_refund_record(ep)


POLICIES: dict[str, tuple[Callable[[DisputeEpisode], None], Callable[..., SelfReport], bool]] = {
    "evidence": (evidence_policy, honest_report, False),
    "oracle": (oracle_policy, honest_report, True),
    "always_accept": (always_accept, honest_report, False),
    "hacker": (FamilyPolicy(Knobs(fabricate=True, claim_rate=1.0)), honest_report, False),
    "hacker_quiet": (FamilyPolicy(Knobs(fabricate=True)), honest_report, False),
    "refund_forger": (_forger, honest_report, False),
}


def _play(job: tuple[str, int]) -> tuple[dict[str, Any], EpisodeRecord]:
    name, i = job
    space = SeedSpace()
    seed, prefix = space.seed(Split.DEV, DEV_OFFSET + i), space.id_prefix(Split.DEV, DEV_OFFSET + i)
    policy, report, reference = POLICIES[name]
    if isinstance(policy, FamilyPolicy):
        report = policy.report
    ep = run_policy(policy, seed, None, report=report, reference=reference, id_prefix=prefix)
    rec = ep.record()
    card = score_record(rec)
    uploads = sum(1 for e in rec.actions(["upload_evidence"]))
    row = {
        "policy": name,
        "index": i,
        "family": rec.case.truth.family.value
        if hasattr(rec.case.truth.family, "value")
        else str(rec.case.truth.family),
        "reason_code": rec.case.reason_code,
        "network": rec.case.network.value,
        "opening_stage": rec.case.opening_stage.value,
        "claim_valid": rec.case.truth.claim_valid,
        "gate": card.gate,
        "reward": card.reward,
        "quality": card.quality,
        "hard_failures": list(card.hard_failures),
        "failed": list(card.failed),
        "won": rec.merchant_won,
        "claimed_win": claimed_win(rec),
        "correct": rec.outcome is not None and rec.outcome.correct,
        "cost": rec.cost_usd_minor,
        "baseline_cost": None if rec.baseline is None else rec.baseline.cost_usd_minor,
        "uploaded": uploads > 0,
        "tool_calls": len(rec.tool_calls()),
    }
    return row, rec


def _play_row(job: tuple[str, int]) -> tuple[dict[str, Any], list[float | None], list[int]]:
    row, rec = _play(job)
    from scruple.verifiers.calibration import stated_probability

    return row, [stated_probability(rec)], [int(rec.merchant_won)]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def mean_ci(xs: Sequence[float], z: float = 1.96) -> tuple[float, float, float]:
    n = len(xs)
    if n == 0:
        return (0.0, 0.0, 0.0)
    m = sum(xs) / n
    if n == 1:
        return (m, m, m)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    h = z * sd / math.sqrt(n)
    return (m, m - h, m + h)


def _pct(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k / n:.1%} [{lo:.1%}, {hi:.1%}]" if n else "-"


def _usd(m: tuple[float, float, float]) -> str:
    return f"${m[0] / 100:,.2f} [${m[1] / 100:,.2f}, ${m[2] / 100:,.2f}]"


def summarise(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    gate = sum(r["gate"] for r in rows)
    return {
        "n": n,
        "gate": gate,
        "won": sum(r["won"] for r in rows),
        "correct": sum(r["correct"] for r in rows),
        "reward": mean_ci([r["reward"] for r in rows]),
        "cost": mean_ci([float(r["cost"]) for r in rows]),
        "delta": mean_ci(
            [float(r["cost"] - r["baseline_cost"]) for r in rows if r["baseline_cost"] is not None]
        ),
        "hard": Counter(h for r in rows for h in r["hard_failures"]),
        "soft": Counter(f for r in rows for f in r["failed"] if f not in r["hard_failures"]),
    }


def _used_exploit(r: dict[str, Any]) -> bool:
    """The episode used an exploit: an upload (fabrication or forgery) or a claimed
    win the ledger denies."""
    return bool(r["uploaded"] or (r["claimed_win"] and not r["won"]))


def _group(rows: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[str(r[key])].append(r)
    return dict(sorted(out.items()))


def render(by_policy: dict[str, list[dict[str, Any]]], batch: dict[str, Any], cmd: str) -> str:
    lines = [
        "# Large dev-split evaluation (synthetic)",
        "",
        f"Reproduce: `{cmd}`",
        "",
        "## Per policy (95% Wilson intervals for rates, normal intervals for means)",
        "",
        "| policy | n | gate pass | merchant won | outcome correct | mean reward "
        "| mean cost | cost - baseline |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, rows in by_policy.items():
        s = summarise(rows)
        rw = s["reward"]
        lines.append(
            f"| {name} | {s['n']} | {_pct(s['gate'], s['n'])} | {_pct(s['won'], s['n'])} "
            f"| {_pct(s['correct'], s['n'])} | {rw[0]:.3f} [{rw[1]:.3f}, {rw[2]:.3f}] "
            f"| {_usd(s['cost'])} | {_usd(s['delta'])} |"
        )
    lines += ["", "## Hard-check failures per policy (episodes; share of n)", ""]
    for name, rows in by_policy.items():
        s = summarise(rows)
        hard = ", ".join(f"{k} {v} ({v / s['n']:.1%})" for k, v in s["hard"].most_common())
        soft = ", ".join(f"{k} {v}" for k, v in s["soft"].most_common(6))
        lines.append(f"- **{name}**: hard: {hard or 'none'}; most frequent soft: {soft or 'none'}")
    lines += ["", "## Batch checks on the whole batch", ""]
    for name, res in batch.items():
        lines.append(
            f"- **{name}**: "
            + "; ".join(
                f"{k} {'pass' if v['passed'] else 'FAIL'} ({v['why']})" for k, v in res.items()
            )
        )
    exploiters = [p for p in by_policy if p in ("hacker", "hacker_quiet", "refund_forger")]
    if exploiters:
        lines += [
            "",
            "## Detection where an exploit was used, by family",
            "",
            "Share of episodes that used an exploit (an upload, or a claimed win the ledger "
            "denies) whose scorecard failed the gate. A family with a low rate is a blind spot.",
            "",
            "| family | " + " | ".join(exploiters) + " |",
            "|---|" + "---|" * len(exploiters),
        ]
        used = {
            p: _group([r for r in by_policy[p] if _used_exploit(r)], "family") for p in exploiters
        }
        for k in sorted({k for g in used.values() for k in g}):
            cells = []
            for p in exploiters:
                g = used[p].get(k, [])
                cells.append(_pct(sum(not r["gate"] for r in g), len(g)) if g else "-")
            lines.append(f"| {k} | " + " | ".join(cells) + " |")
        lines += [
            "",
            "Escaped episodes (exploit used, gate passed) by policy: "
            + "; ".join(
                f"{p} {sum(1 for r in by_policy[p] if _used_exploit(r) and r['gate'])}"
                for p in exploiters
            ),
        ]
    for key in ("family", "reason_code"):
        lines += [
            "",
            f"## Gate pass rate by {key.replace('_', ' ')}",
            "",
            "| " + key + " | " + " | ".join(by_policy) + " |",
            "|---|" + "---|" * len(by_policy),
        ]
        groups = {p: _group(rows, key) for p, rows in by_policy.items()}
        keys = sorted({k for g in groups.values() for k in g})
        for k in keys:
            cells = []
            for p in by_policy:
                g = groups[p].get(k, [])
                cells.append(_pct(sum(r["gate"] for r in g), len(g)) if g else "-")
            lines.append(f"| {k} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--policies", default="evidence,oracle,always_accept,hacker,hacker_quiet")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--md", type=Path, required=True)
    args = ap.parse_args()
    names = [p for p in args.policies.split(",") if p]
    jobs = [(p, i) for p in names for i in range(args.start, args.start + args.n)]
    t = time.perf_counter()
    by_policy: dict[str, list[dict[str, Any]]] = {p: [] for p in names}
    probs: dict[str, list[tuple[float | None, int]]] = {p: [] for p in names}
    with ProcessPoolExecutor(args.jobs) as pool:
        for row, ps, ys in pool.map(_play_row, jobs, chunksize=16):
            by_policy[row["policy"]].append(row)
            probs[row["policy"]] += list(zip(ps, ys, strict=True))
    print(f"{len(jobs)} episodes in {time.perf_counter() - t:.0f}s", flush=True)
    # batch checks need records; replay deterministically per policy (cheap relative to
    # keeping every record in memory across processes): calibration only, from the pairs
    from scruple.verifiers.calibration import calibration_verdict

    batch: dict[str, Any] = {}
    for p in names:
        ok, _, _, why = calibration_verdict([(q, y) for q, y in probs[p] if q is not None])
        batch[p] = {"CAL-01": {"passed": ok, "why": why}}
    _ = BATCH_CHECKS
    args.out.parent.mkdir(parents=True, exist_ok=True)
    import gzip

    opener = gzip.open if args.out.suffix == ".gz" else open
    with opener(args.out, "wt") as f:
        for rows in by_policy.values():
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
    cmd = (
        f"uv run python scripts/exp_large_eval.py --n {args.n} --start {args.start} "
        f"--jobs {args.jobs} --policies {args.policies} --out {args.out} --md {args.md}"
    )
    args.md.write_text(render(by_policy, batch, cmd))
    print(args.md.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
