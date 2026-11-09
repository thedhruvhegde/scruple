"""Adyen dispute (chargeback) notification adapter.

HMAC scheme, ported from Adyen's own library (Adyen/adyen-python-api-library,
`Adyen/util.py` at 2b708a4, MIT) and checked against its test vectors
(`test/UtilTest.py`, `test/mocks/util/*.json`):

- per notification item, the signing string is the values of `pspReference`,
  `originalReference`, `merchantAccountCode`, `merchantReference`, `amount.value`,
  `amount.currency`, `eventCode`, `success`, joined by ":" (missing fields are "",
  no escaping: the library's colon and backslash vectors pass unescaped)
- HMAC-SHA256 keyed with the hex-decoded HMAC key, base64 encoded, compared in
  constant time with `additionalData.hmacSignature`
- webhooks that sign the whole body put the signature in a header instead
  (`verify_payload_hmac`, same key handling, base64 of HMAC over the raw bytes)

Notification shape and the dispute event list come from Adyen/adyen-openapi
`json/Webhooks-v1.json` at d449f09 (MIT): the webhooks tagged "Dispute".

ASSUMPTION: the (stage, state) for each event and for `disputeStatus` values are
scruple's reading of the one-line summaries in that spec, not Adyen or network rules.
Network reason codes are kept verbatim and never mapped to a category.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from scruple.ingest.currency import normalise_currency
from scruple.ingest.errors import (
    IngestError,
    InputTooLargeError,
    LiveModeRefusedError,
    MalformedInputError,
    SchemaMismatchError,
    SignatureError,
    UnmappedValueError,
)
from scruple.ingest.limits import load_json_object
from scruple.ingest.models import CaseState, ExternalCase
from scruple.sim.models import Stage

SOURCE = "adyen"
MAX_ITEMS = 100
SIGNED_FIELDS = (
    "pspReference",
    "originalReference",
    "merchantAccountCode",
    "merchantReference",
    "value",
    "currency",
    "eventCode",
    "success",
)

EVENT_MAP: Mapping[str, tuple[Stage, CaseState]] = {
    "REQUEST_FOR_INFORMATION": (Stage.INQUIRY, CaseState.OPEN),
    "NOTIFICATION_OF_FRAUD": (Stage.INQUIRY, CaseState.OPEN),
    "NOTIFICATION_OF_CHARGEBACK": (Stage.CHARGEBACK, CaseState.OPEN),
    "CHARGEBACK": (Stage.CHARGEBACK, CaseState.OPEN),
    "DISPUTE_DEFENSE_PERIOD_ENDED": (Stage.CHARGEBACK, CaseState.OPEN),
    "INFORMATION_SUPPLIED": (Stage.REPRESENTMENT, CaseState.OPEN),
    "ISSUER_COMMENTS": (Stage.REPRESENTMENT, CaseState.OPEN),
    "CHARGEBACK_REVERSED": (Stage.REPRESENTMENT, CaseState.WON),
    "ISSUER_RESPONSE_TIMEFRAME_EXPIRED": (Stage.RESOLVED, CaseState.WON),
    "SECOND_CHARGEBACK": (Stage.ARBITRATION, CaseState.OPEN),
    "PREARBITRATION_OPEN": (Stage.ARBITRATION, CaseState.OPEN),
    "PREARBITRATION_DECLINED": (Stage.ARBITRATION, CaseState.OPEN),
    "PREARBITRATION_ACCEPTED": (Stage.RESOLVED, CaseState.ACCEPTED),
    "PREARBITRATION_ISSUER_WITHDRAWN": (Stage.RESOLVED, CaseState.WON),
    "PREARBITRATION_WON": (Stage.RESOLVED, CaseState.WON),
    "PREARBITRATION_LOST": (Stage.RESOLVED, CaseState.LOST),
    "SCHEME_ARBITRATION": (Stage.ARBITRATION, CaseState.OPEN),
    "SCHEME_ARBITRATION_WON": (Stage.RESOLVED, CaseState.WON),
    "SCHEME_ARBITRATION_LOST": (Stage.RESOLVED, CaseState.LOST),
}

DisputeStatus = Literal[
    "Unresponded", "Responded", "Expired", "Undefended", "Accepted", "Pending", "Lost", "Won"
]
# ASSUMPTION: Expired and Undefended mean the defence window closed undefended, so the
# chargeback stands (LOST).
STATUS_MAP: Mapping[str, CaseState] = {
    "Unresponded": CaseState.OPEN,
    "Responded": CaseState.OPEN,
    "Pending": CaseState.OPEN,
    "Expired": CaseState.LOST,
    "Undefended": CaseState.LOST,
    "Accepted": CaseState.ACCEPTED,
    "Lost": CaseState.LOST,
    "Won": CaseState.WON,
}


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=False)


class Amount(_Wire):
    currency: str = Field(min_length=3, max_length=3)
    value: StrictInt


class DisputeAdditionalData(_Wire):
    hmac_signature: str | None = Field(default=None, alias="hmacSignature", max_length=128)
    chargeback_reason_code: str | None = Field(
        default=None, alias="chargebackReasonCode", max_length=32
    )
    chargeback_scheme_code: str | None = Field(
        default=None, alias="chargebackSchemeCode", max_length=32
    )
    rfi_reason_code: str | None = Field(default=None, alias="rfiReasonCode", max_length=32)
    rfi_scheme_code: str | None = Field(default=None, alias="rfiSchemeCode", max_length=32)
    nof_reason_code: str | None = Field(default=None, alias="nofReasonCode", max_length=32)
    nof_scheme_code: str | None = Field(default=None, alias="nofSchemeCode", max_length=32)
    defense_period_ends_at: str | None = Field(
        default=None, alias="defensePeriodEndsAt", max_length=64
    )
    dispute_status: DisputeStatus | None = Field(default=None, alias="disputeStatus")


class NotificationItem(_Wire):
    additional_data: DisputeAdditionalData | None = Field(default=None, alias="additionalData")
    amount: Amount
    event_code: str = Field(alias="eventCode", max_length=64)
    event_date: str = Field(alias="eventDate", max_length=64)
    merchant_account_code: str = Field(alias="merchantAccountCode", max_length=255)
    merchant_reference: str = Field(alias="merchantReference", max_length=255)
    original_reference: str | None = Field(default=None, alias="originalReference", max_length=255)
    psp_reference: str = Field(alias="pspReference", min_length=1, max_length=64)
    success: Literal["true", "false"]


# Vendored schema each wire model must match (contract test).
SCHEMA_OF: dict[type[BaseModel], str] = {
    Amount: "Amount",
    DisputeAdditionalData: "DisputesNotificationAdditionalData",
    NotificationItem: "DisputesNotificationRequestItem",
}


def _key(hmac_key_hex: str) -> bytes:
    if not hmac_key_hex or len(hmac_key_hex) % 2 or len(hmac_key_hex) > 256:
        raise SignatureError("HMAC key must be an even-length hex string")
    try:
        return binascii.a2b_hex(hmac_key_hex)
    except (binascii.Error, ValueError):
        raise SignatureError("HMAC key is not hex") from None


def signing_string(item: Mapping[str, Any]) -> str:
    """The ':'-joined string Adyen signs, from a raw NotificationRequestItem dict."""
    amount = item.get("amount")
    if not isinstance(amount, Mapping):
        raise SchemaMismatchError("notification item has no amount object")
    values = dict(item)
    values["value"] = amount.get("value")
    values["currency"] = amount.get("currency")
    parts: list[str] = []
    for f in SIGNED_FIELDS:
        v = values.get(f, "")
        if v is None or isinstance(v, bool | float | dict | list):
            # The library would str() these; real items never carry them. Refuse.
            raise SchemaMismatchError(f"signed field {f} has an unexpected type")
        parts.append(str(v))
    return ":".join(parts)


def item_signature(item: Mapping[str, Any], hmac_key_hex: str) -> str:
    mac = hmac.new(_key(hmac_key_hex), signing_string(item).encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def verify_item(item: Mapping[str, Any], hmac_keys: Sequence[str]) -> None:
    """Raise `SignatureError` unless the item's `additionalData.hmacSignature` matches."""
    if not hmac_keys:
        raise SignatureError("no Adyen HMAC key configured")
    extra = item.get("additionalData")
    got = extra.get("hmacSignature") if isinstance(extra, Mapping) else None
    if not isinstance(got, str) or not got or len(got) > 128:
        raise SignatureError("notification item has no hmacSignature")
    ok = False
    for k in hmac_keys:
        ok |= hmac.compare_digest(item_signature(item, k).encode(), got.encode("utf-8"))
    if not ok:
        raise SignatureError("hmacSignature does not match")


def verify_payload_hmac(payload: bytes, signature: str | None, hmac_keys: Sequence[str]) -> None:
    """Header-signed webhooks: base64 HMAC-SHA256 over the raw body."""
    if not signature or len(signature) > 128 or not signature.isascii():
        raise SignatureError("missing or malformed HMAC signature header")
    if not hmac_keys:
        raise SignatureError("no Adyen HMAC key configured")
    ok = False
    for k in hmac_keys:
        mac = base64.b64encode(hmac.new(_key(k), payload, hashlib.sha256).digest())
        ok |= hmac.compare_digest(mac, signature.encode("ascii"))
    if not ok:
        raise SignatureError("HMAC signature does not match")


def _when(text: str, what: str) -> datetime:
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise SchemaMismatchError(f"{what} {text[:40]!r} is not ISO 8601") from None
    if dt.tzinfo is None:
        raise SchemaMismatchError(f"{what} has no UTC offset")
    return dt


class Skipped(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    psp_reference: str | None
    reason: str


class AdyenResult(BaseModel):
    """Cases from one notification, plus every item not turned into a case and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: tuple[ExternalCase, ...]
    skipped: tuple[Skipped, ...]


def map_item(item: NotificationItem) -> ExternalCase:
    try:
        stage, state = EVENT_MAP[item.event_code]
    except KeyError:
        raise UnmappedValueError(f"{item.event_code!r} is not a dispute event") from None
    ad = item.additional_data or DisputeAdditionalData()
    if ad.dispute_status is not None:
        state = STATUS_MAP[ad.dispute_status]
    network = ad.chargeback_scheme_code or ad.rfi_scheme_code or ad.nof_scheme_code
    code = ad.chargeback_reason_code or ad.rfi_reason_code or ad.nof_reason_code
    if item.amount.value < 0:
        raise SchemaMismatchError("negative dispute amount")
    due = ad.defense_period_ends_at
    try:
        return ExternalCase(
            source=SOURCE,
            external_id=item.psp_reference,
            payment_ref=item.original_reference or item.psp_reference,
            merchant_ref=item.merchant_account_code,
            stage=stage,
            state=state,
            reason_code=None,
            source_reason=item.event_code,
            network=network.strip() if network else None,
            network_reason_code=code.strip() if code else None,
            amount_minor=item.amount.value,
            currency=normalise_currency(item.amount.currency),
            opened_at=_when(item.event_date, "eventDate"),
            evidence_due_at=_when(due, "defensePeriodEndsAt") if due else None,
        )
    except ValidationError as e:
        raise SchemaMismatchError(f"item does not map: {e.error_count()} errors") from None


def ingest_notification(
    body: bytes | str | Mapping[str, Any],
    hmac_keys: Sequence[str],
    *,
    allow_live: bool = False,
    verify: bool = True,
) -> AdyenResult:
    """Verify and map an Adyen standard notification (`notificationItems` envelope).

    Raises on an envelope problem or any bad signature (one forged item rejects the
    whole delivery). Items that are not dispute events or have `success == "false"`
    are reported in `skipped`, never dropped silently.
    """
    raw = body if isinstance(body, Mapping) else load_json_object(body, what="Adyen notification")
    live = raw.get("live")
    if live not in ("true", "false"):
        raise SchemaMismatchError("notification `live` must be 'true' or 'false'")
    if live == "true" and not allow_live:
        raise LiveModeRefusedError("live Adyen notification refused; only test is enabled")
    items = raw.get("notificationItems")
    if not isinstance(items, list) or not items:
        raise SchemaMismatchError("notificationItems must be a non-empty list")
    if len(items) > MAX_ITEMS:
        raise InputTooLargeError(f"more than {MAX_ITEMS} notification items")
    cases: list[ExternalCase] = []
    skipped: list[Skipped] = []
    for i, wrapper in enumerate(items):
        inner = wrapper.get("NotificationRequestItem") if isinstance(wrapper, Mapping) else None
        if not isinstance(inner, Mapping):
            raise SchemaMismatchError(f"item {i} has no NotificationRequestItem")
        if verify:
            verify_item(inner, hmac_keys)
        try:
            item = NotificationItem.model_validate(inner)
        except ValidationError as e:
            raise SchemaMismatchError(f"item {i}: {e.error_count()} validation errors") from None
        if item.event_code not in EVENT_MAP:
            skipped.append(
                Skipped(index=i, psp_reference=item.psp_reference, reason="not a dispute event")
            )
        elif item.success != "true":
            skipped.append(
                Skipped(index=i, psp_reference=item.psp_reference, reason="success is false")
            )
        else:
            cases.append(map_item(item))
    return AdyenResult(cases=tuple(cases), skipped=tuple(skipped))


# Stages that mean the merchant defended (same set as scruple.calibration.facts).
_CONTEST_STAGES = frozenset({Stage.REPRESENTMENT, Stage.PRE_ARBITRATION, Stage.ARBITRATION})


def fold_adyen_events(events: Sequence[ExternalCase]) -> list[ExternalCase]:
    """One case per payment (`originalReference`) from its dispute events.

    The first event opens the case (id, open time, amount); the last event gives its
    state; the latest defence deadline seen is kept; a case that ever reached
    representment or later counts as defended (ASSUMPTION, docs/BACKTEST.md).
    """
    by_payment: dict[str, list[ExternalCase]] = {}
    for e in events:
        by_payment.setdefault(e.payment_ref, []).append(e)
    cases: list[ExternalCase] = []
    for evs in by_payment.values():
        evs = sorted(evs, key=lambda e: e.opened_at)  # stable: input order breaks ties
        first, last = evs[0], evs[-1]
        due = next((e.evidence_due_at for e in reversed(evs) if e.evidence_due_at), None)
        cases.append(
            last.model_copy(
                update={
                    "external_id": first.external_id,
                    "opened_at": first.opened_at,
                    "evidence_due_at": due,
                    "evidence_submitted": any(e.stage in _CONTEST_STAGES for e in evs),
                    "amount_minor": first.amount_minor,
                    "currency": first.currency,
                }
            )
        )
    if len({c.external_id for c in cases}) != len(cases):
        raise SchemaMismatchError("two Adyen disputes share a first pspReference")
    return cases


__all__ = [
    "EVENT_MAP",
    "AdyenResult",
    "IngestError",
    "MalformedInputError",
    "fold_adyen_events",
    "ingest_notification",
    "item_signature",
    "signing_string",
    "verify_item",
    "verify_payload_hmac",
]
