"""On-disk layout of an evaluation run directory.

    OUT/manifest.json        what was run (provider, model, strategy, path, transport)
    OUT/runs/seed-<n>.json   one `EvalRecord` per seed (call log, tokens, score, row)
    OUT/store/ep-*.json      the same call logs as an `EpisodeStore` (replayable with
                             `scruple episode show` and scripts/model_episodes_report.py)
    OUT/summary.json         rows and summaries, with the reference policies
    OUT/SUMMARY.md           the table

All cases are synthetic. `real_model` is true only when a real model produced the
actions (an API call or a CLI player); a fake transport is a harness check.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.agents.strategies.seeds import format_seeds

Path_ = Literal["api", "cli-brief", "mcp"]
Transport = Literal["http", "fake", "cli", "mcp"]


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    strategy: str
    path: Path_
    transport: Transport
    seeds: str  # a spec `parse_seeds` reads
    base_url: str | None = None
    concurrency: int = 1
    rpm: float | None = None
    price: dict[str, float] | None = None
    loop: dict[str, Any] = Field(default_factory=dict)
    synthetic: bool = True

    @property
    def system(self) -> str:
        return f"{self.provider}/{self.model}/{self.strategy}"

    @property
    def real_model(self) -> bool:
        return self.transport != "fake"

    def same_run(self, other: Manifest) -> bool:
        keys = ("provider", "model", "strategy", "path", "transport", "base_url", "loop")
        return all(getattr(self, k) == getattr(other, k) for k in keys)


class EvalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str
    provider: str
    model: str
    strategy: str
    path: Path_
    real_model: bool
    seed: int
    episode_id: str
    stop: str
    turns: int | None = None
    held_submits: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    requests: int | None = None
    retries: int | None = None
    cost_usd: float | None = None
    calls: tuple[dict[str, Any], ...] = ()  # {"tool", "args"} in the order the episode saw
    detail: str | None = None
    score: dict[str, Any]
    row: dict[str, Any]
    synthetic: bool = True


def write_json(path: Path, obj: Any) -> None:
    """Atomic write (so an interrupted run never leaves a half record)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def record_path(out: Path, seed: int) -> Path:
    return out / "runs" / f"seed-{seed}.json"


def load_manifest(out: Path) -> Manifest | None:
    p = out / "manifest.json"
    if not p.exists():
        return None
    return Manifest.model_validate_json(p.read_text())


def load_records(out: Path) -> list[EvalRecord]:
    runs = out / "runs"
    if not runs.exists():
        return []
    recs = [EvalRecord.model_validate_json(p.read_text()) for p in runs.glob("seed-*.json")]
    return sorted(recs, key=lambda r: r.seed)


def write_store_entry(out: Path, rec: EvalRecord, nonce: int = 0) -> None:
    """The call log as an `EpisodeStore` file, replayable by the existing tools."""
    entry = {
        "seed": rec.seed,
        "case_type": None,
        "nonce": nonce,
        "kind": "dispute",
        "calls": [{"tool": c["tool"], "args": c["args"]} for c in rec.calls],
    }
    write_json(out / "store" / f"{rec.episode_id}.json", entry)


def seeds_spec(seeds: list[int]) -> str:
    return format_seeds(seeds)


__all__ = [
    "EvalRecord",
    "Manifest",
    "load_manifest",
    "load_records",
    "record_path",
    "seeds_spec",
    "write_json",
    "write_store_entry",
]
