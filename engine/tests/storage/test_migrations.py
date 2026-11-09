"""Alembic migrations: upgrade on an empty database matches the backend's own DDL."""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from scruple.registry import RuleStatus
from scruple.storage import ImmutableHistoryError
from scruple.storage import migrate as m
from scruple.storage.postgres import PostgresStorage
from tests.helpers import rv
from tests.storage.test_contract import SPEC, _audit

ROOT = Path(__file__).resolve().parents[2]

CATALOG = {
    "columns": """
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name <> 'alembic_version'
        ORDER BY table_name, ordinal_position""",
    "constraints": """
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
        FROM pg_constraint WHERE connamespace = 'public'::regnamespace
          AND conrelid::regclass::text <> 'alembic_version'
        ORDER BY 1, 2""",
    "triggers": """
        SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger
        WHERE NOT tgisinternal ORDER BY tgname""",
    "functions": """
        SELECT proname, pg_get_functiondef(oid) FROM pg_proc
        WHERE pronamespace = 'public'::regnamespace ORDER BY proname""",
}


def _with_db(url: str, db: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{db}"))


@pytest.fixture
def empty_db(postgres_url: str) -> Iterator[str]:
    """A brand new, empty database; dropped afterwards."""
    name = f"m_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    yield _with_db(postgres_url, name)
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _catalog(url: str) -> dict[str, list[tuple[object, ...]]]:
    with psycopg.connect(url) as c:
        return {k: [tuple(r) for r in c.execute(q).fetchall()] for k, q in CATALOG.items()}


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as c:
        rows = c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {r[0] for r in rows}


def test_single_linear_head() -> None:
    assert m.head() == "0001"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u@h/d", "postgresql+psycopg://u@h/d"),
        ("postgres://u@h/d", "postgresql+psycopg://u@h/d"),
        ("postgresql+psycopg://u@h/d", "postgresql+psycopg://u@h/d"),
    ],
)
def test_sqlalchemy_url(url: str, expected: str) -> None:
    assert m.sqlalchemy_url(url) == expected


def test_sqlalchemy_url_refuses_other_databases() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        m.sqlalchemy_url("sqlite:///x.db")


def test_percent_in_password_survives_config() -> None:
    cfg = m.config("postgresql://u:p%25w@h/d")
    assert cfg.get_main_option("sqlalchemy.url") == "postgresql+psycopg://u:p%25w@h/d"


@pytest.mark.postgres
def test_upgrade_head_on_empty_db_matches_the_backend(postgres_url: str, empty_db: str) -> None:
    m.upgrade(empty_db)
    with psycopg.connect(empty_db) as c:
        row = c.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None and row[0] == m.head()
    migrated = _catalog(empty_db)
    assert len(migrated["triggers"]) == 9 and len(migrated["functions"]) == 2

    name = f"m_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    direct_url = _with_db(postgres_url, name)
    try:
        PostgresStorage(direct_url).close()
        assert _catalog(direct_url) == migrated
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as c:
            c.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.mark.postgres
def test_backend_runs_on_a_migrated_db(empty_db: str) -> None:
    m.upgrade(empty_db)
    s = PostgresStorage(empty_db, create_schema=False)
    s.insert_rule_version(rv("r-mig", SPEC))
    s.update_rule_status("r-mig", 1, RuleStatus.SHADOW)
    s.append_audit(_audit(1))
    with pytest.raises(ImmutableHistoryError):
        s.unsafe_execute("DELETE FROM audit_events")
    with pytest.raises(ImmutableHistoryError):
        s.unsafe_execute("UPDATE rule_versions SET owner = 'mallory'")
    assert s.list_audit() == [_audit(1)]
    s.close()


@pytest.mark.postgres
def test_upgrade_is_idempotent_and_downgrade_removes_everything(empty_db: str) -> None:
    m.upgrade(empty_db)
    m.upgrade(empty_db)
    assert _tables(empty_db) == {"alembic_version", "rule_versions", "audit_events", "decisions"}
    m.downgrade(empty_db)
    assert _tables(empty_db) == {"alembic_version"}
    assert _catalog(empty_db)["functions"] == []
    m.upgrade(empty_db)
    assert "audit_events" in _tables(empty_db)


@pytest.mark.postgres
def test_alembic_cli_uses_the_env_url(empty_db: str) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={"SCRUPLE_DATABASE_URL": empty_db, "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "decisions" in _tables(empty_db)


@pytest.mark.postgres
def test_container_migrate_entry_point(empty_db: str) -> None:
    out = subprocess.run(
        [sys.executable, "-m", "scruple.storage.migrate"],
        env={"SCRUPLE_DATABASE_URL": empty_db, "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert {"rule_versions", "audit_events", "decisions"} <= _tables(empty_db)


def test_alembic_cli_without_a_url_fails_clearly() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={"PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode != 0 and "SCRUPLE_DATABASE_URL" in out.stderr
