"""Browser sessions after SAML single sign-on: a signed, expiring cookie.

After the SAML assertion consumer service has verified an assertion
(`scruple.auth.saml`), the browser gets one cookie, `scruple_session`:

    base64url(json claims) "." base64url(HMAC-SHA256(key, base64url(json claims)))

The claims are the subject, tenant and roles the assertion mapped to, the issue and
expiry times, a random session id and the IdP entity id. Permissions are not stored:
they are derived from the roles on every request (`scruple.auth.rbac`), so a change
to the role table applies at once, and separation of duties is checked again.

Scope: the cookie is accepted only on console paths (`/console/...`), where every
POST also needs the console's CSRF token. The JSON API never accepts it, so a
browser that is signed in cannot be driven into API writes by another site. The
cookie is HttpOnly, SameSite=Lax (the IdP's POST to the ACS is cross-site; Lax
still keeps the cookie off cross-site POSTs), and Secure on https.

The key is derived from `SCRUPLE_CONSOLE_SECRET` with a fixed label, so it is not the
CSRF key. Sessions are stateless: one ends at its expiry (default 8 hours) or when
the secret changes; signing out clears the cookie in that browser. ASSUMPTION: a
pilot accepts stateless sessions with this lifetime; a server-side revocation list
is not built.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.auth.models import TENANT_PATTERN, Principal
from scruple.auth.rbac import SeparationOfDutiesError, check_separation, permissions_for
from scruple.registry.models import ACTOR_PATTERN

COOKIE = "scruple_session"
SCOPE_PREFIX = "/console"
MAX_COOKIE_BYTES = 3_500
_LABEL = b"scruple-session-v1"


class SessionClaims(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sub: str = Field(pattern=ACTOR_PATTERN)
    tenant: str = Field(pattern=TENANT_PATTERN)
    roles: tuple[str, ...] = Field(default=(), max_length=20)
    iat: int
    exp: int
    sid: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")
    idp: str = Field(max_length=512)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def in_scope(path: str | None) -> bool:
    return path is not None and (path == SCOPE_PREFIX or path.startswith(SCOPE_PREFIX + "/"))


class SessionSigner:
    """Issue and check session cookies. `now` is injectable for tests."""

    def __init__(
        self,
        secret: bytes,
        *,
        ttl_seconds: int = 8 * 3600,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if len(secret) < 32:
            raise ValueError("the session secret must be at least 32 bytes")
        self._key = hmac.new(secret, _LABEL, hashlib.sha256).digest()
        self.ttl_seconds = ttl_seconds
        self._now = now

    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())

    def issue(self, subject: str, tenant: str, roles: tuple[str, ...], idp: str) -> str:
        now = int(self._now().timestamp())
        claims = SessionClaims(
            sub=subject,
            tenant=tenant,
            roles=tuple(sorted(set(roles))),
            iat=now,
            exp=now + self.ttl_seconds,
            sid=secrets.token_urlsafe(18),
            idp=idp,
        )
        body = _b64(claims.model_dump_json().encode())
        token = f"{body}.{self._mac(body)}"
        if len(token) > MAX_COOKIE_BYTES:
            raise ValueError("session too large for a cookie")
        return token

    def claims(self, token: str) -> SessionClaims | None:
        """The claims of a valid, unexpired token; None for anything else."""
        if len(token) > MAX_COOKIE_BYTES or token.count(".") != 1:
            return None
        body, mac = token.split(".")
        if not hmac.compare_digest(self._mac(body), mac):
            return None
        try:
            data: Any = json.loads(_unb64(body))
            claims = SessionClaims.model_validate(data)
        except (ValueError, ValidationError):
            return None
        now = int(self._now().timestamp())
        if not (claims.iat <= now + 60 and now < claims.exp):
            return None
        return claims

    def principal(self, token: str) -> Principal | None:
        claims = self.claims(token)
        if claims is None:
            return None
        granted = permissions_for(claims.roles, ())
        try:
            check_separation(granted)
        except SeparationOfDutiesError:
            return None
        if not granted:
            return None
        return Principal(
            subject=claims.sub,
            tenant=claims.tenant,
            permissions=granted,
            roles=claims.roles,
            method="saml",
            credential_id=f"saml:{claims.sid}",
        )


def cookie_value(headers: Mapping[str, str]) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel is not None else None


__all__ = ["COOKIE", "SCOPE_PREFIX", "SessionClaims", "SessionSigner", "cookie_value", "in_scope"]
