"""`/policy`: the caller's tenant's policy file.

    GET /policy            read    the policy in force (404 without one)
    GET /policy/history    read    every version, oldest first
    POST /policy           admin   put a policy in force (JSON body, validated, audited)

`POST` appends a new version (history is append-only); posting the policy already in
force changes nothing. A policy with an approval section is also subject to
`AgentGate.set_approval_policy`: the authenticated subject must be a gate operator
(`GateConfig.approvers`), else 403.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException

from scruple.auth import CurrentPrincipal, Permission, require
from scruple.policy.apply import apply_policy
from scruple.policy.models import Policy
from scruple.policy.store import PolicyRecord, PolicyStore
from scruple.service import AgentGate


def _out(rec: PolicyRecord) -> dict[str, Any]:
    return {
        "number": rec.number,
        "policy_sha256": rec.policy_sha256,
        "actor": rec.actor,
        "created_at": rec.created_at.isoformat(),
        "policy": rec.policy.model_dump(mode="json"),
    }


def policy_router(gate_for: Callable[[str], AgentGate]) -> APIRouter:
    router = APIRouter(prefix="/policy", tags=["policy"])

    @router.get("", dependencies=[require(Permission.READ)])
    def current(principal: CurrentPrincipal) -> dict[str, Any]:
        rec = PolicyStore(gate_for(principal.tenant).storage).current()
        if rec is None:
            raise HTTPException(404, "this tenant has no policy in force")
        return _out(rec)

    @router.get("/history", dependencies=[require(Permission.READ)])
    def history(principal: CurrentPrincipal) -> list[dict[str, Any]]:
        return [_out(r) for r in PolicyStore(gate_for(principal.tenant).storage).history()]

    @router.post("", dependencies=[require(Permission.ADMIN)])
    def apply(body: Policy, principal: CurrentPrincipal) -> dict[str, Any]:
        g = gate_for(principal.tenant)
        try:
            body.gate_config(g.config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        rec, changed = apply_policy(g, body, principal.subject)
        return {**_out(rec), "changed": changed}

    return router


__all__ = ["policy_router"]
