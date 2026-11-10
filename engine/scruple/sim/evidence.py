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

import hashlib
import json.encoder as _json_encoder
from collections.abc import Iterable
from datetime import date, timedelta
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


def seal_reference(
    case_id: str, kind: EvidenceKind, issued_on: date, fields: dict[str, str]
) -> str:
    """The seal as defined: SHA-256 of the canonical JSON of (case, kind, date, fields)."""
    return content_hash(
        {"case_id": case_id, "kind": kind.value, "issued_on": issued_on.isoformat(), "f": fields}
    )


_q = _json_encoder.encode_basestring  # the escaper json.dumps uses with ensure_ascii=False


def seal(case_id: str, kind: EvidenceKind, issued_on: date, fields: dict[str, str]) -> str:
    """Same bytes as `seal_reference`, built without the general encoder (038: about 1.7x
    faster on the generator's hot path; a property test checks the two always agree)."""
    try:  # the escaper raises TypeError on anything but str (then use the reference)
        body = ",".join([_q(k) + ":" + _q(fields[k]) for k in sorted(fields)])
        cid = _q(case_id)
    except TypeError:
        return seal_reference(case_id, kind, issued_on, fields)
    text = (
        '{"case_id":'
        + cid
        + ',"f":{'
        + body
        + '},"issued_on":"'
        + issued_on.isoformat()
        + '","kind":'
        + _q(kind.value)
        + "}"
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        self._by_case: dict[str, list[EvidenceDocument]] = {}
        for d in docs:
            self.add(d)

    def add(self, doc: EvidenceDocument) -> None:
        if doc.doc_id in self._docs:
            raise EvidenceError(f"duplicate evidence id {doc.doc_id}")
        self._docs[doc.doc_id] = doc
        self._by_case.setdefault(doc.case_id, []).append(doc)

    def __len__(self) -> int:
        return len(self._docs)

    def for_case(self, case_id: str) -> tuple[EvidenceDocument, ...]:
        return tuple(self._by_case.get(case_id, ()))

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


class Forgery(StrEnum):
    """How a forged document was made (hidden ground truth)."""

    TAMPERED = "tampered"  # fields edited after sealing: the hash no longer matches
    WRONG_ORDER = "wrong_order"  # resealed copy of another order: amount does not match
    BACKDATED = "backdated"  # resealed with a date before the transaction


def _reseal(doc: EvidenceDocument, issued_on: date, fields: dict[str, str]) -> EvidenceDocument:
    return doc.model_copy(
        update={
            "issued_on": issued_on,
            "fields": fields,
            "content_hash": seal(doc.case_id, doc.kind, issued_on, fields),
        }
    )


def forge(doc: EvidenceDocument, how: Forgery, processed_on: date) -> EvidenceDocument:
    """A forged variant of `doc` (same id, same case)."""
    match how:
        case Forgery.TAMPERED:
            return doc.model_copy(update={"fields": {**doc.fields, "edited": "1"}})
        case Forgery.WRONG_ORDER:
            amt = str(int(doc.fields.get("amount_minor", "0")) + 1_000)
            return _reseal(doc, doc.issued_on, {**doc.fields, "amount_minor": amt})
        case Forgery.BACKDATED:
            return _reseal(doc, processed_on - timedelta(days=3), doc.fields)


def _amount_fits(kind: EvidenceKind, amt: str, captured_minor: int) -> bool:
    if kind is EvidenceKind.REFUND_RECORD:
        return amt.isdigit() and 0 < int(amt) <= captured_minor
    return amt == str(captured_minor)


def document_problems(
    doc: EvidenceDocument, amount_minor: int, currency: str, processed_on: date
) -> tuple[str, ...]:
    """Checks anyone can run on a document. An empty tuple means it looks genuine.

    `amount_minor` is the captured (order) amount, which receipts and records carry. A
    refund record carries the amount refunded instead, which may be any part of the
    capture (a partial dispute refunds only the disputed amount, A-23): it is a problem
    only when it is not a positive amount within the capture."""
    out: list[str] = []
    if not doc.hash_ok:
        out.append("hash_mismatch")
    amt = doc.fields.get("amount_minor")
    if amt is not None and not _amount_fits(doc.kind, amt, amount_minor):
        out.append("amount_mismatch")
    ccy = doc.fields.get("currency")
    if ccy is not None and ccy != currency:
        out.append("currency_mismatch")
    if doc.issued_on < processed_on:
        out.append("dated_before_transaction")
    return tuple(out)
