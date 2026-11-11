"""What the console reads besides the gate: agent runs and backtest reports.

Agent runs. An agent "version" is a named dispute policy played over a fixed list of
seeds, so two versions see exactly the same cases and can be diffed case by case.
Built-in versions are the reference policies in `scruple.agents.dispute_baselines`;
episodes an external agent played through the CLI store (`scruple episode`) form one
more version, rebuilt by replaying each stored call log. Episodes are computed on
first use and cached; every one is synthetic.

Backtest reports. JSON files written by `scruple backtest run` (`report.json`), read
from a directory the operator names. Each report says whether it is synthetic.

Tenant data. `data` names a root of per-tenant folders (case store, eval runs,
anchors, backtests, pilot run) that the dashboard reads for the caller's tenant only
(`scruple.console.tenant_files`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from threading import Lock

from scruple.agents.dispute_baselines import BASELINES, DisputePolicy, run_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.store import EpisodeStore, UnknownEpisodeError
from scruple.backtest.engine import BacktestReport
from scruple.verifiers.checks import Scorecard
from scruple.verifiers.profiles import DEFAULT_PROFILE, VerifierProfile, score_profile
from scruple.verifiers.record import EpisodeRecord

NAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_NAME = re.compile(NAME_PATTERN)
DEFAULT_SEEDS = tuple(range(24))


@dataclass
class AgentRun:
    """One agent version: its closed episodes, in seed order."""

    name: str
    description: str
    _load: Callable[[], Sequence[EpisodeRecord]]
    _lock: Lock = field(default_factory=Lock, repr=False)

    @cached_property
    def records(self) -> tuple[EpisodeRecord, ...]:
        with self._lock:
            return tuple(self._load())

    _cards: dict[str, dict[str, Scorecard]] = field(default_factory=dict, repr=False)

    def scorecards_for(
        self, profile: VerifierProfile | str = DEFAULT_PROFILE
    ) -> dict[str, Scorecard]:
        """Scorecards under a verifier profile (a tenant policy's `fairness.profile`,
        carried as `GateConfig.verifier_profile`); computed once per profile."""
        key = VerifierProfile(profile).value
        with self._lock:
            found = self._cards.get(key)
        if found is None:
            found = {r.episode_id: score_profile(r, key) for r in self.records}
            with self._lock:
                found = self._cards.setdefault(key, found)
        return found

    @property
    def scorecards(self) -> dict[str, Scorecard]:
        """Scorecards under the default (`merchant`) profile."""
        return self.scorecards_for(DEFAULT_PROFILE)

    def record(self, episode_id: str) -> EpisodeRecord | None:
        return next((r for r in self.records if r.episode_id == episode_id), None)


def policy_run(
    name: str, policy: DisputePolicy, seeds: Sequence[int], description: str = ""
) -> AgentRun:
    """A version that plays `policy` on each seed (any family the seed draws)."""
    if not _NAME.match(name):
        raise ValueError(f"agent name must match {NAME_PATTERN}")

    def load() -> list[EpisodeRecord]:
        return [run_policy(policy, s).record() for s in seeds]

    return AgentRun(name, description or f"policy {name} on {len(seeds)} seeds", load)


def store_run(root: Path, name: str = "episode-store") -> AgentRun:
    """Closed `dispute` episodes from an `EpisodeStore` directory."""

    def load() -> list[EpisodeRecord]:
        store = EpisodeStore(root)
        out: list[EpisodeRecord] = []
        for path in sorted(root.glob("ep-*.json")):
            try:
                ep = store.load(path.stem)
            except (UnknownEpisodeError, ValueError, KeyError, json.JSONDecodeError):
                continue
            if isinstance(ep, DisputeEpisode) and ep.closed:
                out.append(ep.record())
        return out

    return AgentRun(name, f"closed dispute episodes stored in {root.name}/", load)


def baseline_runs(seeds: Sequence[int] = DEFAULT_SEEDS) -> dict[str, AgentRun]:
    return {
        name.replace("_", "-"): policy_run(
            name.replace("_", "-"), policy, seeds, f"reference policy {name}, synthetic"
        )
        for name, policy in BASELINES.items()
    }


@dataclass(frozen=True)
class ReportEntry:
    name: str
    path: Path

    def load(self) -> BacktestReport:
        return BacktestReport.model_validate_json(self.path.read_text(encoding="utf-8"))


def report_entries(root: Path | None) -> dict[str, ReportEntry]:
    """Backtest report JSON files under `root` (one level of folders), by name."""
    if root is None or not root.is_dir():
        return {}
    out: dict[str, ReportEntry] = {}
    for p in sorted([*root.glob("*.json"), *root.glob("*/*.json")]):
        rel = p.relative_to(root).with_suffix("")
        parts = rel.parts[:-1] if rel.name == "report" and len(rel.parts) > 1 else rel.parts
        name = "-".join(parts).lower()
        name = re.sub(r"[^a-z0-9_.-]+", "-", name).strip("-")[:64]
        if not name or not _NAME.match(name) or name in out:
            continue
        try:
            head = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(head, dict) and {"candidate", "by_reason", "verdict"} <= set(head):
            out[name] = ReportEntry(name, p)
    return out


@dataclass
class ConsoleSources:
    """Everything the console reads that is not the gate itself."""

    agents: Mapping[str, AgentRun] = field(default_factory=baseline_runs)
    reports: Mapping[str, ReportEntry] = field(default_factory=dict)
    replay_seed: int = 4_242  # public replay set for rule diffs; not the held-out seed
    replay_cases: int = 1_000
    data: Path | None = None  # per-tenant folders (scruple.console.tenant_files)

    @classmethod
    def build(
        cls,
        *,
        seeds: Sequence[int] = DEFAULT_SEEDS,
        episodes: Path | None = None,
        reports: Path | None = None,
        replay_seed: int = 4_242,
        replay_cases: int = 1_000,
        data: Path | None = None,
    ) -> ConsoleSources:
        agents = baseline_runs(seeds)
        if episodes is not None:
            agents["episode-store"] = store_run(episodes)
        return cls(agents, report_entries(reports), replay_seed, replay_cases, data)


__all__ = [
    "DEFAULT_SEEDS",
    "AgentRun",
    "ConsoleSources",
    "ReportEntry",
    "baseline_runs",
    "policy_run",
    "report_entries",
    "store_run",
]
