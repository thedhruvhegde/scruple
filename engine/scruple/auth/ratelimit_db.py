"""A shared rate-limit backend on the gate database: one bucket per key, for every
process and replica that uses the same database.

Same semantics as `InMemoryBuckets` (`scruple.auth.ratelimit`): a bucket holds up
to `burst` tokens, refills at `per_minute / 60` a second, a request takes one, an
empty bucket answers with whole seconds until a token is back. The difference is
where the bucket lives: a row of `rate_limit_buckets` (`scruple.storage.ratelimit`).

Concurrency. Each `take` is one transaction: insert the bucket full if it is new
(`ON CONFLICT DO NOTHING`), lock its row, read, refill, take, write. On Postgres the
row lock (`SELECT ... FOR UPDATE`) serialises takers of the same key across every
connection, so two app instances share one limit exactly; different keys do not
wait for each other. The backend has its own connection that does not take the
gate's global write lock. On SQLite the transaction is `BEGIN IMMEDIATE` on the
storage's connection: processes on one host that open the same file share the limit;
`:memory:` is one process only (the fallback the in-memory backend already gives).

Time. Postgres buckets use the database server's clock (`clock_timestamp()`), so
replicas on hosts with skewed clocks agree; SQLite uses this host's wall clock. A
clock that steps backwards refills nothing (elapsed time is clamped at zero). Tests
inject `now`.

Rows whose bucket would be full again are deleted every `prune_every` takes; a
missing row reads as a full bucket, so pruning never changes an answer.

Turn it on with `SCRUPLE_RATE_LIMIT_BACKEND=database`.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from scruple.auth.ratelimit import Limit, Taken
from scruple.storage.sql import Connection


def bucket_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class SQLBuckets:
    """`RateLimitBackend` on a SQL `Connection` (`dialect` "sqlite" or "postgres")."""

    def __init__(
        self,
        conn: Connection,
        dialect: str,
        *,
        now: Callable[[], float] | None = None,
        prune_every: int = 1_000,
        owns_connection: bool = False,
    ) -> None:
        if dialect not in ("sqlite", "postgres"):
            raise ValueError(f"unknown dialect {dialect!r}")
        self._conn = conn
        self._dialect = dialect
        self._now = now
        self._prune_every = max(1, prune_every)
        self._takes = 0
        self._owns = owns_connection
        self._lock = threading.Lock()

    # --- time ---------------------------------------------------------------------
    def _clock(self) -> float:
        if self._now is not None:
            return self._now()
        if self._dialect == "postgres":
            rows = self._conn.query("SELECT extract(epoch from clock_timestamp()) AS now")
            return float(rows[0]["now"])
        return time.time()

    # --- the bucket ---------------------------------------------------------------
    @staticmethod
    def _level(row: dict[str, Any] | None, limit: Limit, now: float) -> float:
        if row is None:
            return float(limit.burst)
        rate = limit.per_minute / 60.0
        elapsed = max(0.0, now - float(row["at"]))
        return min(float(limit.burst), float(row["tokens"]) + elapsed * rate)

    @staticmethod
    def _result(tokens: float, limit: Limit, cost: float, allowed: bool) -> Taken:
        if allowed:
            return Taken(True, int(tokens), 0)
        rate = limit.per_minute / 60.0
        return Taken(False, 0, max(1, math.ceil((cost - tokens) / rate)))

    def _row(self, key: str, lock: bool) -> dict[str, Any] | None:
        sql = "SELECT tokens, at FROM rate_limit_buckets WHERE bucket_key = ?"
        if lock and self._dialect == "postgres":
            sql += " FOR UPDATE"
        rows = self._conn.query(sql, (key,))
        return dict(rows[0]) if rows else None

    def take(self, key: str, limit: Limit, cost: float = 1.0) -> Taken:
        k = bucket_key(key)
        with self._conn.transaction():
            now = self._clock()
            self._conn.execute(
                "INSERT INTO rate_limit_buckets (bucket_key, tokens, at, full_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (bucket_key) DO NOTHING",
                (k, float(limit.burst), now, now),
            )
            tokens = self._level(self._row(k, lock=True), limit, now)
            allowed = tokens >= cost
            if allowed:
                tokens -= cost
            rate = limit.per_minute / 60.0
            full_at = now + (float(limit.burst) - tokens) / rate
            self._conn.execute(
                "UPDATE rate_limit_buckets SET tokens = ?, at = ?, full_at = ? "
                "WHERE bucket_key = ?",
                (tokens, now, full_at, k),
            )
        self._maybe_prune()
        return self._result(tokens, limit, cost, allowed)

    def peek(self, key: str, limit: Limit) -> Taken:
        now = self._clock()
        tokens = self._level(self._row(bucket_key(key), lock=False), limit, now)
        return self._result(tokens, limit, 1.0, tokens >= 1.0)

    def _maybe_prune(self) -> None:
        with self._lock:
            self._takes += 1
            due = self._takes % self._prune_every == 0
        if due:
            self.prune()

    def prune(self) -> int:
        """Delete rows whose bucket is full again. Returns how many remain."""
        with self._conn.transaction():
            self._conn.execute(
                "DELETE FROM rate_limit_buckets WHERE full_at <= ?", (self._clock(),)
            )
        return int(self._conn.query("SELECT count(*) AS n FROM rate_limit_buckets")[0]["n"])

    def close(self) -> None:
        if self._owns:
            self._conn.close()


def buckets_for(
    storage: object, *, now: Callable[[], float] | None = None, prune_every: int = 1_000
) -> SQLBuckets:
    """The shared backend on a gate `Storage`'s database. SQLite shares the storage's
    connection; Postgres opens its own (no global write lock), like the job queue."""
    from scruple.storage.sqlite import SQLiteStorage

    if isinstance(storage, SQLiteStorage):
        return SQLBuckets(storage.connection, "sqlite", now=now, prune_every=prune_every)
    from scruple.storage.postgres import PostgresConnection, PostgresStorage

    if isinstance(storage, PostgresStorage):
        conn = PostgresConnection(
            storage.dsn, storage.schema, create_schema=False, serialise_writes=False
        )
        return SQLBuckets(conn, "postgres", now=now, prune_every=prune_every, owns_connection=True)
    raise ValueError(f"no shared rate-limit backend for {type(storage).__name__}")


__all__ = ["SQLBuckets", "bucket_key", "buckets_for"]
