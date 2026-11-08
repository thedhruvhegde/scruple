from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent_gate.audit.log import AuditLog
from agent_gate.clock import SteppingClock
from agent_gate.registry import Registry
from agent_gate.storage import SQLiteStorage


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
