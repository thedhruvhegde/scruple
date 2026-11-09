"""A minimal double-entry ledger. Postings within an entry must sum to zero."""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict


class Posting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account: str
    amount_minor: int


class JournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: int
    kind: str  # capture | refund | chargeback_debit | fee
    txn_id: str
    case_id: str | None
    currency: str
    postings: tuple[Posting, ...]


class UnbalancedEntryError(ValueError):
    pass


class LedgerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[JournalEntry, ...]


class Ledger:
    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._balances: dict[tuple[str, str], int] = defaultdict(int)

    def post(
        self,
        kind: str,
        txn_id: str,
        case_id: str | None,
        currency: str,
        postings: list[tuple[str, int]],
    ) -> JournalEntry:
        if sum(a for _, a in postings) != 0:
            raise UnbalancedEntryError(f"{kind} for {txn_id} does not balance")
        entry = JournalEntry(
            entry_id=len(self._entries) + 1,
            kind=kind,
            txn_id=txn_id,
            case_id=case_id,
            currency=currency,
            postings=tuple(Posting(account=acc, amount_minor=amt) for acc, amt in postings),
        )
        self._entries.append(entry)
        for p in entry.postings:
            self._balances[(p.account, currency)] += p.amount_minor
        return entry

    def balance(self, account: str, currency: str) -> int:
        return self._balances.get((account, currency), 0)

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries)

    def snapshot(self) -> LedgerSnapshot:
        return LedgerSnapshot(entries=tuple(self._entries))

    @classmethod
    def restore(cls, snap: LedgerSnapshot) -> Ledger:
        ledger = cls()
        for e in snap.entries:
            ledger._entries.append(e)
            for p in e.postings:
                ledger._balances[(p.account, e.currency)] += p.amount_minor
        return ledger
