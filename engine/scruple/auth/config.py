"""Build the request authenticator from `Settings`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.oidc import OIDCConfig, OIDCVerifier
from scruple.clock import Clock

if TYPE_CHECKING:
    from scruple.settings import Settings
    from scruple.storage import Storage


def key_service(settings: Settings, storage: Storage, clock: Clock | None = None) -> ApiKeyService:
    return ApiKeyService(
        storage,
        clock,
        ScryptParams(n=settings.auth_scrypt_n),
        cache_seconds=settings.auth_key_cache_seconds,
    )


def oidc_verifier(settings: Settings) -> OIDCVerifier | None:
    """An OIDC verifier when issuer, audience and JWKS URL are all set; else None.
    Setting only some of them is an error, not a silent fallback to keys only."""
    parts = (settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url)
    if not any(parts):
        return None
    if not all(parts):
        raise ValueError("OIDC needs oidc_issuer, oidc_audience and oidc_jwks_url together")
    cfg = OIDCConfig.model_validate(
        {
            "issuer": settings.oidc_issuer,
            "audience": settings.oidc_audience,
            "jwks_url": settings.oidc_jwks_url,
            "algorithms": tuple(a.strip() for a in settings.oidc_algorithms.split(",") if a),
            "leeway_seconds": settings.oidc_leeway_seconds,
            "subject_claim": settings.oidc_subject_claim,
            "tenant_claim": settings.oidc_tenant_claim,
            "default_tenant": settings.oidc_default_tenant,
            "roles_claim": settings.oidc_roles_claim,
        }
    )
    return OIDCVerifier(cfg)


def build_authenticator(
    settings: Settings, storage: Storage, clock: Clock | None = None
) -> Authenticator:
    return Authenticator(
        key_service(settings, storage, clock),
        oidc_verifier(settings),
        dev_actor_header=settings.auth_dev_actor_header,
    )
