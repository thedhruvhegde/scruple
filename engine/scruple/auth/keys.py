"""API keys: random secrets, stored only as salted scrypt hashes.

A key looks like ``scr_<16 hex>_<43 url-safe chars>``. The first two parts are the
key id: public, the primary key of the stored record, safe to log and show. The
rest is 256 bits from `secrets`, shown once at creation and never stored.

Lookup is by id, then the presented secret is hashed with the stored salt and
parameters and compared with `hmac.compare_digest`. An unknown id, a revoked key
and an expired key all cost one scrypt too, so response time does not reveal which
ids exist. A successful check is cached in memory for `cache_seconds`, keyed by the
SHA-256 of the whole key, so steady traffic does not pay scrypt on every request;
the record itself is re-read on every request, so a revocation takes effect at
once.

`last_used_at` is written at most once per `touch_seconds` per key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import SecretStr

from scruple.audit.log import AuditLog
from scruple.auth.models import (
    DEFAULT_TENANT,
    ApiKeyRecord,
    Permission,
    Principal,
)
from scruple.clock import Clock, SystemClock

if TYPE_CHECKING:
    from scruple.storage import Storage

TOKEN_RE = re.compile(r"^(scr_[0-9a-f]{16})_([A-Za-z0-9_-]{43})$")
EVENT_KEY_CREATED = "auth.key_created"
EVENT_KEY_REVOKED = "auth.key_revoked"


class UnknownKeyError(LookupError):
    """No such key in this tenant."""


class InvalidKeyError(Exception):
    """The presented key is malformed, unknown, revoked or expired.

    The message is deliberately the same for every case."""

    def __init__(self) -> None:
        super().__init__("invalid API key")


@dataclass(frozen=True)
class ScryptParams:
    """scrypt cost. The default (n=2**14, r=8, p=1, 16 MiB) is the interactive-login
    figure from the scrypt paper; keys are 256-bit random secrets, so the hash
    guards against a leaked table, not against guessing. Tests use n=2**10."""

    n: int = 2**14
    r: int = 8
    p: int = 1

    def __post_init__(self) -> None:
        if self.n < 2 or self.n & (self.n - 1):
            raise ValueError("scrypt n must be a power of two")
        if not (1 <= self.r <= 32 and 1 <= self.p <= 16):
            raise ValueError("scrypt r or p out of range")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _scrypt(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        secret.encode("ascii"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * r * n + 2**20
    )


def hash_secret(secret: str, params: ScryptParams, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = _scrypt(secret, salt, params.n, params.r, params.p)
    return f"scrypt${params.n}${params.r}${params.p}${_b64(salt)}${_b64(digest)}"


def verify_secret(secret: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, digest = encoded.split("$")
        if algo != "scrypt":
            return False
        got = _scrypt(secret, _unb64(salt), int(n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(got, _unb64(digest))


def new_token() -> tuple[str, str]:
    """(key id, full key)."""
    key_id = f"scr_{secrets.token_hex(8)}"
    return key_id, f"{key_id}_{secrets.token_urlsafe(32)}"


def parse_token(token: str) -> tuple[str, str] | None:
    m = TOKEN_RE.match(token)
    return (m.group(1), m.group(2)) if m else None


def looks_like_key(token: str) -> bool:
    return token.startswith("scr_")


PermissionsFor = Callable[[ApiKeyRecord], frozenset[Permission]]


def scope_permissions(rec: ApiKeyRecord) -> frozenset[Permission]:
    return frozenset(rec.scopes)


class ApiKeyService:
    """Create, list, revoke and check API keys stored in `storage`.

    Key events (created, revoked) are appended to the key's tenant audit chain.
    """

    def __init__(
        self,
        storage: Storage,
        clock: Clock | None = None,
        params: ScryptParams | None = None,
        *,
        cache_seconds: float = 300.0,
        cache_size: int = 10_000,
        touch_seconds: float = 60.0,
        permissions_for: PermissionsFor = scope_permissions,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._storage = storage
        self._clock = clock or SystemClock()
        self._params = params or ScryptParams()
        self._cache: OrderedDict[bytes, tuple[str, float]] = OrderedDict()
        self._cache_seconds = cache_seconds
        self._cache_size = cache_size
        self._touch_seconds = touch_seconds
        self._permissions_for = permissions_for
        self._monotonic = monotonic
        self._lock = threading.Lock()
        # Hash of a random secret: what an unknown id is checked against.
        self._decoy = hash_secret(secrets.token_urlsafe(32), self._params)

    # --- management (CLI, admin) ---------------------------------------------------

    def create(
        self,
        subject: str,
        scopes: Iterable[Permission],
        created_by: str,
        *,
        tenant: str = DEFAULT_TENANT,
        roles: Iterable[str] = (),
        description: str = "",
        expires_at: datetime | None = None,
        rate_per_minute: int | None = None,
        burst: int | None = None,
        max_body_bytes: int | None = None,
    ) -> tuple[ApiKeyRecord, SecretStr]:
        key_id, token = new_token()
        _, secret = parse_token(token) or ("", "")
        rec = ApiKeyRecord(
            key_id=key_id,
            tenant=tenant,
            subject=subject,
            roles=tuple(sorted(set(roles))),
            scopes=tuple(sorted(set(scopes))),
            secret_hash=hash_secret(secret, self._params),
            created_at=self._clock.now(),
            created_by=created_by,
            description=description,
            expires_at=expires_at,
            rate_per_minute=rate_per_minute,
            burst=burst,
            max_body_bytes=max_body_bytes,
        )
        if not self._permissions_for(rec):
            raise ValueError("a key needs at least one scope or role")
        store = self._tenant_store(tenant)
        with store.transaction():
            store.insert_api_key(rec)
            AuditLog(store, self._clock).append(
                EVENT_KEY_CREATED,
                created_by,
                {
                    "key_id": rec.key_id,
                    "subject": rec.subject,
                    "roles": list(rec.roles),
                    "scopes": [s.value for s in rec.scopes],
                    "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
                },
            )
        return rec, SecretStr(token)

    def list(self, tenant: str = DEFAULT_TENANT) -> list[ApiKeyRecord]:
        return self._storage.list_api_keys(tenant)

    def revoke(self, key_id: str, revoked_by: str, tenant: str = DEFAULT_TENANT) -> ApiKeyRecord:
        rec = self._storage.get_api_key(key_id)
        if rec is None or rec.tenant != tenant:
            raise UnknownKeyError(f"no key {key_id} in tenant {tenant}")
        if rec.revoked_at is not None:
            return rec
        now = self._clock.now()
        store = self._tenant_store(tenant)
        with store.transaction():
            store.revoke_api_key(key_id, now)
            AuditLog(store, self._clock).append(
                EVENT_KEY_REVOKED, revoked_by, {"key_id": key_id, "subject": rec.subject}
            )
        with self._lock:
            for k in [k for k, (kid, _) in self._cache.items() if kid == key_id]:
                del self._cache[k]
        out = self._storage.get_api_key(key_id)
        assert out is not None
        return out

    # --- authentication (every request) --------------------------------------------

    def authenticate(self, token: str) -> tuple[Principal, ApiKeyRecord]:
        """The principal for a presented key, or `InvalidKeyError`."""
        parsed = parse_token(token)
        if parsed is None:
            verify_secret("x" * 43, self._decoy)  # same cost as a real check
            raise InvalidKeyError
        key_id, secret = parsed
        rec = self._storage.get_api_key(key_id)
        fingerprint = hashlib.sha256(token.encode("ascii")).digest()
        if not self._cached(fingerprint, key_id):
            ok = verify_secret(secret, rec.secret_hash if rec else self._decoy)
            if not ok or rec is None:
                raise InvalidKeyError
            self._remember(fingerprint, key_id)
        assert rec is not None  # a cache hit implies the record existed
        now = self._clock.now()
        if not rec.active(now):
            raise InvalidKeyError
        try:
            permissions = self._permissions_for(rec)
        except ValueError:  # e.g. separation of duties: refuse, never widen
            raise InvalidKeyError from None
        if not permissions:
            raise InvalidKeyError
        self._touch(rec, now)
        principal = Principal(
            subject=rec.subject,
            tenant=rec.tenant,
            permissions=permissions,
            roles=rec.roles,
            method="api_key",
            credential_id=rec.key_id,
        )
        return principal, rec

    # --- internals -------------------------------------------------------------------

    def _tenant_store(self, tenant: str) -> Storage:
        for_tenant = getattr(self._storage, "for_tenant", None)
        if for_tenant is None:
            return self._storage
        store: Storage = for_tenant(tenant)
        return store

    def _cached(self, fingerprint: bytes, key_id: str) -> bool:
        with self._lock:
            hit = self._cache.get(fingerprint)
            if hit is None:
                return False
            kid, at = hit
            if kid != key_id or self._monotonic() - at > self._cache_seconds:
                del self._cache[fingerprint]
                return False
            self._cache.move_to_end(fingerprint)
            return True

    def _remember(self, fingerprint: bytes, key_id: str) -> None:
        with self._lock:
            self._cache[fingerprint] = (key_id, self._monotonic())
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def _touch(self, rec: ApiKeyRecord, now: datetime) -> None:
        last = rec.last_used_at
        if last is None or (now - last).total_seconds() >= self._touch_seconds:
            self._storage.touch_api_key(rec.key_id, now)
