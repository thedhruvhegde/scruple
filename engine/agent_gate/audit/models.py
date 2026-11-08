from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

GENESIS_HASH = "0" * 64


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
