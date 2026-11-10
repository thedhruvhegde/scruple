"""Permission placeholder for the console. A STUB, to be swapped at merge.

Every console route declares the permission it needs with one dependency::

    @router.get("/approvals")
    def inbox(principal: Annotated[Principal, requires("read")]) -> HTMLResponse: ...

`requires` is the single swap point. Real authentication (API keys, OIDC) and RBAC are
being added elsewhere; at merge, replace the body of `requires` (or `_resolve`) with
the real check so that it raises 401/403 and returns the authenticated principal. The
route code does not change.

What the stub does today, and nothing more:

- it never refuses a request (every principal holds every permission);
- identity is the `X-Actor` header, trusted as-is, exactly like the HTTP API stub
  (`scruple.api.app.actor_header`). A request without the header is anonymous
  (`authenticated=False`); the console then asks the person to type who they are
  acting as on approval forms, and the service, not the console, decides whether that
  name may approve (named approvers, separation of duties).

Permissions used by the console: `read` (every page), `approve` (approve, reject and
request approval) and `export` (evidence pack and report downloads).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, HTTPException, Request

from scruple.registry.models import ACTOR_PATTERN

Permission = Literal["read", "approve", "export"]
PERMISSIONS: tuple[Permission, ...] = ("read", "approve", "export")
PERMISSION_ATTR = "__scruple_permission__"
_ACTOR = re.compile(ACTOR_PATTERN)


@dataclass(frozen=True)
class Principal:
    """Who is asking. `name` is None for an anonymous request."""

    name: str | None
    permissions: frozenset[str]
    authenticated: bool


def _resolve(request: Request, permission: Permission) -> Principal:
    """STUB: trust X-Actor, grant everything. Replace with the real check at merge."""
    raw = request.headers.get("x-actor")
    if raw is None:
        return Principal(name=None, permissions=frozenset(PERMISSIONS), authenticated=False)
    if len(raw) > 128 or not _ACTOR.match(raw):
        raise HTTPException(400, "malformed X-Actor header")
    return Principal(name=raw, permissions=frozenset(PERMISSIONS), authenticated=True)


def requires(permission: Permission) -> Any:
    """A FastAPI dependency that checks `permission` and returns the `Principal`."""
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown permission {permission!r}")

    def dependency(request: Request) -> Principal:
        return _resolve(request, permission)

    setattr(dependency, PERMISSION_ATTR, permission)
    return Depends(dependency)


def declared_permission(fn: Callable[..., Any]) -> str | None:
    """The permission a dependency created by `requires` checks (for tests)."""
    value = getattr(fn, PERMISSION_ATTR, None)
    return value if isinstance(value, str) else None


__all__ = [
    "PERMISSIONS",
    "Permission",
    "Principal",
    "declared_permission",
    "requires",
]
