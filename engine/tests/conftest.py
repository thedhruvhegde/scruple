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
