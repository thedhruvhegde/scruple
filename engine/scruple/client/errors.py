"""Errors raised by `scruple.Client`. Every HTTP failure is an `APIError` subclass."""

from __future__ import annotations

from typing import Any


class ScrupleError(Exception):
    """Base class for every error the client raises."""


class TransportError(ScrupleError):
    """The request did not get an HTTP response (connection refused, timeout, ...)."""


class APIError(ScrupleError):
    """The server answered with a non-2xx status."""

    def __init__(
        self, status: int, detail: Any, *, method: str, path: str, request_id: str | None
    ) -> None:
        self.status = status
        self.detail = detail
        self.method = method
        self.path = path
        self.request_id = request_id
        super().__init__(f"{method} {path} -> {status}: {detail}")


class BadRequestError(APIError):
    """400."""


class ForbiddenError(APIError):
    """403: the actor may not do this (e.g. approving its own rule)."""


class NotFoundError(APIError):
    """404: unknown rule, version or decision."""


class ConflictError(APIError):
    """409: duplicate content or an invalid state transition."""


class ValidationError(APIError):
    """422: the request failed input validation; `detail` lists the problems."""


class RateLimitedError(APIError):
    """429."""


class ServerError(APIError):
    """5xx."""


_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitedError,
}


def error_for(status: int) -> type[APIError]:
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    return ServerError if status >= 500 else APIError
