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

from scruple.audit.models import LEGACY_GENESIS_TENANT, AuditEntry, genesis_hash
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
        # The chain is the storage's tenant's; it starts from that tenant's genesis.
        self.tenant: str = getattr(storage, "tenant", LEGACY_GENESIS_TENANT)
        self.genesis = genesis_hash(self.tenant)

    def append(self, event_type: str, actor: str, payload: dict[str, Any]) -> AuditEntry:
        # Normalise to plain JSON types so what we hash is exactly what we store.
        normalised: dict[str, Any] = json.loads(canonical_json(payload))
        with self._storage.transaction():
            last = self._storage.last_audit()
            seq = 1 if last is None else last.seq + 1
            prev = self.genesis if last is None else last.entry_hash
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
        return self.genesis if last is None else last.entry_hash

    def verify(self, expected_head: str | None = None) -> VerifyResult:
        return verify_entries(self.entries(), expected_head, tenant=self.tenant)


def verify_entries(
    entries: Sequence[AuditEntry],
    expected_head: str | None = None,
    *,
    tenant: str = LEGACY_GENESIS_TENANT,
) -> VerifyResult:
    """Verify `tenant`'s chain from its genesis (`genesis_hash(tenant)`).

    `first_bad_seq` is the first position (1-based) that cannot be trusted. It is the
    position, not the seq the entry claims: after a deletion or a swap at position i,
    the entry found there claims seq i+1, but entry i is the one that is missing."""
    genesis = genesis_hash(tenant)
    prev = genesis
    for i, e in enumerate(entries, start=1):
        if e.seq != i:
            return _bad(entries, i, f"sequence gap: expected seq {i}, found {e.seq}", genesis)
        if e.prev_hash != prev:
            why = (
                f"first entry does not start from tenant {tenant!r}'s genesis "
                "(copied from another tenant?)"
                if i == 1
                else "prev_hash does not match the previous entry"
            )
            return _bad(entries, i, why, genesis)
        if compute_hash(e) != e.entry_hash:
            return _bad(entries, i, "entry contents do not match its hash (edited)", genesis)
        prev = e.entry_hash
    if expected_head is not None and prev != expected_head:
        why = "head hash differs from anchored head (truncated or rewritten)"
        return _bad(entries, None, why, genesis)
    return VerifyResult(ok=True, entries=len(entries), head_hash=prev)


def _bad(
    entries: Sequence[AuditEntry], seq: int | None, problem: str, genesis: str
) -> VerifyResult:
    head = entries[-1].entry_hash if entries else genesis
    return VerifyResult(
        ok=False, entries=len(entries), head_hash=head, first_bad_seq=seq, problem=problem
    )
