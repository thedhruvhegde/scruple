"""Every HMAC the integrations use: outbound webhooks, Slack requests, approval links.

All comparisons are constant time (`hmac.compare_digest`) and every scheme bounds the
size of what it parses before it computes anything.

Outbound webhooks (scruple signs, the receiver verifies):

    Scruple-Signature: t=1700000000,v1=<hex>[,v1=<hex>]
    v1 = hex(HMAC-SHA256(secret, f"{t}.{raw body}"))

One `v1` per active secret of the endpoint, so a receiver keeps verifying while a
secret is rotated. The receiver should reject a `t` more than five minutes from its
own clock (replay) and accept any `v1` that matches one of its secrets.
`verify_webhook` is the reference verifier; docs/INTEGRATIONS.md has a standalone
snippet that the tests run against `sign_webhook`.

Slack requests (Slack signs, scruple verifies), per Slack's public documentation
"Verifying requests from Slack" (api.slack.com/authentication/verifying-requests-from-slack):

    X-Slack-Request-Timestamp: <unix seconds>
    X-Slack-Signature: v0=hex(HMAC-SHA256(signing secret, f"v0:{timestamp}:{raw body}"))

rejected when the timestamp is more than five minutes from now, as that page advises.

Approval links (scruple signs and verifies its own):

    v1.<kid>.<base64url(canonical JSON claims)>.<base64url(HMAC-SHA256)>

keyed from a ring (`SCRUPLE_APPROVAL_LINK_KEYS`, `kid:base64url,...`, first active),
over `"scruple-approval-link:v1:<kid>:<claims>"`. Rotating keeps old keys verifying
until they are removed from the ring. Claims carry their own expiry; single use is
enforced by the store (`IntegrationStore.consume_nonce`), not by the signature.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SIGNATURE_HEADER = "Scruple-Signature"
SLACK_TIMESTAMP = "X-Slack-Request-Timestamp"
SLACK_SIGNATURE = "X-Slack-Signature"
TOLERANCE_S = 300
MAX_HEADER = 4_096
MAX_TOKEN = 2_048
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_B64 = re.compile(r"^[A-Za-z0-9_-]+$")


class SignatureError(ValueError):
    """A signature is missing, malformed, stale or wrong. Says which, never a secret."""


def _key(secret: str | bytes) -> bytes:
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def _mac(secret: str | bytes, message: bytes) -> str:
    return hmac.new(_key(secret), message, hashlib.sha256).hexdigest()


# --- outbound webhooks ------------------------------------------------------------


def sign_webhook(secret: str | bytes, timestamp: int, body: bytes) -> str:
    return _mac(secret, f"{timestamp}.".encode() + body)


def signature_header(secrets: Sequence[str | bytes], timestamp: int, body: bytes) -> str:
    if not secrets:
        raise ValueError("an endpoint needs at least one signing secret")
    sigs = ",".join(f"v1={sign_webhook(s, timestamp, body)}" for s in secrets)
    return f"t={timestamp},{sigs}"


def verify_webhook(
    header: str | None,
    body: bytes,
    secrets: Sequence[str | bytes],
    now: float,
    tolerance_s: int = TOLERANCE_S,
) -> int:
    """Check a `Scruple-Signature` header; returns its timestamp."""
    if not header or len(header) > MAX_HEADER:
        raise SignatureError("missing or oversized signature header")
    ts: list[str] = []
    v1: list[str] = []
    for part in header.split(","):
        k, sep, v = part.strip().partition("=")
        if not sep:
            raise SignatureError("malformed signature header")
        if k == "t":
            ts.append(v)
        elif k == "v1":
            v1.append(v)
    if len(ts) != 1 or not ts[0].isdigit() or len(ts[0]) > 12:
        raise SignatureError("signature header needs exactly one numeric t")
    t = int(ts[0])
    if abs(now - t) > tolerance_s:
        raise SignatureError("signature timestamp outside the tolerance")
    if not v1 or len(v1) > 8 or not all(_HEX64.match(s) for s in v1):
        raise SignatureError("signature header needs 1 to 8 hex v1 signatures")
    ok = False
    for secret in secrets:
        expected = sign_webhook(secret, t, body)
        for s in v1:
            ok |= hmac.compare_digest(expected, s)
    if not ok:
        raise SignatureError("no signature matches")
    return t


# --- Slack ------------------------------------------------------------------------


def slack_signature(signing_secret: str | bytes, timestamp: str, body: bytes) -> str:
    return "v0=" + _mac(signing_secret, f"v0:{timestamp}:".encode() + body)


def verify_slack(
    signing_secrets: Sequence[str | bytes],
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float,
    tolerance_s: int = TOLERANCE_S,
) -> int:
    """Verify a request from Slack against any of `signing_secrets` (rotation)."""
    if not timestamp or not timestamp.isdigit() or len(timestamp) > 12:
        raise SignatureError("missing or malformed X-Slack-Request-Timestamp")
    t = int(timestamp)
    if abs(now - t) > tolerance_s:
        raise SignatureError("Slack request timestamp outside five minutes (replay?)")
    if not signature or not signature.startswith("v0=") or not _HEX64.match(signature[3:]):
        raise SignatureError("missing or malformed X-Slack-Signature")
    ok = False
    for secret in signing_secrets:
        ok |= hmac.compare_digest(slack_signature(secret, timestamp, body), signature)
    if not ok:
        raise SignatureError("Slack signature does not match")
    return t


# --- approval links ---------------------------------------------------------------


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    if not _B64.match(s):
        raise SignatureError("malformed link")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError):
        raise SignatureError("malformed link") from None


class LinkSigner:
    """Mint and verify approval-link tokens with a key ring (first key active)."""

    def __init__(self, keys: Mapping[str, bytes], active: str) -> None:
        if active not in keys:
            raise ValueError("the active link key must be in the ring")
        for kid, key in keys.items():
            if not _KID.match(kid):
                raise ValueError("link key ids are 1-32 of A-Z a-z 0-9 _ -")
            if len(key) < 32:
                raise ValueError("link keys must be at least 32 bytes")
        self._keys = dict(keys)
        self.active = active

    def __repr__(self) -> str:
        return f"LinkSigner(active={self.active!r}, key_ids={sorted(self._keys)!r})"

    def _sig(self, kid: str, claims: str) -> str:
        msg = f"scruple-approval-link:v1:{kid}:{claims}".encode()
        return _b64e(hmac.new(self._keys[kid], msg, hashlib.sha256).digest())

    def mint(self, claims: Mapping[str, Any]) -> str:
        body = _b64e(
            json.dumps(dict(claims), sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        return f"v1.{self.active}.{body}.{self._sig(self.active, body)}"

    def verify(self, token: str) -> tuple[str, dict[str, Any]]:
        """(key id, claims) of a genuine token. Expiry is the caller's check."""
        if len(token) > MAX_TOKEN:
            raise SignatureError("link is too long")
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "v1":
            raise SignatureError("malformed link")
        _, kid, body, sig = parts
        if kid not in self._keys:
            raise SignatureError("link was signed with a key that is no longer accepted")
        if not hmac.compare_digest(self._sig(kid, body), sig):
            raise SignatureError("link signature does not match")
        try:
            claims = json.loads(_b64d(body))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SignatureError("malformed link") from None
        if not isinstance(claims, dict):
            raise SignatureError("malformed link")
        return kid, claims


__all__ = [
    "SIGNATURE_HEADER",
    "SLACK_SIGNATURE",
    "SLACK_TIMESTAMP",
    "TOLERANCE_S",
    "LinkSigner",
    "SignatureError",
    "sign_webhook",
    "signature_header",
    "slack_signature",
    "verify_slack",
    "verify_webhook",
]
