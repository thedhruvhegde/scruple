"""Postgres-specific behaviour. The shared contract runs in test_contract.py."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from scruple.audit.log import AuditLog
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.registry import Provenance, RuleStatus
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID
from scruple.storage import ImmutableHistoryError, StorageError, open_storage
from scruple.storage.postgres import PostgresStorage, app_role_grants
from tests.helpers import rv
from tests.storage.conftest import RawStorage
from tests.storage.test_contract import SPEC, _audit

pytestmark = pytest.mark.postgres


def _url_for(base: str, user: str, password: str) -> str:
    return make_conninfo(base, user=user, password=password)


@pytest.mark.parametrize("table", ["audit_events", "decisions", "rule_versions"])
def test_truncate_is_refused(pg_store: RawStorage, table: str) -> None:
    pg_store.insert_rule_version(rv("r-t", SPEC))
    pg_store.append_audit(_audit(1))
    pg_store.insert_decision("dec-00001", "r-t", 1, "{}")
    with pytest.raises(ImmutableHistoryError):
        pg_store.unsafe_execute(f"TRUNCATE {table}")
    assert pg_store.count_decisions() == 1 and len(pg_store.list_audit()) == 1


def test_schema_creation_is_idempotent(postgres_url: str) -> None:
    schema = f"t_{uuid.uuid4().hex[:12]}"
    a = PostgresStorage(postgres_url, schema=schema)
    a.insert_decision("dec-00001", "r-a", 1, "{}")
    b = PostgresStorage(postgres_url, schema=schema)  # re-runs the DDL
    assert b.get_decision("dec-00001") == "{}"
    a.close()
    b.close()


def test_schemas_isolate_stores(postgres_url: str) -> None:
    a = PostgresStorage(postgres_url, schema=f"t_{uuid.uuid4().hex[:12]}")
    b = PostgresStorage(postgres_url, schema=f"t_{uuid.uuid4().hex[:12]}")
    a.insert_decision("dec-00001", "r-a", 1, "{}")
    assert b.count_decisions() == 0
    a.close()
    b.close()


@pytest.mark.parametrize("bad", ["Public", "x;drop", "1abc", "a" * 64, ""])
def test_invalid_schema_names_are_refused(postgres_url: str, bad: str) -> None:
    with pytest.raises(ValueError, match="invalid schema"):
        PostgresStorage(postgres_url, schema=bad)


def test_unreachable_server_is_a_storage_error() -> None:
    with pytest.raises(StorageError):
        PostgresStorage("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")


def test_open_storage_dispatches_postgres_urls(postgres_url: str) -> None:
    s = open_storage(postgres_url)
    try:
        assert isinstance(s, PostgresStorage)
        s.list_audit()
    finally:
        s.close()
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute("DROP TABLE rule_versions, audit_events, decisions CASCADE")


def test_concurrent_writers_keep_the_hash_chain_intact(postgres_url: str) -> None:
    schema = f"t_{uuid.uuid4().hex[:12]}"
    stores = [PostgresStorage(postgres_url, schema=schema) for _ in range(4)]
    errors: list[BaseException] = []

    def work(s: PostgresStorage, n: int) -> None:
        log = AuditLog(s, SteppingClock())
        try:
            for i in range(15):
                log.append("x", f"writer-{n}", {"i": i})
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(s, n)) for n, s in enumerate(stores)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    log = AuditLog(stores[0], SteppingClock())
    assert log.verify().ok and log.verify().entries == 60
    for s in stores:
        s.close()


@pytest.fixture
def app_role(postgres_url: str) -> Iterator[tuple[str, str]]:
    """A non-owner login role with only the grants the service needs."""
    schema = f"t_{uuid.uuid4().hex[:12]}"
    role = f"app_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex  # throwaway, for servers that require password auth
    PostgresStorage(postgres_url, schema=schema).close()  # owner creates the schema
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        c.execute(app_role_grants(role, schema))
    yield _url_for(postgres_url, role, password), schema
    with psycopg.connect(postgres_url, autocommit=True) as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")
        c.execute(f"DROP OWNED BY {role}")
        c.execute(f"DROP ROLE {role}")


def test_service_runs_as_a_non_owner_role(app_role: tuple[str, str]) -> None:
    url, schema = app_role
    s = PostgresStorage(url, schema=schema, create_schema=False)
    g = AgentGate(s, SteppingClock(), GateConfig(n_cases=500, bootstrap_resamples=100))
    g.bootstrap_baseline()
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", Provenance(agent_id="agent-1"))
    g.evaluate(BAD_RULE_ID, 1)
    assert g.registry.get(BAD_RULE_ID, 1).status is RuleStatus.REJECTED
    assert g.verify_integrity().ok
    s.close()


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TRIGGER audit_no_update ON audit_events",
        "ALTER TABLE audit_events DISABLE TRIGGER ALL",
        "UPDATE rule_versions SET spec_json = '{}'",
        "DELETE FROM decisions",
        "DROP TABLE audit_events",
    ],
)
def test_non_owner_cannot_bypass_append_only(app_role: tuple[str, str], sql: str) -> None:
    url, schema = app_role
    s = PostgresStorage(url, schema=schema, create_schema=False)
    s.insert_rule_version(rv("r-p", SPEC))
    s.append_audit(_audit(1))
    s.insert_decision("dec-00001", "r-p", 1, "{}")
    with pytest.raises(StorageError, match=r"permission denied|must be owner"):
        s.unsafe_execute(sql)
    s.update_rule_status("r-p", 1, RuleStatus.SHADOW)  # the allowed column still works
    assert s.list_audit() == [_audit(1)] and s.count_decisions() == 1
    s.close()


def test_app_role_grants_validates_identifiers() -> None:
    with pytest.raises(ValueError, match="invalid identifier"):
        app_role_grants("x; drop role y")
