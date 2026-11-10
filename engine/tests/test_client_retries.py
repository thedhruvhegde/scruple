"""scruple.Client retries and error mapping, against a scripted transport."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.client import (
    APIError,
    BadRequestError,
    Client,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    RetryPolicy,
    ServerError,
    TransportError,
    ValidationError,
)

Step = httpx.Response | type[httpx.TransportError]


def _client(
    steps: list[Step], policy: RetryPolicy | None = None
) -> tuple[Client, list[str], list[float]]:
    calls: list[str] = []
    slept: list[float] = []
    script = iter(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        step = next(script)
        if isinstance(step, httpx.Response):
            return step
        raise step("scripted", request=request)

    http = httpx.Client(base_url="http://scruple.test", transport=httpx.MockTransport(handler))
    return Client(http_client=http, retry=policy, sleep=slept.append, actor="a"), calls, slept


OK = httpx.Response(200, json={"status": "ok"})


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_get_retries_retryable_statuses(status: int) -> None:
    c, calls, slept = _client([httpx.Response(status), httpx.Response(status), OK])
    assert c.health() is True
    assert len(calls) == 3
    assert slept == [0.25, 0.5]


def test_get_gives_up_after_the_last_attempt() -> None:
    c, calls, _ = _client([httpx.Response(503)] * 3)
    with pytest.raises(ServerError) as exc:
        c.health()
    assert exc.value.status == 503 and len(calls) == 3


@pytest.mark.parametrize("err", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
def test_get_retries_transport_errors(err: type[httpx.TransportError]) -> None:
    c, calls, _ = _client([err, OK])
    assert c.health() is True and len(calls) == 2


def test_transport_error_after_last_attempt() -> None:
    c, _, _ = _client([httpx.ConnectError] * 3)
    with pytest.raises(TransportError):
        c.health()


def test_post_is_not_retried_on_a_status() -> None:
    c, calls, _ = _client([httpx.Response(503, json={"detail": "busy"})])
    with pytest.raises(ServerError):
        c.approve("r", 1)
    assert calls == ["POST /rules/r/versions/1/approve"]


def test_post_is_not_retried_once_the_request_may_have_been_sent() -> None:
    c, calls, _ = _client([httpx.ReadTimeout])
    with pytest.raises(TransportError):
        c.approve("r", 1)
    assert len(calls) == 1


def test_post_is_retried_when_the_connection_never_opened() -> None:
    body = {
        "decision_id": "d1",
        "stage": "activation",
        "outcome": "activated",
        "rule_id": "r",
        "version": 1,
        "summary": "s",
        "reasons": [],
        "report_url": "/decisions/d1/report",
    }
    c, calls, _ = _client([httpx.ConnectError, httpx.Response(200, json=body)])
    assert c.approve("r", 1).decision_id == "d1"
    assert len(calls) == 2


def test_retry_after_is_honoured_and_capped() -> None:
    c, _, slept = _client(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(429, headers={"Retry-After": "999"}),
            OK,
        ]
    )
    c.health()
    assert slept == [2.0, 8.0]


def test_unparseable_retry_after_falls_back_to_the_schedule() -> None:
    c, _, slept = _client(
        [httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}), OK]
    )
    c.health()
    assert slept == [0.25]


def test_no_retry_policy() -> None:
    c, calls, _ = _client([httpx.Response(503)], RetryPolicy(attempts=1))
    with pytest.raises(ServerError):
        c.health()
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (400, BadRequestError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, ValidationError),
        (429, RateLimitedError),
        (500, ServerError),
        (418, APIError),
    ],
)
def test_status_maps_to_error_class(status: int, cls: type[APIError]) -> None:
    resp = httpx.Response(status, json={"detail": "nope"}, headers={"X-Request-ID": "req-1"})
    c, _, _ = _client([resp], RetryPolicy(attempts=1))
    with pytest.raises(cls) as exc:
        c.version()
    assert type(exc.value) is cls
    assert (exc.value.status, exc.value.detail, exc.value.request_id) == (status, "nope", "req-1")
    assert exc.value.method == "GET" and exc.value.path == "/version"


def test_non_json_error_body() -> None:
    c, _, _ = _client([httpx.Response(500, text="<html>boom</html>")], RetryPolicy(attempts=1))
    with pytest.raises(ServerError) as exc:
        c.health()
    assert exc.value.detail == "<html>boom</html>"


@pytest.mark.parametrize(
    "make",
    [lambda: RetryPolicy(attempts=0), lambda: RetryPolicy(backoff=-1)],
)
def test_policy_validation(make: Callable[[], RetryPolicy]) -> None:
    with pytest.raises(ValueError):
        make()


@given(
    attempts=st.integers(1, 6),
    backoff=st.floats(0, 5, allow_nan=False),
    cap=st.floats(0, 20, allow_nan=False),
)
def test_delays_are_monotone_and_capped(attempts: int, backoff: float, cap: float) -> None:
    p = RetryPolicy(attempts=attempts, backoff=backoff, max_backoff=cap)
    delays = [p.delay(i) for i in range(1, attempts + 1)]
    assert delays == sorted(delays)
    assert all(0 <= d <= cap for d in delays)
