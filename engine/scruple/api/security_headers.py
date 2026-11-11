"""Security headers on every HTTP response (pure ASGI middleware).

The HTML surfaces (the console, signed approval pages) set their own, stricter
page policies (`scruple.console.security`); this layer fills in what a response
did not set, so the JSON API, probes, errors from the auth middleware and plain
text endpoints carry them too:

- `X-Content-Type-Options: nosniff`: a JSON body is never sniffed into HTML.
- `Referrer-Policy: no-referrer`.
- `X-Frame-Options: DENY` and a `Content-Security-Policy` that allows nothing
  (`default-src 'none'; frame-ancestors 'none'`): a JSON response has no business
  loading anything or being framed. Responses that set their own policy keep it.
- `Cache-Control: no-store` when the response did not choose a cache policy, and
  always on `/auth/*` (who am I, keys, SAML sign-on): nothing that carries a
  principal or a decision is kept by a shared cache.
- `Strict-Transport-Security` only when the API is served over TLS: the
  `tls_terminated` setting (TLS ends at a proxy in front of the API) or a request
  whose scheme is already https. Browsers ignore it over plain HTTP (RFC 6797,
  section 7.2), and sending it from a plain-HTTP development server is a footgun.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

JSON_CSP = "default-src 'none'; frame-ancestors 'none'"
AUTH_PREFIX = "/auth/"

DEFAULTS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", JSON_CSP),
    ("Cache-Control", "no-store"),
)


def hsts_value(max_age_seconds: int) -> str:
    return f"max-age={max_age_seconds}; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(
        self, app: ASGIApp, *, tls_terminated: bool = False, hsts_max_age_seconds: int = 31_536_000
    ) -> None:
        self.app = app
        self.tls_terminated = tls_terminated
        self.hsts = hsts_value(hsts_max_age_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        auth_route = str(scope.get("path", "")).startswith(AUTH_PREFIX)
        tls = self.tls_terminated or scope.get("scheme") == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in DEFAULTS:
                    if name not in headers:
                        headers[name] = value
                if auth_route:
                    headers["Cache-Control"] = "no-store"
                if tls and "Strict-Transport-Security" not in headers:
                    headers["Strict-Transport-Security"] = self.hsts
            await send(message)

        await self.app(scope, receive, send_with_headers)
