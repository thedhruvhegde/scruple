"""Turn request headers into a `Principal`.

Credentials, in the order they are looked for:

1. ``X-API-Key: scr_...`` or ``Authorization: Bearer scr_...``: an API key
   (`scruple.auth.keys`).
2. ``Authorization: Bearer <JWT>``: an OIDC access or id token, when an issuer is
   configured (`scruple.auth.oidc`).
3. ``X-Actor: <name>``: ONLY when `dev_actor_header` is on. The header is trusted
   as-is and the caller gets every permission in the default tenant. It exists so
   the synthetic demo and local experiments run without keys; never enable it on
   a network anyone else can reach. It is off by default and logs a warning when on.

No credentials gives an anonymous request: public routes still answer, every other
route returns 401. Presented credentials that do not check out are always a 401,
on every route, with the same message whatever the reason.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from scruple.auth.keys import ApiKeyService, InvalidKeyError, looks_like_key
from scruple.auth.models import (
    ALL_PERMISSIONS,
    DEFAULT_TENANT,
    ApiKeyRecord,
    Principal,
)
from scruple.registry.models import ACTOR_PATTERN

log = logging.getLogger("scruple.auth")

DEV_WARNING = (
    "INSECURE: dev_actor_header is on. The X-Actor header is trusted as the caller's "
    "identity with every permission. Development only; never expose this server."
)


class AuthError(Exception):
    """A credential was presented and refused (HTTP 401)."""

    def __init__(self, detail: str = "invalid credentials", scheme: str = "Bearer") -> None:
        super().__init__(detail)
        self.detail = detail
        self.scheme = scheme


class TokenVerifier(Protocol):
    """A bearer-token checker (OIDC): a principal, or `AuthError`."""

    def verify(self, token: str) -> Principal: ...


@dataclass(frozen=True)
class Authenticated:
    principal: Principal
    key: ApiKeyRecord | None = None
    """The API key record when the caller used one (per-key limits read it)."""


class Authenticator:
    def __init__(
        self,
        keys: ApiKeyService | None = None,
        tokens: TokenVerifier | None = None,
        *,
        dev_actor_header: bool = False,
    ) -> None:
        self.keys = keys
        self.tokens = tokens
        self.dev_actor_header = dev_actor_header
        if dev_actor_header:
            log.warning(DEV_WARNING)

    def authenticate(self, headers: Mapping[str, str]) -> Authenticated | None:
        """`headers` is case-insensitive (Starlette `Headers`) or lower-cased keys."""
        api_key = headers.get("x-api-key")
        authz = headers.get("authorization")
        bearer: str | None = None
        if authz is not None:
            scheme, _, value = authz.strip().partition(" ")
            if scheme.lower() != "bearer" or not value.strip():
                raise AuthError("unsupported Authorization scheme; use Bearer")
            bearer = value.strip()
        if api_key is not None and bearer is not None:
            raise AuthError("send one credential: X-API-Key or Authorization, not both")
        if api_key is not None or (bearer is not None and looks_like_key(bearer)):
            return self._key(api_key if api_key is not None else bearer or "")
        if bearer is not None:
            if self.tokens is None:
                raise AuthError("bearer tokens are not configured; use an API key")
            return Authenticated(self.tokens.verify(bearer))
        if self.dev_actor_header:
            actor = headers.get("x-actor")
            if actor is not None:
                return Authenticated(self._dev(actor))
        return None

    def _key(self, token: str) -> Authenticated:
        if self.keys is None:
            raise AuthError("API keys are not configured")
        try:
            principal, rec = self.keys.authenticate(token.strip())
        except InvalidKeyError:
            raise AuthError("invalid API key") from None
        return Authenticated(principal, rec)

    @staticmethod
    def _dev(actor: str) -> Principal:
        if not re.match(ACTOR_PATTERN, actor):
            raise AuthError(f"X-Actor must match {ACTOR_PATTERN}", scheme="X-Actor")
        return Principal(
            subject=actor,
            tenant=DEFAULT_TENANT,
            permissions=ALL_PERMISSIONS,
            roles=("dev",),
            method="dev_header",
            credential_id=f"dev:{actor}",
        )
