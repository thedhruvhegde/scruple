"""Aggregate run directories into a leaderboard across (provider, model, strategy).

Every system is compared with the `evidence` reference policy on exactly the seeds
it played (paired bootstrap over seeds, the same method as
scripts/model_episodes_report.py), for merchant cost and for the scorecard reward.
References are replayed in-process on the union of seeds.

Honesty rules the output follows:

- every case is synthetic, and the page says so; fake-model runs are labelled as
  harness checks, not model results;
- small samples get a warning (n < 30) and the order is never called a ranking
  when the intervals overlap;
- systems run on different seeds get a warning, and only the paired columns
  compare them;
- lapsed episodes and transient failures are counted, never dropped.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.agents.providers import TRANSIENT_STOPS
from scruple.eval.records import EvalRecord, load_manifest, load_records, seeds_spec
from scruple.eval.scoring import (
    EVIDENCE_REF,
    N_BOOT,
    REFERENCES,
    SMALL_N,
    fmt_ci,
    paired_bootstrap,
    reference_rows,
)

CI = tuple[float, float, float]


class Entry(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str
    provider: str
    model: str
    strategy: str
    path: str
    real_model: bool
    reference: bool
    n: int
    seeds: str
    mean_cost_usd: float
    cost_vs_evidence: CI | None
    mean_reward: float
    reward_vs_evidence: CI | None
    gate: int
    won_partial: int
    fair: int
    lapsed: int
    stops: dict[str, int]
    input_tokens: int | None
    output_tokens: int | None
    api_cost_usd: float | None
    top_hard_failures: tuple[tuple[str, int], ...]
    warnings: tuple[str, ...]


class Leaderboard(BaseModel):
    model_config = ConfigDict(frozen=True)

    entries: tuple[Entry, ...]
    references: tuple[Entry, ...]
    n_boot: int
    warnings: tuple[str, ...]
    seeds: str
    synthetic_cases: bool = True

    def markdown(self) -> str:
        real = any(e.real_model for e in self.entries)
        fake = any(not e.real_model for e in self.entries)
        what = (
            "real model behaviour"
            if real and not fake
            else "a mix of real and FAKE models"
            if real
            else "FAKE models only (a harness check, not model results)"
        )
        lines = [
            "# leaderboard: agents on simulated disputes",
            "",
            f"Synthetic cases (seeds {self.seeds}); {what}. Cost = merchant net loss in USD, "
            "lower is better. Reward = scruple scorecard (0 when a hard check fails). "
            f"Paired bootstrap against the evidence reference on each system's own seeds, "
            f"{self.n_boot} resamples, 95% intervals. Sorted by mean reward; where the "
            "intervals overlap the order is not established.",
            "",
            "| system | path | n | mean cost $ | cost vs evidence | mean reward "
            "| reward vs evidence | gate pass | won/partial | fair (OUT-01) | lapsed "
            "| tokens in/out | notes |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        notes: list[str] = []
        for e in (*self.entries, *self.references):
            refs = []
            for w in e.warnings:
                if w not in notes:
                    notes.append(w)
                refs.append(str(notes.index(w) + 1))
            toks = "" if e.input_tokens is None else f"{e.input_tokens}/{e.output_tokens}"
            lines.append(
                f"| {e.system} | {e.path} | {e.n} | {e.mean_cost_usd:.2f} "
                f"| {fmt_ci(e.cost_vs_evidence)} | {e.mean_reward:.3f} "
                f"| {fmt_ci(e.reward_vs_evidence, 3)} | {e.gate}/{e.n} | {e.won_partial}/{e.n} "
                f"| {e.fair}/{e.n} | {e.lapsed} | {toks} | {', '.join(refs)} |"
            )
        lines.append("")
        if notes:
            lines.append("Notes:")
            lines += [f"{i}. {w}" for i, w in enumerate(notes, 1)]
            lines.append("")
        if self.warnings:
            lines.append("Warnings:")
            lines += [f"- {w}" for w in self.warnings]
            lines.append("")
        return "\n".join(lines)


def _ci_has_zero(ci: CI | None) -> bool:
    return ci is not None and ci[1] <= 0.0 <= ci[2]


def _entry(
    system: str,
    rows: list[dict[str, Any]],
    ev_rows: dict[int, dict[str, Any]],
    *,
    recs: list[EvalRecord] | None,
    meta: dict[str, Any],
    n_boot: int,
) -> Entry:
    n = len(rows)
    seeds = [r["seed"] for r in rows]
    reference = system in REFERENCES
    cost = {r["seed"]: r["cost_usd"] for r in rows}
    reward = {r["seed"]: r["reward"] for r in rows}
    ev_cost = {s: ev_rows[s]["cost_usd"] for s in seeds}
    ev_reward = {s: ev_rows[s]["reward"] for s in seeds}
    paired = system != EVIDENCE_REF
    c_ci = paired_bootstrap(cost, ev_cost, n_boot) if paired else None
    r_ci = paired_bootstrap(reward, ev_reward, n_boot) if paired else None
    stops: Counter[str] = Counter(r.stop for r in recs) if recs else Counter()
    lapsed = sum("lapsed" in r.score["flags"] for r in recs) if recs else 0
    warnings: list[str] = []
    if not reference:
        if not meta["real_model"]:
            warnings.append("fake model: a harness check, not a model result")
        if n < SMALL_N:
            warnings.append(
                f"small n ({n} < {SMALL_N}): intervals are wide; treat the order as unestablished"
            )
        if _ci_has_zero(c_ci):
            warnings.append(
                "cost not distinguishable from the evidence reference (interval includes 0)"
            )
        if lapsed:
            warnings.append(f"{lapsed} of {n} episodes lapsed; they are scored, not dropped")
        transient = sum(v for k, v in stops.items() if k in TRANSIENT_STOPS)
        if transient:
            warnings.append(
                f"{transient} episodes ended on HTTP errors or timeouts; resume the run"
            )
    tin = [r.input_tokens for r in recs or [] if r.input_tokens is not None]
    tout = [r.output_tokens for r in recs or [] if r.output_tokens is not None]
    costs = [r.cost_usd for r in recs or [] if r.cost_usd is not None]
    return Entry(
        system=system,
        provider=meta["provider"],
        model=meta["model"],
        strategy=meta["strategy"],
        path=meta["path"],
        real_model=meta["real_model"],
        reference=reference,
        n=n,
        seeds=seeds_spec(seeds),
        mean_cost_usd=sum(cost.values()) / n,
        cost_vs_evidence=c_ci,
        mean_reward=sum(reward.values()) / n,
        reward_vs_evidence=r_ci,
        gate=sum(bool(r["gate"]) for r in rows),
        won_partial=sum(r["resolution"] in ("won", "partial") for r in rows),
        fair=sum(bool(r["fair_outcome"]) for r in rows),
        lapsed=lapsed,
        stops=dict(sorted(stops.items())),
        input_tokens=sum(tin) if tin else None,
        output_tokens=sum(tout) if tout else None,
        api_cost_usd=round(sum(costs), 6) if costs else None,
        top_hard_failures=tuple(
            Counter(f for r in rows for f in r["hard_failures"]).most_common(4)
        ),
        warnings=tuple(warnings),
    )


def build_leaderboard(dirs: Sequence[Path], *, n_boot: int = N_BOOT) -> Leaderboard:
    """Entries for every system in `dirs`; one system may span several directories
    (disjoint seeds), but a seed played twice by the same system is an error."""
    if n_boot < 100:
        raise ValueError("n_boot must be at least 100")
    by: dict[str, list[EvalRecord]] = {}
    metas: dict[str, dict[str, Any]] = {}
    for d in dirs:
        m = load_manifest(d)
        recs = load_records(d)
        if m is None or not recs:
            raise ValueError(f"{d} has no evaluation records")
        have = {r.seed for r in by.get(m.system, [])}
        dup = sorted(have & {r.seed for r in recs})
        if dup:
            raise ValueError(f"{m.system} played seed(s) {dup[:5]} twice (in {d})")
        by.setdefault(m.system, []).extend(recs)
        metas[m.system] = {
            "provider": m.provider,
            "model": m.model,
            "strategy": m.strategy,
            "path": m.path,
            "real_model": m.real_model,
        }
    if not by:
        raise ValueError("no run directories given")
    all_seeds = sorted({r.seed for recs in by.values() for r in recs})
    refs = reference_rows(all_seeds)
    ev_rows = {r["seed"]: r for r in refs if r["system"] == EVIDENCE_REF}
    entries = [
        _entry(
            s,
            [r.row for r in sorted(recs, key=lambda r: r.seed)],
            ev_rows,
            recs=recs,
            meta=metas[s],
            n_boot=n_boot,
        )
        for s, recs in by.items()
    ]
    entries.sort(key=lambda e: (-e.mean_reward, e.mean_cost_usd, e.system))
    ref_meta = {"provider": "reference", "path": "in-process", "real_model": False}
    references = [
        _entry(
            name,
            [r for r in refs if r["system"] == name],
            ev_rows,
            recs=None,
            meta={**ref_meta, "model": name.removeprefix("ref:"), "strategy": "policy"},
            n_boot=n_boot,
        )
        for name in REFERENCES
    ]
    warnings: list[str] = []
    seed_sets = {e.seeds for e in entries}
    if len(seed_sets) > 1:
        warnings.append(
            "systems were run on different seeds; compare them only through the paired "
            "columns (each against the evidence reference on its own seeds)"
        )
    if any(e.n < SMALL_N for e in entries):
        warnings.append(
            f"some systems have fewer than {SMALL_N} cases: this is a demonstration of the "
            "method, not a ranking"
        )
    paths = {e.path for e in entries}
    if len(paths) > 1:
        warnings.append(
            "API and CLI-brief runs are mixed; the harnesses differ (tool sandboxing, "
            "turn limits), so differences may come from the harness"
        )
    return Leaderboard(
        entries=tuple(entries),
        references=tuple(references),
        n_boot=n_boot,
        warnings=tuple(warnings),
        seeds=seeds_spec(all_seeds),
    )


__all__ = ["Entry", "Leaderboard", "build_leaderboard"]
