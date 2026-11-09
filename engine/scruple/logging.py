"""Structured JSON logging with request ids (stdlib logging plus contextvars).

`configure_logging()` installs one handler on the root logger that writes one JSON
object per line. Every record carries the current request id, if any, from a
context variable that `RequestIDMiddleware` sets for the duration of each HTTP
request (contextvars follow FastAPI's threadpool, so sync endpoints see it too).

Fields: ts (UTC, ISO 8601), level, logger, msg, request_id, any `extra={...}`
keys, and exc (formatted traceback) when logging an exception.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_request_id: ContextVar[str | None] = ContextVar("scruple_request_id", default=None)

# Attributes every LogRecord has; anything else came from `extra=`.
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def request_context(request_id: str) -> Iterator[None]:
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = current_request_id()
        if rid is not None:
            out["request_id"] = rid
        for k, v in record.__dict__.items():
            if k not in _STANDARD and not k.startswith("_") and k not in out:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        rid = current_request_id()
        return f"{line} request_id={rid}" if rid else line


class _ScrupleHandler(logging.StreamHandler[TextIO]):
    """Marker type so reconfiguring replaces our handler and leaves others alone."""


def configure_logging(
    level: str = "INFO", fmt: Literal["json", "text"] = "json", stream: TextIO | None = None
) -> logging.Handler:
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h, _ScrupleHandler)]:
        root.removeHandler(h)
    handler = _ScrupleHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    return handler


# --- ASGI middleware --------------------------------------------------------------

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

access_log = logging.getLogger("scruple.api")


class RequestIDMiddleware:
    """Give every HTTP request an id, echo it as X-Request-ID, log one line per request.

    A well-formed incoming X-Request-ID (1-128 chars of [A-Za-z0-9._:-]) is kept so
    ids can be traced across services; anything else is replaced by a fresh one.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        rid = incoming if _VALID_ID.match(incoming) else new_request_id()
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", rid.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        with request_context(rid):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                access_log.info(
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
