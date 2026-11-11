"""Console permissions, enforced by the engine's real authentication and RBAC.

Every console route declares the permission it needs with one dependency::

    @router.get("/approvals")
    def inbox(principal: Annotated[Principal, requires("read")]) -> HTMLResponse: ...

`requires` wraps `scruple.auth.require`: the request has already been authenticated by
`scruple.auth.middleware.AuthMiddleware` (API key, OIDC bearer token, or the
development-only `X-Actor` trust), and the route answers 401 without credentials and
403 without the permission, exactly like the JSON API. Because the auth requirement is
a sub-dependency, the app-wide fail-closed guard and the route-enumeration test see
each console route's declared permission.

The console's three permissions map onto the engine's (`scruple.auth.Permission`):

    console   engine     why
    read      read       every page
    approve   approve    approve, reject and request approval
    export    read       evidence packs and report downloads: the same data the
                         read-only JSON API returns (`/decisions/{id}/report`), so an
                         auditor (read only) can take evidence away
    admin     admin      save and delete the tenant's shared saved filters: they
                         change what every reader of the tenant is offered, so
                         they are tenant configuration (read-only stays read-only)

The principal is always authenticated and names its tenant; the console serves that
tenant's gate only. The service, not the console, still decides whether that subject
may approve (named approvers, separation of duties).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends

from scruple.auth import Permission as AuthPermission
from scruple.auth import Principal as AuthPrincipal
from scruple.auth import require

Permission = Literal["read", "approve", "export", "admin"]
PERMISSIONS: tuple[Permission, ...] = ("read", "approve", "export", "admin")
PERMISSION_ATTR = "__scruple_permission__"
ENGINE_PERMISSION: dict[Permission, AuthPermission] = {
    "read": AuthPermission.READ,
    "approve": AuthPermission.APPROVE,
    "export": AuthPermission.READ,
    "admin": AuthPermission.ADMIN,
}


@dataclass(frozen=True)
class Principal:
    """Who is asking, as the console sees it. Built only from an authenticated
    `scruple.auth.Principal`."""

    name: str
    tenant: str
    permissions: frozenset[str]


def _console_permissions(p: AuthPrincipal) -> frozenset[str]:
    return frozenset(c for c in PERMISSIONS if p.can(ENGINE_PERMISSION[c]))


def requires(permission: Permission) -> Any:
    """A FastAPI dependency that checks `permission` and returns the `Principal`."""
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}")
    # A default, not `Annotated[...]`: string annotations cannot see this closure.
    check = require(ENGINE_PERMISSION[permission])

    def dependency(p: AuthPrincipal = check) -> Principal:
        return Principal(name=p.subject, tenant=p.tenant, permissions=_console_permissions(p))

    setattr(dependency, PERMISSION_ATTR, permission)
    return Depends(dependency)


def declared_permission(fn: Callable[..., Any]) -> str | None:
    """The console permission a dependency created by `requires` checks (for tests)."""
    value = getattr(fn, PERMISSION_ATTR, None)
    return value if isinstance(value, str) else None


__all__ = [
    "ENGINE_PERMISSION",
    "PERMISSIONS",
    "Permission",
    "Principal",
    "declared_permission",
    "requires",
]
