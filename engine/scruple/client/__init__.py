"""Typed Python client for the scruple HTTP API. See `scruple.client._client`."""

from __future__ import annotations

from scruple.client._client import NO_RETRY, Client, RetryPolicy
from scruple.client.errors import (
    APIError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    ScrupleError,
    ServerError,
    TransportError,
    ValidationError,
)

__all__ = [
    "NO_RETRY",
    "APIError",
    "BadRequestError",
    "Client",
    "ConflictError",
    "ForbiddenError",
    "NotFoundError",
    "RateLimitedError",
    "RetryPolicy",
    "ScrupleError",
    "ServerError",
    "TransportError",
    "ValidationError",
]
