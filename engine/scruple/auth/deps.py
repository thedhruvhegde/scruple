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
enumerates every route, and the app-wide guard `guard_route`, find routes
that declare nothing: such a route answers 403 until it declares something.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi import routing as fastapi_routing
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


def protection_of(route: Any) -> list[Requirement]:
    """Every protection a route declares, directly, on its router, on an
    `include_router(..., dependencies=...)` or on the app. `route` is an `APIRoute`
    or a FastAPI route context (anything with a `dependant`)."""
    dependant = getattr(route, "dependant", None)
    if not isinstance(dependant, Dependant):
        return []
    return [d.call for d in _walk(dependant) if isinstance(d.call, Requirement)]


def iter_routes(app: FastAPI) -> Iterator[Any]:
    """Every HTTP route of `app` with its effective dependencies, routers included.

    FastAPI 0.16x resolves `include_router` lazily, so `app.routes` holds the
    included router, not its routes; `iter_route_contexts` flattens them."""
    contexts = getattr(fastapi_routing, "iter_route_contexts", None)
    items = contexts(app.routes) if contexts is not None else app.routes
    for item in items:
        if isinstance(getattr(item, "dependant", None), Dependant):
            yield item


def effective_route(request: Request) -> Any:
    """The route (or FastAPI route context) a request matched, or None."""
    ctx = (request.scope.get("fastapi") or {}).get("effective_route_context")
    if ctx is not None and isinstance(getattr(ctx, "dependant", None), Dependant):
        return ctx
    route = request.scope.get("route")
    return route if isinstance(route, APIRoute) else None


def guard_route(request: Request) -> None:
    """App-wide dependency, first in line: enforce the route's declared protection
    before anything else looks at the request (so an anonymous caller learns
    nothing from input validation), and refuse a route that declares none (403).

    Fail closed: a route added without `require(...)`, `public()` or `signed(...)`
    is unreachable until it declares one, instead of being open by accident."""
    route = effective_route(request)
    if route is None:  # pragma: no cover - mounts and plain routes
        return
    declared = protection_of(route)
    if not declared:
        raise HTTPException(403, "this route declares no permission and is closed")
    for req in declared:
        req(request)
