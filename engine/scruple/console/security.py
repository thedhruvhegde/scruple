"""Browser-facing protections for the console: CSRF, security headers, form parsing.

CSRF: a per-browser random id lives in an HttpOnly, SameSite=Strict cookie; every
form carries `HMAC-SHA256(secret, id)` as a hidden field. A POST is accepted only if
the cookie is present, the field matches it (constant-time compare) and, when the
browser sends an `Origin` header, the origin is this host. The secret comes from
`SCRUPLE_CONSOLE_SECRET` (environment only, like every secret here) or is random per
process, which is fine for one process and logs everyone out on restart.

Headers: a strict Content-Security-Policy with no script at all and no inline style
(the stylesheet is a file served by the console), no framing, no
referrer to other origins, no caching of pages that may hold decisions.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request, Response

SECRET_ENV = "SCRUPLE_CONSOLE_SECRET"  # noqa: S105 - the variable's name, not a secret
COOKIE = "scruple_console_sid"
FIELD = "csrf"
MAX_FORM_BYTES = 8_192

CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'none'",
        "style-src 'self'",
        "font-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def apply_headers[R: Response](response: R, *, cache: bool = False) -> R:
    for k, v in SECURITY_HEADERS.items():
        response.headers[k] = v
    response.headers["Cache-Control"] = "public, max-age=3600" if cache else "no-store"
    return response


def load_secret() -> bytes:
    raw = os.environ.get(SECRET_ENV, "")
    if raw:
        if len(raw) < 32:
            raise ValueError(f"{SECRET_ENV} must be at least 32 characters")
        return raw.encode()
    return secrets.token_bytes(32)


class Csrf:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("CSRF secret too short")
        self._secret = secret

    def token_for(self, sid: str) -> str:
        return hmac.new(self._secret, sid.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def session(request: Request) -> tuple[str, bool]:
        """The browser's id from its cookie, or a fresh one (True: set the cookie)."""
        sid = request.cookies.get(COOKIE, "")
        if len(sid) == 43 and sid.replace("-", "").replace("_", "").isalnum():
            return sid, False
        return secrets.token_urlsafe(32), True

    @staticmethod
    def set_cookie(request: Request, response: Response, sid: str) -> None:
        response.set_cookie(
            COOKIE,
            sid,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/console",
        )

    def verify(self, request: Request, token: str | None) -> None:
        """Raise 403 unless the POST carries this browser's token from this origin."""
        origin = request.headers.get("origin")
        if origin is not None and origin != "null":
            o = urlsplit(origin)
            if (o.scheme, o.netloc) != (request.url.scheme, request.url.netloc):
                raise HTTPException(403, "cross-origin form post refused")
        elif origin == "null":
            raise HTTPException(403, "cross-origin form post refused")
        sid = request.cookies.get(COOKIE)
        if not sid or not token:
            raise HTTPException(403, "missing CSRF token")
        if not hmac.compare_digest(self.token_for(sid), token):
            raise HTTPException(403, "bad CSRF token")


async def read_form(request: Request) -> dict[str, str]:
    """Parse an urlencoded form body: one value per field, size-capped, no files."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        raise HTTPException(415, "forms must be application/x-www-form-urlencoded")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_FORM_BYTES):
        raise HTTPException(413, "form too large")
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(413, "form too large")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "form is not UTF-8") from exc
    out: dict[str, str] = {}
    for k, values in parsed.items():
        if len(values) != 1:
            raise HTTPException(422, f"field {k!r} given more than once")
        out[k] = values[0]
    return out


__all__ = [
    "COOKIE",
    "CSP",
    "FIELD",
    "SECRET_ENV",
    "SECURITY_HEADERS",
    "Csrf",
    "apply_headers",
    "load_secret",
    "read_form",
]
