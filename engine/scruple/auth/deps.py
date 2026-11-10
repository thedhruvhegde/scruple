"""Route protection: declare what each route needs, in one line.

    from scruple.auth import Permission, require

    @router.get("/console/rules", dependencies=[require(Permission.READ)])
    def rules(): ...

    # or for a whole router
    router = APIRouter(prefix="/console", dependencies=[require(Permission.READ)])

    # or to receive the caller as well
    @router.post("/things")
    def make(p: Annotated[Principal, require(Permission.PROPOSE)]): ...

`public()` marks a route that needs no credentials (health, version) and
`signed("stripe")` one that authenticates the request itself (a webhook HMAC).
`protection_of(route)` reads the declaration back, which is how the test that
enumerates every route, and the app-wide guard, find routes that declare nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from scruple.auth.models import Permission, Principal

STATE_KEY = "scruple_principal"
Kind = Literal["permission", "public", "signed"]


def principal_of(request: Request) -> Principal | None:
    state = request.scope.get("state") or {}
    p = state.get(STATE_KEY)
    return p if isinstance(p, Principal) else None


def _unauthenticated() -> HTTPException:
    return HTTPException(
        401,
        "authentication required: send an API key (X-API-Key) or a bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


@dataclass(frozen=True)
class Requirement:
    """A route's declared protection. Callable as a FastAPI dependency."""

    kind: Kind
    permission: Permission | None = None
    scheme: str | None = None

    def __call__(self, request: Request) -> Principal | None:
        if self.kind != "permission":
            return principal_of(request)
        p = principal_of(request)
        if p is None:
            raise _unauthenticated()
        assert self.permission is not None
        if not p.can(self.permission):
            raise HTTPException(403, f"permission {self.permission.value!r} required")
        return p


# The markers return FastAPI's `Depends(...)`, typed `Any` by FastAPI itself, so
# they fit both `dependencies=[...]` and `Annotated[Principal, require(...)]`.


def require(permission: Permission) -> Any:
    """`Depends` marker: the caller must hold `permission`. Resolves to the principal."""
    return Depends(Requirement("permission", permission))


def public() -> Any:
    """`Depends` marker: no credentials needed. Only for routes that reveal nothing."""
    return Depends(Requirement("public"))


def signed(scheme: str) -> Any:
    """`Depends` marker: the route verifies a request signature itself (webhooks)."""
    return Depends(Requirement("signed", scheme=scheme))


def current_principal(request: Request) -> Principal:
    p = principal_of(request)
    if p is None:
        raise _unauthenticated()
    return p


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def _walk(dep: Dependant) -> list[Dependant]:
    out = [dep]
    for sub in dep.dependencies:
        out.extend(_walk(sub))
    return out


def protection_of(route: APIRoute) -> list[Requirement]:
    """Every protection the route declares (directly, on its router or on the app)."""
    return [d.call for d in _walk(route.dependant) if isinstance(d.call, Requirement)]
