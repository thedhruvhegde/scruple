"""Change tickets for gate events: one Jira or Linear issue per candidate version.

The first gate decision about a candidate (`rule_id` at a version) opens an issue that
describes it; every later decision about it adds a comment. Work runs as one
`ticket_sync` job per (provider, decision), queued by the dispatcher, idempotent by
decision id:

- `ticket_updates` records each decision once it reached the tracker; a retried or
  re-dispatched job for a recorded decision does nothing;
- `ticket_links` holds the candidate's issue. Creating it is claimed with a row first
  (`claim_ticket`), so two jobs for the same candidate never both create one: the
  other waits (a retry) until the issue exists. A failed attempt releases its claim; a
  claim left by a crashed worker is taken over after `STALE_CLAIM` (two minutes) by a
  job that first searches the tracker for the issue the crashed one may have created
  (by label in Jira, by id in Linear) before creating;
- Linear issues and comments get ids derived from (tenant, candidate) and (tenant,
  decision), so even a lost response cannot produce a second issue or comment. Jira has
  no client-chosen ids: a crash after Jira accepted a comment but before it was
  recorded can repeat that comment (documented limit).

APIs, per the vendors' public documentation:

- Jira Cloud REST v3: `POST /rest/api/3/issue` (fields project.key, summary,
  issuetype.name, labels, description in Atlassian Document Format; answers 201 with
  `id`, `key`), `POST /rest/api/3/issue/{key}/comment` (body in ADF),
  `GET /rest/api/3/search/jql?jql=...` (issues by label). Basic auth: account email and
  an API token.
- Linear GraphQL (`https://api.linear.app/graphql`): `issueCreate(input: {id, teamId,
  title, description})`, `commentCreate(input: {id, issueId, body})`, `issue(id)`.
  A personal API key goes in the `Authorization` header as is.

Errors: network failures, 429 and 5xx are retried by the job queue; other 4xx (bad
credentials, unknown project) fail the job at once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from scruple.clock import Clock, SystemClock
from scruple.integrations.config import IntegrationSettings, JiraConfig, LinearConfig, SecretError
from scruple.integrations.runtime import Runtime
from scruple.integrations.safehttp import Response, UnsafeURLError
from scruple.integrations.store import GateEvent, IntegrationStore
from scruple.jobs.worker import PermanentJobError

TICKET_KIND = "ticket_sync"
PROVIDERS = ("jira", "linear")
MAX_ATTEMPTS = 8
TIMEOUT_S = 120
STALE_CLAIM = timedelta(minutes=2)
LINEAR_URL = "https://api.linear.app/graphql"


class TransientTicketError(RuntimeError):
    """Retry later."""


class TicketRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_id: str
    issue_key: str
    url: str


def candidate_of(event: GateEvent) -> str:
    return f"{event.data['rule_id']}@v{event.data['version']}"


def _lines(event: GateEvent) -> list[str]:
    d = event.data
    out = [
        f"Decision {d['decision_id']}: {d['stage']} stage, outcome {d['outcome']}.",
        str(d.get("summary", "")),
        *(f"- {r}" for r in d.get("reasons", [])),
        f"Rule {d['rule_id']} v{d['version']}, content hash {d['content_hash']}.",
        f"Decision record sha256 {d['decision_sha256']}; audit seq {event.audit_seq}.",
    ]
    if d.get("policy_sha256"):
        out.append(f"Policy sha256 {d['policy_sha256']}.")
    if d.get("report_path"):
        out.append(f"Evidence report: {d['report_path']} on the scruple API.")
    out.append("Gate numbers come from scruple's synthetic simulator (labelled synthetic).")
    return [line for line in out if line]


def _title(event: GateEvent) -> str:
    d = event.data
    return f"scruple change: {d['rule_id']} v{d['version']}"[:250]


def _check(resp: Response, what: str) -> None:
    code = resp.status_code
    if 200 <= code < 300:
        return
    detail = resp.body[:200].decode("utf-8", "replace")
    if code == 429 or code >= 500:
        raise TransientTicketError(f"{what}: HTTP {code}")
    raise PermanentJobError(f"{what}: HTTP {code}: {detail}")


class Connector(Protocol):
    provider: str

    def find(self, tenant: str, candidate: str) -> TicketRef | None: ...
    def create(self, tenant: str, candidate: str, event: GateEvent) -> TicketRef: ...
    def update(self, tenant: str, ref: TicketRef, event: GateEvent) -> None: ...


def marker(tenant: str, candidate: str) -> str:
    """A label that names the candidate's issue without naming the tenant or rule."""
    return "scruple-" + hashlib.sha256(f"{tenant}\x00{candidate}".encode()).hexdigest()[:16]


def _adf(lines: list[str]) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line[:2_000]}]}
            for line in lines
        ],
    }


class JiraConnector:
    provider = "jira"

    def __init__(self, cfg: JiraConfig, token: str, runtime: Runtime) -> None:
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        self.http = runtime.http
        auth = base64.b64encode(f"{cfg.email}:{token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def __repr__(self) -> str:
        return f"JiraConnector({self.base!r}, project={self.cfg.project_key!r})"

    def _send(self, method: str, path: str, body: Any | None, what: str) -> Any:
        content = None if body is None else json.dumps(body).encode()
        resp = self.http.request(method, self.base + path, headers=self._headers, content=content)
        _check(resp, what)
        return resp.json() if resp.body else {}

    def _ref(self, issue_id: str, key: str) -> TicketRef:
        return TicketRef(issue_id=issue_id, issue_key=key, url=f"{self.base}/browse/{key}")

    def find(self, tenant: str, candidate: str) -> TicketRef | None:
        jql = f'project = "{self.cfg.project_key}" AND labels = "{marker(tenant, candidate)}"'
        out = self._send(
            "GET",
            f"/rest/api/3/search/jql?jql={quote(jql)}&fields=key&maxResults=1",
            None,
            "jira search",
        )
        issues = out.get("issues") or []
        if not issues:
            return None
        return self._ref(str(issues[0]["id"]), str(issues[0]["key"]))

    def create(self, tenant: str, candidate: str, event: GateEvent) -> TicketRef:
        fields = {
            "project": {"key": self.cfg.project_key},
            "summary": _title(event),
            "issuetype": {"name": self.cfg.issue_type},
            "labels": [*self.cfg.labels, marker(tenant, candidate)],
            "description": _adf(_lines(event)),
        }
        out = self._send("POST", "/rest/api/3/issue", {"fields": fields}, "jira create issue")
        return self._ref(str(out["id"]), str(out["key"]))

    def update(self, tenant: str, ref: TicketRef, event: GateEvent) -> None:
        self._send(
            "POST",
            f"/rest/api/3/issue/{quote(ref.issue_key)}/comment",
            {"body": _adf(_lines(event))},
            "jira comment",
        )


def stable_uuid(*parts: str) -> str:
    """A UUID in version-4 format derived from `parts` (Linear accepts client ids)."""
    digest = hashlib.sha256("\x00".join(parts).encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


ISSUE_CREATE = (
    "mutation IssueCreate($input: IssueCreateInput!) "
    "{ issueCreate(input: $input) { success issue { id identifier url } } }"
)
COMMENT_CREATE = (
    "mutation CommentCreate($input: CommentCreateInput!) "
    "{ commentCreate(input: $input) { success comment { id } } }"
)
ISSUE_QUERY = "query Issue($id: String!) { issue(id: $id) { id identifier url } }"


class LinearConnector:
    provider = "linear"

    def __init__(self, cfg: LinearConfig, api_key: str, runtime: Runtime) -> None:
        self.cfg = cfg
        self.http = runtime.http
        self._headers = {"Authorization": api_key, "Content-Type": "application/json"}

    def __repr__(self) -> str:
        return f"LinearConnector(team={self.cfg.team_id!r})"

    def _gql(self, query: str, variables: dict[str, Any], what: str) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode()
        resp = self.http.post(LINEAR_URL, headers=self._headers, content=body)
        _check(resp, what)
        out: dict[str, Any] = resp.json()
        return out

    @staticmethod
    def _errors(out: dict[str, Any]) -> str:
        return "; ".join(str(e.get("message", "")) for e in out.get("errors") or [])

    @staticmethod
    def _ref(issue: dict[str, Any]) -> TicketRef:
        return TicketRef(
            issue_id=str(issue["id"]), issue_key=str(issue["identifier"]), url=str(issue["url"])
        )

    def find(self, tenant: str, candidate: str) -> TicketRef | None:
        out = self._gql(ISSUE_QUERY, {"id": stable_uuid(tenant, candidate)}, "linear issue")
        issue = (out.get("data") or {}).get("issue")
        if issue:
            return self._ref(issue)
        errors = self._errors(out)
        if errors and "not found" not in errors.lower():
            raise TransientTicketError(f"linear issue: {errors[:200]}")
        return None

    def create(self, tenant: str, candidate: str, event: GateEvent) -> TicketRef:
        variables = {
            "input": {
                "id": stable_uuid(tenant, candidate),
                "teamId": self.cfg.team_id,
                "title": _title(event),
                "description": "\n\n".join(_lines(event)),
            }
        }
        out = self._gql(ISSUE_CREATE, variables, "linear issueCreate")
        result = (out.get("data") or {}).get("issueCreate") or {}
        if result.get("success") and result.get("issue"):
            return self._ref(result["issue"])
        found = self.find(tenant, candidate)  # an earlier attempt created it
        if found is not None:
            return found
        raise PermanentJobError(f"linear issueCreate failed: {self._errors(out)[:200]}")

    def update(self, tenant: str, ref: TicketRef, event: GateEvent) -> None:
        cid = stable_uuid(tenant, "comment", str(event.data["decision_id"]))
        variables = {
            "input": {"id": cid, "issueId": ref.issue_id, "body": "\n\n".join(_lines(event))}
        }
        out = self._gql(COMMENT_CREATE, variables, "linear commentCreate")
        result = (out.get("data") or {}).get("commentCreate") or {}
        if result.get("success"):
            return
        errors = self._errors(out)
        if "already exists" in errors.lower() or "duplicate" in errors.lower():
            return  # this decision's comment was created by an earlier attempt
        raise PermanentJobError(f"linear commentCreate failed: {errors[:200]}")


def connector_for(
    provider: str, settings: IntegrationSettings, runtime: Runtime, tenant: str
) -> Connector:
    try:
        if provider == "jira" and settings.jira is not None:
            return JiraConnector(
                settings.jira, runtime.secret(settings.jira.api_token_env, tenant), runtime
            )
        if provider == "linear" and settings.linear is not None:
            return LinearConnector(
                settings.linear, runtime.secret(settings.linear.api_key_env, tenant), runtime
            )
    except SecretError as exc:
        raise PermanentJobError(str(exc)) from None
    raise PermanentJobError(f"{provider} is not configured for tenant {tenant}")


def configured(settings: IntegrationSettings) -> tuple[str, ...]:
    return tuple(p for p in PROVIDERS if getattr(settings, p) is not None)


def sync(
    storage: object,
    runtime: Runtime,
    provider: str,
    event_id: str,
    *,
    clock: Clock | None = None,
    connector: Connector | None = None,
) -> dict[str, Any]:
    """Bring one decision to the tracker. Safe to run any number of times."""
    clock = clock or SystemClock()
    store = IntegrationStore(storage).ensure()
    event = store.event(event_id)
    if event is None or event.type != "decision.recorded":
        raise PermanentJobError(f"no decision event {event_id}")
    decision = str(event.data["decision_id"])
    if store.ticket_updated(provider, decision):
        return {"provider": provider, "decision_id": decision, "skipped": True}
    tenant, candidate = store.tenant, candidate_of(event)
    conn = connector or connector_for(provider, store.settings(), runtime, tenant)
    now = clock.now()
    try:
        row = store.ticket(provider, candidate)
        created = False
        if row is None or row["issue_id"] is None:
            mine = store.claim_ticket(provider, candidate, now) or store.reclaim_ticket(
                provider, candidate, now - STALE_CLAIM, now
            )
            if not mine:
                raise TransientTicketError(f"{candidate}: another job is creating its issue")
            try:
                ref = conn.find(tenant, candidate) if row is not None else None
                if ref is None:
                    ref = conn.create(tenant, candidate, event)
                    created = True
                else:
                    conn.update(tenant, ref, event)
            except BaseException:
                store.release_ticket(provider, candidate)
                raise
            store.complete_ticket(provider, candidate, ref.issue_id, ref.issue_key, ref.url)
        else:
            ref = TicketRef(issue_id=row["issue_id"], issue_key=row["issue_key"], url=row["url"])
            conn.update(tenant, ref, event)
    except UnsafeURLError as exc:
        raise PermanentJobError(f"{provider} URL refused: {exc}") from None
    except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        raise TransientTicketError(f"{provider}: {type(exc).__name__}: {exc}") from None
    store.mark_ticket_updated(provider, decision, candidate, now)
    return {
        "provider": provider,
        "decision_id": decision,
        "issue": ref.issue_key,
        "url": ref.url,
        "created": created,
    }


__all__ = [
    "MAX_ATTEMPTS",
    "PROVIDERS",
    "TICKET_KIND",
    "Connector",
    "JiraConnector",
    "LinearConnector",
    "TicketRef",
    "TransientTicketError",
    "configured",
    "connector_for",
    "marker",
    "stable_uuid",
    "sync",
]
