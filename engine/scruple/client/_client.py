"""`scruple.Client`: a typed, synchronous client for the scruple HTTP API.

    from scruple import Client

    with Client("http://127.0.0.1:8000", actor="agent-7") as c:
        print(c.version().line())
        for rule in c.iter_rules():
            print(rule.ref, rule.status)

Responses are parsed into the server's own Pydantic models (`RuleVersion`,
`Decision`, `BuildInfo`, ...), so a field the server adds or renames shows up as a
type error here, not as a silent `KeyError` later.

Retries: idempotent requests (GET) are retried on connection errors, timeouts and
429/502/503/504, with exponential backoff (honouring `Retry-After`). Non-idempotent
requests (POST) are retried only when the connection failed before anything was
sent, so an evaluation or approval never runs twice because of a retry. Backoff has
no jitter, so behaviour is reproducible; pass `sleep` to control waiting in tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, TypeAdapter

from scruple.client.errors import APIError, TransportError, error_for
from scruple.client.models import (
    MAX_PAGE,
    TOTAL_COUNT,
    BatchResult,
    BuildInfo,
    Decision,
    DecisionOut,
    IntegrityReport,
    LiveDecision,
    LiveDecisionBatch,
    Provenance,
    RegisterIn,
    RollbackIn,
    RuleSpec,
    RuleVersion,
    ShadowSummary,
)
from scruple.logging import REQUEST_ID_HEADER

M = TypeVar("M", bound=BaseModel)
T = TypeVar("T")

DEFAULT_TIMEOUT = 30.0
IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to try a request and how long to wait between tries."""

    attempts: int = 3  # total tries, including the first
    backoff: float = 0.25  # seconds before the first retry; doubles each time
    max_backoff: float = 8.0
    statuses: frozenset[int] = frozenset({429, 502, 503, 504})

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.backoff < 0 or self.max_backoff < 0:
            raise ValueError("backoff must be non-negative")

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        """Seconds to wait after failed try number `attempt` (1-based)."""
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), self.max_backoff)
            except ValueError:
                pass  # an HTTP date or junk: fall back to the schedule
        return float(min(self.backoff * 2 ** (attempt - 1), self.max_backoff))


NO_RETRY = RetryPolicy(attempts=1)


class Client:
    """Client for one scruple server. Thread-safe as far as `httpx.Client` is."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        actor: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry: RetryPolicy | None = None,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`actor` is sent as `X-Actor` on calls that change state.

        Pass `http_client` to reuse a configured `httpx.Client` (or a Starlette
        `TestClient` for an in-process app); its base URL is then used as is and the
        client is not closed by `close()`.
        """
        self.actor = actor
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(base_url=base_url, timeout=timeout)

    # --- lifecycle --------------------------------------------------------------------

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- transport --------------------------------------------------------------------

    def _headers(self, actor: str | None, needs_actor: bool) -> dict[str, str]:
        who = actor or self.actor
        if needs_actor and not who:
            raise ValueError("this call needs an actor: pass actor= here or to Client()")
        return {"X-Actor": who} if who and needs_actor else {}

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request with the retry policy; raise `APIError` on non-2xx."""
        method = method.upper()
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            attempt += 1
            last = attempt >= self.retry.attempts
            try:
                resp = self._http.request(
                    method, path, json=json, params=clean or None, headers=headers
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Nothing reached the server: safe to retry any method.
                if last:
                    raise TransportError(f"{method} {path}: {exc}") from exc
                self._sleep(self.retry.delay(attempt))
                continue
            except httpx.TransportError as exc:
                if last or method not in IDEMPOTENT:
                    raise TransportError(f"{method} {path}: {exc}") from exc
                self._sleep(self.retry.delay(attempt))
                continue
            if resp.status_code in self.retry.statuses and method in IDEMPOTENT and not last:
                self._sleep(self.retry.delay(attempt, resp.headers.get("Retry-After")))
                continue
            if resp.is_success:
                return resp
            raise _api_error(resp, method, path)

    def _get(self, path: str, model: type[M], **params: Any) -> M:
        return model.model_validate(self.request("GET", path, params=params).json())

    def _list(self, path: str, adapter: TypeAdapter[list[T]], **params: Any) -> list[T]:
        return adapter.validate_python(self.request("GET", path, params=params).json())

    def _pages(
        self, path: str, adapter: TypeAdapter[list[T]], page_size: int, **params: Any
    ) -> Iterator[T]:
        if not 1 <= page_size <= MAX_PAGE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE}")
        offset = 0
        while True:
            resp = self.request(
                "GET", path, params={**params, "limit": page_size, "offset": offset}
            )
            page = adapter.validate_python(resp.json())
            yield from page
            offset += len(page)
            total = resp.headers.get(TOTAL_COUNT)
            done = offset >= int(total) if total is not None else len(page) < page_size
            if done or not page:
                return

    # --- meta -------------------------------------------------------------------------

    def health(self) -> bool:
        return bool(self.request("GET", "/health").json().get("status") == "ok")

    def version(self) -> BuildInfo:
        return self._get("/version", BuildInfo)

    def openapi(self) -> dict[str, Any]:
        schema: dict[str, Any] = self.request("GET", "/openapi.json").json()
        return schema

    # --- rules ------------------------------------------------------------------------

    def propose(
        self,
        rule_id: str,
        spec: RuleSpec,
        provenance: Provenance,
        *,
        owner: str | None = None,
        actor: str | None = None,
    ) -> RuleVersion:
        """Register a candidate rule version. The actor must be `provenance.agent_id`."""
        body = RegisterIn(
            rule_id=rule_id, spec=spec, owner=owner or provenance.agent_id, provenance=provenance
        )
        resp = self.request(
            "POST",
            "/rules",
            json=body.model_dump(mode="json"),
            headers=self._headers(actor or provenance.agent_id, True),
        )
        return RuleVersion.model_validate(resp.json())

    def rules(self, *, limit: int | None = None, offset: int = 0) -> list[RuleVersion]:
        return self._list("/rules", _RULES, limit=limit, offset=offset or None)

    def iter_rules(self, *, page_size: int = 100) -> Iterator[RuleVersion]:
        """Every rule version, fetched `page_size` at a time."""
        return self._pages("/rules", _RULES, page_size)

    def rule_versions(self, rule_id: str) -> list[RuleVersion]:
        return self._list(f"/rules/{_seg(rule_id)}", _RULES)

    def evaluate(
        self, rule_id: str, version: int, *, actor: str | None = None
    ) -> list[DecisionOut]:
        resp = self.request(
            "POST",
            f"/rules/{_seg(rule_id)}/versions/{int(version)}/evaluate",
            headers=self._headers(actor, True),
        )
        return _DECISIONS.validate_python(resp.json())

    def approve(self, rule_id: str, version: int, *, actor: str | None = None) -> DecisionOut:
        resp = self.request(
            "POST",
            f"/rules/{_seg(rule_id)}/versions/{int(version)}/approve",
            headers=self._headers(actor, True),
        )
        return DecisionOut.model_validate(resp.json())

    def rollback(self, rule_id: str, reason: str, *, actor: str | None = None) -> DecisionOut:
        resp = self.request(
            "POST",
            f"/rules/{_seg(rule_id)}/rollback",
            json=RollbackIn(reason=reason).model_dump(mode="json"),
            headers=self._headers(actor, True),
        )
        return DecisionOut.model_validate(resp.json())

    # --- decisions --------------------------------------------------------------------

    def decisions(
        self, rule_id: str | None = None, *, limit: int | None = None, offset: int = 0
    ) -> list[DecisionOut]:
        return self._list(
            "/decisions", _DECISIONS, rule_id=rule_id, limit=limit, offset=offset or None
        )

    def iter_decisions(
        self, rule_id: str | None = None, *, page_size: int = 100
    ) -> Iterator[DecisionOut]:
        return self._pages("/decisions", _DECISIONS, page_size, rule_id=rule_id)

    def decision(self, decision_id: str) -> Decision:
        return self._get(f"/decisions/{_seg(decision_id)}", Decision)

    def report(self, decision_id: str) -> str:
        """The evidence report for a decision, as Markdown."""
        return self.request("GET", f"/decisions/{_seg(decision_id)}/report").text

    # --- shadow mode ------------------------------------------------------------------

    def shadow_record(
        self,
        rule_id: str,
        version: int,
        decisions: list[LiveDecision],
        *,
        actor: str | None = None,
    ) -> BatchResult:
        body = LiveDecisionBatch(decisions=tuple(decisions))
        resp = self.request(
            "POST",
            f"/shadow/rules/{_seg(rule_id)}/versions/{int(version)}/decisions",
            json=body.model_dump(mode="json"),
            headers=self._headers(actor, True),
        )
        return BatchResult.model_validate(resp.json())

    def shadow_summary(self, rule_id: str, version: int) -> ShadowSummary:
        return self._get(f"/shadow/rules/{_seg(rule_id)}/versions/{int(version)}", ShadowSummary)

    # --- audit ------------------------------------------------------------------------

    def verify_integrity(self, expected_head: str | None = None) -> IntegrityReport:
        return self._get("/audit/verify", IntegrityReport, expected_head=expected_head)


_RULES: TypeAdapter[list[RuleVersion]] = TypeAdapter(list[RuleVersion])
_DECISIONS: TypeAdapter[list[DecisionOut]] = TypeAdapter(list[DecisionOut])


def _seg(value: str) -> str:
    """One URL path segment; refuses values that would change the path."""
    if not value or "/" in value or value in {".", ".."}:
        raise ValueError(f"not a valid path segment: {value!r}")
    return quote(value, safe="")


def _api_error(resp: httpx.Response, method: str, path: str) -> APIError:
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
    except ValueError:
        detail = resp.text
    return error_for(resp.status_code)(
        resp.status_code,
        detail,
        method=method,
        path=path,
        request_id=resp.headers.get(REQUEST_ID_HEADER),
    )
