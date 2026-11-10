"""Token buckets, and per-key rate and size limits over HTTP."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.limits import Limits
from scruple.auth.ratelimit import InMemoryBuckets, Limit
from scruple.auth.rbac import Role, key_permissions
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage

FAST = ScryptParams(n=1024)


class Mono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# --- buckets --------------------------------------------------------------------


def test_burst_then_refill() -> None:
    m = Mono()
    b = InMemoryBuckets(m)
    lim = Limit(per_minute=60, burst=3)
    assert [b.take("k", lim).allowed for _ in range(4)] == [True, True, True, False]
    denied = b.take("k", lim)
    assert not denied.allowed and denied.retry_after == 1
    m.t = 1.0
    assert b.take("k", lim).allowed
    assert not b.take("k", lim).allowed


def test_retry_after_rounds_up_to_whole_seconds() -> None:
    m = Mono()
    b = InMemoryBuckets(m)
    lim = Limit(per_minute=6, burst=1)  # one token every 10 s
    b.take("k", lim)
    assert b.take("k", lim).retry_after == 10
    m.t = 7.5
    assert b.take("k", lim).retry_after == 3


def test_buckets_are_independent_and_peek_does_not_consume() -> None:
    b = InMemoryBuckets(Mono())
    lim = Limit(per_minute=60, burst=1)
    assert b.peek("a", lim).allowed and b.peek("a", lim).allowed
    assert b.take("a", lim).allowed and not b.take("a", lim).allowed
    assert b.take("b", lim).allowed
    assert not b.peek("a", lim).allowed


def test_bucket_never_exceeds_burst() -> None:
    m = Mono()
    b = InMemoryBuckets(m)
    lim = Limit(per_minute=600, burst=2)
    b.take("k", lim)
    m.t = 3600
    assert [b.take("k", lim).allowed for _ in range(3)] == [True, True, False]


def test_key_table_is_bounded() -> None:
    b = InMemoryBuckets(Mono(), max_keys=2)
    lim = Limit(per_minute=1, burst=1)
    for k in ("a", "b", "c"):
        b.take(k, lim)
    assert b.take("a", lim).allowed  # evicted, came back full
    assert not b.take("c", lim).allowed


@pytest.mark.parametrize(("rate", "burst"), [(0, 1), (1, 0), (-5, 3)])
def test_limits_must_be_positive(rate: int, burst: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Limit(rate, burst)


@given(
    st.integers(1, 600),
    st.integers(1, 50),
    st.lists(st.floats(0, 30, allow_nan=False), min_size=1, max_size=60),
)
def test_admitted_requests_never_exceed_burst_plus_refill(
    rate: int, burst: int, gaps: list[float]
) -> None:
    """Over any schedule, admitted <= burst + rate * elapsed (the bucket invariant)."""
    m = Mono()
    b = InMemoryBuckets(m)
    lim = Limit(rate, burst)
    admitted = 0
    for g in gaps:
        m.t += g
        t = b.take("k", lim)
        admitted += t.allowed
        assert t.allowed or t.retry_after >= 1
    assert admitted <= burst + rate / 60.0 * m.t + 1e-9


# --- over HTTP ------------------------------------------------------------------


@pytest.fixture
def setup() -> Iterator[tuple[TestClient, ApiKeyService, Mono]]:
    db = SQLiteStorage(":memory:")
    g = AgentGate(db)
    g.bootstrap_baseline()
    svc = ApiKeyService(db, params=FAST, permissions_for=key_permissions)
    m = Mono()
    limits = Limits(
        default=Limit(per_minute=60, burst=2),
        anonymous=Limit(per_minute=60, burst=3),
        auth_failures=Limit(per_minute=6, burst=2),
        max_body_bytes=2048,
        backend=InMemoryBuckets(m),
    )
    yield TestClient(create_app(g, auth=Authenticator(svc), limits=limits)), svc, m
    db.close()


def _key(svc: ApiKeyService, subject: str, **kw: int) -> dict[str, str]:
    _, token = svc.create(subject, [], "local-admin", roles=[Role.AUDITOR], **kw)  # type: ignore[arg-type]
    return {"X-API-Key": token.get_secret_value()}


def test_429_with_retry_after_per_key(setup: tuple[TestClient, ApiKeyService, Mono]) -> None:
    c, svc, m = setup
    a, b = _key(svc, "alice"), _key(svc, "bob")
    assert [c.get("/rules", headers=a).status_code for _ in range(3)] == [200, 200, 429]
    r = c.get("/rules", headers=a)
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    assert "rate limit" in r.json()["detail"]
    assert c.get("/rules", headers=b).status_code == 200  # bob has his own bucket
    m.t += 1
    assert c.get("/rules", headers=a).status_code == 200


def test_per_key_limit_overrides_the_default(
    setup: tuple[TestClient, ApiKeyService, Mono],
) -> None:
    c, svc, _ = setup
    big = _key(svc, "batch-job", rate_per_minute=600, burst=5)
    assert [c.get("/rules", headers=big).status_code for _ in range(6)] == [200] * 5 + [429]


def test_anonymous_requests_share_an_address_bucket(
    setup: tuple[TestClient, ApiKeyService, Mono],
) -> None:
    c, _, _ = setup
    assert [c.get("/version").status_code for _ in range(4)] == [200, 200, 200, 429]


def test_probes_are_never_rate_limited(setup: tuple[TestClient, ApiKeyService, Mono]) -> None:
    """An orchestrator's liveness and readiness probes must never see 429."""
    c, _, _ = setup
    assert {c.get(p).status_code for _ in range(10) for p in ("/health", "/ready")} == {200}
    assert c.get("/version").status_code == 200  # and they spent none of the bucket


def test_failed_credentials_are_throttled_before_any_hash(
    setup: tuple[TestClient, ApiKeyService, Mono], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scruple.auth import keys as keys_mod

    c, svc, _ = setup
    good = _key(svc, "alice")
    bad = {"X-API-Key": "scr_0123456789abcdef_" + "a" * 43}
    assert [c.get("/rules", headers=bad).status_code for _ in range(2)] == [401, 401]
    calls: list[int] = []
    real = keys_mod._scrypt

    def counting(*a: Any) -> bytes:
        calls.append(1)
        return real(*a)

    monkeypatch.setattr(keys_mod, "_scrypt", counting)
    r = c.get("/rules", headers=bad)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert calls == []  # refused without paying for a scrypt
    assert c.get("/rules", headers=good).status_code == 429  # same address, still cooling


def test_body_size_limits(setup: tuple[TestClient, ApiKeyService, Mono]) -> None:
    c, svc, _ = setup
    h = _key(svc, "alice", burst=50)
    big = "x" * 4000
    r = c.post("/rules/x/rollback", json={"reason": big}, headers=h)
    assert r.status_code == 413 and "2048" in r.json()["detail"]
    small = _key(svc, "tiny", max_body_bytes=16, burst=50)
    r = c.post("/rules/x/rollback", json={"reason": "a long enough reason"}, headers=small)
    assert r.status_code == 413


def test_streamed_bodies_without_length_are_cut_off(
    setup: tuple[TestClient, ApiKeyService, Mono],
) -> None:
    c, svc, _ = setup
    h = _key(svc, "alice", burst=50)

    def chunks() -> Iterator[bytes]:
        for _ in range(10):
            yield b"x" * 512

    r = c.post(
        "/rules/x/rollback",
        content=chunks(),
        headers={**h, "content-type": "application/json"},
    )
    assert r.status_code == 413


@pytest.mark.parametrize("value", ["abc", "-1", "1e9"])
def test_malformed_content_length_is_refused(
    setup: tuple[TestClient, ApiKeyService, Mono], value: str
) -> None:
    c, svc, _ = setup
    h = _key(svc, "alice", burst=50)
    r = c.post("/rules/x/rollback", content=b"{}", headers={**h, "content-length": value})
    assert r.status_code in (400, 413)
