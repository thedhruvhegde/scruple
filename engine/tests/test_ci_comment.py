"""PR comment rendering (snapshot-tested) and posting through a fake transport."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scruple.ci import cli as ci_cli
from scruple.ci.comment import MARKER, render_comment
from scruple.ci.github import GitHubClient, GitHubError, Response, token_from_env
from scruple.ci.scorecard import Regression, Scorecard, Verdict
from scruple.cli.main import main
from tests.ci_support import cached_run

SNAPSHOTS = Path(__file__).parent / "snapshots"


def check_snapshot(name: str, text: str) -> None:
    """Compare with tests/snapshots/<name>. Set SCRUPLE_UPDATE_SNAPSHOTS=1 to rewrite."""
    path = SNAPSHOTS / name
    if os.environ.get("SCRUPLE_UPDATE_SNAPSHOTS") == "1":
        path.write_text(text)
    assert path.exists(), f"missing snapshot {name}; run with SCRUPLE_UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(), f"snapshot {name} changed"


def _hold_card() -> Scorecard:
    good = cached_run("good").scorecard
    return good.model_copy(
        update={
            "verdict": Verdict.HOLD,
            "exit_code": 1,
            "rule_description": "pipes | and\nnewlines in a description",
            "regressions": (Regression(kind="check", name="a|b", detail="x | y"),),
            "slices": (),
            "statistics": None,
            "canary": None,
        }
    )


@pytest.mark.parametrize("which", ["good", "bad"])
def test_comment_snapshot(which: str) -> None:
    check_snapshot(f"ci_comment_{which}.md", render_comment(cached_run(which).scorecard))


def test_comment_snapshot_hold_escapes_table_cells() -> None:
    md = render_comment(_hold_card())
    check_snapshot("ci_comment_hold.md", md)
    assert "a\\|b" in md and "pipes \\| and newlines" in md
    assert "HOLD" in md and "### Statistics" not in md


def test_comment_is_deterministic_and_marked() -> None:
    card = cached_run("bad").scorecard
    a, b = render_comment(card), render_comment(Scorecard.model_validate_json(card.to_json()))
    assert a == b and a.startswith(MARKER)
    assert "BLOCK" in a and "**worse**" in a and "no_new_policy_violations" in a


def test_comment_lists_every_check() -> None:
    card = cached_run("good").scorecard
    md = render_comment(card)
    for c in card.checks:
        assert f"| {c.name} |" in md
    assert "None found." in md


@dataclass
class FakeTransport:
    """Scripted GitHub: holds comments in memory and records every request."""

    comments: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, str], object]] = field(default_factory=list)
    fail_with: int | None = None
    next_id: int = 100

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Response:
        payload = json.loads(body) if body else None
        self.calls.append((method, url, headers, payload))
        if self.fail_with:
            return Response(self.fail_with, '{"message":"nope"}')
        if method == "GET":
            page = int(url.rsplit("page=", 1)[1])
            return Response(200, json.dumps(self.comments[(page - 1) * 100 : page * 100]))
        if method == "POST" and url.endswith("/comments"):
            self.next_id += 1
            assert isinstance(payload, dict)
            self.comments.append({"id": self.next_id, "body": payload["body"]})
            return Response(201, json.dumps({"id": self.next_id}))
        if method == "PATCH":
            cid = int(url.rsplit("/", 1)[1])
            for c in self.comments:
                if c["id"] == cid:
                    assert isinstance(payload, dict)
                    c["body"] = payload["body"]
            return Response(200, "{}")
        return Response(404, "")


def test_first_post_creates_then_updates_in_place() -> None:
    t = FakeTransport(comments=[{"id": 1, "body": "unrelated"}])
    gh = GitHubClient("acme/rules", "tok", t)
    assert gh.upsert_pr_comment(7, "first") == ("created", 101)
    assert gh.upsert_pr_comment(7, "second") == ("updated", 101)
    assert len(t.comments) == 2
    assert t.comments[1]["body"] == f"{MARKER}\nsecond"
    method, url, headers, _ = t.calls[0]
    assert (method, url) == (
        "GET",
        "https://api.github.com/repos/acme/rules/issues/7/comments?per_page=100&page=1",
    )
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_paginates_to_find_existing_comment() -> None:
    old = [{"id": i, "body": f"c{i}"} for i in range(150)]
    old.append({"id": 999, "body": f"{MARKER}\nold"})
    t = FakeTransport(comments=old)
    assert GitHubClient("a/b", "t", t).upsert_pr_comment(1, "new") == ("updated", 999)
    assert [c[1].rsplit("=", 1)[1] for c in t.calls if c[0] == "GET"] == ["1", "2"]


def test_http_error_raises_without_leaking_token() -> None:
    t = FakeTransport(fail_with=403)
    with pytest.raises(GitHubError, match="HTTP 403") as e:
        GitHubClient("a/b", "secret-token", t).upsert_pr_comment(1, "x")
    assert "secret-token" not in str(e.value)


@pytest.mark.parametrize("repo", ["", "nope", "a/b/c", "a b/c", "../x"])
def test_repo_is_validated(repo: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        GitHubClient(repo, "t", FakeTransport())


def test_token_from_env() -> None:
    assert token_from_env({"GITHUB_TOKEN": "a"}) == "a"
    assert token_from_env({"GH_TOKEN": "b"}) == "b"
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        token_from_env({})


def test_cli_render_and_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    card_path = tmp_path / "card.json"
    card_path.write_text(cached_run("bad").scorecard.to_json())
    md_path = tmp_path / "c.md"
    assert main(["ci", "render", "--scorecard", str(card_path), "--out", str(md_path)]) == 0
    assert md_path.read_text() == render_comment(cached_run("bad").scorecard)
    assert main(["ci", "render", "--scorecard", str(card_path)]) == 0
    assert capsys.readouterr().out.startswith(MARKER)

    fake = FakeTransport()
    monkeypatch.setattr(ci_cli, "make_transport", lambda: fake)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.test/api/v3")
    args = ["ci", "comment", "--scorecard", str(card_path), "--repo", "a/b", "--pr", "3"]
    assert main(args) == 0
    assert "created comment 101 on a/b#3" in capsys.readouterr().out
    assert fake.calls[-1][1] == "https://ghe.example.test/api/v3/repos/a/b/issues/3/comments"
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert main(args) == 2
