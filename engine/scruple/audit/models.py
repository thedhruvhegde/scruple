from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

GENESIS_HASH = "0" * 64
"""The default tenant's genesis: the `prev_hash` of its first entry (unchanged since
before tenancy, so every chain written by an earlier version still verifies)."""

GENESIS_DOMAIN = b"scruple-audit-genesis"
LEGACY_GENESIS_TENANT = "default"


def genesis_hash(tenant: str = LEGACY_GENESIS_TENANT) -> str:
    """The `prev_hash` a tenant's chain starts from.

    Every tenant other than "default" starts from
    `SHA-256("scruple-audit-genesis" || 0x00 || tenant_id)`, so its chain is bound to
    its tenant: rows copied into another tenant (whose genesis differs) fail
    verification at the first entry. Tenant ids never contain a NUL byte, so the
    encoding is unambiguous.

    "default" keeps `GENESIS_HASH` (all zeros). Databases from before tenancy hold one
    chain, which migration 0003 assigned to "default"; keeping its genesis is the
    migration path (no history is rewritten). The cost: the default chain is not bound
    by its genesis. Copying another tenant's rows into "default" still fails (their
    first `prev_hash` is that tenant's genesis, not zeros); a default chain copied into
    another tenant fails too. Only a chain copied between two databases' default
    tenants is not told apart, which is what an externally anchored head is for."""
    if tenant == LEGACY_GENESIS_TENANT:
        return GENESIS_HASH
    return hashlib.sha256(GENESIS_DOMAIN + b"\x00" + tenant.encode("utf-8")).hexdigest()


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    at: datetime
    event_type: str
    actor: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str

    def hashed_body(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "event_type": self.event_type,
            "actor": self.actor,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }
