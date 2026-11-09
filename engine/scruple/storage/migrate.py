"""Run the PostgreSQL migrations from code (tests, the container entrypoint).

From a shell, `SCRUPLE_DATABASE_URL=postgresql://... uv run alembic upgrade head`
does the same using alembic.ini at the repository root.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).parent / "migrations"


def sqlalchemy_url(url: str) -> str:
    """libpq URL -> SQLAlchemy URL on the psycopg 3 driver."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    raise ValueError("migrations are for PostgreSQL URLs only")


def config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    # ConfigParser interpolation: a literal % in a password must be doubled.
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(url).replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(config(url), revision)


def main() -> int:
    """`python -m scruple.storage.migrate`: upgrade the configured database to head."""
    from scruple.settings import load_settings

    upgrade(load_settings().database_dsn())
    return 0


def head() -> str:
    heads = ScriptDirectory.from_config(config("postgresql://")).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected one migration head, found {heads}")
    return heads[0]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
