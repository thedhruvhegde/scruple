"""`scruple backtest run | calibrate | synth`. See docs/BACKTEST.md."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from scruple.agents.providers import Price, make_provider, provider_names
from scruple.backtest.candidate import Candidate, load_candidate
from scruple.backtest.engine import BacktestConfig, run_backtest
from scruple.backtest.history import History, load_adyen, load_store, load_stripe, load_tabular
from scruple.backtest.llm import AgentBackend, HttpAgent, LLMCandidate, ModelAgent
from scruple.backtest.report import write_report
from scruple.calibration import (
    DEFAULT_PROFILE,
    CalibrationConfig,
    CalibrationProfile,
    calibrate,
    stripe_disputes,
)
from scruple.ingest.errors import IngestError
from scruple.ingest.tabular import ImportConfig


def _history(args: argparse.Namespace) -> History:
    path = Path(args.history)
    if args.format == "store":
        return load_store(str(path), label=args.label)
    data = path.read_bytes()
    label = args.label or f"{path.name} ({args.format} export)"
    if args.format == "stripe":
        return load_stripe(data, label=label, allow_live=args.allow_live)
    if args.format == "adyen":
        return load_adyen(
            data,
            label=label,
            hmac_keys=args.adyen_hmac_key or (),
            verify=not args.no_verify,
            allow_live=args.allow_live,
        )
    if not args.csv_config:
        raise ValueError("--format csv needs --csv-config (an ImportConfig JSON file)")
    cfg = ImportConfig.model_validate_json(Path(args.csv_config).read_text(encoding="utf-8"))
    return load_tabular(data, cfg, label=label, jsonl=path.suffix == ".jsonl")


def _price(args: argparse.Namespace) -> Price | None:
    if args.price_in is None and args.price_out is None:
        return None
    if args.price_in is None or args.price_out is None:
        raise ValueError("give both --price-in and --price-out (USD per million tokens)")
    return Price(input_per_mtok=args.price_in, output_per_mtok=args.price_out)


def _agent(args: argparse.Namespace) -> LLMCandidate:
    common: dict[str, Any] = {
        "timeout_s": args.agent_timeout,
        "max_retries": args.agent_retries,
        "backoff_s": args.agent_backoff,
        "rpm": args.agent_rpm,
    }
    backend: AgentBackend
    if args.agent_url:
        headers: dict[str, str] = {}
        if args.agent_auth_env:
            token = os.environ.get(args.agent_auth_env, "")
            if not token:
                raise ValueError(f"--agent-auth-env: {args.agent_auth_env} is not set")
            headers["authorization"] = f"Bearer {token}"
        backend = HttpAgent(args.agent_url, agent_id=args.agent_id, headers=headers, **common)
    else:
        if not args.agent_model or not args.agent_prompt:
            raise ValueError("--agent-provider needs --agent-model and --agent-prompt")
        provider = make_provider(args.agent_provider, base_url=args.agent_base_url)
        prompt = Path(args.agent_prompt).read_text(encoding="utf-8")
        backend = ModelAgent(
            provider,
            args.agent_model,
            prompt,
            max_tokens=args.agent_max_tokens,
            agent_id=args.agent_id,
            **common,
        )
    cache = Path(args.agent_cache) if args.agent_cache else Path(args.out) / "agent-cache"
    return LLMCandidate(
        backend,
        cache_dir=cache,
        concurrency=args.agent_concurrency,
        allow_free_text=args.agent_send_free_text,
        price=_price(args),
    )


def cmd_run(args: argparse.Namespace) -> int:
    if sum(map(bool, (args.candidate, args.agent_url, args.agent_provider))) != 1:
        raise ValueError("give exactly one of --candidate, --agent-url, --agent-provider")
    history = _history(args)
    profile = None
    if args.profile:
        profile = CalibrationProfile.model_validate_json(Path(args.profile).read_text())
    cfg = BacktestConfig(
        k=args.k,
        currency=args.currency,
        include_cases=args.include_cases,
        escalation_cost_minor=args.escalation_cost,
        contest_fee_minor=args.contest_fee,
        on_agent_error=args.on_agent_error,
    )
    candidate: Candidate
    candidate = _agent(args) if not args.candidate else load_candidate(args.candidate)
    try:
        report = run_backtest(history, candidate, config=cfg, profile=profile)
    finally:
        if isinstance(candidate, LLMCandidate):
            candidate.close()
    files = write_report(report, Path(args.out), pdf=False if args.no_pdf else None)
    tag = " [SYNTHETIC DATA]" if report.synthetic else ""
    print(
        f"{report.verdict}{tag}: {report.n_included} cases "
        f"({report.n_observed} observed, {report.n_estimated} estimated)"
    )
    if report.agent is not None:
        a = report.agent
        cost = "unpriced" if a.cost_usd is None else f"USD {a.cost_usd:.4f}"
        print(
            "agent: "
            + ", ".join(f"{k} {v}" for k, v in a.outcomes.items())
            + f"; {a.calls_made} calls, {a.cache_hits} cached; "
            f"{a.input_tokens + a.output_tokens} tokens, {cost}; "
            f"latency total {a.latency_ms_total:.0f} ms"
        )
    for f in files:
        print(f"wrote {f}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    history = _history(args)
    rep = calibrate(
        [hc.case for hc in history.cases], history.movements, CalibrationConfig(k=args.k)
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = rep.model_dump(mode="json")
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.profile_out:
        Path(args.profile_out).write_text(rep.profile.model_dump_json(indent=2) + "\n")
    print(
        f"divergence: default {rep.before.overall:.4f}, calibrated {rep.after.overall:.4f}; "
        f"wrote {out}"
    )
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    profile = DEFAULT_PROFILE
    if args.profile:
        profile = CalibrationProfile.model_validate_json(Path(args.profile).read_text())
    objs = stripe_disputes(profile, args.n, args.seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(objs, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(objs)} SYNTHETIC Stripe-shaped disputes to {out}")
    return 0


def _input_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--history", required=True, help="the exported file, or a case store")
    sp.add_argument("--format", required=True, choices=["stripe", "adyen", "csv", "store"])
    sp.add_argument("--csv-config", default=None, help="ImportConfig JSON for --format csv")
    sp.add_argument("--adyen-hmac-key", action="append", help="hex HMAC key (repeatable)")
    sp.add_argument("--no-verify", action="store_true", help="skip Adyen HMAC checks")
    sp.add_argument("--allow-live", action="store_true", help="accept live-mode records")
    sp.add_argument("--label", default=None, help="dataset name shown on the report")
    sp.add_argument("--k", type=int, default=10, help="small-cell threshold (default 10)")


def add_backtest_parser(sub: Any) -> None:
    bt = sub.add_parser("backtest", help="replay a dispute history against a candidate")
    bsub = bt.add_subparsers(dest="backtest_cmd", required=True)
    run = bsub.add_parser("run", help="backtest a rule or agent on an exported history")
    _input_args(run)
    run.add_argument("--candidate", default=None, help="rule .json, agent .py or module[:attr]")
    ag = run.add_argument_group(
        "LLM agent", "backtest a partner's LLM agent instead of --candidate (docs/BACKTEST.md)"
    )
    ag.add_argument("--agent-url", default=None, help="the partner's endpoint (POST JSON)")
    ag.add_argument("--agent-auth-env", default=None, help="env var holding a bearer token")
    ag.add_argument("--agent-provider", default=None, choices=provider_names())
    ag.add_argument("--agent-model", default=None, help="model id for --agent-provider")
    ag.add_argument("--agent-prompt", default=None, help="the partner's system prompt file")
    ag.add_argument("--agent-base-url", default=None, help="override the provider's endpoint")
    ag.add_argument("--agent-max-tokens", type=int, default=512)
    ag.add_argument("--agent-id", default=None, help="cache identity; change it with the agent")
    ag.add_argument("--agent-cache", default=None, help="reply cache dir (default OUT/agent-cache)")
    ag.add_argument("--agent-concurrency", type=int, default=4, help="calls in flight at once")
    ag.add_argument("--agent-timeout", type=float, default=30.0, help="seconds per request")
    ag.add_argument("--agent-retries", type=int, default=3, help="retries on 429, 5xx, transport")
    ag.add_argument("--agent-backoff", type=float, default=1.0, help="first retry wait, seconds")
    ag.add_argument("--agent-rpm", type=float, default=None, help="max requests per minute")
    ag.add_argument(
        "--agent-send-free-text",
        action="store_true",
        help="opt in: send free-text fields (redacted) instead of withholding them",
    )
    ag.add_argument(
        "--on-agent-error",
        choices=["exclude", "escalate", "historical"],
        default="exclude",
        help="cases whose reply was malformed, failed or timed out (default exclude)",
    )
    ag.add_argument("--price-in", type=float, default=None, help="USD per million input tokens")
    ag.add_argument("--price-out", type=float, default=None, help="USD per million output tokens")
    run.add_argument("--out", required=True, help="report directory")
    run.add_argument("--profile", default=None, help="use this CalibrationProfile JSON")
    run.add_argument("--currency", default=None, help="default: the most common currency")
    run.add_argument("--escalation-cost", type=int, default=BacktestConfig().escalation_cost_minor)
    run.add_argument("--contest-fee", type=int, default=0, help="extra fee per contest, minor")
    run.add_argument("--include-cases", action="store_true", help="per-case lines in the JSON")
    run.add_argument("--no-pdf", action="store_true")
    run.set_defaults(fn=_guard(cmd_run))
    cal = bsub.add_parser("calibrate", help="fit the simulator from aggregates of a history")
    _input_args(cal)
    cal.add_argument("--out", required=True, help="calibration report JSON")
    cal.add_argument("--profile-out", default=None, help="also write the profile JSON")
    cal.set_defaults(fn=_guard(cmd_calibrate))
    syn = bsub.add_parser("synth", help="write a SYNTHETIC Stripe-shaped history")
    syn.add_argument("--n", type=int, default=5_000)
    syn.add_argument("--seed", type=int, default=0)
    syn.add_argument("--profile", default=None, help="CalibrationProfile JSON (default: built in)")
    syn.add_argument("--out", required=True)
    syn.set_defaults(fn=_guard(cmd_synth))


def _guard(fn: Any) -> Any:
    def run(args: argparse.Namespace) -> int:
        try:
            return int(fn(args))
        except IngestError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    return run
