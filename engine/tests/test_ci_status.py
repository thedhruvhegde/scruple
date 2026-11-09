"""Commit status and check-run payloads for the required status check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scruple.ci import cli as ci_cli
from scruple.ci.scorecard import Scorecard, Verdict
from scruple.ci.status import (
    DESCRIPTION_LIMIT,
    check_run_payload,
    commit_status_payload,
    conclusion,
    description,
    payload_for,
    status_state,
)
from scruple.cli.main import main
from tests.ci_support import cached_run
from tests.test_ci_comment import FakeTransport

SHA = "a" * 40


def _card(verdict: Verdict, code: int) -> Scorecard:
    return cached_run("good").scorecard.model_copy(update={"verdict": verdict, "exit_code": code})


@pytest.mark.parametrize(
    ("verdict", "code", "state", "concl"),
    [
        (Verdict.PASS, 0, "success", "success"),
        (Verdict.HOLD, 0, "success", "neutral"),
        (Verdict.HOLD, 1, "failure", "failure"),
        (Verdict.BLOCK, 1, "failure", "failure"),
    ],
)
def test_state_and_conclusion_table(verdict: Verdict, code: int, state: str, concl: str) -> None:
    card = _card(verdict, code)
    assert status_state(card) == state
    assert conclusion(card) == concl
    assert state in {"error", "failure", "pending", "success"}
    assert concl in {"action_required", "cancelled", "failure", "neutral", "success", "skipped"}


def test_commit_status_payload_shape() -> None:
    p = commit_status_payload(cached_run("bad").scorecard, target_url="https://ci.example.test/1")
    assert p == {
        "state": "failure",
        "context": "scruple/gate",
        "description": "BLOCK: inr-delivered-contest-fast@v1, 6 regressions (synthetic data)",
        "target_url": "https://ci.example.test/1",
    }
    assert "target_url" not in commit_status_payload(cached_run("good").scorecard)


def test_description_is_bounded() -> None:
    card = cached_run("good").scorecard.model_copy(update={"rule_id": "r" * 200})
    assert len(description(card)) == DESCRIPTION_LIMIT
    assert description(card).endswith("...")
    assert "0 regressions" in description(cached_run("good").scorecard)


def test_check_run_payload_shape() -> None:
    card = cached_run("good").scorecard
    p = check_run_payload(card, SHA, details_url="https://ci.example.test/2")
    assert p["status"] == "completed" and p["conclusion"] == "success"
    assert p["head_sha"] == SHA and p["name"] == "scruple/gate"
    assert p["output"]["summary"].startswith("<!-- scruple-gate -->")
    assert p["output"]["title"].startswith("PASS")
    assert p["details_url"] == "https://ci.example.test/2"


def test_check_run_summary_truncated() -> None:
    card = cached_run("good").scorecard.model_copy(update={"rule_description": "x" * 70_000})
    summary = check_run_payload(card, SHA)["output"]["summary"]
    assert len(summary) <= 65_535 and summary.endswith("(truncated)\n")


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "a" * 41])
def test_bad_sha_rejected(sha: str) -> None:
    with pytest.raises(ValueError, match="sha"):
        payload_for("status", cached_run("good").scorecard, sha, "c", None)


def test_payloads_are_deterministic() -> None:
    card = cached_run("bad").scorecard
    assert payload_for("check-run", card, SHA, "c", None) == payload_for(
        "check-run", card, SHA, "c", None
    )


@pytest.mark.parametrize(
    ("kind", "path"), [("status", f"/statuses/{SHA}"), ("check-run", "/check-runs")]
)
def test_cli_status_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    path: str,
) -> None:
    card_path = tmp_path / "card.json"
    card_path.write_text(cached_run("bad").scorecard.to_json())
    fake = FakeTransport()
    monkeypatch.setattr(ci_cli, "make_transport", lambda: fake)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    args = ["ci", "status", "--scorecard", str(card_path), "--sha", SHA, "--kind", kind]
    assert main([*args, "--post", "--repo", "acme/rules"]) == 0
    method, url, _, body = fake.calls[-1]
    assert method == "POST" and url == f"https://api.github.com/repos/acme/rules{path}"
    out = capsys.readouterr().out
    printed = json.loads(out[: out.rindex("}") + 1])
    assert printed == body
    assert main([*args, "--mirror-exit"]) == 1
    assert main([*args, "--post"]) == 2  # --repo missing


def test_action_posts_status_with_stable_context() -> None:
    import yaml

    action = yaml.safe_load(
        (
            Path(__file__).resolve().parent.parent / ".github/actions/scruple-gate/action.yml"
        ).read_text()
    )
    steps = {s.get("name"): s for s in action["runs"]["steps"]}
    st = steps["commit status"]
    assert "scruple ci status" in st["run"] and "--post" in st["run"]
    assert st["continue-on-error"] is True
    assert action["inputs"]["status-context"]["default"] == "scruple/gate"
