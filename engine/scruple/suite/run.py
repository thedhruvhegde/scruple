"""`scruple suite run`: play a client's agent through a plan, resumably.

    DIR/run.json            plan hash, agent, strategy, the scenario subset
    DIR/items/<id>.json     one `ItemRecord` per scenario (calls, tokens, latency,
                            outcome graded by replay)
    DIR/transcripts/        request/response bodies for API agents
    DIR/lapsed/             transient attempts kept before a replay

Subsets: `--envs` keeps some environments; `--per-env K` takes the first K scenarios
of each environment in the plan's (seeded, outcome-blind) run order, a stratified
random sample; `--limit N` takes the first N overall. Rerunning the same command
skips finished scenarios. Local agents (python, http, scripted) run in parallel;
API agents run one at a time, paced by `--rpm` and the provider-limit rules.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from scruple.suite.agents import Agent
from scruple.suite.plan import Scenario, load_plan, scenarios_of
from scruple.suite.runner import Item, ItemRecord, run_items
from scruple.version import package_version


def select(
    scen: Sequence[Scenario],
    *,
    envs: Sequence[str] | None = None,
    per_env: int | None = None,
    limit: int | None = None,
) -> list[Scenario]:
    out = sorted(scen, key=lambda s: s.index)
    if envs:
        out = [s for s in out if s.env in envs]
    if per_env is not None:
        seen: dict[str, int] = {}
        keep = []
        for s in out:
            if seen.get(s.env, 0) < per_env:
                keep.append(s)
                seen[s.env] = seen.get(s.env, 0) + 1
        out = keep
    if limit is not None:
        out = out[:limit]
    return out


def to_item(s: Scenario, strategy: str) -> Item:
    return Item(
        scenario_id=s.scenario_id,
        env=s.env,
        seed=s.seed,
        case_type=s.case_type,
        strategy=strategy,
        family=s.family,
        difficulty=s.difficulty,
        adversarial=bool(s.adversarial),
    )


def run_suite(
    plan_path: Path,
    agent: Agent,
    out: Path,
    *,
    strategy: str = "zero-shot",
    envs: Sequence[str] | None = None,
    per_env: int | None = None,
    limit: int | None = None,
    concurrency: int = 4,
    progress: Callable[[ItemRecord], None] | None = None,
) -> dict[str, Any]:
    plan = load_plan(plan_path)
    chosen = select(scenarios_of(plan), envs=envs, per_env=per_env, limit=limit)
    if not chosen:
        raise ValueError("the selection is empty")
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "plan": str(plan_path),
        "plan_hash": plan["plan_hash"],
        "bank_hash": plan["bank_hash"],
        "agent": agent.label,
        "strategy": strategy,
        "selection": {"envs": list(envs) if envs else None, "per_env": per_env, "limit": limit},
        "scenario_ids": [s.scenario_id for s in chosen],
        "scruple_version": package_version(),
        "synthetic": True,
    }
    rp = out / "run.json"
    if rp.exists():
        old = json.loads(rp.read_text())
        for k in ("plan_hash", "agent", "strategy"):
            if old.get(k) != meta[k]:
                raise ValueError(f"{out} holds a different run ({k} differs); use a new --out")
        ids = list(dict.fromkeys([*old["scenario_ids"], *meta["scenario_ids"]]))
        meta["scenario_ids"] = ids
        meta["started_at"] = old.get("started_at")
    meta.setdefault("started_at", round(time.time()))
    rp.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    t0 = time.perf_counter()
    written = run_items(
        [to_item(s, strategy) for s in chosen],
        agent,
        out,
        concurrency=concurrency,
        progress=progress,
    )
    dt = time.perf_counter() - t0
    meta["last_session"] = {
        "played": len(written),
        "wall_s": round(dt, 2),
        "scenarios_per_s": round(len(written) / dt, 2) if dt > 0 and written else None,
    }
    rp.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return meta


__all__ = ["run_suite", "select", "to_item"]
