"""OIDC bearer tokens: verify a JWT against the issuer's JWKS (PyJWT).

What is checked, on every token:

- the header names an allowed algorithm (RS256 or ES256 only; `none`, HMAC and
  anything else are refused before any key is looked up, so a public key can
  never be used as an HMAC secret) and a `kid`;
- the signature, with the JWKS key of that `kid`, whose type must fit the
  algorithm;
- `iss` equals the configured issuer, `aud` contains the configured audience,
  `exp` is in the future and `nbf` (if present) in the past, each with
  `leeway_seconds` of clock skew; `exp`, `iat`, `iss`, `aud` and `sub` must be present;
- the subject claim is a valid actor name and the tenant claim a valid tenant id.

Key rotation: the JWKS is cached for `jwks_cache_seconds`. A token whose `kid`
is not in the cached set triggers one refetch, at most once per
`jwks_min_refresh_seconds`, so an attacker sending random `kid`s cannot make the
server hammer the issuer. Keys removed from the JWKS stop verifying after the
next refresh.

Permissions come from the token's roles claim and its space-separated `scope`
claim, combined as for API keys (`scruple.auth.rbac`). The JWKS is fetched with an
injectable `httpx` client, so tests use a local fake endpoint and make no network
calls.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, Literal

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.auth.authn import AuthError
from scruple.auth.models import Permission, Principal
from scruple.auth.rbac import SeparationOfDutiesError, check_separation, permissions_for

log = logging.getLogger("scruple.auth.oidc")

Algorithm = Literal["RS256", "ES256"]
_KTY = {"RS256": "RSA", "ES256": "EC"}
MAX_TOKEN_BYTES = 8192
MAX_JWKS_BYTES = 256 * 1024


class OIDCConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    jwks_url: str = Field(pattern=r"^https?://")
    algorithms: tuple[Algorithm, ...] = Field(default=("RS256", "ES256"), min_length=1)
    leeway_seconds: int = Field(default=60, ge=0, le=600)
    subject_claim: str = "sub"
    tenant_claim: str = "scruple_tenant"
    default_tenant: str | None = Field(
        default=None, description="used when a token has no tenant claim; None refuses it"
    )
    roles_claim: str = "scruple_roles"
    scope_claim: str = "scope"
    jwks_cache_seconds: float = Field(default=600.0, ge=0)
    jwks_min_refresh_seconds: float = Field(default=30.0, ge=0)


PermissionsFor = Callable[[Iterable[str], Iterable[str]], frozenset[Permission]]


def token_permissions(roles: Iterable[str], scopes: Iterable[str]) -> frozenset[Permission]:
    """Roles claim and scope claim, combined as for API keys (`scruple.auth.rbac`):
    unknown names grant nothing, scopes narrow roles, duties stay separated."""
    granted = permissions_for(roles, scopes)
    check_separation(granted)
    return granted


class JWKSCache:
    """The issuer's signing keys by `kid`, refreshed on expiry or an unknown `kid`."""

    def __init__(
        self,
        url: str,
        client: httpx.Client | None = None,
        *,
        algorithms: Iterable[str] = ("RS256", "ES256"),
        cache_seconds: float = 600.0,
        min_refresh_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._client = client or httpx.Client(timeout=5.0, follow_redirects=False)
        self._algorithms = set(algorithms)
        self._cache_seconds = cache_seconds
        self._min_refresh = min_refresh_seconds
        self._monotonic = monotonic
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()
        self.fetches = 0

    def key(self, kid: str, alg: str) -> jwt.PyJWK:
        with self._lock:
            now = self._monotonic()
            stale = self._fetched_at is None or now - self._fetched_at > self._cache_seconds
            may_refresh = self._fetched_at is None or now - self._fetched_at >= self._min_refresh
            if stale or (kid not in self._keys and may_refresh):
                self._refresh(now)
            k = self._keys.get(kid)
        if k is None:
            raise AuthError("invalid bearer token")
        if k.key_type != _KTY[alg] or k.algorithm_name != alg:
            raise AuthError("invalid bearer token")
        return k

    def _refresh(self, now: float) -> None:
        self.fetches += 1
        try:
            r = self._client.get(self._url)
            r.raise_for_status()
            if len(r.content) > MAX_JWKS_BYTES:
                raise ValueError("JWKS too large")
            data = r.json()
            keys: dict[str, jwt.PyJWK] = {}
            for jwk in data.get("keys", []):
                if not isinstance(jwk, dict) or "kid" not in jwk:
                    continue
                if jwk.get("use", "sig") != "sig":
                    continue
                alg = jwk.get("alg")
                if alg is not None and alg not in self._algorithms:
                    continue
                try:
                    keys[str(jwk["kid"])] = jwt.PyJWK(jwk)
                except jwt.PyJWTError:
                    continue
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.warning("JWKS fetch failed: %s", type(exc).__name__)
            # Keep serving the keys we had; try again after min_refresh.
            self._fetched_at = now if self._keys else None
            return
        self._keys = keys
        self._fetched_at = now


class OIDCVerifier:
    def __init__(
        self,
        config: OIDCConfig,
        jwks: JWKSCache | None = None,
        *,
        permissions_for: PermissionsFor = token_permissions,
    ) -> None:
        self.config = config
        self.jwks = jwks or JWKSCache(
            config.jwks_url,
            algorithms=config.algorithms,
            cache_seconds=config.jwks_cache_seconds,
            min_refresh_seconds=config.jwks_min_refresh_seconds,
        )
        self._permissions_for = permissions_for

    def verify(self, token: str) -> Principal:
        claims = self.claims(token)
        c = self.config
        roles = _strings(claims.get(c.roles_claim))
        scope = claims.get(c.scope_claim)
        scopes = scope.split() if isinstance(scope, str) else _strings(scope)
        try:
            permissions = self._permissions_for(roles, scopes)
        except SeparationOfDutiesError as exc:
            raise AuthError(str(exc)) from None
        tenant = claims.get(c.tenant_claim, c.default_tenant)
        if not isinstance(tenant, str):
            raise AuthError("bearer token subject or tenant is not acceptable")
        try:
            return Principal(
                subject=claims[c.subject_claim],
                tenant=tenant,
                permissions=permissions,
                roles=tuple(sorted(set(roles))),
                method="oidc",
                credential_id=f"{claims['iss']}#{claims['sub']}"[:512],
            )
        except (KeyError, ValidationError):
            raise AuthError("bearer token subject or tenant is not acceptable") from None

    def claims(self, token: str) -> dict[str, Any]:
        """The verified claims, or `AuthError` (one message for every reason)."""
        c = self.config
        if len(token) > MAX_TOKEN_BYTES:
            raise AuthError("invalid bearer token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("invalid bearer token") from None
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in c.algorithms or not isinstance(kid, str):
            raise AuthError("invalid bearer token")
        key = self.jwks.key(kid, alg)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=c.audience,
                issuer=c.issuer,
                leeway=c.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            log.debug("bearer token refused: %s", type(exc).__name__)
            raise AuthError("invalid bearer token") from None
        return claims


def _strings(v: object) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list | tuple):
        return [x for x in v if isinstance(x, str)]
    return []
