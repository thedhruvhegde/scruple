"""Internal records that every adapter produces.

External systems (Stripe, Adyen, CSV exports, bank files) are mapped into these
records and nothing else. They hold observable facts only: an imported case has no
hidden ground truth, unlike the simulator's `DisputeCase`. Free text is never kept
here; evidence is recorded as the *names* of the evidence fields present.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from scruple.domain import ReasonCode
from scruple.ingest.currency import Currency
from scruple.sim.models import Stage

SourceName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
ExternalId = Annotated[str, StringConstraints(min_length=1, max_length=255)]
ShortText = Annotated[str, StringConstraints(max_length=255)]


class CaseState(StrEnum):
    """Where an external case stands. Mapped per adapter; see docs/INTEGRATIONS.md."""

    OPEN = "open"  # still in progress at the processor or network
    WON = "won"  # merchant kept the funds
    LOST = "lost"  # funds went to the cardholder
    ACCEPTED = "accepted"  # merchant accepted liability without contesting
    PREVENTED = "prevented"  # resolved before becoming a dispute
    CLOSED = "closed"  # an inquiry closed with no chargeback


class ExternalCase(BaseModel):
    """One dispute (or inquiry) as reported by an external system."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: SourceName
    external_id: ExternalId
    payment_ref: ExternalId  # charge id, PSP reference or transaction id
    merchant_ref: ShortText | None = None
    stage: Stage
    state: CaseState
    reason_code: ReasonCode | None  # scruple category; None when the source reason has none
    source_reason: ShortText  # the source's own reason value, verbatim
    network: ShortText | None = None
    network_reason_code: ShortText | None = None
    amount_minor: int = Field(ge=0)
    currency: Currency
    opened_at: AwareDatetime
    evidence_due_at: AwareDatetime | None = None
    evidence_fields: tuple[str, ...] = ()
    evidence_submitted: bool = False
    evidence_past_due: bool = False
    livemode: bool = False

    @property
    def case_id(self) -> str:
        return f"{self.source}:{self.external_id}"


class MovementKind(StrEnum):
    DISPUTE_DEBIT = "dispute_debit"  # funds withdrawn from the merchant
    DISPUTE_CREDIT = "dispute_credit"  # funds reinstated to the merchant
    OTHER = "other"


class LedgerMovement(BaseModel):
    """A balance change tied to a case, from the merchant's point of view.

    `amount_minor` is gross (negative = withdrawn), `fee_minor` is a fee charged
    (positive when assessed), and `net_minor == amount_minor - fee_minor`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: SourceName
    external_id: ExternalId
    case_external_id: ExternalId
    payment_ref: ExternalId
    kind: MovementKind
    amount_minor: int
    fee_minor: int = Field(ge=0)
    net_minor: int
    currency: Currency
    occurred_at: AwareDatetime
    category: ShortText

    @model_validator(mode="after")
    def _net(self) -> LedgerMovement:
        if self.net_minor != self.amount_minor - self.fee_minor:
            raise ValueError("net_minor must equal amount_minor - fee_minor")
        return self

    def journal_postings(
        self, merchant_account: str
    ) -> list[tuple[str, str, str, list[tuple[str, int]]]]:
        """Balanced (kind, txn_id, currency, postings) rows for `scruple.sim.ledger.Ledger`.

        The counter-accounts are `processor:disputes` and `processor:fees`, so every
        row sums to zero by construction.
        """
        rows: list[tuple[str, str, str, list[tuple[str, int]]]] = []
        if self.amount_minor:
            kind = "chargeback_debit" if self.amount_minor < 0 else "chargeback_reversal"
            rows.append(
                (
                    kind,
                    self.payment_ref,
                    self.currency,
                    [
                        (merchant_account, self.amount_minor),
                        ("processor:disputes", -self.amount_minor),
                    ],
                )
            )
        if self.fee_minor:
            rows.append(
                (
                    "fee",
                    self.payment_ref,
                    self.currency,
                    [(merchant_account, -self.fee_minor), ("processor:fees", self.fee_minor)],
                )
            )
        return rows


class IngestBatch(BaseModel):
    """What one adapter call produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: tuple[ExternalCase, ...] = ()
    movements: tuple[LedgerMovement, ...] = ()
