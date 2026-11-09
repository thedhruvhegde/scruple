"""Minimal GitHub REST client for posting gate results.

Endpoints used (public GitHub REST API documentation, API version 2022-11-28):
- issue comments: https://docs.github.com/en/rest/issues/comments
  GET/POST /repos/{owner}/{repo}/issues/{number}/comments,
  PATCH /repos/{owner}/{repo}/issues/comments/{comment_id}

HTTP goes through an injectable `Transport`, so tests never touch the network.
The token is read from the environment by the caller and is never logged.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from scruple.ci.comment import MARKER

API_VERSION = "2022-11-28"
DEFAULT_API = "https://api.github.com"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")


class GitHubError(OSError):
    pass


@dataclass(frozen=True)
class Response:
    status: int
    body: str

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class Transport(Protocol):
    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Response: ...


class UrllibTransport:
    """Real HTTP via the standard library. Not used in tests."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Response:  # pragma: no cover - network
        if not url.startswith("https://"):
            raise GitHubError("refusing a non-https GitHub API URL")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                return Response(r.status, r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read().decode("utf-8", "replace"))


def token_from_env(env: dict[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    token = e.get("GITHUB_TOKEN") or e.get("GH_TOKEN") or ""
    if not token:
        raise GitHubError("set GITHUB_TOKEN (or GH_TOKEN) to post to GitHub")
    return token


class GitHubClient:
    def __init__(
        self,
        repo: str,
        token: str,
        transport: Transport | None = None,
        api_url: str = DEFAULT_API,
    ) -> None:
        if not REPO_PATTERN.match(repo):
            raise ValueError(f"repo must look like owner/name, got {repo!r}")
        self.repo = repo
        self._token = token
        self.transport: Transport = transport or UrllibTransport()
        self.api_url = api_url.rstrip("/")

    def _call(self, method: str, path: str, payload: Any = None) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "scruple-gate",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
        r = self.transport.request(method, f"{self.api_url}{path}", headers, body)
        if not 200 <= r.status < 300:
            raise GitHubError(f"{method} {path} failed with HTTP {r.status}")
        return r.json()

    def upsert_pr_comment(self, pr: int, body: str) -> tuple[str, int]:
        """Update scruple's earlier comment on the PR if there is one, else add one.
        Returns ("created" | "updated", comment id)."""
        if MARKER not in body:
            body = f"{MARKER}\n{body}"
        page = 1
        while True:
            items = self._call(
                "GET", f"/repos/{self.repo}/issues/{pr}/comments?per_page=100&page={page}"
            )
            for c in items or []:
                if MARKER in (c.get("body") or ""):
                    cid = int(c["id"])
                    self._call("PATCH", f"/repos/{self.repo}/issues/comments/{cid}", {"body": body})
                    return "updated", cid
            if not items or len(items) < 100:
                break
            page += 1
        made = self._call("POST", f"/repos/{self.repo}/issues/{pr}/comments", {"body": body})
        return "created", int(made["id"])
