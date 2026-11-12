"""HTTP API. Every input is validated by Pydantic (extra fields forbidden).

Every request is authenticated by `scruple.auth.middleware.AuthMiddleware` (API
key or OIDC bearer token) and every route declares the permission it needs with
`require(Permission.X)` (or `public()` / `signed(...)`). The caller's identity,
recorded as the actor in the audit log, comes from the credential, never from a
header the caller chooses. The `X-Actor` header is trusted only when the
development-only `auth_dev_actor_header` setting is on (off by default).
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from scruple.api.health import PROBE_PATHS, health_router
from scruple.api.security_headers import SecurityHeadersMiddleware
from scruple.api.strict import reject_unknown_inputs
from scruple.auth import (
    CurrentPrincipal,
    Permission,
    Principal,
    guard_route,
    public,
    require,
)
from scruple.auth.authn import Authenticator
from scruple.auth.limits import Limits
from scruple.auth.middleware import AuthMiddleware
from scruple.console import console_router
from scruple.console.sources import ConsoleSources
from scruple.domain import RuleSpec
from scruple.gate.models import Decision, Outcome, Stage
from scruple.gate.shadow import BatchResult, LiveDecisionBatch, ShadowRecorder, ShadowSummary
from scruple.gate.shadow_imported import (
    ImportedBatchResult,
    ImportedDecisionBatch,
    ImportedObserveResult,
    ImportedOutcomeBatch,
    ImportedShadowSummary,
)
from scruple.ingest.api import ingest_router
from scruple.jobs.api import jobs_router
from scruple.jobs.queue import JobError, JobQueue, queue_for
from scruple.logging import RequestIDMiddleware
from scruple.observability import install_observability
from scruple.observability.metrics import METRICS_PATH
from scruple.registry import (
    DuplicateContentError,
    InvalidTransitionError,
    Provenance,
    RuleNotFoundError,
    RuleVersion,
)
from scruple.registry.models import ACTOR_PATTERN, RULE_ID_PATTERN
from scruple.service import AgentGate, ApprovalError, IntegrityReport
from scruple.settings import Settings, SettingsError
from scruple.storage.imported_shadow import DurableImportedShadow, imported_shadows
from scruple.version import BuildInfo, build_info, package_version


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(pattern=RULE_ID_PATTERN)
    spec: RuleSpec
    owner: str = Field(pattern=ACTOR_PATTERN)
    provenance: Provenance


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class DecisionOut(BaseModel):
    decision_id: str
    stage: Stage
    outcome: Outcome
    rule_id: str
    version: int
    summary: str
    reasons: tuple[str, ...]
    report_url: str

    @classmethod
    def of(cls, d: Decision) -> DecisionOut:
        return cls(
            decision_id=d.decision_id,
            stage=d.stage,
            outcome=d.outcome,
            rule_id=d.rule_id,
            version=d.version,
            summary=d.summary,
            reasons=d.reasons,
            report_url=f"/decisions/{d.decision_id}/report",
        )


def actor_of(principal: CurrentPrincipal) -> str:
    """The audit actor: the authenticated subject, never a caller-chosen header."""
    return principal.subject


Actor = Annotated[str, Depends(actor_of)]
READ = require(Permission.READ)
PROPOSE = require(Permission.PROPOSE)
SHADOW = require(Permission.SHADOW)
APPROVE = require(Permission.APPROVE)
ADMIN = require(Permission.ADMIN)


class WhoAmI(BaseModel):
    subject: str
    tenant: str
    method: str
    credential_id: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]

    @classmethod
    def of(cls, p: Principal) -> WhoAmI:
        return cls(
            subject=p.subject,
            tenant=p.tenant,
            method=p.method,
            credential_id=p.credential_id,
            roles=p.roles,
            permissions=tuple(sorted(x.value for x in p.permissions)),
        )


class TenantGates:
    """One `AgentGate` per tenant over the same storage, made on first use, under the
    tenant's current policy (`scruple.policy`) when it has one."""

    def __init__(self, root: AgentGate) -> None:
        from scruple.policy.apply import PolicyGates

        self._root = root
        self._gates = {root.storage.tenant: root}
        self._lock = threading.Lock()
        self._policies = PolicyGates()  # each tenant's policy file, if it has one

    def get(self, tenant: str) -> AgentGate:
        with self._lock:
            if tenant not in self._gates:
                r = self._root
                g = AgentGate(r.storage.for_tenant(tenant), r.clock, r.config)
                g.bootstrap_baseline()
                self._gates[tenant] = g
            base = self._gates[tenant]
        return self._policies.gate(base)


def tenant_gate(request: Request, principal: CurrentPrincipal) -> AgentGate:
    """The caller's tenant's gate: the only one a route ever sees."""
    gates: TenantGates = request.app.state.gates
    return gates.get(principal.tenant)


Gate = Annotated[AgentGate, Depends(tenant_gate)]

# Versions are 1-based and must fit a 64-bit SQL integer (an unbounded int reached
# the driver and came back as a 500).
VersionPath = Annotated[int, Path(ge=1, le=2**63 - 1)]

# Optional paging for list routes. Without `limit` a list route returns everything, as
# before. Lists are in insertion order and history is append-only, so offset paging
# is stable. `X-Total-Count` carries the full length either way.
MAX_PAGE = 1_000
Limit = Annotated[int | None, Query(ge=1, le=MAX_PAGE)]
Offset = Annotated[int, Query(ge=0, le=2**63 - 1)]
TOTAL_COUNT = "X-Total-Count"


def _page[T](items: list[T], response: Response, limit: int | None, offset: int) -> list[T]:
    response.headers[TOTAL_COUNT] = str(len(items))
    return items[offset:] if limit is None else items[offset : offset + limit]


def create_app(
    gate: AgentGate | None = None,
    settings: Settings | None = None,
    *,
    auth: Authenticator | None = None,
    limits: Limits | None = None,
    console_sources: ConsoleSources | None = None,
    demo: bool = False,
) -> FastAPI:
    """The API over `gate`. `auth` defaults to one built from settings (API keys in
    the gate's database; `X-Actor` trust only if `auth_dev_actor_header` is on).

    Tenancy: `gate` serves its own tenant (normally "default"). A caller from another
    tenant is served by a gate over `gate.storage.for_tenant(tenant)`, with the same
    clock and config, created on first use with the synthetic baseline policy. Every
    route reads and writes only the caller's tenant: another tenant's rules,
    decisions, reports and shadow state answer 404 as if they did not exist.

    `limits` (rate and body size per caller) defaults to the settings' values.

    `demo=True` is the hosted read-only demo (`scruple.demo.host` builds it): every
    route but a `read` one answers 403 and the console prints the demo banner. With
    the `demo` setting on, only that builder may make the app, so a server started
    some other way (gunicorn) refuses to start instead of serving a writable demo."""
    if settings is None:
        from scruple.settings import load_settings

        settings = load_settings()
    if settings.demo and not demo:
        raise SettingsError(
            "SCRUPLE_DEMO is on: start the demo with `scruple serve --demo`, which seeds "
            "synthetic data and refuses every write"
        )
    guards = [Depends(guard_route), Depends(reject_unknown_inputs)]
    if demo:
        from scruple.demo.guard import demo_guard

        guards.insert(0, Depends(demo_guard))
    app = FastAPI(
        title="agent-gate",
        version=package_version(),
        description="Change control for agent learning (synthetic demo).",
        dependencies=guards,
        # The schema lists every route: served below to authenticated readers only.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    if gate is None:
        # AGENT_GATE_DB is the pre-rename variable; SCRUPLE_DATABASE_URL replaces it.
        gate = AgentGate.open(os.environ.get("AGENT_GATE_DB") or settings.database_dsn())
        gate.bootstrap_baseline()
    root = gate
    app.state.gates = TenantGates(root)
    if auth is None:
        from scruple.auth.config import build_authenticator

        auth = build_authenticator(settings, root.storage)
    if limits is None:
        limits = Limits.from_settings(settings, storage=root.storage)
    app.add_middleware(
        AuthMiddleware,
        authenticator=auth,
        limits=limits,
        self_authenticating=(METRICS_PATH,),  # checks its own scrape token
        unlimited=PROBE_PATHS,  # orchestrator probes are never rate limited
    )
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware,
        tls_terminated=settings.tls_terminated,
        hsts_max_age_seconds=settings.hsts_max_age_seconds,
    )
    install_observability(app, root, settings)

    def _err(status: int):  # type: ignore[no-untyped-def]
        async def handler(_r: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        return handler

    app.add_exception_handler(RuleNotFoundError, _err(404))
    app.add_exception_handler(DuplicateContentError, _err(409))
    app.add_exception_handler(InvalidTransitionError, _err(409))
    app.add_exception_handler(ApprovalError, _err(403))
    # Webhooks declare `signed(...)` per route; health and readiness are public.
    app.include_router(ingest_router())
    app.include_router(health_router(root, settings))
    # HTML console at /console (scruple/console): the caller's tenant only, like the API.
    app.include_router(console_router(app.state.gates.get, console_sources, demo=demo))
    from scruple.auth.config import install_saml

    install_saml(app, settings)  # /auth/saml/* when SAML sign-on is configured
    from scruple.usage.api import usage_router

    app.include_router(usage_router())  # GET /usage, /usage/events (admin)
    # Tenant policy files (scruple.policy): read, history, and admin-only apply.
    from scruple.policy.api import policy_router

    app.include_router(policy_router(app.state.gates.get))
    with contextlib.suppress(JobError):  # a Storage the queue cannot share has no /jobs
        app.include_router(jobs_router(queue_for(root.storage, clock=root.clock)))
    # Outbound integrations (scruple.integrations): settings, webhooks, delivery log.
    from scruple.integrations.api import integrations_router

    try:
        integrations_queue: JobQueue | None = queue_for(root.storage, clock=root.clock)
    except JobError:
        integrations_queue = None
    app.include_router(integrations_router(app.state.gates.get, integrations_queue))
    # Slack interactivity and signed approval links: each verifies its own signature.
    from scruple.integrations.approvals_api import approvals_router

    app.include_router(approvals_router(app.state.gates.get, root.storage))

    if demo:
        # The link a demo visitor is given is the bare host: send them to the console.
        @app.get("/", dependencies=[public()], include_in_schema=False)
        def demo_root() -> RedirectResponse:
            return RedirectResponse("/console/", status_code=307)

    @app.get("/version", dependencies=[public()])
    def version() -> BuildInfo:
        return build_info()

    @app.get("/openapi.json", dependencies=[READ], include_in_schema=False)
    def openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/auth/whoami", dependencies=[READ])
    def whoami(principal: CurrentPrincipal) -> WhoAmI:
        return WhoAmI.of(principal)

    @app.post("/rules", status_code=201, dependencies=[PROPOSE])
    def register(body: RegisterIn, actor: Actor, g: Gate) -> RuleVersion:
        if actor != body.provenance.agent_id:
            raise HTTPException(403, "the authenticated caller must be provenance.agent_id")
        return g.propose(body.rule_id, body.spec, body.owner, body.provenance)

    @app.get("/rules", dependencies=[READ])
    def list_rules(
        g: Gate, response: Response, limit: Limit = None, offset: Offset = 0
    ) -> list[RuleVersion]:
        return _page(g.registry.all_versions(), response, limit, offset)

    @app.get("/rules/{rule_id}", dependencies=[READ])
    def rule_versions(rule_id: str, g: Gate) -> list[RuleVersion]:
        versions = g.registry.versions(rule_id)
        if not versions:
            raise HTTPException(404, f"unknown rule {rule_id}")
        return versions

    @app.post("/rules/{rule_id}/versions/{version}/evaluate", dependencies=[PROPOSE])
    def evaluate(rule_id: str, version: VersionPath, actor: Actor, g: Gate) -> list[DecisionOut]:
        return [DecisionOut.of(r.decision) for r in g.evaluate(rule_id, version, actor)]

    @app.post("/rules/{rule_id}/versions/{version}/approve", dependencies=[APPROVE])
    def approve(rule_id: str, version: VersionPath, actor: Actor, g: Gate) -> DecisionOut:
        return DecisionOut.of(g.approve(rule_id, version, actor).decision)

    @app.post("/rules/{rule_id}/rollback", dependencies=[APPROVE])
    def rollback(rule_id: str, body: RollbackIn, actor: Actor, g: Gate) -> DecisionOut:
        if not g.registry.versions(rule_id):
            raise HTTPException(404, f"unknown rule {rule_id}")
        return DecisionOut.of(g.rollback(rule_id, actor, body.reason).decision)

    @app.get("/decisions", dependencies=[READ])
    def decisions(
        g: Gate,
        response: Response,
        rule_id: str | None = None,
        limit: Limit = None,
        offset: Offset = 0,
    ) -> list[DecisionOut]:
        page = _page(g.decisions(rule_id), response, limit, offset)
        return [DecisionOut.of(r.decision) for r in page]

    @app.get("/decisions/{decision_id}", dependencies=[READ])
    def decision(decision_id: str, g: Gate) -> Decision:
        rec = g.decision(decision_id)
        if rec is None:
            raise HTTPException(404, "unknown decision")
        return rec.decision

    @app.get(
        "/decisions/{decision_id}/report", response_class=PlainTextResponse, dependencies=[READ]
    )
    def report(decision_id: str, g: Gate) -> PlainTextResponse:
        md = g.report(decision_id)
        if md is None:
            raise HTTPException(404, "unknown decision")
        return PlainTextResponse(md, media_type="text/markdown")

    # Shadow mode: live decisions are scored, never acted on and never stored.
    # Keyed by tenant too: a recorder holds one tenant's live decisions.
    shadow: dict[tuple[str, str, int], ShadowRecorder] = {}

    def _recorder(g: AgentGate, rule_id: str, version: int) -> ShadowRecorder:
        key = (g.storage.tenant, rule_id, version)
        if key not in shadow:
            shadow[key] = g.shadow_recorder(rule_id, version)
        return shadow[key]

    @app.post("/shadow/rules/{rule_id}/versions/{version}/decisions", dependencies=[SHADOW])
    def shadow_record(
        rule_id: str, version: VersionPath, body: LiveDecisionBatch, actor: Actor, g: Gate
    ) -> BatchResult:
        if any(d.agent_id != actor for d in body.decisions):
            raise HTTPException(403, "the caller must be every decision's agent_id")
        try:
            return _recorder(g, rule_id, version).record(body.decisions)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/shadow/rules/{rule_id}/versions/{version}", dependencies=[READ])
    def shadow_summary(rule_id: str, version: VersionPath, g: Gate) -> ShadowSummary:
        return _recorder(g, rule_id, version).summary()

    # Imported (real) cases: no ground truth; metrics are labelled observed or estimated.
    # Accepted batches are stored (append-only) and replayed, so a restart loses nothing.
    # One lookup per tenant: each reads and writes only its own tenant's rows.
    imported_by_tenant: dict[str, Callable[[str, int], DurableImportedShadow]] = {}
    imported_lock = threading.Lock()

    def imported(g: AgentGate, rule_id: str, version: int) -> DurableImportedShadow:
        with imported_lock:
            tenant = g.storage.tenant
            if tenant not in imported_by_tenant:
                imported_by_tenant[tenant] = imported_shadows(g, tenant)
            lookup = imported_by_tenant[tenant]
        return lookup(rule_id, version)

    @app.post(
        "/shadow/rules/{rule_id}/versions/{version}/imported/decisions", dependencies=[SHADOW]
    )
    def shadow_record_imported(
        rule_id: str, version: int, body: ImportedDecisionBatch, actor: Actor, g: Gate
    ) -> ImportedBatchResult:
        if any(d.agent_id != actor for d in body.decisions):
            raise HTTPException(403, "the caller must be every decision's agent_id")
        try:
            return imported(g, rule_id, version).record(body.decisions, actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/shadow/rules/{rule_id}/versions/{version}/imported/outcomes", dependencies=[SHADOW])
    def shadow_observe_imported(
        rule_id: str, version: int, body: ImportedOutcomeBatch, actor: Actor, g: Gate
    ) -> ImportedObserveResult:
        try:
            return imported(g, rule_id, version).observe(body.outcomes, actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/shadow/rules/{rule_id}/versions/{version}/imported", dependencies=[READ])
    def shadow_summary_imported(rule_id: str, version: int, g: Gate) -> ImportedShadowSummary:
        return imported(g, rule_id, version).summary()

    @app.get("/shadow/rules/{rule_id}/versions/{version}/imported/history", dependencies=[READ])
    def shadow_history_imported(rule_id: str, version: int, g: Gate) -> list[ImportedShadowSummary]:
        """Every stored summary, oldest first (aggregates; kept by retention)."""
        return [
            ImportedShadowSummary.model_validate_json(s.body_json)
            for s in imported(g, rule_id, version).history()
        ]

    @app.get("/audit/verify", dependencies=[READ])
    def verify(g: Gate, expected_head: str | None = None) -> IntegrityReport:
        return g.verify_integrity(expected_head)

    @app.get("/auth/keys", dependencies=[ADMIN])
    def list_keys(principal: CurrentPrincipal) -> list[dict[str, object]]:
        """The caller's tenant's API keys, without their hashes."""
        assert auth is not None
        if auth.keys is None:
            return []
        return [k.public() for k in auth.keys.list(principal.tenant)]

    return app
