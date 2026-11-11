"""The table behind the shared rate-limit backend (`scruple.auth.ratelimit_db`).

`rate_limit_buckets` is mutable state, not history: one row per bucket, rewritten on
every request, and rows whose bucket is full again are deleted (a missing row reads
as a full bucket, so deleting one changes nothing). Nothing in it is audited. The key
is the SHA-256 of the principal's limit key, so client addresses and credential ids
are not stored in clear. Times are seconds since the epoch (floating point).
"""

from __future__ import annotations

_COLS = """
    bucket_key TEXT PRIMARY KEY,
    tokens DOUBLE PRECISION NOT NULL,
    at DOUBLE PRECISION NOT NULL,
    full_at DOUBLE PRECISION NOT NULL
"""

SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS rate_limit_buckets ({_COLS});
CREATE INDEX IF NOT EXISTS rate_limit_buckets_full ON rate_limit_buckets (full_at);
"""

POSTGRES_DDL = SQLITE_DDL

__all__ = ["POSTGRES_DDL", "SQLITE_DDL"]
