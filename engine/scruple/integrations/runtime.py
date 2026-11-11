"""What the integrations need from the deployment: HTTP, keys, and the environment.

`Runtime.from_env()` reads (all optional):

    SCRUPLE_FIELD_KEYS              seals webhook signing secrets at rest (AES-256-GCM)
    SCRUPLE_APPROVAL_LINK_KEYS      kid:base64url,... (>= 32 bytes each) for approval links
    SCRUPLE_INTEGRATIONS_ALLOW_HOSTS  comma separated hosts exempt from the address check
    SCRUPLE_INTEGRATIONS_DEV        "1": allow http:// targets and unsealed webhook
                                    secrets (local development only)

and tenant secrets (`SCRUPLE_SECRET_<TENANT>_...`, `scruple.integrations.config`).
Tests build a `Runtime` directly with an `httpx.MockTransport` and a fake resolver.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from scruple.ingest.keys import cipher_from_env, parse_key_ring
from scruple.integrations.config import resolve_secret
from scruple.integrations.safehttp import Resolver, SafeHttp, system_resolver
from scruple.integrations.signing import LinkSigner
from scruple.security.crypto import FieldCipher

LINK_KEYS_ENV = "SCRUPLE_APPROVAL_LINK_KEYS"
ALLOW_HOSTS_ENV = "SCRUPLE_INTEGRATIONS_ALLOW_HOSTS"
DEV_ENV = "SCRUPLE_INTEGRATIONS_DEV"
PLAIN = "plain:"


class SealError(RuntimeError):
    """A webhook secret cannot be sealed or opened with the configured keys."""


@dataclass
class Runtime:
    env: Mapping[str, str] = field(default_factory=dict)
    http: SafeHttp = field(default_factory=SafeHttp)
    cipher: FieldCipher | None = None
    links: LinkSigner | None = None
    dev: bool = False  # http targets and unsealed secrets allowed

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> Runtime:
        env = dict(os.environ if env is None else env)
        dev = env.get(DEV_ENV, "") == "1"
        links = None
        raw = env.get(LINK_KEYS_ENV, "").strip()
        if raw:
            keys, active = parse_key_ring(raw, LINK_KEYS_ENV)
            links = LinkSigner(keys, active)
        hosts = tuple(
            h.strip().lower() for h in env.get(ALLOW_HOSTS_ENV, "").split(",") if h.strip()
        )
        http = SafeHttp(transport=transport, resolver=resolver, allow_http=dev, allow_hosts=hosts)
        return cls(env=env, http=http, cipher=cipher_from_env(env), links=links, dev=dev)

    def secret(self, name: str, tenant: str) -> str:
        return resolve_secret(name, tenant, self.env)

    def seal(self, secret: str, context: str) -> str:
        if self.cipher is not None:
            return self.cipher.encrypt(secret, context)
        if self.dev:
            return PLAIN + secret
        raise SealError(
            "webhook secrets are stored sealed: set SCRUPLE_FIELD_KEYS "
            "(or SCRUPLE_INTEGRATIONS_DEV=1 for local development)"
        )

    def open(self, sealed: str, context: str) -> str:
        if sealed.startswith(PLAIN):
            if not self.dev:
                raise SealError("an unsealed webhook secret is only usable in development mode")
            return sealed[len(PLAIN) :]
        if self.cipher is None:
            raise SealError("SCRUPLE_FIELD_KEYS is needed to open webhook secrets")
        return self.cipher.decrypt_str(sealed, context)


__all__ = ["DEV_ENV", "LINK_KEYS_ENV", "Runtime", "SealError"]
