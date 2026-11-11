"""Who is calling, and what they may do.

A `Principal` is the authenticated caller of one request: a subject (the identity
recorded as the actor in the audit log), the tenant whose data it may touch, and a
set of permissions. Every authentication method produces one: an API key, an OIDC
bearer token, the local command line, or (development only) the trusted
`X-Actor` header.

Nothing here holds a secret. `ApiKeyRecord.secret_hash` is a salted scrypt hash;
the key itself is shown once, at creation, and never stored or logged.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.registry.models import ACTOR_PATTERN

TENANT_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
DEFAULT_TENANT = "default"
KEY_ID_PATTERN = r"^scr_[0-9a-f]{16}$"


class Permission(StrEnum):
    """What a route needs. Key scopes and roles are sets of these."""

    READ = "read"  # rules, decisions, reports, shadow summaries, audit verification
    PROPOSE = "propose"  # register candidate rules and request their evaluation
    SHADOW = "shadow"  # submit live decisions and outcomes to shadow mode
    APPROVE = "approve"  # approve activation, roll back
    ADMIN = "admin"  # manage keys and tenant settings


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

AuthMethod = Literal["api_key", "oidc", "local", "dev_header", "saml"]


class Principal(BaseModel):
    """The authenticated caller. Built by an authenticator, never from request input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(pattern=ACTOR_PATTERN)
    tenant: str = Field(pattern=TENANT_PATTERN)
    permissions: frozenset[Permission]
    roles: tuple[str, ...] = ()
    method: AuthMethod
    credential_id: str = Field(min_length=1, max_length=512)
    """Key id, `issuer#subject` for a token, or a fixed label; never a secret."""

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def limit_key(self) -> str:
        """The bucket this caller's requests are counted in (rate limits)."""
        return f"{self.method}:{self.tenant}:{self.credential_id}"


def local_admin(tenant: str = DEFAULT_TENANT, subject: str = "local-admin") -> Principal:
    """The command line's principal. Whoever can run the CLI can open the database
    directly, so it holds every permission; it never authenticates over HTTP."""
    return Principal(
        subject=subject,
        tenant=tenant,
        permissions=ALL_PERMISSIONS,
        roles=("local_admin",),
        method="local",
        credential_id="local",
    )


class ApiKeyRecord(BaseModel):
    """A stored API key. `secret_hash` is `scrypt$n$r$p$salt$hash` (base64)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: str = Field(pattern=KEY_ID_PATTERN)
    tenant: str = Field(pattern=TENANT_PATTERN)
    subject: str = Field(pattern=ACTOR_PATTERN)
    roles: tuple[str, ...] = ()
    scopes: tuple[Permission, ...]
    secret_hash: str = Field(repr=False)
    created_at: datetime
    created_by: str = Field(pattern=ACTOR_PATTERN)
    description: str = Field(default="", max_length=200)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    rate_per_minute: int | None = Field(default=None, ge=1, le=1_000_000)
    burst: int | None = Field(default=None, ge=1, le=1_000_000)
    max_body_bytes: int | None = Field(default=None, ge=1, le=64 * 1024 * 1024)

    def active(self, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or now < self.expires_at

    def public(self) -> dict[str, object]:
        """Everything except the hash, for listings."""
        return self.model_dump(mode="json", exclude={"secret_hash"})
