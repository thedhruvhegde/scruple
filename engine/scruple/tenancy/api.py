"""GET /tenant: the caller's own tenant record, quota and today's gate runs.

Needs `admin`, like `/usage`: quota and usage are tenant administration. A caller
sees only its own tenant; listing, creating and offboarding tenants is the
operator's command line (`scruple tenant`), never an API call, because no
credential belongs to more than one tenant.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from scruple.auth import Permission, Principal, require
from scruple.tenancy.directory import TenantDirectory, TenantRecord, gate_runs_today

Admin = Annotated[Principal, require(Permission.ADMIN)]


class TenantOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant: TenantRecord
    gate_runs_today: int  # the `gate_run` meter for the current UTC day


def tenancy_router() -> APIRouter:
    router = APIRouter(tags=["tenancy"])

    @router.get("/tenant")
    def tenant(request: Request, principal: Admin) -> TenantOut:
        """The caller's tenant: status, quota and gate runs used today (UTC)."""
        gate = request.app.state.gates.get(principal.tenant)
        directory = TenantDirectory(gate.storage, gate.clock)
        return TenantOut(
            tenant=directory.get(principal.tenant),
            gate_runs_today=gate_runs_today(gate, gate.clock.now()),
        )

    return router


__all__ = ["TenantOut", "tenancy_router"]
