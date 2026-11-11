"""The shared rate-limit backend on the gate database (SQLite and Postgres).

Same answers as the in-memory buckets for the same clock; one limit across
connections, processes and app instances on the same database.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService
from scruple.auth.limits import Limits
from scruple.auth.ratelimit import InMemoryBuckets, Limit
from scruple.auth.ratelimit_db import SQLBuckets, bucket_key, buckets_for
from scruple.auth.rbac import key_permissions
from scruple.service import AgentGate
from scruple.settings import Settings, load_settings
from scruple.storage import SQLiteStorage
from scruple.storage.postgres import PostgresStorage
from tests.test_auth_ratelimit import FAST, Mono, _key

LIM = Limit(per_minute=60, burst=3)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def opener(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    """Opens a new storage on one shared database on every call."""
    opened: list[Any] = []
    if request.param == "sqlite":
        path = str(tmp_path / "gate.db")

        def open_sqlite() -> SQLiteStorage:
            s = SQLiteStorage(path)
            opened.append(s)
            return s

        yield open_sqlite
    else:
        url = request.getfixturevalue("postgres_url")
        schema = f"t_{uuid.uuid4().hex[:16]}"

        def open_pg() -> PostgresStorage:
            s = PostgresStorage(url, schema=schema)
            opened.append(s)
            return s

        yield open_pg
        with psycopg.connect(url, autocommit=True) as conn:
            for s in opened:
                s.close()
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
        return
    for s in opened:
        s.close()


def test_burst_refill_and_retry_after(opener: Any) -> None:
    m = Mono()
    b = buckets_for(opener(), now=m)
    assert [b.take("k", LIM).allowed for _ in range(4)] == [True, True, True, False]
    assert b.take("k", LIM).retry_after == 1
    assert not b.peek("k", LIM).allowed
    m.t = 1.0
    assert b.peek("k", LIM).allowed and b.take("k", LIM).allowed
    assert not b.take("k", LIM).allowed
    m.t = 3_600.0
    assert [b.take("k", LIM).allowed for _ in range(4)] == [True, True, True, False]
    assert b.take("other", LIM).allowed


def test_one_limit_across_storages_on_the_same_database(opener: Any) -> None:
    m = Mono()
    a, b = buckets_for(opener(), now=m), buckets_for(opener(), now=m)
    got = [(a if i % 2 else b).take("shared", LIM).allowed for i in range(6)]
    assert got.count(True) == LIM.burst


def test_a_clock_stepping_back_refills_nothing(opener: Any) -> None:
    m = Mono()
    m.t = 100.0
    b = buckets_for(opener(), now=m)
    for _ in range(3):
        b.take("k", LIM)
    m.t = 0.0
    assert not b.take("k", LIM).allowed


def test_keys_are_stored_hashed_and_full_buckets_are_pruned(opener: Any) -> None:
    m = Mono()
    storage = opener()
    b = buckets_for(storage, now=m)
    b.take("10.0.0.7", LIM)
    rows = storage.connection.query("SELECT bucket_key FROM rate_limit_buckets")
    assert [r["bucket_key"] for r in rows] == [bucket_key("10.0.0.7")]
    assert "10.0.0.7" not in rows[0]["bucket_key"]
    assert b.prune() == 1  # not full yet
    m.t = 0.99
    assert b.prune() == 1
    m.t = 1.0  # one token back: full again
    assert b.prune() == 0
    assert b.take("10.0.0.7", LIM).remaining == LIM.burst - 1  # a missing row is full


def test_pruning_runs_every_n_takes(opener: Any) -> None:
    m = Mono()
    storage = opener()
    b = buckets_for(storage, now=m, prune_every=3)
    b.take("a", LIM)
    m.t = 10.0  # "a" is full again
    b.take("b", LIM)
    b.take("c", LIM)  # third take: prunes "a"
    rows = storage.connection.query("SELECT bucket_key FROM rate_limit_buckets")
    assert {r["bucket_key"] for r in rows} == {bucket_key("b"), bucket_key("c")}


@settings(max_examples=30, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["a", "b"]),
            st.floats(0, 5, allow_nan=False),
            st.sampled_from([1.0, 2.0]),
            st.booleans(),
        ),
        max_size=25,
    )
)
def test_property_same_answers_as_the_in_memory_buckets(
    ops: list[tuple[str, float, float, bool]],
) -> None:
    m = Mono()
    memory = InMemoryBuckets(m)
    s = SQLiteStorage(":memory:")
    db = buckets_for(s, now=m)
    lim = Limit(per_minute=30, burst=4)
    for key, dt, cost, peek in ops:
        m.t += dt
        if peek:
            assert db.peek(key, lim) == memory.peek(key, lim)
        else:
            assert db.take(key, lim, cost) == memory.take(key, lim, cost)
    s.close()


@pytest.mark.postgres
def test_concurrent_takers_on_two_connections_share_one_bucket(postgres_url: str) -> None:
    schema = f"t_{uuid.uuid4().hex[:16]}"
    stores = [PostgresStorage(postgres_url, schema=schema) for _ in range(2)]
    lim = Limit(per_minute=1, burst=20)
    backends = [buckets_for(s, now=lambda: 0.0) for s in stores]
    results: list[bool] = []
    lock = threading.Lock()

    def worker(b: SQLBuckets) -> None:
        for _ in range(15):
            ok = b.take("race", lim).allowed
            with lock:
                results.append(ok)

    threads = [threading.Thread(target=worker, args=(backends[i % 2],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 60 and results.count(True) == lim.burst
    for b in backends:
        b.close()
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        for s in stores:
            s.close()
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.mark.postgres
def test_two_app_instances_share_a_postgres_limit(postgres_url: str) -> None:
    schema = f"t_{uuid.uuid4().hex[:16]}"
    cfg = Settings(rate_limit_backend="database", rate_limit_per_minute=1, rate_limit_burst=3)
    stores = [PostgresStorage(postgres_url, schema=schema) for _ in range(2)]
    gates = [AgentGate(s) for s in stores]
    gates[0].bootstrap_baseline()
    svc = ApiKeyService(stores[0], params=FAST, permissions_for=key_permissions)
    headers = _key(svc, "alice")
    apps = [
        TestClient(
            create_app(
                g,
                settings=cfg,
                auth=Authenticator(
                    ApiKeyService(g.storage, params=FAST, permissions_for=key_permissions)
                ),
            )
        )
        for g in gates
    ]
    codes = [apps[i % 2].get("/rules", headers=headers).status_code for i in range(6)]
    assert codes.count(200) == 3 and codes.count(429) == 3, codes
    assert codes[:3] == [200, 200, 200]  # alternating instances: one bucket, not two
    other = _key(svc, "bob")
    assert apps[1].get("/rules", headers=other).status_code == 200
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        for s in stores:
            s.close()
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_in_memory_limits_stay_per_instance() -> None:
    """The default backend: each instance has its own buckets (the documented gap the
    database backend closes)."""
    s = SQLiteStorage(":memory:")
    lim = Limits.from_settings(Settings(rate_limit_burst=1), storage=s)
    other = Limits.from_settings(Settings(rate_limit_burst=1), storage=s)
    assert isinstance(lim.backend, InMemoryBuckets)
    assert lim.backend.take("k", lim.default).allowed
    assert other.backend.take("k", other.default).allowed
    s.close()


def test_database_backend_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRUPLE_RATE_LIMIT_BACKEND", "database")
    cfg = load_settings(env={"SCRUPLE_RATE_LIMIT_BACKEND": "database"})
    assert cfg.rate_limit_backend == "database"
    s = SQLiteStorage(":memory:")
    assert isinstance(Limits.from_settings(cfg, storage=s).backend, SQLBuckets)
    with pytest.raises(ValueError, match="needs the gate storage"):
        Limits.from_settings(cfg)
    with pytest.raises(ValueError):
        load_settings(env={"SCRUPLE_RATE_LIMIT_BACKEND": "redis"})
    s.close()


def test_unknown_storage_and_dialect_are_refused() -> None:
    with pytest.raises(ValueError, match="no shared rate-limit backend"):
        buckets_for(object())
    with pytest.raises(ValueError, match="dialect"):
        SQLBuckets(SQLiteStorage(":memory:").connection, "mysql")
