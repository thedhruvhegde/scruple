from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, settings

from scruple.audit.log import AuditLog
from scruple.clock import SteppingClock
from scruple.registry import Registry
from scruple.storage import SQLiteStorage

# Hypothesis profiles. "ci" is derandomized: the same examples on every run, so a
# CI failure always reproduces. "dev" explores fresh examples each run. "thorough"
# backs `make test-slow`. Pick one with HYPOTHESIS_PROFILE (default: dev).
_SLOW_OK = [HealthCheck.too_slow]
settings.register_profile(
    "ci",
    derandomize=True,
    max_examples=100,
    print_blob=True,
    deadline=None,
    suppress_health_check=_SLOW_OK,
)
settings.register_profile("dev", max_examples=50, deadline=None, suppress_health_check=_SLOW_OK)
settings.register_profile(
    "thorough", max_examples=1_000, deadline=None, suppress_health_check=_SLOW_OK
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


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
