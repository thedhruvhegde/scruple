"""Required-status-check payloads built from a scorecard.

Two shapes, both from the public GitHub REST documentation (API 2022-11-28):
- commit status: https://docs.github.com/en/rest/commits/statuses
  POST /repos/{owner}/{repo}/statuses/{sha}; state is one of error, failure,
  pending, success; description is a short string.
- check run: https://docs.github.com/en/rest/checks/runs
  POST /repos/{owner}/{repo}/check-runs; conclusion is one of action_required,
  cancelled, failure, neutral, success, skipped, stale, timed_out.

Branch protection can require either by its context or name (docs/CI.md).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from scruple.ci.comment import render_comment
from scruple.ci.scorecard import Scorecard, Verdict

SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DESCRIPTION_LIMIT = 140  # ASSUMPTION: GitHub truncates or rejects longer descriptions
SUMMARY_LIMIT = 65_535  # check-run output.summary maximum, per the checks API docs

Kind = Literal["status", "check-run"]


def _check_sha(sha: str) -> str:
    if not SHA_PATTERN.match(sha):
        raise ValueError(f"not a full commit sha: {sha!r}")
    return sha


def description(card: Scorecard) -> str:
    n = len(card.regressions)
    text = (
        f"{card.verdict.value.upper()}: {card.rule_id}@v{card.version}, "
        f"{n} regression{'s' if n != 1 else ''} (synthetic data)"
    )
    return text if len(text) <= DESCRIPTION_LIMIT else text[: DESCRIPTION_LIMIT - 3] + "..."


def status_state(card: Scorecard) -> str:
    return "success" if card.exit_code == 0 else "failure"


def conclusion(card: Scorecard) -> str:
    if card.exit_code != 0:
        return "failure"
    return "neutral" if card.verdict is Verdict.HOLD else "success"


def commit_status_payload(
    card: Scorecard, context: str = "scruple/gate", target_url: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": status_state(card),
        "context": context,
        "description": description(card),
    }
    if target_url:
        payload["target_url"] = target_url
    return payload


def check_run_payload(
    card: Scorecard, head_sha: str, name: str = "scruple/gate", details_url: str | None = None
) -> dict[str, Any]:
    summary = render_comment(card)
    if len(summary) > SUMMARY_LIMIT:
        summary = summary[: SUMMARY_LIMIT - 20] + "\n\n(truncated)\n"
    payload: dict[str, Any] = {
        "name": name,
        "head_sha": _check_sha(head_sha),
        "status": "completed",
        "conclusion": conclusion(card),
        "external_id": f"{card.rule_id}@v{card.version}:{card.content_hash[:16]}",
        "output": {"title": description(card), "summary": summary},
    }
    if details_url:
        payload["details_url"] = details_url
    return payload


def payload_for(
    kind: Kind, card: Scorecard, sha: str, context: str, url: str | None
) -> dict[str, Any]:
    _check_sha(sha)
    if kind == "status":
        return commit_status_payload(card, context, url)
    return check_run_payload(card, sha, context, url)
