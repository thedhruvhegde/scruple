"""Build the request authenticator from `Settings`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.oidc import OIDCConfig, OIDCVerifier
from scruple.auth.rbac import key_permissions
from scruple.clock import Clock

if TYPE_CHECKING:
    from scruple.auth.saml import SAMLServiceProvider
    from scruple.auth.session import SessionSigner
    from scruple.settings import Settings
    from scruple.storage import Storage

SECRET_ENV = "SCRUPLE_CONSOLE_SECRET"  # noqa: S105 - the variable's name, not a secret


def key_service(settings: Settings, storage: Storage, clock: Clock | None = None) -> ApiKeyService:
    return ApiKeyService(
        storage,
        clock,
        ScryptParams(n=settings.auth_scrypt_n),
        cache_seconds=settings.auth_key_cache_seconds,
        permissions_for=key_permissions,
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


def saml_setup(
    settings: Settings, secret: bytes | None = None
) -> tuple[SAMLServiceProvider, SessionSigner] | None:
    """The SAML service provider and the console session signer, when SAML is
    configured; else None. Setting only some of the five required settings is an
    error. `secret` defaults to `SCRUPLE_CONSOLE_SECRET`, which is then required:
    sessions must survive restarts and be shared by workers."""
    parts = (
        settings.saml_sp_entity_id,
        settings.saml_acs_url,
        settings.saml_idp_entity_id,
        settings.saml_idp_sso_url,
        settings.saml_idp_cert_file,
    )
    if not any(parts):
        return None
    if not all(parts):
        raise ValueError(
            "SAML needs saml_sp_entity_id, saml_acs_url, saml_idp_entity_id, "
            "saml_idp_sso_url and saml_idp_cert_file together"
        )
    from scruple.auth.saml import SAMLConfig, SAMLServiceProvider
    from scruple.auth.session import SessionSigner

    if secret is None:
        raw = os.environ.get(SECRET_ENV, "")
        if len(raw) < 32:
            raise ValueError(f"SAML sign-on needs {SECRET_ENV} (at least 32 characters)")
        secret = raw.encode()
    role_map = dict(pair.split("=", 1) for pair in settings.saml_role_map.split(",") if "=" in pair)
    cfg = SAMLConfig(
        sp_entity_id=str(settings.saml_sp_entity_id),
        acs_url=str(settings.saml_acs_url),
        idp_entity_id=str(settings.saml_idp_entity_id),
        idp_sso_url=str(settings.saml_idp_sso_url),
        idp_x509_cert=Path(str(settings.saml_idp_cert_file)).read_text(encoding="ascii"),
        subject_attribute=settings.saml_subject_attribute,
        tenant_attribute=settings.saml_tenant_attribute,
        default_tenant=settings.saml_default_tenant,
        roles_attribute=settings.saml_roles_attribute,
        role_map={k.strip(): v.strip() for k, v in role_map.items()},
    )
    return SAMLServiceProvider(cfg, secret), SessionSigner(
        secret, ttl_seconds=settings.saml_session_seconds
    )


def build_authenticator(
    settings: Settings, storage: Storage, clock: Clock | None = None
) -> Authenticator:
    from scruple.tenancy.directory import directory_for

    saml = saml_setup(settings)
    directory = directory_for(storage, clock)
    return Authenticator(
        key_service(settings, storage, clock),
        oidc_verifier(settings),
        dev_actor_header=settings.auth_dev_actor_header,
        sessions=saml[1] if saml is not None else None,
        tenant_refused=directory.is_offboarded if directory is not None else None,
    )


def install_saml(app: object, settings: Settings) -> bool:
    """Mount the SAML routes on a FastAPI app when SAML is configured."""
    saml = saml_setup(settings)
    if saml is None:
        return False
    from fastapi import FastAPI

    from scruple.auth.saml_routes import saml_router

    assert isinstance(app, FastAPI)
    app.include_router(saml_router(*saml))
    return True
