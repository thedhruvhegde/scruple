"""Fault injection for storage: fail the k-th call to a backend (roadmap 170).

`FaultyStorage(inner)` forwards every `Storage` call to `inner` and counts them.
`arm(k, exc)` makes the k-th call from now raise `exc` instead of running (calls are
0-based; entering `transaction()` counts as a call). `exc` may be any
`BaseException`, so a test can model a crash (`SystemExit`, `KeyboardInterrupt`) as
well as an error; `kill=True` ends the process with `os._exit` to model SIGKILL.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from scruple.storage import Storage

METHODS = (
    "insert_rule_version",
    "update_rule_status",
    "set_rollback_to",
    "get_rule_version",
    "list_rule_versions",
    "append_audit",
    "last_audit",
    "list_audit",
    "insert_decision",
    "get_decision",
    "list_decisions",
    "count_decisions",
)


class InjectedFaultError(Exception):
    """The error a FaultyStorage raises by default."""


class FaultyStorage:
    def __init__(self, inner: Storage) -> None:
        self.inner = inner
        self.calls: list[str] = []
        self._at: int | None = None
        self._exc: BaseException | None = None
        self._kill = False

    def arm(self, k: int, exc: BaseException | None = None, *, kill: bool = False) -> None:
        self._at = len(self.calls) + k
        self._exc = exc if exc is not None else InjectedFaultError(f"fault at call {k}")
        self._kill = kill

    def disarm(self) -> None:
        self._at = None

    def _hit(self, name: str) -> None:
        if self._at is not None and len(self.calls) == self._at:
            self.calls.append(f"{name}!")
            self._at = None
            if self._kill:
                os._exit(17)
            assert self._exc is not None
            raise self._exc
        self.calls.append(name)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._hit("transaction")
        with self.inner.transaction():
            yield

    def close(self) -> None:
        self.inner.close()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if name not in METHODS:
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            self._hit(name)
            return target(*args, **kwargs)

        return call

    def as_storage(self) -> Storage:
        return cast(Storage, self)
