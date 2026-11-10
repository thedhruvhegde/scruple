"""ASGI middleware: authenticate every HTTP request before any route runs.

It reads only headers. A refused credential is answered here with 401; a valid one
puts the `Principal` in the request scope, where `scruple.auth.deps` finds it;
no credentials passes the request on anonymously, for public routes to answer and
every other route to refuse. Credentials are never logged: only the principal's
subject, tenant and credential id are attached to the request scope.
"""

from __future__ import annotations

import json

import anyio
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scruple.auth.authn import Authenticated, Authenticator, AuthError
from scruple.auth.deps import STATE_KEY

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


class AuthMiddleware:
    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.authn = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self.authn.dev_actor_header:
            send = _flag_dev(send)
        headers = Headers(scope=scope)
        try:
            # Off the event loop: a key check costs one scrypt and a database read.
            who = await anyio.to_thread.run_sync(self.authn.authenticate, headers)
        except AuthError as e:
            await send_json(
                send, 401, e.detail, [(b"www-authenticate", e.scheme.encode("latin-1"))]
            )
            return
        if who is not None:
            state = scope.setdefault("state", {})
            state[STATE_KEY] = who.principal
            state[STATE_KEY + "_auth"] = who
        await self.app(scope, receive, send)


def authenticated_of(scope: Scope) -> Authenticated | None:
    who = (scope.get("state") or {}).get(STATE_KEY + "_auth")
    return who if isinstance(who, Authenticated) else None


def _flag_dev(send: Send) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            message["headers"] = [*message.get("headers", []), (DEV_HEADER, b"true")]
        await send(message)

    return wrapped
