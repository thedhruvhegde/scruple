"""Field-level encryption with key ids and rotation (AES-256-GCM via `cryptography`).

Envelope (a plain string, safe to store in any text column):

    enc:v1:<key id>:<base64url nonce>:<base64url ciphertext and 16-byte tag>

- AES-256-GCM with a fresh 96-bit random nonce per encryption. With random nonces,
  NIST SP 800-38D (section 8.3) limits one key to 2**32 encryptions; rotate well before.
- The associated data binds the envelope version, the key id and a caller `context`
  (for example "case:stripe:dp_123:evidence_note"), so a ciphertext copied to another
  field or record, or relabelled with another key id, fails authentication.
- Rotation: `rotate` decrypts with whichever configured key the envelope names and
  re-encrypts with the active key; `rotate_record` does it for chosen fields of a dict.
  Removing a key from the ring makes its envelopes undecryptable (crypto-shredding).
- Keys are 32 bytes, never in `repr` or error messages. The nonce source is injectable
  only so tests can pin vectors; production uses `os.urandom`.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeGuard

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scruple.ingest.errors import IngestError, InputTooLargeError

VERSION = "v1"
PREFIX = f"enc:{VERSION}:"
NONCE_BYTES = 12
TAG_BYTES = 16
MAX_PLAINTEXT = 1024 * 1024
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_B64 = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")


class CryptoError(IngestError):
    """Base class for envelope problems."""


class MalformedEnvelopeError(CryptoError):
    pass


class UnknownKeyError(CryptoError):
    pass


class TamperedError(CryptoError):
    """Authentication failed: wrong key, wrong context, or modified bytes."""


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str, what: str) -> bytes:
    if not _B64.match(s) or len(s) > 2 * MAX_PLAINTEXT:
        raise MalformedEnvelopeError(f"{what} is not base64url")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError):
        raise MalformedEnvelopeError(f"{what} is not base64url") from None


def _aad(key_id: str, context: str) -> bytes:
    return f"scruple:{VERSION}:{key_id}:{context}".encode()


def is_envelope(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and value.startswith(PREFIX)


class FieldCipher:
    def __init__(
        self,
        keys: Mapping[str, bytes],
        active: str,
        *,
        nonce_source: Callable[[int], bytes] = os.urandom,
    ) -> None:
        if active not in keys:
            raise ValueError("active key id is not among the keys")
        for kid, key in keys.items():
            if not _KEY_ID.match(kid):
                raise ValueError("key ids are 1-32 letters, digits, '_' or '-'")
            if len(key) != 32:
                raise ValueError("AES-256-GCM keys must be exactly 32 bytes")
        self._aead = {kid: AESGCM(key) for kid, key in keys.items()}
        self._active = active
        self._nonce = nonce_source

    @property
    def active_key_id(self) -> str:
        return self._active

    def __repr__(self) -> str:
        return f"FieldCipher(active={self._active!r}, key_ids={sorted(self._aead)!r})"

    def encrypt(self, plaintext: str | bytes, context: str) -> str:
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        if len(data) > MAX_PLAINTEXT:
            raise InputTooLargeError(f"plaintext over {MAX_PLAINTEXT} bytes")
        nonce = self._nonce(NONCE_BYTES)
        if len(nonce) != NONCE_BYTES:
            raise CryptoError("nonce source returned the wrong length")
        ct = self._aead[self._active].encrypt(nonce, data, _aad(self._active, context))
        return f"{PREFIX}{self._active}:{_b64e(nonce)}:{_b64e(ct)}"

    def key_id_of(self, envelope: str) -> str:
        return self._parse(envelope)[0]

    def _parse(self, envelope: str) -> tuple[str, bytes, bytes]:
        if not isinstance(envelope, str) or not envelope.startswith(PREFIX):
            raise MalformedEnvelopeError(f"not an {PREFIX} envelope")
        parts = envelope[len(PREFIX) :].split(":")
        if len(parts) != 3:
            raise MalformedEnvelopeError("envelope needs key id, nonce and ciphertext")
        kid, n64, c64 = parts
        if not _KEY_ID.match(kid):
            raise MalformedEnvelopeError("bad key id in envelope")
        nonce = _b64d(n64, "nonce")
        ct = _b64d(c64, "ciphertext")
        if len(nonce) != NONCE_BYTES or len(ct) < TAG_BYTES:
            raise MalformedEnvelopeError("nonce or ciphertext has the wrong length")
        return kid, nonce, ct

    def decrypt(self, envelope: str, context: str) -> bytes:
        kid, nonce, ct = self._parse(envelope)
        aead = self._aead.get(kid)
        if aead is None:
            raise UnknownKeyError(f"key {kid!r} is not in the key ring")
        try:
            return aead.decrypt(nonce, ct, _aad(kid, context))
        except InvalidTag:
            raise TamperedError("envelope failed authentication") from None

    def decrypt_str(self, envelope: str, context: str) -> str:
        try:
            return self.decrypt(envelope, context).decode("utf-8")
        except UnicodeDecodeError:
            raise CryptoError("plaintext is not UTF-8") from None

    def rotate(self, envelope: str, context: str) -> str:
        """Re-encrypt under the active key (always a fresh nonce)."""
        return self.encrypt(self.decrypt(envelope, context), context)

    def encrypt_fields(
        self, record: Mapping[str, Any], fields: Iterable[str], context: Callable[[str], str]
    ) -> dict[str, Any]:
        """Copy of `record` with the named string fields encrypted (None stays None)."""
        out = dict(record)
        for f in fields:
            v = out.get(f)
            if v is None or is_envelope(v):
                continue
            if not isinstance(v, str):
                raise CryptoError(f"field {f!r} is not a string")
            out[f] = self.encrypt(v, context(f))
        return out

    def rotate_record(
        self, record: Mapping[str, Any], fields: Iterable[str], context: Callable[[str], str]
    ) -> tuple[dict[str, Any], int]:
        """Re-encrypt every named envelope not already under the active key."""
        out = dict(record)
        changed = 0
        for f in fields:
            v = out.get(f)
            if is_envelope(v) and self.key_id_of(v) != self._active:
                out[f] = self.rotate(v, context(f))
                changed += 1
        return out, changed
