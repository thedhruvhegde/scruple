"""E5 (a): play dispute episodes with an open model through its API, with transcripts.

Usage (keys come from the environment; behind the E5 proxy they are placeholders):
    uv run python results/real/open_models/run_episodes.py PROVIDER MODEL STRATEGY SEEDS OUT

A thin wrapper over `scruple eval run` (scruple.eval.batch.run_eval): same runner,
same scoring, same run directory. Two additions:

- every request and response BODY is logged to OUT/transcripts/seed-N.jsonl
  (logging_transport.py; headers are never read);
- a seed that ends on a transient stop (`http_error`, `timeout`) is played again,
  up to 3 more times; one that still fails stays in the run as a lapsed episode.
  Every replaced lapsed record is kept under OUT/lapsed/ first;
- provider limits (harness_transport.py, E5 deviation 4): a 413 elides the oldest
  tool outputs until the request fits; a daily quota stops the driver (exit 3)
  with the seed left to replay from the start on the next run. Both, and every
  attempt, are logged to OUT/harness.jsonl.

`openrouter` as PROVIDER means the generic OpenAI-compatible provider at
openrouter.ai (key: SCRUPLE_OPENAI_COMPAT_API_KEY).
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from harness_transport import DailyQuota, LimitTransport
from logging_transport import LoggingTransport

from scruple.agents.providers import TRANSIENT_STOPS, RunSettings
from scruple.agents.strategies import parse_seeds
from scruple.eval.batch import EvalConfig, run_eval, write_summary
from scruple.eval.records import EvalRecord, record_path

RERUNS = 3
OPENROUTER = "https://openrouter.ai/api/v1"


def note(out: Path, **fields: Any) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "harness.jsonl").open("a") as f:
        f.write(json.dumps({"t": round(time.time()), **fields}, sort_keys=True) + "\n")


def archive_lapse(out: Path, seed: int) -> None:
    """Keep a lapsed record before it is played again (never dropped silently)."""
    p = record_path(out, seed)
    if not p.exists():
        return
    rec = EvalRecord.model_validate_json(p.read_text())
    if rec.stop not in TRANSIENT_STOPS:
        return
    dest = out / "lapsed"
    dest.mkdir(exist_ok=True)
    k = len(list(dest.glob(f"seed-{seed}.*.json"))) + 1
    shutil.copy(p, dest / f"seed-{seed}.{k}.json")
    note(out, seed=seed, event="archived_lapse", stop=rec.stop, detail=(rec.detail or "")[:200])


def prior_attempts(out: Path, seed: int) -> int:
    """Scored attempts already logged for this seed (quota-abandoned ones do not count)."""
    p = out / "harness.jsonl"
    if not p.exists():
        return 0
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    return sum(1 for r in rows if r.get("seed") == seed and r.get("event") == "attempt")


def main() -> int:
    provider, model, strategy, seeds_arg, out_arg = sys.argv[1:6]
    out = Path(out_arg)
    base_url = None
    if provider == "openrouter":
        provider, base_url = "openai-compatible", OPENROUTER
    for seed in parse_seeds(seeds_arg):
        log = out / "transcripts" / f"seed-{seed}.jsonl"

        limits: list[LimitTransport] = []

        def factory(log: Path = log, limits: list[LimitTransport] = limits) -> httpx.BaseTransport:
            t = LimitTransport(LoggingTransport(log))
            limits.append(t)
            return t

        cfg = EvalConfig(
            provider=provider,
            model=model,
            strategy=strategy,
            seeds=(seed,),
            base_url=base_url,
            concurrency=1,
            rpm=20,
            settings=RunSettings(timeout_s=180, max_retries=6, backoff_s=4.0),
        )
        rp = record_path(out, seed)
        if (
            rp.exists()
            and EvalRecord.model_validate_json(rp.read_text()).stop not in TRANSIENT_STOPS
        ):
            continue
        for attempt in range(prior_attempts(out, seed), 1 + RERUNS):
            archive_lapse(out, seed)
            try:
                run_eval(cfg, out, transport_factory=factory)
            except DailyQuota as exc:
                note(out, seed=seed, event="daily_quota_stop", detail=str(exc)[:200])
                with log.open("a") as f:
                    f.write(json.dumps({"harness_note": "daily quota: attempt abandoned"}) + "\n")
                print(f"seed {seed}: daily quota, stopping; rerun to resume", flush=True)
                write_summary(out)
                return 3
            rec = EvalRecord.model_validate_json(record_path(out, seed).read_text())
            elisions = limits[-1].elisions if limits else 0
            note(
                out,
                seed=seed,
                event="attempt",
                attempt=attempt + 1,
                stop=rec.stop,
                elisions=elisions,
            )
            print(
                f"seed {seed} attempt {attempt + 1}: {rec.stop}, {rec.turns} turns, "
                f"{rec.input_tokens}+{rec.output_tokens} tokens, rejected {rec.rejected_turns}, "
                f"gate {rec.score['scorecard']['gate']}, elisions {elisions}",
                flush=True,
            )
            if rec.stop not in TRANSIENT_STOPS:
                break
    write_summary(out)
    print((out / "SUMMARY.md").read_text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
