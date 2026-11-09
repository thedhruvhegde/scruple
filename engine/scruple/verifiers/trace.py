"""Append-only, hash-chained episode trace: the input every dispute verifier reads.

The environment, never the agent, writes the trace. Every tool call (including
refused ones and calls to tools that do not exist), every lifecycle transition,
every journal posting, every evidence document that appears, every incident the
ledger caught and every clock movement becomes one `TraceEvent`, stamped with the
simulator's date (not a time the agent supplied).

Each event carries the hash of the previous one, so deleting, reordering or editing
an event breaks the chain (`Trace.verify`, `verify_events`). The chain protects the
record after the episode; during the episode the agent has no handle on it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.hashing import content_hash

GENESIS = "0" * 64


class Actor(StrEnum):
    AGENT = "agent"  # the agent called a tool
    ENV = "env"  # the environment recorded a consequence
    ISSUER = "issuer"  # the issuer or network decided something
    CLOCK = "clock"  # simulated time moved


class EventKind(StrEnum):
    TOOL_CALL = "tool_call"  # data: result summary; ok/error set
    TRANSITION = "transition"  # data: from_stage, event, to_stage, on
    POSTING = "posting"  # data: entry_id, kind, currency, postings, case_id
    EVIDENCE_ADDED = "evidence_added"  # data: doc_id, kind, source (env|agent|refund)
    INCIDENT = "incident"  # data: kind, detail
    CLOCK = "clock"  # data: from, to
    STATE = "state"  # data: stage, awaiting, returned_minor, deadline_due
    PROTECTED_READ = "protected_read"  # data: what was read (ground truth access)
    CLOSE = "close"  # data: reason


class TraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    day: date
    actor: Actor
    kind: EventKind
    tool: str | None = None
    args: dict[str, Any] | None = None
    ok: bool = True
    error: str | None = None
    data: dict[str, Any] = {}
    prev_hash: str
    hash: str

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"hash"})


def event_hash(body: dict[str, Any]) -> str:
    return content_hash(body)


def verify_events(events: Iterable[TraceEvent]) -> tuple[int, ...]:
    """Sequence numbers where the chain is broken (empty: intact)."""
    bad: list[int] = []
    prev = GENESIS
    for i, e in enumerate(events):
        if e.seq != i or e.prev_hash != prev or event_hash(e.body()) != e.hash:
            bad.append(e.seq)
        prev = e.hash
    return tuple(bad)


class Trace:
    """Append-only. There is no update or delete path."""

    def __init__(self) -> None:
        self._events: list[TraceEvent] = []

    def append(
        self,
        day: date,
        actor: Actor,
        kind: EventKind,
        *,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
        ok: bool = True,
        error: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> TraceEvent:
        prev = self._events[-1].hash if self._events else GENESIS
        body: dict[str, Any] = {
            "seq": len(self._events),
            "day": day.isoformat(),
            "actor": actor.value,
            "kind": kind.value,
            "tool": tool,
            "args": args,
            "ok": ok,
            "error": error,
            "data": data or {},
            "prev_hash": prev,
        }
        ev = TraceEvent.model_validate({**body, "hash": event_hash(body)})
        self._events.append(ev)
        return ev

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    @property
    def head(self) -> str:
        return self._events[-1].hash if self._events else GENESIS

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[TraceEvent]:
        return iter(tuple(self._events))

    def verify(self) -> tuple[int, ...]:
        return verify_events(self._events)


__all__ = [
    "GENESIS",
    "Actor",
    "EventKind",
    "Trace",
    "TraceEvent",
    "event_hash",
    "verify_events",
]
