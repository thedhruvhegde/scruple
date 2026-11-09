"""`scruple ci ...`: the commands CI templates call. Exit codes: 0 pass, 1 gate
failed (block, or hold with fail_on_hold), 2 usage or input error."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from scruple.ci.comment import render_comment
from scruple.ci.config import load_candidate, load_config
from scruple.ci.github import DEFAULT_API, GitHubClient, Transport, UrllibTransport, token_from_env
from scruple.ci.runner import run_candidate
from scruple.ci.scorecard import Scorecard, load_scorecard


def make_transport() -> Transport:
    """Seam for tests: replaced with a fake so no test talks to GitHub."""
    return UrllibTransport()


def _card(path: str) -> Scorecard:
    return load_scorecard(Path(path).read_text())


def _client(args: argparse.Namespace) -> GitHubClient:
    import os

    api = args.api_url or os.environ.get("GITHUB_API_URL") or DEFAULT_API
    return GitHubClient(args.repo, token_from_env(), make_transport(), api)


def cmd_ci_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.db:
        cfg = cfg.model_copy(update={"ci": cfg.ci.model_copy(update={"db": args.db})})
    cand_path = Path(args.candidate) if args.candidate else cfg.resolve(cfg.candidate.file)
    run = run_candidate(cfg, load_candidate(cand_path))
    out = Path(args.out) if args.out else cfg.resolve(cfg.ci.scorecard)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(run.scorecard.to_json())
    card = run.scorecard
    print(f"scruple gate: {card.verdict.value.upper()} {card.rule_id}@v{card.version}")
    for d in card.decisions:
        print(f"  {d.decision_id} {d.stage.value:<7} {d.outcome.value:<14} {d.summary}")
    for r in card.regressions:
        print(f"  REGRESSION {r.kind}: {r.name}: {r.detail}")
    print(f"scorecard written to {out}")
    return card.exit_code


def cmd_ci_render(args: argparse.Namespace) -> int:
    md = render_comment(_card(args.scorecard))
    if args.out:
        Path(args.out).write_text(md)
    else:
        print(md, end="")
    return 0


def cmd_ci_comment(args: argparse.Namespace) -> int:
    action, cid = _client(args).upsert_pr_comment(args.pr, render_comment(_card(args.scorecard)))
    print(f"{action} comment {cid} on {args.repo}#{args.pr}")
    return 0


def add_ci_parser(sub: Any) -> None:
    """Attach `ci` and its subcommands to the top-level subparsers (additive)."""
    ci = sub.add_parser("ci", help="gate a candidate in CI and report the result")
    ci_sub = ci.add_subparsers(dest="ci_cmd", required=True)

    r = ci_sub.add_parser("run", help="evaluate the candidate and write a JSON scorecard")
    r.set_defaults(fn=cmd_ci_run)
    r.add_argument("--config", default="scruple.toml", help="path to scruple.toml")
    r.add_argument("--candidate", default=None, help="candidate rule JSON (overrides config)")
    r.add_argument("--out", default=None, help="scorecard path (overrides config)")
    r.add_argument("--db", default=None, help="SQLite path (default: in-memory)")

    m = ci_sub.add_parser("render", help="render a scorecard as markdown")
    m.set_defaults(fn=cmd_ci_render)
    m.add_argument("--scorecard", required=True)
    m.add_argument("--out", default=None, help="write here instead of stdout")

    c = ci_sub.add_parser("comment", help="post or update the scorecard comment on a PR")
    c.set_defaults(fn=cmd_ci_comment)
    c.add_argument("--scorecard", required=True)
    c.add_argument("--repo", required=True, help="owner/name")
    c.add_argument("--pr", type=int, required=True)
    c.add_argument("--api-url", default=None, help="GitHub API base (default: env or public)")
