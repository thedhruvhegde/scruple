"""E6 driver: play the pre-registered cells for one model, in order, resumably.

    uv run python results/llm_sims/drive.py MODEL CELLS_JSON [--deadline UNIX_TS] [--rpm N]

Each cell is `{"env", "strategy", "seeds": "A-B", "difficulty"}` and goes to
`results/llm_sims/runs/<model-slug>/<env>/<strategy>/` through
`scruple.eval.env_batch.run_env_eval` with a paced Groq agent (the same code path as
`scruple eval run --env ENV --provider groq --paced`). The cell list is re-read
after every cell, so cells can be appended while the driver runs. A per-day quota
that cannot be waited out inside the provider-limit rule pauses the driver for 10
minutes; past `--deadline` the driver stops cleanly between episodes' records.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

from scruple.agents.providers import RunSettings
from scruple.agents.providers.limits import DailyQuota
from scruple.agents.strategies import parse_seeds
from scruple.eval.env_batch import run_env_eval, write_env_summary
from scruple.suite.agents import ProviderAgent

ROOT = Path(__file__).parent / "runs"


def slug(model: str) -> str:
    return model.replace("/", "__")


def cell_dir(model: str, cell: dict) -> Path:
    return ROOT / slug(model) / cell["env"] / cell["strategy"]


def log(model: str, **kw: object) -> None:
    p = ROOT / slug(model) / "driver.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"t": round(time.time()), **kw}, sort_keys=True, default=str) + "\n")
    print(json.dumps(kw, default=str), flush=True)


def _finished(rec: dict) -> bool:
    p = rec["play"]
    return p["stop"] not in ("http_error", "timeout") or (p.get("detail") or "").startswith(
        "HTTP 413"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("cells")
    ap.add_argument("--deadline", type=float, default=None)
    ap.add_argument("--rpm", type=float, default=6.0)
    args = ap.parse_args()
    agent = ProviderAgent(
        "groq",
        args.model,
        settings=RunSettings(timeout_s=180.0, max_retries=12, backoff_s=5.0),
        rpm=args.rpm,
        paced=True,
    )

    def progress(rec) -> None:  # type: ignore[no-untyped-def]
        p, o = rec.play, rec.outcome
        log(
            args.model,
            event="episode",
            env=rec.item.env,
            strategy=rec.item.strategy,
            seed=rec.item.seed,
            stop=p.stop,
            turns=p.turns,
            tokens=[p.input_tokens, p.output_tokens],
            gate=o.gate,
            reward=o.reward,
            hard=o.hard_failures,
            latency_s=p.latency_s,
            elisions=p.elisions,
        )

    while True:
        cells = json.loads(Path(args.cells).read_text())
        todo = None
        for c in cells:
            d = cell_dir(args.model, c)
            seeds = parse_seeds(c["seeds"])
            done = (
                {
                    json.loads(p.read_text())["item"]["seed"]
                    for p in (d / "items").glob("*.json")
                    if _finished(json.loads(p.read_text()))
                }
                if (d / "items").is_dir()
                else set()
            )
            if not set(seeds) <= done:
                todo = c
                break
        if todo is None:
            log(args.model, event="all_cells_done")
            return 0
        if args.deadline and time.time() > args.deadline:
            log(args.model, event="deadline_stop", cell=todo)
            return 0
        log(args.model, event="cell_start", cell=todo)
        try:
            run_env_eval(
                todo["env"],
                agent,
                todo["strategy"],
                parse_seeds(todo["seeds"]),
                cell_dir(args.model, todo),
                difficulty=todo.get("difficulty", 0.5),
                progress=progress,
            )
        except DailyQuota as exc:
            log(args.model, event="daily_quota_pause", detail=str(exc)[:300])
            if (cell_dir(args.model, todo) / "manifest.json").exists():
                with contextlib.suppress(Exception):
                    write_env_summary(cell_dir(args.model, todo))
            time.sleep(600)
        except Exception as exc:
            log(args.model, event="cell_error", error=repr(exc)[:500])
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
