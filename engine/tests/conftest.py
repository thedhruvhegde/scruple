from __future__ import annotations

from collections.abc import Iterator

import pytest

from scruple.audit.log import AuditLog
from scruple.clock import SteppingClock
from scruple.registry import Registry
from scruple.storage import SQLiteStorage


@pytest.fixture
def storage() -> Iterator[SQLiteStorage]:
    s = SQLiteStorage(":memory:")
    yield s
    s.close()


@pytest.fixture
def clock() -> SteppingClock:
    return SteppingClock()


@pytest.fixture
def audit(storage: SQLiteStorage, clock: SteppingClock) -> AuditLog:
    return AuditLog(storage, clock)


@pytest.fixture
def registry(storage: SQLiteStorage, audit: AuditLog, clock: SteppingClock) -> Registry:
    return Registry(storage, audit, clock)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Superuser URL of a throwaway PostgreSQL cluster (skips if none is available)."""
    from tests import pgcluster

    cluster = pgcluster.start()
    if cluster is None:
        pytest.skip("PostgreSQL binaries not found and SCRUPLE_TEST_POSTGRES_URL not set")
    yield cluster.url
    cluster.stop()
