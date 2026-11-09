"""Merchant-side card ledger for the dispute engine.

Wraps the double-entry `Ledger` (every entry balances) and adds per-transaction
tracking of what has gone back to the cardholder, so money-movement invariants can
be checked as postings happen rather than only after a run.

Double refunds (roadmap 027): a merchant refund that would return more than was
captured is refused (`DoubleRefundError`). A network chargeback debit cannot be
refused, so when it over-credits the cardholder the ledger records a `DoubleRefund`
violation; `over_credited()` lists transactions that are still over-credited.

Multi-currency (029): limits are tracked exactly in the transaction (presentment)
currency, while journal entries are posted in the merchant's settlement currency,
converted with `FxTable` at the rate of the posting day. So a chargeback debited at
one rate and reversed at another leaves the merchant an FX gain or loss, exactly as
at settlement, without ever blurring the double-refund check.

Accounts: `merchant:<id>`, `cardholder:<txn>`, `processor:fees`, `network:fees`.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from pydantic import BaseModel, ConfigDict

from scruple.sim.fx import FxTable
from scruple.sim.ledger import JournalEntry, Ledger

FEE_CURRENCY = "USD"


class DoubleRefundError(ValueError):
    """A refund that would return more to the cardholder than was captured."""


class DoubleRefund(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    txn_id: str
    case_id: str
    entry_id: int  # the chargeback debit that over-credited
    over_by: int  # txn currency minor units


class CardLedger:
    def __init__(self, fx: FxTable | None = None) -> None:
        self.ledger = Ledger()
        self.fx = fx or FxTable()
        self._captured: dict[str, int] = {}
        self._currency: dict[str, str] = {}
        self._settle: dict[str, str] = {}
        self._merchant: dict[str, str] = {}
        # net amount returned to the cardholder per txn (refunds + chargebacks - reversals)
        self._returned: dict[str, int] = defaultdict(int)
        self._violations: list[DoubleRefund] = []

    # --- queries ---------------------------------------------------------
    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return self.ledger.entries

    def captured(self, txn_id: str) -> int:
        return self._captured[txn_id]

    def returned(self, txn_id: str) -> int:
        return self._returned[txn_id]

    def currency(self, txn_id: str) -> str:
        return self._currency[txn_id]

    def settlement_currency(self, txn_id: str) -> str:
        return self._settle[txn_id]

    def headroom(self, txn_id: str) -> int:
        """How much more may go back to the cardholder before it exceeds the capture."""
        return self._captured[txn_id] - self._returned[txn_id]

    def balance(self, account: str, currency: str) -> int:
        return self.ledger.balance(account, currency)

    def currency_totals(self) -> dict[str, int]:
        """Sum of every balance per currency. Always zero for a sound ledger."""
        totals: dict[str, int] = defaultdict(int)
        for e in self.ledger.entries:
            for p in e.postings:
                totals[e.currency] += p.amount_minor
        return dict(totals)

    # --- postings --------------------------------------------------------
    def capture(
        self,
        txn_id: str,
        merchant_id: str,
        amount: int,
        currency: str,
        settlement_currency: str | None = None,
        on: date | None = None,
    ) -> JournalEntry:
        if txn_id in self._captured:
            raise ValueError(f"{txn_id} already captured")
        if amount <= 0:
            raise ValueError("capture must be positive")
        self._captured[txn_id] = amount
        self._currency[txn_id] = currency
        self._settle[txn_id] = settlement_currency or currency
        self._merchant[txn_id] = merchant_id
        posted = self._settled(txn_id, amount, on)
        return self.ledger.post(
            "capture",
            txn_id,
            None,
            self._settle[txn_id],
            [(f"cardholder:{txn_id}", -posted), (f"merchant:{merchant_id}", posted)],
        )

    def _settled(self, txn_id: str, amount: int, on: date | None) -> int:
        """`amount` (txn currency) in the settlement currency on day `on`."""
        src, dst = self._currency[txn_id], self._settle[txn_id]
        if src == dst:
            return amount
        if on is None:
            raise ValueError(f"{txn_id} settles in {dst}: a posting date is required")
        return self.fx.convert(amount, src, dst, on)

    def refund(
        self, txn_id: str, case_id: str | None, amount: int, on: date | None = None
    ) -> JournalEntry:
        """A merchant-initiated refund. Strict: refuses anything above the headroom."""
        if amount > self.headroom(txn_id):
            raise DoubleRefundError(
                f"refund of {amount} on {txn_id} exceeds the {self.headroom(txn_id)} "
                "not yet returned to the cardholder"
            )
        return self.to_cardholder("refund", txn_id, case_id, amount, on)

    def chargeback_debit(
        self, txn_id: str, case_id: str, amount: int, on: date | None = None
    ) -> JournalEntry:
        """A network-forced debit. It always posts; if it over-credits the cardholder the
        ledger records a `DoubleRefund` violation instead of refusing."""
        over = amount - max(0, self.headroom(txn_id))
        entry = self.to_cardholder("chargeback_debit", txn_id, case_id, amount, on)
        if over > 0:
            self._violations.append(
                DoubleRefund(txn_id=txn_id, case_id=case_id, entry_id=entry.entry_id, over_by=over)
            )
        return entry

    @property
    def violations(self) -> tuple[DoubleRefund, ...]:
        return tuple(self._violations)

    def over_credited(self) -> dict[str, int]:
        """Transactions where the cardholder currently holds more than was captured."""
        return {
            t: r - self._captured[t] for t, r in self._returned.items() if r > self._captured[t]
        }

    def to_cardholder(
        self, kind: str, txn_id: str, case_id: str | None, amount: int, on: date | None = None
    ) -> JournalEntry:
        """Move `amount` (txn currency) from merchant to cardholder.
        Prefer `refund` / `chargeback_debit`, which add the double-refund checks."""
        if amount <= 0:
            raise ValueError(f"{kind} must be positive")
        posted = self._settled(txn_id, amount, on)
        self._returned[txn_id] += amount
        m = self._merchant[txn_id]
        return self.ledger.post(
            kind,
            txn_id,
            case_id,
            self._settle[txn_id],
            [(f"merchant:{m}", -posted), (f"cardholder:{txn_id}", posted)],
        )

    def to_merchant(
        self, kind: str, txn_id: str, case_id: str | None, amount: int, on: date | None = None
    ) -> JournalEntry:
        """Move `amount` (txn currency) back from cardholder to merchant (reversal)."""
        if amount <= 0:
            raise ValueError(f"{kind} must be positive")
        if amount > self._returned[txn_id]:
            raise ValueError(f"{kind} of {amount} exceeds what the cardholder received")
        posted = self._settled(txn_id, amount, on)
        self._returned[txn_id] -= amount
        m = self._merchant[txn_id]
        return self.ledger.post(
            kind,
            txn_id,
            case_id,
            self._settle[txn_id],
            [(f"cardholder:{txn_id}", -posted), (f"merchant:{m}", posted)],
        )

    def fee(
        self, kind: str, txn_id: str, case_id: str | None, amount_usd: int, payee: str
    ) -> JournalEntry | None:
        if amount_usd < 0:
            raise ValueError("fees cannot be negative")
        if amount_usd == 0:
            return None
        m = self._merchant[txn_id]
        return self.ledger.post(
            kind,
            txn_id,
            case_id,
            FEE_CURRENCY,
            [(f"merchant:{m}", -amount_usd), (payee, amount_usd)],
        )
