"""Batch evaluation: one (provider, model, strategy) over many seeds.

`run_eval` plays each seed as a fresh `DisputeEpisode` with a `ProviderRunner`,
writes one record per seed as soon as it finishes, and then the summary. It is

- resumable: seeds with a finished record are skipped; records that ended on a
  transient failure (`http_error`, `timeout`) are played again;
- parallel: up to `concurrency` episodes at once, one shared rate limiter;
- leak-checked: before anything is sent, the harness-authored text (the system
  prompt and each first message) is checked for ground-truth markers, and a hit
  aborts the run. The rest of the conversation is tool output, which the episode
  builds from whitelisted fields only (tests/test_eval_batch.py checks every
  request body).

`import_store` brings episodes a CLI player wrote (the CLI-brief path) and
`import_calls` a call log an MCP client kept (the MCP path) into the same record
format, so every path feeds the same summary and leaderboard.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.providers import (
    TRANSIENT_STOPS,
    EpisodeRun,
    FakeModel,
    Price,
    ProviderRunner,
    RateLimiter,
    RunSettings,
    make_provider,
)
from scruple.agents.providers.runner import observation_message
from scruple.agents.store import EpisodeStore
from scruple.agents.strategies import get_strategy
from scruple.eval.records import (
    EvalRecord,
    Manifest,
    load_manifest,
    load_records,
    record_path,
    seeds_spec,
    write_json,
    write_store_entry,
)
from scruple.eval.scoring import reference_rows, row, summarise, summary_table
from scruple.sim.families import CaseFamily

GROUND_TRUTH_MARKERS: tuple[str, ...] = (
    "claim_valid",
    "ground_truth",
    "liability_minor",
    "forged",
    "missing_evidence",
    *(f.value for f in CaseFamily),
)


class GroundTruthLeakError(RuntimeError):
    pass


def check_no_ground_truth(text: str, where: str) -> None:
    hits = [m for m in GROUND_TRUTH_MARKERS if m in text]
    if hits:
        raise GroundTruthLeakError(f"ground-truth marker(s) {hits} in {where}")


@dataclass(frozen=True)
class EvalConfig:
    provider: str
    model: str
    strategy: str
    seeds: tuple[int, ...]
    transport: str = "http"  # "http" (the real API) or "fake" (FakeModel, no network)
    base_url: str | None = None
    concurrency: int = 4
    rpm: float | None = None
    price: Price | None = None
    settings: RunSettings = field(default_factory=RunSettings)

    def manifest(self, loop: dict[str, Any]) -> Manifest:
        return Manifest(
            provider=self.provider,
            model=self.model,
            strategy=self.strategy,
            path="api",
            transport="fake" if self.transport == "fake" else "http",
            seeds=seeds_spec(list(self.seeds)),
            base_url=self.base_url,
            concurrency=self.concurrency,
            rpm=self.rpm,
            price=None if self.price is None else self.price.model_dump(),
            loop=loop,
        )


def record_from_run(m: Manifest, run: EpisodeRun) -> EvalRecord:
    return EvalRecord(
        system=m.system,
        provider=m.provider,
        model=m.model,
        strategy=m.strategy,
        path=m.path,
        real_model=m.real_model,
        seed=run.seed,
        episode_id=run.episode_id,
        stop=run.stop,
        turns=run.turns,
        held_submits=run.held_submits,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        requests=run.requests,
        retries=run.retries,
        cost_usd=run.cost_usd,
        calls=tuple({"tool": t, "args": a} for t, a in run.replay_calls()),
        detail=run.detail,
        score=run.score,
        row=row(m.system, run.score),
    )


def _pending(out: Path, seeds: Sequence[int]) -> list[int]:
    todo = []
    for s in seeds:
        p = record_path(out, s)
        if p.exists():
            rec = EvalRecord.model_validate_json(p.read_text())
            if rec.stop not in TRANSIENT_STOPS:
                continue
        todo.append(s)
    return todo


def _start(out: Path, manifest: Manifest) -> None:
    old = load_manifest(out)
    if old is not None and not old.same_run(manifest):
        raise ValueError(f"{out} holds a different run ({old.system}); use a new --out directory")
    if old is not None:  # widen the seed list on resume
        from scruple.agents.strategies.seeds import parse_seeds

        seeds = sorted(set(parse_seeds(old.seeds)) | set(parse_seeds(manifest.seeds)))
        manifest = manifest.model_copy(update={"seeds": seeds_spec(seeds)})
    write_json(out / "manifest.json", manifest.model_dump(mode="json"))


def run_eval(
    cfg: EvalConfig,
    out: Path,
    *,
    transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[EvalRecord], None] | None = None,
) -> dict[str, Any]:
    """Play every pending seed, then write the summary. Returns the summary."""
    if cfg.concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if not cfg.seeds:
        raise ValueError("no seeds")
    strategy = get_strategy(cfg.strategy)
    plan = strategy.api_plan()
    check_no_ground_truth(plan.system, f"the {cfg.strategy} system prompt")
    fake = cfg.transport == "fake"
    if cfg.transport not in ("http", "fake"):
        raise ValueError(f"unknown transport {cfg.transport!r}; use http or fake")
    provider = make_provider(
        cfg.provider,
        base_url=cfg.base_url,
        api_key="fake-key" if fake else None,
    )
    manifest = cfg.manifest(plan.loop.model_dump(mode="json"))
    _start(out, manifest)
    if transport_factory is None and fake:
        model = FakeModel(provider.wire)

        def transport_factory() -> httpx.BaseTransport:
            return model.transport()

    limiter = RateLimiter(cfg.rpm, sleep=sleep)

    def play(seed: int) -> EvalRecord:
        ep = DisputeEpisode(seed)
        check_no_ground_truth(observation_message(ep), f"the observation for seed {seed}")
        runner = ProviderRunner(
            provider,
            cfg.model,
            plan,
            settings=cfg.settings,
            transport=None if transport_factory is None else transport_factory(),
            limiter=limiter,
            sleep=sleep,
            price=cfg.price,
        )
        try:
            run = runner.run(ep)
        finally:
            runner.close()
        rec = record_from_run(manifest, run)
        write_json(record_path(out, seed), rec.model_dump(mode="json"))
        write_store_entry(out, rec)
        if progress is not None:
            progress(rec)
        return rec

    todo = _pending(out, cfg.seeds)
    if cfg.concurrency == 1:
        for s in todo:
            play(s)
    else:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            list(pool.map(play, todo))
    return write_summary(out)


def write_summary(out: Path) -> dict[str, Any]:
    """Score every record in `out` with the reference policies on the same seeds."""
    manifest = load_manifest(out)
    recs = load_records(out)
    if manifest is None or not recs:
        raise ValueError(f"{out} has no evaluation records")
    rows = [r.row for r in recs]
    seeds = [r.seed for r in recs]
    rows += reference_rows(seeds)
    sums = summarise(rows)
    stops: dict[str, int] = {}
    for r in recs:
        stops[r.stop] = stops.get(r.stop, 0) + 1
    tin = [r.input_tokens for r in recs if r.input_tokens is not None]
    tout = [r.output_tokens for r in recs if r.output_tokens is not None]
    costs = [r.cost_usd for r in recs if r.cost_usd is not None]
    summary: dict[str, Any] = {
        "manifest": manifest.model_dump(mode="json"),
        "system": manifest.system,
        "real_model": manifest.real_model,
        "synthetic_cases": True,
        "n": len(recs),
        "seeds": seeds_spec(seeds),
        "stops": dict(sorted(stops.items())),
        "input_tokens": sum(tin) if tin else None,
        "output_tokens": sum(tout) if tout else None,
        "api_cost_usd": round(sum(costs), 6) if costs else None,
        "systems": [s.model_dump(mode="json") for s in sums],
        "rows": rows,
    }
    write_json(out / "summary.json", summary)
    what = (
        "real model behaviour"
        if manifest.real_model
        else "a FAKE model (harness check; says nothing about any real model)"
    )
    lines = [
        f"# {manifest.system} on simulated disputes",
        "",
        f"Seeds {seeds_spec(seeds)} ({len(recs)} cases, one per seed). Synthetic cases; "
        f"{what}. Path: {manifest.path}.",
        "References replay the same seeds in-process. Cost = merchant net loss in USD "
        "(lower is better). Gate = no hard verifier failure.",
        "",
        *summary_table(sums),
        "",
        "Stops: " + ", ".join(f"{k} {v}" for k, v in summary["stops"].items()) + ".",
    ]
    if summary["input_tokens"] is not None:
        lines.append(
            f"Tokens: {summary['input_tokens']} in, {summary['output_tokens']} out"
            + (f"; API cost ${summary['api_cost_usd']:.4f}." if costs else ".")
        )
    lines.append("")
    (out / "SUMMARY.md").write_text("\n".join(lines))
    return summary


def import_store(
    store: Path,
    out: Path,
    *,
    model: str,
    strategy: str,
    provider: str = "cli-brief",
) -> dict[str, Any]:
    """Records from an `EpisodeStore` a CLI player wrote. One episode per seed: the
    first one started (lowest nonce) counts; an episode never submitted is lapsed
    (`not_submitted`) so it is scored, not dropped."""
    get_strategy(strategy)  # the brief must be one of ours
    st = EpisodeStore(store)
    firsts: dict[int, tuple[int, str]] = {}
    for meta in sorted(Path(store).glob("ep-*.json")):
        raw = json.loads(meta.read_text())
        if raw.get("kind", "simple") != "dispute":
            continue
        cur = firsts.get(raw["seed"])
        if cur is None or raw["nonce"] < cur[0]:
            firsts[raw["seed"]] = (raw["nonce"], meta.stem)
    if not firsts:
        raise ValueError(f"no dispute episodes in {store}")
    manifest = Manifest(
        provider=provider,
        model=model,
        strategy=strategy,
        path="cli-brief",
        transport="cli",
        seeds=seeds_spec(sorted(firsts)),
    )
    _start(out, manifest)
    for seed, (_nonce, eid) in sorted(firsts.items()):
        ep = st.load(eid)
        assert isinstance(ep, DisputeEpisode)
        calls = [(c["tool"], c["args"]) for c in st.log(eid)]
        _write_replayed(out, manifest, seed, eid, ep, calls)
    return write_summary(out)


def _write_replayed(
    out: Path,
    manifest: Manifest,
    seed: int,
    episode_id: str,
    ep: DisputeEpisode,
    calls: Sequence[tuple[str, Any]],
) -> None:
    stop = "submitted" if ep.closed else "not_submitted"
    score = ep.score if ep.score is not None else ep.lapse("not_submitted")
    dumped = json.loads(score.model_dump_json())
    rec = EvalRecord(
        system=manifest.system,
        provider=manifest.provider,
        model=manifest.model,
        strategy=manifest.strategy,
        path=manifest.path,
        real_model=manifest.real_model,
        seed=seed,
        episode_id=episode_id,
        stop=stop,
        calls=tuple({"tool": t, "args": a} for t, a in calls),
        score=dumped,
        row=row(manifest.system, dumped),
    )
    write_json(record_path(out, seed), rec.model_dump(mode="json"))


MCP_RENAMES = {"submit_dispute": "submit_decision"}


def mcp_call(tool: str, args: Any) -> tuple[str, Any]:
    """An MCP sim-server call as the episode tool call it made: `submit_dispute` is
    `submit_decision`, and the `episode_id` argument is the server's routing key."""
    if isinstance(args, dict):
        args = {k: v for k, v in args.items() if k != "episode_id"}
    return MCP_RENAMES.get(tool, tool), args


def import_calls(
    log: Path,
    out: Path,
    *,
    model: str,
    strategy: str,
    provider: str = "mcp",
) -> dict[str, Any]:
    """Records from a JSONL call log, one line per episode:
    `{"seed": 5001, "calls": [{"tool": "get_case", "args": {"episode_id": "..."}}, ...]}`
    (plus `"family"` if the episode was started with one).

    This is the MCP path: the sim server keeps episodes in memory, so the client logs
    the tool calls it made (MCP names and arguments as sent) and the episodes are
    rebuilt here by replay, exactly as the server ran them. The first line per seed
    counts; an episode without a successful submit is lapsed and scored."""
    get_strategy(strategy)
    episodes: dict[int, tuple[str | None, list[tuple[str, Any]]]] = {}
    for n, line in enumerate(Path(log).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            seed = int(item["seed"])
            family = item.get("family")
            calls = [mcp_call(str(c["tool"]), c.get("args")) for c in item["calls"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{log}:{n}: not a call-log line ({exc})") from exc
        if seed < 0:
            raise ValueError(f"{log}:{n}: bad seed {seed}")
        episodes.setdefault(seed, (None if family is None else str(family), calls))
    if not episodes:
        raise ValueError(f"no episodes in {log}")
    manifest = Manifest(
        provider=provider,
        model=model,
        strategy=strategy,
        path="mcp",
        transport="mcp",
        seeds=seeds_spec(sorted(episodes)),
    )
    _start(out, manifest)
    for seed, (family, calls) in sorted(episodes.items()):
        ep = DisputeEpisode.replay(seed, family, calls)
        _write_replayed(out, manifest, seed, ep.episode_id, ep, calls)
    return write_summary(out)


__all__ = [
    "GROUND_TRUTH_MARKERS",
    "EvalConfig",
    "GroundTruthLeakError",
    "check_no_ground_truth",
    "import_calls",
    "import_store",
    "mcp_call",
    "record_from_run",
    "run_eval",
    "write_summary",
]
