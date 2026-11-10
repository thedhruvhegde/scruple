"""Build the request authenticator from `Settings`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
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


def build_authenticator(
    settings: Settings, storage: Storage, clock: Clock | None = None
) -> Authenticator:
    return Authenticator(
        key_service(settings, storage, clock),
        dev_actor_header=settings.auth_dev_actor_header,
    )
