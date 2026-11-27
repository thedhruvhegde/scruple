"""Play one scenario with one agent and grade it by replay: the unit both
`scruple eval run --env ...` and `scruple suite run` are built from.

`play_item` returns an `ItemRecord`: the agent's `PlayResult` (calls, tokens,
latency, stop), the `Outcome` of replaying those calls through a fresh episode, and
whether the replay reached the same trace head as the live episode (`replay_match`,
a determinism check: a mismatch means the grade is not of the episode the agent saw,
and the report says so). `run_items` plays many, resumably: an item with a finished
record is skipped, and one that ended on a transient stop (`http_error`, `timeout`)
is played again up to `reruns` times, with the earlier attempt kept under `lapsed/`.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.agents.providers import TRANSIENT_STOPS
from scruple.agents.providers.limits import DailyQuota
from scruple.suite.agents import Agent, PlayResult
from scruple.suite.envs import Outcome, get_adapter, replay


class Item(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    env: str
    seed: int
    case_type: str | None
    strategy: str = "zero-shot"
    family: str | None = None
    difficulty: float | None = None
    adversarial: bool = False


class ItemRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: Item
    play: PlayResult
    outcome: Outcome
    replay_match: bool
    attempt: int = 1
    synthetic: bool = True


def grade_calls(item: Item, calls: Sequence[dict[str, Any]]) -> tuple[Outcome, str]:
    a = get_adapter(item.env)
    ep = replay(a, item.seed, item.case_type, calls)
    return a.outcome(ep), str(ep.trace.head)


def play_item(
    item: Item, agent: Agent, *, transcript: Path | None = None, attempt: int = 1
) -> ItemRecord:
    a = get_adapter(item.env)
    res = agent.play(a, item.seed, item.case_type, item.strategy, transcript=transcript)
    if res.live_outcome is not None:  # a scripted policy: graded on its own episode
        outcome, match = res.live_outcome, True
    else:
        outcome, head = grade_calls(item, res.calls)
        match = (head == res.trace_head) if res.trace_head else True
    return ItemRecord(
        item=item,
        play=res,
        outcome=outcome,
        replay_match=match,
        attempt=attempt,
    )


def record_file(out: Path, item: Item) -> Path:
    return out / "items" / f"{item.scenario_id}.json"


def load_item_records(out: Path) -> list[ItemRecord]:
    d = out / "items"
    if not d.is_dir():
        return []
    return [ItemRecord.model_validate_json(p.read_text()) for p in sorted(d.glob("*.json"))]


def _deterministic(rec: ItemRecord) -> bool:
    """An HTTP 413 (the request alone exceeds the provider's per-minute input limit)
    fails the same way on every replay, so it is not played again."""
    return (rec.play.detail or "").startswith("HTTP 413")


def _done(out: Path, item: Item) -> tuple[bool, int]:
    p = record_file(out, item)
    if not p.exists():
        return False, 0
    rec = ItemRecord.model_validate_json(p.read_text())
    return rec.play.stop not in TRANSIENT_STOPS or _deterministic(rec), rec.attempt


def run_items(
    items: Sequence[Item],
    agent: Agent,
    out: Path,
    *,
    concurrency: int = 1,
    reruns: int = 3,
    transcripts: bool = True,
    progress: Callable[[ItemRecord], None] | None = None,
) -> list[ItemRecord]:
    """Play every pending item; returns the records written in this call. A
    `DailyQuota` from a paced provider stops the run cleanly (rerun to resume)."""
    (out / "items").mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    written: list[ItemRecord] = []

    def one(item: Item) -> None:
        done, attempts = _done(out, item)
        if done:
            return
        p = record_file(out, item)
        for attempt in range(attempts + 1, attempts + 2 + reruns):
            if p.exists():  # keep the transient attempt before playing again
                dest = out / "lapsed"
                dest.mkdir(exist_ok=True)
                shutil.copy(p, dest / f"{item.scenario_id}.{attempt - 1}.json")
            tr = (
                out / "transcripts" / f"{item.scenario_id}.{attempt}.jsonl" if transcripts else None
            )
            rec = play_item(item, agent, transcript=tr, attempt=attempt)
            p.write_text(rec.model_dump_json(indent=1) + "\n")
            if rec.play.stop not in TRANSIENT_STOPS or _deterministic(rec):
                break
        with lock:
            written.append(rec)
        if progress is not None:
            progress(rec)

    workers = concurrency if agent.parallel_safe else 1
    if workers <= 1:
        for it in items:
            try:
                one(it)
            except DailyQuota:
                (out / "STOPPED_DAILY_QUOTA").write_text(json.dumps({"at": it.scenario_id}) + "\n")
                raise
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, items))
    return written


__all__ = [
    "Item",
    "ItemRecord",
    "grade_calls",
    "load_item_records",
    "play_item",
    "record_file",
    "run_items",
]
