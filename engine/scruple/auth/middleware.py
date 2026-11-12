"""ASGI middleware: authenticate and limit every HTTP request before any route runs.

It reads only headers. A refused credential is answered here with 401; a valid one
puts the `Principal` in the request scope, where `scruple.auth.deps` finds it;
no credentials passes the request on anonymously, for public routes to answer and
every other route to refuse. Credentials are never logged: only the principal's
subject, tenant and credential id are attached to the request scope.

With `limits` (`scruple.auth.limits`), it also answers 429 with `Retry-After` when
the caller's token bucket is empty, and 413 when the body is over the caller's
size limit.

`self_authenticating` names exact paths whose route checks a credential of its own
in the `Authorization` header (`GET /metrics` and its scrape token, declared
`signed("metrics-token")`). Those requests are not authenticated here, so a scrape
token is never mistaken for an API key or OIDC token; they count as anonymous for
rate limits.

The hosted demo's visitor (`scruple.demo`, method "demo") is one principal shared by
everyone, so its requests are counted per client address in the anonymous bucket.

`unlimited` names exact paths that are never rate limited: the liveness and
readiness probes (`/health`, `/ready`), which an orchestrator calls from one address
on a fixed schedule and must never see 429 (it would restart a healthy pod or take it
out of service). Both are public and cheap (`/ready` caches its audit verification).
"""

from __future__ import annotations

import json
from collections.abc import Collection

import anyio
from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scruple.auth.authn import Authenticated, Authenticator, AuthError
from scruple.auth.deps import STATE_KEY
from scruple.auth.limits import Limits
from scruple.auth.ratelimit import Limit, Taken

DEV_HEADER = b"x-scruple-insecure-dev-auth"


async def send_json(
    send: Send, status: int, detail: str, headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _too_many(send: Send, taken: Taken, what: str) -> None:
    await send_json(
        send,
        429,
        f"{what} rate limit exceeded; retry after {taken.retry_after} s",
        [(b"retry-after", str(taken.retry_after).encode())],
    )


class AuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        authenticator: Authenticator,
        limits: Limits | None = None,
        self_authenticating: Collection[str] = (),
        unlimited: Collection[str] = (),
    ) -> None:
        self.app = app
        self.authn = authenticator
        self.limits = limits
        self.self_authenticating = frozenset(self_authenticating)
        self.unlimited = frozenset(unlimited)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self.authn.dev_actor_header:
            send = _flag_dev(send)
        headers = Headers(scope=scope)
        lim = self.limits
        address = (scope.get("client") or ("unknown", 0))[0]
        failures_key = f"auth-failures:{address}"
        own = scope.get("path") in self.self_authenticating
        presents = not own and ("authorization" in headers or "x-api-key" in headers)
        if lim is not None and presents:
            peek = lim.backend.peek(failures_key, lim.auth_failures)
            if not peek.allowed:
                await _too_many(send, peek, "failed authentication")
                return
        try:
            # Off the event loop: a key check costs one scrypt and a database read.
            who = (
                None
                if own
                else await anyio.to_thread.run_sync(
                    self.authn.authenticate, headers, scope.get("path")
                )
            )
        except AuthError as e:
            if lim is not None:
                lim.backend.take(failures_key, lim.auth_failures)
            await send_json(
                send, 401, e.detail, [(b"www-authenticate", e.scheme.encode("latin-1"))]
            )
            return
        if who is not None:
            state = scope.setdefault("state", {})
            state[STATE_KEY] = who.principal
            state[STATE_KEY + "_auth"] = who
        if lim is not None and scope.get("path") not in self.unlimited:
            if who is None or who.principal.method != "dev_header":
                bucket, limit = self._bucket(who, address, lim)
                taken = lim.backend.take(bucket, limit)
                if not taken.allowed:
                    await _too_many(send, taken, "request")
                    return
            max_body = (who.key.max_body_bytes if who and who.key else None) or lim.max_body_bytes
            declared = headers.get("content-length")
            if declared is not None:
                if not declared.isdigit():
                    await send_json(send, 400, "malformed Content-Length")
                    return
                if int(declared) > max_body:
                    await send_json(send, 413, f"request body over {max_body} bytes")
                    return
            receive = _capped(receive, max_body)
        await self.app(scope, receive, send)

    @staticmethod
    def _bucket(who: Authenticated | None, address: str, lim: Limits) -> tuple[str, Limit]:
        # The hosted demo's visitor is one shared principal: count it per address,
        # like an anonymous caller, so one visitor cannot use up everyone's bucket.
        if who is None or who.principal.method == "demo":
            return f"anonymous:{address}", lim.anonymous
        rec = who.key
        if rec is not None and (rec.rate_per_minute or rec.burst):
            limit = Limit(
                rec.rate_per_minute or lim.default.per_minute, rec.burst or lim.default.burst
            )
        else:
            limit = lim.default
        return who.principal.limit_key, limit


def _capped(receive: Receive, max_body: int) -> Receive:
    seen = 0

    async def wrapped() -> Message:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > max_body:
                # FastAPI re-raises HTTPException from body reading; its handler
                # renders the 413 (the response has not started yet).
                raise HTTPException(413, f"request body over {max_body} bytes")
        return message

    return wrapped


def authenticated_of(scope: Scope) -> Authenticated | None:
    who = (scope.get("state") or {}).get(STATE_KEY + "_auth")
    return who if isinstance(who, Authenticated) else None


def _flag_dev(send: Send) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            message["headers"] = [*message.get("headers", []), (DEV_HEADER, b"true")]
        await send(message)

    return wrapped
