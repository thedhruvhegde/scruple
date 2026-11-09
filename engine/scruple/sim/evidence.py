"""Evidence kinds and sealed evidence documents.

The kinds are scruple's own vocabulary, distilled from the key-evidence text in the
public reason code dataset (docs/SOURCES.md S1). The mapping from that text to these
kinds is ours (docs/ASSUMPTIONS.md A-15).

Documents are sealed with a SHA-256 content hash over (case id, kind, date, fields).
A document whose fields were altered after sealing fails `hash_ok`. Evidence ids are
derived from the owning case, and `EvidenceVault.resolve` refuses an id that belongs
to another case.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from scruple.hashing import content_hash, sha256_hex


class EvidenceKind(StrEnum):
    RECEIPT = "receipt"
    AUTHORIZATION_RECORD = "authorization_record"
    AVS_CVV_MATCH = "avs_cvv_match"
    DEVICE_IP_MATCH = "device_ip_match"
    ACCOUNT_ACTIVITY = "account_activity"
    PRIOR_UNDISPUTED_HISTORY = "prior_undisputed_history"
    EMV_CHIP_READ = "emv_chip_read"
    TERMINAL_LOG = "terminal_log"
    DELIVERY_PROOF = "delivery_proof"
    PRODUCT_DESCRIPTION = "product_description"
    TERMS_ACCEPTANCE = "terms_acceptance"
    CANCELLATION_RECORD = "cancellation_record"
    COMMUNICATION_LOG = "communication_log"
    REFUND_RECORD = "refund_record"
    SETTLEMENT_RECORD = "settlement_record"
    PRESENTMENT_RECORD = "presentment_record"
    AUTHENTICITY_PROOF = "authenticity_proof"


_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EvidenceError(ValueError):
    """An evidence id that is unknown, repeated, or owned by a different case."""


def seal(case_id: str, kind: EvidenceKind, issued_on: date, fields: dict[str, str]) -> str:
    return content_hash(
        {"case_id": case_id, "kind": kind.value, "issued_on": issued_on.isoformat(), "f": fields}
    )


class EvidenceDocument(BaseModel):
    """One piece of evidence. `content_hash` seals (case, kind, date, fields)."""

    model_config = _FROZEN

    doc_id: str
    case_id: str
    kind: EvidenceKind
    issued_on: date
    fields: dict[str, str]
    content_hash: str

    def computed_hash(self) -> str:
        return seal(self.case_id, self.kind, self.issued_on, self.fields)

    @property
    def hash_ok(self) -> bool:
        return self.computed_hash() == self.content_hash


def doc_id_for(case_id: str, kind: EvidenceKind, index: int) -> str:
    return f"ev-{sha256_hex(f'{case_id}/{index}/{kind.value}')[:16]}"


def make_document(
    case_id: str, kind: EvidenceKind, issued_on: date, fields: dict[str, str], index: int
) -> EvidenceDocument:
    """A sealed document. The id is derived from the case, so it is unique per case."""
    return EvidenceDocument(
        doc_id=doc_id_for(case_id, kind, index),
        case_id=case_id,
        kind=kind,
        issued_on=issued_on,
        fields=fields,
        content_hash=seal(case_id, kind, issued_on, fields),
    )


class EvidenceVault:
    """Every document by id. Resolution is always scoped to one case."""

    def __init__(self, docs: Iterable[EvidenceDocument] = ()) -> None:
        self._docs: dict[str, EvidenceDocument] = {}
        for d in docs:
            self.add(d)

    def add(self, doc: EvidenceDocument) -> None:
        if doc.doc_id in self._docs:
            raise EvidenceError(f"duplicate evidence id {doc.doc_id}")
        self._docs[doc.doc_id] = doc

    def __len__(self) -> int:
        return len(self._docs)

    def for_case(self, case_id: str) -> tuple[EvidenceDocument, ...]:
        return tuple(d for d in self._docs.values() if d.case_id == case_id)

    def resolve(self, case_id: str, ids: Iterable[str]) -> tuple[EvidenceDocument, ...]:
        out: list[EvidenceDocument] = []
        seen: set[str] = set()
        for i in ids:
            doc = self._docs.get(i)
            if doc is None:
                raise EvidenceError(f"unknown evidence id {i}")
            if doc.case_id != case_id:
                raise EvidenceError(f"evidence {i} belongs to another case")
            if i in seen:
                raise EvidenceError(f"evidence {i} submitted twice")
            seen.add(i)
            out.append(doc)
        return tuple(out)
