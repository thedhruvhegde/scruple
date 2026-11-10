"""Authentication, authorisation and tenancy for the HTTP API.

- `models`: `Principal`, `Permission`, `ApiKeyRecord`
- `keys`: API keys (scrypt-hashed, prefixed ids, revocable), `scruple keys ...`
- `authn`: headers -> principal; `middleware`: runs it before every route
- `deps`: `require(Permission.X)`, `public()`, `signed(...)`: one line per route

See docs/ARCHITECTURE.md section 8 and docs/pilot/SECURITY.md.
"""

from __future__ import annotations

from scruple.auth.deps import CurrentPrincipal, protection_of, public, require, signed
from scruple.auth.models import (
    ALL_PERMISSIONS,
    DEFAULT_TENANT,
    Permission,
    Principal,
    local_admin,
)

__all__ = [
    "ALL_PERMISSIONS",
    "DEFAULT_TENANT",
    "CurrentPrincipal",
    "Permission",
    "Principal",
    "local_admin",
    "protection_of",
    "public",
    "require",
    "signed",
]
