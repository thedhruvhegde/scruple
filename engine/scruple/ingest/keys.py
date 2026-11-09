"""Ingest keys from the environment: card tokenisation, field encryption, Adyen HMAC.

Secrets never come from files or command-line flags (flags end up in shell history and
process listings), the same rule as `scruple.settings`. Each variable is a comma
separated key ring; the first entry is the active key, the rest are kept so values
written under an older key can still be read or matched:

- `SCRUPLE_TOKEN_KEYS`: `kid:base64url-key,...`, keys of at least 32 bytes
  (`scruple.security.pan.PanTokenizer`).
- `SCRUPLE_FIELD_KEYS`: `kid:base64url-key,...`, keys of exactly 32 bytes
  (`scruple.security.crypto.FieldCipher`, AES-256-GCM).
- `SCRUPLE_ADYEN_HMAC_KEYS`: `hexkey,...` (Adyen's own key format).

Generate a key with
`python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.
Missing variables mean "not configured": ingest then refuses inputs that need the key
(a card-number column without a tokenisation key, an encrypted column without a field
key) instead of storing the value some other way.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Mapping

from scruple.ingest.errors import IngestError
from scruple.security.crypto import FieldCipher
from scruple.security.pan import PanTokenizer

TOKEN_KEYS_ENV = "SCRUPLE_TOKEN_KEYS"  # noqa: S105 - a variable name
FIELD_KEYS_ENV = "SCRUPLE_FIELD_KEYS"
ADYEN_HMAC_KEYS_ENV = "SCRUPLE_ADYEN_HMAC_KEYS"
_KID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyConfigError(IngestError):
    """A key variable is set but malformed. The message never contains key material."""


def parse_key_ring(raw: str, what: str) -> tuple[dict[str, bytes], str]:
    """`kid:base64url,...` -> (keys, active key id). The first entry is active."""
    keys: dict[str, bytes] = {}
    active = ""
    for i, part in enumerate(p.strip() for p in raw.split(",")):
        if not part:
            continue
        kid, sep, b64 = part.partition(":")
        if not sep or not _KID.match(kid):
            raise KeyConfigError(f"{what}: entry {i + 1} must be kid:base64url-key")
        if kid in keys:
            raise KeyConfigError(f"{what}: key id {kid!r} appears twice")
        try:
            key = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        except (binascii.Error, ValueError):
            raise KeyConfigError(f"{what}: key {kid!r} is not base64url") from None
        keys[kid] = key
        active = active or kid
    if not keys:
        raise KeyConfigError(f"{what} is set but holds no key")
    return keys, active


def tokenizer_from_env(env: Mapping[str, str] | None = None) -> PanTokenizer | None:
    raw = (os.environ if env is None else env).get(TOKEN_KEYS_ENV, "").strip()
    if not raw:
        return None
    keys, active = parse_key_ring(raw, TOKEN_KEYS_ENV)
    try:
        return PanTokenizer(keys, active)
    except ValueError as e:
        raise KeyConfigError(f"{TOKEN_KEYS_ENV}: {e}") from None


def cipher_from_env(env: Mapping[str, str] | None = None) -> FieldCipher | None:
    raw = (os.environ if env is None else env).get(FIELD_KEYS_ENV, "").strip()
    if not raw:
        return None
    keys, active = parse_key_ring(raw, FIELD_KEYS_ENV)
    try:
        return FieldCipher(keys, active)
    except ValueError as e:
        raise KeyConfigError(f"{FIELD_KEYS_ENV}: {e}") from None


def adyen_keys_from_env(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    raw = (os.environ if env is None else env).get(ADYEN_HMAC_KEYS_ENV, "")
    return tuple(k.strip() for k in raw.split(",") if k.strip())


__all__ = [
    "ADYEN_HMAC_KEYS_ENV",
    "FIELD_KEYS_ENV",
    "TOKEN_KEYS_ENV",
    "KeyConfigError",
    "adyen_keys_from_env",
    "cipher_from_env",
    "parse_key_ring",
    "tokenizer_from_env",
]
