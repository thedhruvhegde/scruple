"""E5 (b): answer the CFPB real-data prompts with an open model through its API.

Usage (keys come from the environment; behind the E5 proxy they are placeholders):
    uv run python results/real/open_models/run_cfpb.py PROVIDER MODEL PROMPTS OUT [--rpm N]

One plain chat completion per prompt, no tools: the task's own system and prompt
text, temperature 0, `max_tokens` 2048 (reasoning models spend part of it
thinking). HTTP retries come from scruple.agents.providers.http (429 and 5xx,
Retry-After honoured). A prompt whose request still fails is retried up to 3
times; a prompt that never succeeds is written with an empty answer and an
`error`, so the grader counts it as malformed, never dropped. Resumable: ids
already answered without error are skipped. Raw bodies go to OUT.log.jsonl.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from logging_transport import LoggingTransport

from scruple.agents.providers import make_provider
from scruple.agents.providers.base import GenSettings, ProviderError
from scruple.agents.providers.http import ProviderClient, RateLimiter

ATTEMPTS = 1 + 3
OPENROUTER = "https://openrouter.ai/api/v1"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("provider")
    ap.add_argument("model")
    ap.add_argument("prompts")
    ap.add_argument("out")
    ap.add_argument("--rpm", type=float, default=20)
    ap.add_argument("--max-tokens", type=int, default=2048)
    a = ap.parse_args()
    if a.provider == "openrouter":
        provider = make_provider("openai-compatible", base_url=OPENROUTER)
    else:
        provider = make_provider(a.provider)
    out = Path(a.out)
    done: dict[str, dict[str, Any]] = {}
    if out.exists():
        for line in out.read_text().splitlines():
            row = json.loads(line)
            if not row.get("error"):
                done[row["id"]] = row
    client = ProviderClient(
        base_url=provider.base_url,
        headers=provider.headers(),
        transport=LoggingTransport(out.with_suffix(".log.jsonl")),
        max_retries=6,
        backoff_s=4.0,
        limiter=RateLimiter(a.rpm),
    )
    rows: list[dict[str, Any]] = []
    prompts = [json.loads(x) for x in Path(a.prompts).read_text().splitlines() if x.strip()]
    for p in prompts:
        if p["id"] in done:
            rows.append(done[p["id"]])
            continue
        gen = GenSettings(max_tokens=a.max_tokens, temperature=0.0)
        body = provider.body(a.model, p["system"], [provider.user_message(p["prompt"])], None, gen)
        row: dict[str, Any] = {"id": p["id"], "answer": "", "model": a.model}
        for attempt in range(ATTEMPTS):
            try:
                turn = provider.parse(client.post(provider.path(a.model), body), 1)
            except (httpx.HTTPError, ProviderError, ValueError, LookupError, TypeError) as exc:
                row["error"] = f"attempt {attempt + 1}: {str(exc)[:300]}"
                continue
            row = {
                "id": p["id"],
                "answer": turn.text,
                "model": a.model,
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "stop": turn.stop,
                "attempts": attempt + 1,
            }
            break
        rows.append(row)
        out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        print(p["id"], row.get("error") or repr(row["answer"][:60]), flush=True)
    client.close()
    errors = sum(1 for r in rows if r.get("error"))
    print(f"{len(rows)} answers, {errors} failed after {ATTEMPTS} attempts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
