"""Hash-chained, append-only audit log.

Each entry's hash covers its body and the previous entry's hash, so editing, deleting
or reordering any entry breaks every hash after it. Truncating the tail keeps the
chain internally valid; detect that by comparing against an externally anchored head
hash (`verify(expected_head=...)`). See docs/DESIGN.md "Audit" for the threat model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from scruple.audit.models import GENESIS_HASH, AuditEntry
from scruple.clock import Clock
from scruple.hashing import canonical_json, sha256_hex

if TYPE_CHECKING:
    from scruple.storage import Storage


class VerifyResult(BaseModel):
    ok: bool
    entries: int
    head_hash: str
    first_bad_seq: int | None = None
    problem: str | None = None


def compute_hash(entry: AuditEntry) -> str:
    return sha256_hex(canonical_json(entry.hashed_body()))


class AuditLog:
    def __init__(self, storage: Storage, clock: Clock) -> None:
        self._storage = storage
        self._clock = clock

    def append(self, event_type: str, actor: str, payload: dict[str, Any]) -> AuditEntry:
        # Normalise to plain JSON types so what we hash is exactly what we store.
        normalised: dict[str, Any] = json.loads(canonical_json(payload))
        with self._storage.transaction():
            last = self._storage.last_audit()
            seq = 1 if last is None else last.seq + 1
            prev = GENESIS_HASH if last is None else last.entry_hash
            draft = AuditEntry(
                seq=seq,
                at=self._clock.now(),
                event_type=event_type,
                actor=actor,
                payload=normalised,
                prev_hash=prev,
                entry_hash="",
            )
            entry = draft.model_copy(update={"entry_hash": compute_hash(draft)})
            self._storage.append_audit(entry)
        return entry

    def entries(self) -> list[AuditEntry]:
        return self._storage.list_audit()

    def head(self) -> str:
        last = self._storage.last_audit()
        return GENESIS_HASH if last is None else last.entry_hash

    def verify(self, expected_head: str | None = None) -> VerifyResult:
        return verify_entries(self.entries(), expected_head)


def verify_entries(entries: Sequence[AuditEntry], expected_head: str | None = None) -> VerifyResult:
    prev = GENESIS_HASH
    for i, e in enumerate(entries, start=1):
        if e.seq != i:
            return _bad(entries, e.seq, f"sequence gap: expected seq {i}, found {e.seq}")
        if e.prev_hash != prev:
            return _bad(entries, e.seq, "prev_hash does not match the previous entry")
        if compute_hash(e) != e.entry_hash:
            return _bad(entries, e.seq, "entry contents do not match its hash (edited)")
        prev = e.entry_hash
    if expected_head is not None and prev != expected_head:
        return _bad(entries, None, "head hash differs from anchored head (truncated or rewritten)")
    return VerifyResult(ok=True, entries=len(entries), head_hash=prev)


def _bad(entries: Sequence[AuditEntry], seq: int | None, problem: str) -> VerifyResult:
    head = entries[-1].entry_hash if entries else GENESIS_HASH
    return VerifyResult(
        ok=False, entries=len(entries), head_hash=head, first_bad_seq=seq, problem=problem
    )
