"""HTTP routes for usage, mounted by `scruple.api.app` with one line.

    GET /usage          totals per meter per period (`period=day|month`, `start`, `end`)
    GET /usage/events   the raw, append-only events (`start`, `end`, `limit`, `offset`)

Both need `admin`: usage is what a tenant is billed on, which is tenant
administration, not review. Each answers for the caller's tenant only, and syncs
that tenant's decisions first, so the answer includes every decision made so far.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BeforeValidator

from scruple.auth import Permission, Principal, require
from scruple.usage.meter import Period, UsageAggregate, UsageEvent, UsageStore, usage_store_for

Admin = Annotated[Principal, require(Permission.ADMIN)]


def _iso_day(v: object) -> object:
    """Only YYYY-MM-DD: pydantic would otherwise read "0" as a Unix timestamp."""
    if v is None or isinstance(v, date):
        return v
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    raise ValueError("expected a date as YYYY-MM-DD")


Day = Annotated[date | None, BeforeValidator(_iso_day), Query()]
MAX_PAGE = 1_000


def _store(request: Request, principal: Principal) -> UsageStore:
    gate = request.app.state.gates.get(principal.tenant)
    store = usage_store_for(gate.storage, gate.clock)
    if store is None:  # pragma: no cover - every backend here is SQL
        raise HTTPException(404, "this database keeps no usage events")
    store.sync(gate)
    return store


def usage_router() -> APIRouter:
    router = APIRouter(prefix="/usage", tags=["usage"])

    @router.get("")
    def totals(
        request: Request,
        principal: Admin,
        period: Period = "month",
        start: Day = None,
        end: Day = None,
    ) -> list[UsageAggregate]:
        """Usage totals for the caller's tenant, oldest period first (UTC)."""
        return _store(request, principal).aggregate(principal.tenant, period, start, end)

    @router.get("/events")
    def events(
        request: Request,
        principal: Admin,
        response: Response,
        start: Day = None,
        end: Day = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100,
        offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    ) -> list[UsageEvent]:
        """The caller's tenant's usage events, oldest first. `X-Total-Count` has the total."""

        def at(d: date | None) -> datetime | None:
            return datetime(d.year, d.month, d.day, tzinfo=UTC) if d else None

        rows = _store(request, principal).events(principal.tenant, at(start), at(end))
        response.headers["X-Total-Count"] = str(len(rows))
        return rows[offset : offset + limit]

    return router


__all__ = ["usage_router"]
