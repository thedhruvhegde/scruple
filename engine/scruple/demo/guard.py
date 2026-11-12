"""Who a demo visitor is, and what the demo refuses.

Every request to the hosted demo is served as one fixed principal, `VISITOR`: tenant
`demo`, permission `read` only, whatever credentials it sends (they are never
checked, so a demo never runs scrypt for a stranger and never answers 401). On top of
the visitor's role, two checks refuse everything that is not a read:

1. `DemoHost` (`scruple.demo.host`) answers 403 to every method but GET and HEAD
   before the request reaches authentication or reads a body.
2. `demo_guard`, an app-wide dependency, answers 403 to a GET route that needs more
   than `read` or checks its own signature (metrics scrape, signed approval links,
   key listing, usage events), so nothing beyond reading the seeded tenant is open.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException, Request

from scruple.auth.authn import Authenticated, Authenticator
from scruple.auth.deps import effective_route, protection_of
from scruple.auth.models import Permission, Principal
from scruple.console.demo_seed import DEMO_TENANT
from scruple.demo import BANNER

READ_METHODS = frozenset({"GET", "HEAD"})
READ_ONLY = (
    "the hosted demo is read only: writes are refused (approvals, rollbacks, "
    "proposals, shadow and ingest batches, webhooks, jobs and key creation are off)"
)
CLOSED = "the hosted demo is read only: this route needs more than read access and is off"

VISITOR = Principal(
    subject="demo-visitor",
    tenant=DEMO_TENANT,
    permissions=frozenset({Permission.READ}),
    roles=("auditor",),
    method="demo",
    credential_id="demo",
)


class DemoAuthenticator(Authenticator):
    """Every request is the visitor. No keys, tokens or sessions are configured."""

    def __init__(self) -> None:
        super().__init__()

    def authenticate(
        self, headers: Mapping[str, str], path: str | None = None
    ) -> Authenticated | None:
        return Authenticated(VISITOR)


def demo_guard(request: Request) -> None:
    """App-wide dependency in demo mode, first in line: reads of `read` routes only."""
    if request.method not in READ_METHODS:
        raise HTTPException(403, READ_ONLY)
    route = effective_route(request)
    if route is None:  # pragma: no cover - mounts and plain routes
        return
    for req in protection_of(route):
        if req.kind == "signed" or (
            req.kind == "permission" and req.permission is not Permission.READ
        ):
            raise HTTPException(403, CLOSED)


__all__ = [
    "BANNER",
    "CLOSED",
    "READ_METHODS",
    "READ_ONLY",
    "VISITOR",
    "DemoAuthenticator",
    "demo_guard",
]
