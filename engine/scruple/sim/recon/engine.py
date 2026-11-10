"""Event-sourced reconciliation engine for one bank account and one statement period.

The engine parses the bank's statement file with the real parsers (camt.053 or
BAI2), books the cash book onto a double-entry `Ledger` (`cash:bank` against each
line's offset account, plus the opening balance against `equity:opening`), and then
records the reconciler's work as events:

- `Matched` / `Unmatched`: a group of statement lines against a group of book lines
- `Adjusted`: an adjusting entry, `cash:bank` +amount against an offset account
  chosen by its kind (fee: `expense:bank_fees`, fx: `fx:gain_loss`, missing and
  partial: `ar:<invoice>`, or `suspense:unidentified` with no invoice;
  duplicate_reversal: the reversed book line's own offset). It cites statement lines
  (its evidence) and book lines (the ones it corrects).
- `MarkedTiming`: book lines left outstanding (on the bank's next statement)
- `Escalated`: lines a human must resolve with the bank or the business

The engine refuses malformed actions (unknown ids, a line already matched, an
adjustment of zero) and changes nothing when it does. It does not judge whether an
adjustment is justified: that is the verifier's job (`scruple.verifiers.recon`), so a
fabricated entry is expressible and can be caught. `ReconEngine.replay(case, events)`
rebuilds the state exactly.

Tie-out: bank closing balance = book cash balance - timing items - escalated book
lines + escalated statement lines. The difference is reported, never forced to zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from scruple.hashing import content_hash
from scruple.sim.ledger import JournalEntry, Ledger
from scruple.sim.recon.models import ADJUST_KINDS, AdjustKind, LedgerLine, ReconCase
from scruple.sim.recon.statement import ParsedStatement, StatementLine, read_statement

CASH = "cash:bank"
OPENING = "equity:opening"
FEE_ACCOUNT = "expense:bank_fees"
FX_ACCOUNT = "fx:gain_loss"
SUSPENSE = "suspense:unidentified"

_EV = ConfigDict(frozen=True, extra="forbid")


class Matched(BaseModel):
    model_config = _EV
    type: Literal["matched"] = "matched"
    match_id: str
    statement_line_ids: tuple[str, ...]
    ledger_line_ids: tuple[str, ...]


class Unmatched(BaseModel):
    model_config = _EV
    type: Literal["unmatched"] = "unmatched"
    match_id: str


class Adjusted(BaseModel):
    model_config = _EV
    type: Literal["adjusted"] = "adjusted"
    adjustment_id: str
    kind: AdjustKind
    amount_minor: int
    offset_account: str
    statement_line_ids: tuple[str, ...]
    ledger_line_ids: tuple[str, ...]
    note: str = ""


class MarkedTiming(BaseModel):
    model_config = _EV
    type: Literal["marked_timing"] = "marked_timing"
    ledger_line_ids: tuple[str, ...]


class Escalated(BaseModel):
    model_config = _EV
    type: Literal["escalated"] = "escalated"
    escalation_id: str
    statement_line_ids: tuple[str, ...]
    ledger_line_ids: tuple[str, ...]
    reason: str = ""


ReconEvent = Annotated[
    Matched | Unmatched | Adjusted | MarkedTiming | Escalated, Field(discriminator="type")
]
_EVENTS: TypeAdapter[tuple[ReconEvent, ...]] = TypeAdapter(tuple[ReconEvent, ...])


# --- actions --------------------------------------------------------------------------
class Match(BaseModel):
    model_config = _EV
    kind: Literal["match"] = "match"
    statement_line_ids: tuple[str, ...] = Field(min_length=1)
    ledger_line_ids: tuple[str, ...] = Field(min_length=1)


class Unmatch(BaseModel):
    model_config = _EV
    kind: Literal["unmatch"] = "unmatch"
    match_id: str


class Adjust(BaseModel):
    model_config = _EV
    kind: Literal["adjust"] = "adjust"
    adjust_kind: AdjustKind
    amount_minor: int
    statement_line_ids: tuple[str, ...] = ()
    ledger_line_ids: tuple[str, ...] = ()
    invoice_id: str | None = None
    note: str = ""


class MarkTiming(BaseModel):
    model_config = _EV
    kind: Literal["mark_timing"] = "mark_timing"
    ledger_line_ids: tuple[str, ...] = Field(min_length=1)


class Escalate(BaseModel):
    model_config = _EV
    kind: Literal["escalate"] = "escalate"
    statement_line_ids: tuple[str, ...] = ()
    ledger_line_ids: tuple[str, ...] = ()
    reason: str = ""


ReconAction = Annotated[
    Match | Unmatch | Adjust | MarkTiming | Escalate, Field(discriminator="kind")
]


class ReconError(StrEnum):
    UNKNOWN_LINE = "unknown_line"
    ALREADY_HANDLED = "already_handled"
    UNKNOWN_MATCH = "unknown_match"
    UNKNOWN_INVOICE = "unknown_invoice"
    BAD_ADJUSTMENT = "bad_adjustment"
    EMPTY = "empty"


class ReconResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    action: str
    error: ReconError | None = None
    detail: str = ""
    id: str | None = None
    entries: tuple[int, ...] = ()


class ReconOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    matches: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]
    adjustments: tuple[Adjusted, ...]
    timing: tuple[str, ...]
    escalated_statement: tuple[str, ...]
    escalated_ledger: tuple[str, ...]
    book_balance_minor: int
    bank_closing_minor: int
    difference_minor: int  # bank closing minus the adjusted book balance
    unhandled_statement: tuple[str, ...]
    unhandled_ledger: tuple[str, ...]


@dataclass
class _State:
    matches: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=dict)
    adjustments: list[Adjusted] = field(default_factory=list)
    timing: list[str] = field(default_factory=list)
    escalations: list[Escalated] = field(default_factory=list)
    n_match: int = 0


class ReconEngine:
    def __init__(self, case: ReconCase) -> None:
        self.case = case
        self.statement: ParsedStatement = read_statement(case.statement_format, case.statement_file)
        self.lines: dict[str, StatementLine] = {x.line_id: x for x in self.statement.lines}
        self.book: dict[str, LedgerLine] = {x.line_id: x for x in case.ledger_lines}
        self.invoices = {i.invoice_id: i for i in case.invoices}
        self.ledger = Ledger()
        self._st = _State()
        self._events: list[ReconEvent] = []
        ccy = case.currency
        self.ledger.post(
            "opening", "opening", case.case_id, ccy,
            [(CASH, case.opening_balance_minor), (OPENING, -case.opening_balance_minor)],
        )  # fmt: skip
        for x in case.ledger_lines:
            self.ledger.post(
                "book", x.line_id, case.case_id, ccy,
                [(CASH, x.amount_minor), (x.offset_account, -x.amount_minor)],
            )  # fmt: skip
        self.opening_entries = len(self.ledger.entries)

    @classmethod
    def replay(cls, case: ReconCase, events: tuple[ReconEvent, ...]) -> ReconEngine:
        eng = cls(case)
        for ev in events:
            eng._emit(ev)
        return eng

    # --- event log ------------------------------------------------------------------
    def _emit(self, ev: ReconEvent) -> None:
        self._events.append(ev)
        st = self._st
        match ev:
            case Matched():
                st.n_match += 1
                st.matches[ev.match_id] = (ev.statement_line_ids, ev.ledger_line_ids)
            case Unmatched():
                del st.matches[ev.match_id]
            case Adjusted():
                st.adjustments.append(ev)
                self.ledger.post(
                    "recon_adjustment", ev.adjustment_id, self.case.case_id, self.case.currency,
                    [(CASH, ev.amount_minor), (ev.offset_account, -ev.amount_minor)],
                )  # fmt: skip
            case MarkedTiming():
                st.timing += list(ev.ledger_line_ids)
            case Escalated():
                st.escalations.append(ev)

    @property
    def events(self) -> tuple[ReconEvent, ...]:
        return tuple(self._events)

    def events_json(self) -> str:
        return json.dumps([e.model_dump(mode="json") for e in self._events], sort_keys=True)

    @staticmethod
    def events_from_json(raw: str | bytes) -> tuple[ReconEvent, ...]:
        return _EVENTS.validate_json(raw)

    def state_digest(self) -> str:
        return content_hash(
            {
                "events": [e.model_dump(mode="json") for e in self._events],
                "ledger": [e.model_dump(mode="json") for e in self.ledger.entries],
            }
        )

    # --- queries ----------------------------------------------------------------------
    @property
    def journal(self) -> tuple[JournalEntry, ...]:
        return self.ledger.entries

    def _matched(self) -> tuple[set[str], set[str]]:
        s: set[str] = set()
        lg: set[str] = set()
        for a, b in self._st.matches.values():
            s |= set(a)
            lg |= set(b)
        return s, lg

    def _escalated(self) -> tuple[set[str], set[str]]:
        s = {x for e in self._st.escalations for x in e.statement_line_ids}
        lg = {x for e in self._st.escalations for x in e.ledger_line_ids}
        return s, lg

    def handled(self) -> tuple[set[str], set[str]]:
        ms, ml = self._matched()
        es, el = self._escalated()
        adj_s = {x for a in self._st.adjustments for x in a.statement_line_ids}
        adj_l = {x for a in self._st.adjustments for x in a.ledger_line_ids}
        return ms | es | adj_s, ml | el | adj_l | set(self._st.timing)

    def book_balance(self) -> int:
        return self.ledger.balance(CASH, self.case.currency)

    def bank_closing(self) -> int:
        c = self.statement.closing_balance_minor
        if c is None:  # pragma: no cover - the generator always writes one
            c = (self.statement.opening_balance_minor or 0) + sum(
                x.amount_minor for x in self.statement.lines
            )
        return c

    def difference(self) -> int:
        es, el = self._escalated()
        adjusted_book = (
            self.book_balance()
            - sum(self.book[x].amount_minor for x in self._st.timing)
            - sum(self.book[x].amount_minor for x in el)
            + sum(self.lines[x].amount_minor for x in es)
        )
        return self.bank_closing() - adjusted_book

    def status(self) -> dict[str, object]:
        hs, hl = self.handled()
        return {
            "book_balance_minor": self.book_balance(),
            "bank_closing_minor": self.bank_closing(),
            "difference_minor": self.difference(),
            "matches": {
                k: {"statement": list(a), "ledger": list(b)}
                for k, (a, b) in self._st.matches.items()
            },
            "adjustments": [a.adjustment_id for a in self._st.adjustments],
            "timing": list(self._st.timing),
            "escalations": [e.escalation_id for e in self._st.escalations],
            "unhandled_statement_lines": [x for x in self.lines if x not in hs],
            "unhandled_ledger_lines": [x for x in self.book if x not in hl],
        }

    def outcome(self) -> ReconOutcome:
        hs, hl = self.handled()
        es, el = self._escalated()
        return ReconOutcome(
            matches=tuple(self._st.matches.values()),
            adjustments=tuple(self._st.adjustments),
            timing=tuple(self._st.timing),
            escalated_statement=tuple(sorted(es)),
            escalated_ledger=tuple(sorted(el)),
            book_balance_minor=self.book_balance(),
            bank_closing_minor=self.bank_closing(),
            difference_minor=self.difference(),
            unhandled_statement=tuple(x for x in self.lines if x not in hs),
            unhandled_ledger=tuple(x for x in self.book if x not in hl),
        )

    # --- actions ----------------------------------------------------------------------
    def act(self, action: ReconAction) -> ReconResult:
        def refuse(err: ReconError, detail: str = "") -> ReconResult:
            return ReconResult(ok=False, action=action.kind, error=err, detail=detail)

        s_ids = tuple(getattr(action, "statement_line_ids", ()))
        l_ids = tuple(getattr(action, "ledger_line_ids", ()))
        bad = [x for x in s_ids if x not in self.lines] + [x for x in l_ids if x not in self.book]
        if bad:
            return refuse(ReconError.UNKNOWN_LINE, ", ".join(bad))
        if len(set(s_ids)) != len(s_ids) or len(set(l_ids)) != len(l_ids):
            return refuse(ReconError.BAD_ADJUSTMENT, "a line is listed twice")
        ms, ml = self._matched()
        es, el = self._escalated()
        timing = set(self._st.timing)
        before = len(self.ledger.entries)
        new_id: str | None = None
        match action:
            case Match():
                taken = [x for x in s_ids if x in ms | es] + [
                    x for x in l_ids if x in ml | el | timing
                ]
                if taken:
                    return refuse(ReconError.ALREADY_HANDLED, ", ".join(taken))
                new_id = f"M-{self._st.n_match + 1}"
                self._emit(
                    Matched(match_id=new_id, statement_line_ids=s_ids, ledger_line_ids=l_ids)
                )
            case Unmatch():
                if action.match_id not in self._st.matches:
                    return refuse(ReconError.UNKNOWN_MATCH, action.match_id)
                self._emit(Unmatched(match_id=action.match_id))
            case Adjust():
                if action.amount_minor == 0 or action.adjust_kind not in ADJUST_KINDS:
                    return refuse(ReconError.BAD_ADJUSTMENT, "amount must be non-zero")
                if [x for x in s_ids if x in es]:
                    return refuse(ReconError.ALREADY_HANDLED, "cited line is escalated")
                if action.invoice_id is not None and action.invoice_id not in self.invoices:
                    return refuse(ReconError.UNKNOWN_INVOICE, action.invoice_id)
                offset = self._offset(action)
                if offset is None:
                    return refuse(
                        ReconError.BAD_ADJUSTMENT,
                        "duplicate_reversal needs exactly one ledger line",
                    )
                new_id = f"ADJ-{len(self._st.adjustments) + 1}"
                self._emit(
                    Adjusted(
                        adjustment_id=new_id,
                        kind=action.adjust_kind,
                        amount_minor=action.amount_minor,
                        offset_account=offset,
                        statement_line_ids=s_ids,
                        ledger_line_ids=l_ids,
                        note=action.note,
                    )
                )
            case MarkTiming():
                taken = [x for x in l_ids if x in ml | el | timing]
                if taken:
                    return refuse(ReconError.ALREADY_HANDLED, ", ".join(taken))
                self._emit(MarkedTiming(ledger_line_ids=l_ids))
            case Escalate():
                if not s_ids and not l_ids:
                    return refuse(ReconError.EMPTY, "name at least one line")
                taken = [x for x in s_ids if x in ms | es] + [
                    x for x in l_ids if x in ml | el | timing
                ]
                if taken:
                    return refuse(ReconError.ALREADY_HANDLED, ", ".join(taken))
                new_id = f"ESC-{len(self._st.escalations) + 1}"
                self._emit(
                    Escalated(
                        escalation_id=new_id,
                        statement_line_ids=s_ids,
                        ledger_line_ids=l_ids,
                        reason=action.reason,
                    )
                )
        entries = tuple(e.entry_id for e in self.ledger.entries[before:])
        return ReconResult(ok=True, action=action.kind, id=new_id, entries=entries)

    def _offset(self, a: Adjust) -> str | None:
        match a.adjust_kind:
            case "fee":
                return FEE_ACCOUNT
            case "fx":
                return FX_ACCOUNT
            case "duplicate_reversal":
                if len(a.ledger_line_ids) != 1:
                    return None
                return self.book[a.ledger_line_ids[0]].offset_account
        if a.invoice_id is not None:
            return f"ar:{a.invoice_id}"
        if a.adjust_kind == "partial" and a.ledger_line_ids:
            return self.book[a.ledger_line_ids[0]].offset_account
        return SUSPENSE


__all__ = [
    "CASH",
    "FEE_ACCOUNT",
    "FX_ACCOUNT",
    "OPENING",
    "SUSPENSE",
    "Adjust",
    "Adjusted",
    "Escalate",
    "Escalated",
    "MarkTiming",
    "MarkedTiming",
    "Match",
    "Matched",
    "ReconAction",
    "ReconEngine",
    "ReconError",
    "ReconEvent",
    "ReconOutcome",
    "ReconResult",
    "Unmatch",
    "Unmatched",
]
