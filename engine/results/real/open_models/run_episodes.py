"""E5 (a): play dispute episodes with an open model through its API, with transcripts.

Usage (keys come from the environment; behind the E5 proxy they are placeholders):
    uv run python results/real/open_models/run_episodes.py PROVIDER MODEL STRATEGY SEEDS OUT

A thin wrapper over `scruple eval run` (scruple.eval.batch.run_eval): same runner,
same scoring, same run directory. Two additions:

- every request and response BODY is logged to OUT/transcripts/seed-N.jsonl
  (logging_transport.py; headers are never read);
- a seed that ends on a transient stop (`http_error`, `timeout`) is played again,
  up to 3 more times; one that still fails stays in the run as a lapsed episode.

`openrouter` as PROVIDER means the generic OpenAI-compatible provider at
openrouter.ai (key: SCRUPLE_OPENAI_COMPAT_API_KEY).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from logging_transport import LoggingTransport

from scruple.agents.providers import TRANSIENT_STOPS, RunSettings
from scruple.agents.strategies import parse_seeds
from scruple.eval.batch import EvalConfig, run_eval, write_summary
from scruple.eval.records import EvalRecord, record_path

RERUNS = 3
OPENROUTER = "https://openrouter.ai/api/v1"


def main() -> int:
    provider, model, strategy, seeds_arg, out_arg = sys.argv[1:6]
    out = Path(out_arg)
    base_url = None
    if provider == "openrouter":
        provider, base_url = "openai-compatible", OPENROUTER
    for seed in parse_seeds(seeds_arg):
        log = out / "transcripts" / f"seed-{seed}.jsonl"

        def factory(log: Path = log) -> httpx.BaseTransport:
            return LoggingTransport(log)

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
        for attempt in range(1 + RERUNS):
            run_eval(cfg, out, transport_factory=factory)
            rec = EvalRecord.model_validate_json(record_path(out, seed).read_text())
            print(
                f"seed {seed} attempt {attempt + 1}: {rec.stop}, {rec.turns} turns, "
                f"{rec.input_tokens}+{rec.output_tokens} tokens, rejected {rec.rejected_turns}, "
                f"gate {rec.score['scorecard']['gate']}",
                flush=True,
            )
            if rec.stop not in TRANSIENT_STOPS:
                break
    write_summary(out)
    print((out / "SUMMARY.md").read_text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
