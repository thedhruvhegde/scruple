"""Alembic environment for the PostgreSQL schema.

The schema is plain SQL (no SQLAlchemy models), so there is no autogenerate:
every revision states its DDL explicitly. The URL comes from, in order, the
`sqlalchemy.url` option set by `scruple.storage.migrate`, `-x url=...`, or
`SCRUPLE_DATABASE_URL` (plus `SCRUPLE_DATABASE_PASSWORD`, via scruple.settings).
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from scruple.settings import load_settings
from scruple.storage.migrate import sqlalchemy_url


def _url() -> str:
    cfg = context.config
    url = cfg.get_main_option("sqlalchemy.url") or context.get_x_argument(as_dictionary=True).get(
        "url"
    )
    if not url and os.environ.get("SCRUPLE_DATABASE_URL"):
        url = load_settings().database_dsn()  # merges SCRUPLE_DATABASE_PASSWORD
    if not url:
        raise RuntimeError("set SCRUPLE_DATABASE_URL or pass -x url=postgresql://...")
    return sqlalchemy_url(url)


def run_offline() -> None:
    context.configure(url=_url(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
