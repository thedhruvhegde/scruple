"""HTTP API. Every input is validated by Pydantic (extra fields forbidden).

Authentication is a STUB: the caller's identity is the `X-Actor` header, which is
trusted as-is. Replace it with real authn (mTLS/OIDC) before any non-demo use.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from scruple.domain import RuleSpec
from scruple.gate.models import Decision, Outcome, Stage
from scruple.gate.shadow import BatchResult, LiveDecisionBatch, ShadowRecorder, ShadowSummary
from scruple.registry import (
    DuplicateContentError,
    InvalidTransitionError,
    Provenance,
    RuleNotFoundError,
    RuleVersion,
)
from scruple.registry.models import ACTOR_PATTERN, RULE_ID_PATTERN
from scruple.service import AgentGate, ApprovalError, IntegrityReport


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


def actor_header(
    x_actor: Annotated[str, Header(pattern=ACTOR_PATTERN, max_length=128)],
) -> str:
    return x_actor


Actor = Annotated[str, Depends(actor_header)]


def create_app(gate: AgentGate | None = None) -> FastAPI:
    app = FastAPI(
        title="agent-gate",
        version="0.1.0",
        description="Change control for agent learning (synthetic demo).",
    )
    if gate is None:
        gate = AgentGate.open(os.environ.get("AGENT_GATE_DB", "agent-gate.db"))
        gate.bootstrap_baseline()
    g = gate

    def _err(status: int):  # type: ignore[no-untyped-def]
        async def handler(_r: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        return handler

    app.add_exception_handler(RuleNotFoundError, _err(404))
    app.add_exception_handler(DuplicateContentError, _err(409))
    app.add_exception_handler(InvalidTransitionError, _err(409))
    app.add_exception_handler(ApprovalError, _err(403))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/rules", status_code=201)
    def register(body: RegisterIn, actor: Actor) -> RuleVersion:
        if actor != body.provenance.agent_id:
            raise HTTPException(403, "X-Actor must match provenance.agent_id")
        return g.propose(body.rule_id, body.spec, body.owner, body.provenance)

    @app.get("/rules")
    def list_rules() -> list[RuleVersion]:
        return g.registry.all_versions()

    @app.get("/rules/{rule_id}")
    def rule_versions(rule_id: str) -> list[RuleVersion]:
        versions = g.registry.versions(rule_id)
        if not versions:
            raise HTTPException(404, f"unknown rule {rule_id}")
        return versions

    @app.post("/rules/{rule_id}/versions/{version}/evaluate")
    def evaluate(rule_id: str, version: int, actor: Actor) -> list[DecisionOut]:
        return [DecisionOut.of(r.decision) for r in g.evaluate(rule_id, version, actor)]

    @app.post("/rules/{rule_id}/versions/{version}/approve")
    def approve(rule_id: str, version: int, actor: Actor) -> DecisionOut:
        return DecisionOut.of(g.approve(rule_id, version, actor).decision)

    @app.post("/rules/{rule_id}/rollback")
    def rollback(rule_id: str, body: RollbackIn, actor: Actor) -> DecisionOut:
        return DecisionOut.of(g.rollback(rule_id, actor, body.reason).decision)

    @app.get("/decisions")
    def decisions(rule_id: str | None = None) -> list[DecisionOut]:
        return [DecisionOut.of(r.decision) for r in g.decisions(rule_id)]

    @app.get("/decisions/{decision_id}")
    def decision(decision_id: str) -> Decision:
        rec = g.decision(decision_id)
        if rec is None:
            raise HTTPException(404, "unknown decision")
        return rec.decision

    @app.get("/decisions/{decision_id}/report", response_class=PlainTextResponse)
    def report(decision_id: str) -> PlainTextResponse:
        md = g.report(decision_id)
        if md is None:
            raise HTTPException(404, "unknown decision")
        return PlainTextResponse(md, media_type="text/markdown")

    # Shadow mode: live decisions are scored, never acted on and never stored.
    shadow: dict[tuple[str, int], ShadowRecorder] = {}

    def _recorder(rule_id: str, version: int) -> ShadowRecorder:
        key = (rule_id, version)
        if key not in shadow:
            shadow[key] = g.shadow_recorder(rule_id, version)
        return shadow[key]

    @app.post("/shadow/rules/{rule_id}/versions/{version}/decisions")
    def shadow_record(
        rule_id: str, version: int, body: LiveDecisionBatch, actor: Actor
    ) -> BatchResult:
        if any(d.agent_id != actor for d in body.decisions):
            raise HTTPException(403, "X-Actor must match every decision's agent_id")
        try:
            return _recorder(rule_id, version).record(body.decisions)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/shadow/rules/{rule_id}/versions/{version}")
    def shadow_summary(rule_id: str, version: int) -> ShadowSummary:
        return _recorder(rule_id, version).summary()

    @app.get("/audit/verify")
    def verify(expected_head: str | None = None) -> IntegrityReport:
        return g.verify_integrity(expected_head)

    return app
