"""Event-sourced ACH return engine for one originated debit entry.

State is the fold of an append-only event log (`AchEngine.events`) over the case's
opening state, exactly like `scruple.sim.disputes.DisputeEngine`: every state change
goes through `_emit(event)`, `AchEngine.replay(case, events)` rebuilds the engine
without consulting the receiver or the RDFI, and `state_digest()` hashes the result.

Money is a double-entry `Ledger` on the originator's books (USD, minor units):

- invoice: `receivable:<entry>` +A, `revenue:sales` -A
- debit settles (original or a retry): `cash:odfi` +A, receivable -A
- return settles: cash -A, receivable +A; plus the Unauthorized Entry Fee for
  R05/R07/R10/R11/R29 (`expense:ach_fees` +450, cash -450; W4, AA-3)
- dishonor settles: cash +A, receivable -A; a contested dishonor reverses it
- accept: `contra:ach_returns_accepted` +open, receivable -open (the debit is conceded)
- write off: `expense:bad_debt` +open, receivable -open (owed, not collectable)

So collected + written off + voided + open receivable always equals the invoice.

Time moves only through the injected `ManualClock`; `sync()` processes everything due
up to today in date order. The RDFI answers by rule (`scruple.sim.ach.rules`) and by
the case's hidden truth (when the receiver's funds arrive). Rule violations the
network would not stop at submission (an improper reinitiation) are allowed and
recorded as `Violation` events; refused actions change nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from scruple.hashing import content_hash
from scruple.sim.ach.models import AchCase, Allocation
from scruple.sim.ach.rules import (
    CODES,
    CONTEST_BANKING_DAYS,
    DISHONOR_CODES,
    REINIT_DESCRIPTION,
    REINIT_MAX,
    TWO_DAY_RETURN_BANKING_DAYS,
    UNAUTHORIZED_ENTRY_FEE_MINOR,
    Reinit,
    dishonor_deadline,
    is_timely,
    next_banking_day,
    reinit_deadline,
)
from scruple.sim.deadlines import BusinessCalendar, ManualClock, today
from scruple.sim.ledger import JournalEntry, Ledger

CASH = "cash:odfi"
REVENUE = "revenue:sales"
CONTRA = "contra:ach_returns_accepted"
BAD_DEBT = "expense:bad_debt"
FEES = "expense:ach_fees"
MAX_DAYS = 400

_EV = ConfigDict(frozen=True, extra="forbid")


class _Ev(BaseModel):
    model_config = _EV
    on: date


class Invoiced(_Ev):
    type: Literal["invoiced"] = "invoiced"


class DebitSettled(_Ev):
    type: Literal["debit_settled"] = "debit_settled"
    attempt: int  # 0 is the original entry, 1.. are reinitiations


class ReturnScheduled(_Ev):
    type: Literal["return_scheduled"] = "return_scheduled"
    return_id: str
    code: str
    attempt: int
    settles_on: date
    trace_number: str
    addenda_info: str = ""


class ReturnSettled(_Ev):
    type: Literal["return_settled"] = "return_settled"
    return_id: str


class FeeCharged(_Ev):
    type: Literal["fee_charged"] = "fee_charged"
    return_id: str
    amount_minor: int


class DishonorSubmitted(_Ev):
    type: Literal["dishonor_submitted"] = "dishonor_submitted"
    return_id: str
    code: str
    settles_on: date


class DishonorSettled(_Ev):
    type: Literal["dishonor_settled"] = "dishonor_settled"
    return_id: str


class DishonorContested(_Ev):
    type: Literal["dishonor_contested"] = "dishonor_contested"
    return_id: str
    code: str


class DishonorFinal(_Ev):
    type: Literal["dishonor_final"] = "dishonor_final"
    return_id: str


class RetrySubmitted(_Ev):
    type: Literal["retry_submitted"] = "retry_submitted"
    attempt: int
    description: str
    settles_on: date


class Accepted(_Ev):
    type: Literal["accepted"] = "accepted"
    amount_minor: int


class WrittenOff(_Ev):
    type: Literal["written_off"] = "written_off"
    amount_minor: int


class Violation(_Ev):
    type: Literal["violation"] = "violation"
    kind: str
    detail: str


class ActionApplied(_Ev):
    type: Literal["action_applied"] = "action_applied"
    action: str
    args: dict[str, str] = {}


AchEvent = Annotated[
    Invoiced
    | DebitSettled
    | ReturnScheduled
    | ReturnSettled
    | FeeCharged
    | DishonorSubmitted
    | DishonorSettled
    | DishonorContested
    | DishonorFinal
    | RetrySubmitted
    | Accepted
    | WrittenOff
    | Violation
    | ActionApplied,
    Field(discriminator="type"),
]
_EVENTS: TypeAdapter[tuple[AchEvent, ...]] = TypeAdapter(tuple[AchEvent, ...])


# --- actions --------------------------------------------------------------------------
class _Act(BaseModel):
    model_config = _EV


class AcceptReturn(_Act):
    kind: Literal["accept"] = "accept"


class WriteOff(_Act):
    kind: Literal["write_off"] = "write_off"


class Dishonor(_Act):
    kind: Literal["dishonor"] = "dishonor"
    return_id: str
    code: str


class Reinitiate(_Act):
    kind: Literal["reinitiate"] = "reinitiate"
    company_entry_description: str = Field(max_length=10)


AchAction = Annotated[AcceptReturn | WriteOff | Dishonor | Reinitiate, Field(discriminator="kind")]
ACTION_KINDS = ("accept", "write_off", "dishonor", "reinitiate")


class ActionError(StrEnum):
    NOTHING_OPEN = "nothing_open"  # the receivable is already closed
    PENDING = "pending"  # a dishonor or a retry has not resolved yet
    UNKNOWN_RETURN = "unknown_return"
    ALREADY_DISHONORED = "already_dishonored"
    BAD_CODE = "bad_code"
    NO_RETURN = "no_return"


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    action: str
    error: ActionError | None = None
    detail: str = ""
    entries: tuple[int, ...] = ()
    violations: tuple[str, ...] = ()


class AchOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    allocation: Allocation
    fees_minor: int
    violations: tuple[str, ...]
    contested_dishonors: int
    final_dishonors: int
    retries: int
    correct: bool  # the allocation equals the truth's, nothing left open
    cost_minor: int  # uncollected principal plus fees


# --- state ----------------------------------------------------------------------------
@dataclass
class _Return:
    return_id: str
    code: str
    attempt: int
    settles_on: date
    trace_number: str
    addenda_info: str
    settled: bool = False
    dishonor_code: str | None = None
    dishonor_settles_on: date | None = None
    dishonor_settled: bool = False
    dishonor_resolved: str | None = None  # "final" or a contest code


@dataclass
class _State:
    debits: dict[int, date] = field(default_factory=dict)  # attempt -> settled on
    returns: dict[str, _Return] = field(default_factory=dict)
    retry: tuple[int, date] | None = None  # submitted, not settled
    attempts: int = 0
    violations: list[str] = field(default_factory=list)
    closed_by: str | None = None


class AchEngine:
    def __init__(self, case: AchCase, clock: ManualClock | None = None) -> None:
        self.case = case
        self.calendar = BusinessCalendar(frozenset(case.holidays))
        if clock is None:
            clock = ManualClock()
            clock.set_date(case.opened_on)
        self.clock = clock
        self.ledger = Ledger()
        self._st = _State()
        self._events: list[AchEvent] = []
        self._genesis()
        self.sync()

    # --- event log ------------------------------------------------------------------
    def _genesis(self) -> None:
        c = self.case
        self._emit(Invoiced(on=c.invoiced_on))
        self._emit(DebitSettled(on=c.settled_on, attempt=0))
        for r in c.returns:
            self._emit(
                ReturnScheduled(
                    on=c.settled_on,
                    return_id=r.return_id,
                    code=r.code,
                    attempt=0,
                    settles_on=r.settles_on,
                    trace_number=r.trace_number,
                    addenda_info=r.addenda_info,
                )
            )

    @classmethod
    def replay(
        cls, case: AchCase, events: tuple[AchEvent, ...], clock: ManualClock | None = None
    ) -> AchEngine:
        eng = cls.__new__(cls)
        eng.case = case
        eng.calendar = BusinessCalendar(frozenset(case.holidays))
        if clock is None:
            clock = ManualClock()
            clock.set_date(max([case.opened_on, *(e.on for e in events)]))
        eng.clock = clock
        eng.ledger = Ledger()
        eng._st = _State()
        eng._events = []
        for ev in events:
            eng._emit(ev)
        return eng

    def _emit(self, ev: AchEvent) -> None:
        self._events.append(ev)
        self._apply_event(ev)

    def _post(self, kind: str, postings: list[tuple[str, int]]) -> None:
        self.ledger.post(kind, self.case.entry_id, self.case.case_id, "USD", postings)

    def _apply_event(self, ev: AchEvent) -> None:
        st, a, rcv = self._st, self.case.amount_minor, f"receivable:{self.case.entry_id}"
        match ev:
            case Invoiced():
                self._post("invoice", [(rcv, a), (REVENUE, -a)])
            case DebitSettled():
                st.debits[ev.attempt] = ev.on
                if ev.attempt and st.retry is not None and st.retry[0] == ev.attempt:
                    st.retry = None
                self._post("ach_debit", [(CASH, a), (rcv, -a)])
            case ReturnScheduled():
                st.returns[ev.return_id] = _Return(
                    ev.return_id,
                    ev.code,
                    ev.attempt,
                    ev.settles_on,
                    ev.trace_number,
                    ev.addenda_info,
                )
            case ReturnSettled():
                st.returns[ev.return_id].settled = True
                st.closed_by = None
                self._post("ach_return", [(CASH, -a), (rcv, a)])
            case FeeCharged():
                self._post("ach_fee", [(FEES, ev.amount_minor), (CASH, -ev.amount_minor)])
            case DishonorSubmitted():
                r = st.returns[ev.return_id]
                r.dishonor_code, r.dishonor_settles_on = ev.code, ev.settles_on
            case DishonorSettled():
                st.returns[ev.return_id].dishonor_settled = True
                self._post("ach_dishonor", [(CASH, a), (rcv, -a)])
            case DishonorContested():
                st.returns[ev.return_id].dishonor_resolved = ev.code
                self._post("ach_contested_dishonor", [(CASH, -a), (rcv, a)])
            case DishonorFinal():
                st.returns[ev.return_id].dishonor_resolved = "final"
            case RetrySubmitted():
                st.attempts = ev.attempt
                st.retry = (ev.attempt, ev.settles_on)
            case Accepted():
                st.closed_by = "accepted"
                self._post(
                    "ach_return_accepted", [(CONTRA, ev.amount_minor), (rcv, -ev.amount_minor)]
                )
            case WrittenOff():
                st.closed_by = "written_off"
                self._post("ach_write_off", [(BAD_DEBT, ev.amount_minor), (rcv, -ev.amount_minor)])
            case Violation():
                st.violations.append(ev.kind)
            case ActionApplied():
                pass

    @property
    def events(self) -> tuple[AchEvent, ...]:
        return tuple(self._events)

    def events_json(self) -> str:
        return json.dumps([e.model_dump(mode="json") for e in self._events], sort_keys=True)

    @staticmethod
    def events_from_json(raw: str | bytes) -> tuple[AchEvent, ...]:
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
    def today(self) -> date:
        return today(self.clock)

    @property
    def journal(self) -> tuple[JournalEntry, ...]:
        return self.ledger.entries

    def receivable(self) -> int:
        return self.ledger.balance(f"receivable:{self.case.entry_id}", "USD")

    def settled_returns(self) -> tuple[_Return, ...]:
        return tuple(r for r in self._st.returns.values() if r.settled)

    def pending(self) -> tuple[str, ...]:
        """What the originator is waiting for (empty: nothing can change by itself)."""
        st, out = self._st, []
        if st.retry is not None:
            out.append(f"retry {st.retry[0]} settles {st.retry[1]}")
        for r in st.returns.values():
            if not r.settled and r.attempt > 0:
                out.append(f"retry {r.attempt} outcome")
            if r.dishonor_code is not None and r.dishonor_resolved is None:
                out.append(f"dishonor of {r.return_id}")
        return tuple(out)

    def quiet(self) -> bool:
        """Nothing pending and nothing scheduled."""
        st = self._st
        return (
            st.retry is None
            and all(r.settled for r in st.returns.values())
            and all(r.dishonor_code is None or r.dishonor_resolved for r in st.returns.values())
        )

    def is_resolved(self) -> bool:
        return self.quiet() and self.receivable() == 0

    def is_duplicate(self, r: _Return) -> bool:
        return any(
            o.attempt == r.attempt and (o.settles_on, o.return_id) < (r.settles_on, r.return_id)
            for o in self._st.returns.values()
        )

    def is_timely(self, r: _Return) -> bool:
        entry = self._st.debits.get(r.attempt, self.case.settled_on)
        return is_timely(r.code, entry, r.settles_on, self.calendar)

    def allowed_actions(self) -> tuple[str, ...]:
        out: list[str] = []
        busy = bool(self.pending())
        open_ = self.receivable()
        if open_ > 0 and not busy:
            out += ["accept", "write_off"]
            if open_ >= self.case.amount_minor and self.settled_returns():
                out.append("reinitiate")
        if open_ >= self.case.amount_minor and any(
            r.dishonor_code is None for r in self.settled_returns()
        ):
            out.append("dishonor")
        return tuple(out)

    def view_returns(self) -> list[dict[str, object]]:
        return [
            {
                "return_id": r.return_id,
                "code": r.code,
                "title": CODES[r.code].title,
                "of_attempt": r.attempt,
                "settled_on": r.settles_on.isoformat(),
                "trace_number": r.trace_number,
                "original_trace": self.case.trace_number,
                "addenda_info": r.addenda_info,
                "dishonor": None
                if r.dishonor_code is None
                else {
                    "code": r.dishonor_code,
                    "settles_on": str(r.dishonor_settles_on),
                    "result": r.dishonor_resolved,
                },
            }
            for r in sorted(self.settled_returns(), key=lambda r: (r.settles_on, r.return_id))
        ]

    def status(self) -> dict[str, object]:
        st = self._st
        return {
            "today": self.today.isoformat(),
            "open_receivable_minor": self.receivable(),
            "retries": st.attempts,
            "debits_settled": {str(k): v.isoformat() for k, v in sorted(st.debits.items())},
            "pending": list(self.pending()),
            "allowed_actions": list(self.allowed_actions()),
            "resolved": self.is_resolved(),
        }

    def outcome(self) -> AchOutcome:
        bal = self.ledger.balance
        principal = {"ach_debit", "ach_return", "ach_dishonor", "ach_contested_dishonor"}
        collected = sum(
            p.amount_minor
            for e in self.ledger.entries
            if e.kind in principal
            for p in e.postings
            if p.account == CASH
        )
        alloc = Allocation(
            collected_minor=collected,
            written_off_minor=bal(BAD_DEBT, "USD"),
            voided_minor=bal(CONTRA, "USD"),
            open_minor=self.receivable(),
        )
        rs = self._st.returns.values()
        fees = bal(FEES, "USD")
        return AchOutcome(
            allocation=alloc,
            fees_minor=fees,
            violations=tuple(self._st.violations),
            contested_dishonors=sum(r.dishonor_resolved not in (None, "final") for r in rs),
            final_dishonors=sum(r.dishonor_resolved == "final" for r in rs),
            retries=self._st.attempts,
            correct=alloc == self.case.truth.expected,
            cost_minor=self.case.amount_minor - collected + fees,
        )

    # --- time -----------------------------------------------------------------------
    def advance(self, days: int = 1) -> None:
        for _ in range(days):
            self.clock.advance(days=1)
            self.sync()

    def run_out(self, max_days: int = MAX_DAYS) -> None:
        """Let time pass with the originator idle until nothing is pending."""
        for _ in range(max_days):
            if self.quiet():
                return
            self.advance(1)

    def _due(self) -> list[tuple[date, int, str]]:
        st, out = self._st, []
        for r in st.returns.values():
            if not r.settled:
                out.append((r.settles_on, 0, f"return:{r.return_id}"))
            if r.dishonor_settles_on is not None and not r.dishonor_settled:
                out.append((r.dishonor_settles_on, 1, f"dishonor:{r.return_id}"))
            elif r.dishonor_settled and r.dishonor_resolved is None:
                assert r.dishonor_settles_on is not None
                when = self.calendar.add_business_days(r.dishonor_settles_on, CONTEST_BANKING_DAYS)
                out.append((when, 2, f"contest:{r.return_id}"))
        if st.retry is not None:
            out.append((st.retry[1], 3, "retry"))
        return sorted(out)

    def sync(self) -> None:
        now = self.today
        while True:
            due = [d for d in self._due() if d[0] <= now]
            if not due:
                return
            day, _, what = due[0]
            kind, _, rid = what.partition(":")
            if kind == "return":
                self._settle_return(rid, day)
            elif kind == "dishonor":
                self._emit(DishonorSettled(on=day, return_id=rid))
            elif kind == "contest":
                self._resolve_dishonor(rid, day)
            else:
                self._settle_retry(day)

    def _settle_return(self, rid: str, day: date) -> None:
        r = self._st.returns[rid]
        self._emit(ReturnSettled(on=day, return_id=rid))
        if CODES[r.code].unauthorized_fee:
            self._emit(FeeCharged(on=day, return_id=rid, amount_minor=UNAUTHORIZED_ENTRY_FEE_MINOR))

    def contest_code(self, r: _Return) -> str | None:
        """What the RDFI contests a dishonor with (W5), or None when it stands."""
        assert r.dishonor_settles_on is not None
        if r.dishonor_settles_on > dishonor_deadline(r.settles_on, self.calendar):
            return "R72"  # untimely dishonored return
        if r.dishonor_code == "R67" and not self.is_duplicate(r):
            return "R75"  # return not a duplicate
        if r.dishonor_code == "R68" and self.is_timely(r):
            return "R73"  # timely original return
        return None

    def _resolve_dishonor(self, rid: str, day: date) -> None:
        r = self._st.returns[rid]
        code = self.contest_code(r)
        if code is None:
            self._emit(DishonorFinal(on=day, return_id=rid))
        else:
            self._emit(DishonorContested(on=day, return_id=rid, code=code))

    def _last_code(self) -> str:
        rs = sorted(self.settled_returns(), key=lambda r: (r.settles_on, r.return_id))
        return rs[-1].code

    def _settle_retry(self, day: date) -> None:
        st = self._st
        assert st.retry is not None
        attempt = st.retry[0]
        code = self._last_code()
        self._emit(DebitSettled(on=day, attempt=attempt))
        funds = self.case.truth.funds_on
        nsf = CODES[code].reinit is Reinit.NSF
        if nsf and funds is not None and day >= funds:
            return
        back = "R01" if nsf else code
        self._emit(
            ReturnScheduled(
                on=day,
                return_id=f"RET-R{attempt}",
                code=back,
                attempt=attempt,
                settles_on=self.calendar.add_business_days(day, TWO_DAY_RETURN_BANKING_DAYS),
                trace_number=f"{attempt:03d}{self.case.trace_number[3:]}",
            )
        )

    # --- agent actions ------------------------------------------------------------------
    def act(self, action: AchAction) -> ActionResult:
        def refuse(err: ActionError, detail: str = "") -> ActionResult:
            return ActionResult(ok=False, action=action.kind, error=err, detail=detail)

        self.sync()
        now, open_, a = self.today, self.receivable(), self.case.amount_minor
        busy = self.pending()
        before = len(self.ledger.entries)
        violations: list[str] = []
        match action:
            case AcceptReturn() | WriteOff():
                if open_ <= 0:
                    return refuse(ActionError.NOTHING_OPEN, "nothing is receivable")
                if busy:
                    return refuse(ActionError.PENDING, "; ".join(busy))
                self._emit(ActionApplied(on=now, action=action.kind))
                if isinstance(action, AcceptReturn):
                    self._emit(Accepted(on=now, amount_minor=open_))
                else:
                    self._emit(WrittenOff(on=now, amount_minor=open_))
            case Dishonor():
                r = self._st.returns.get(action.return_id)
                if r is None or not r.settled:
                    return refuse(ActionError.UNKNOWN_RETURN, action.return_id)
                if action.code not in DISHONOR_CODES:
                    return refuse(
                        ActionError.BAD_CODE, f"dishonor codes are {', '.join(DISHONOR_CODES)}"
                    )
                if r.dishonor_code is not None:
                    return refuse(ActionError.ALREADY_DISHONORED, action.return_id)
                if open_ < a:
                    return refuse(ActionError.NOTHING_OPEN, "the returned amount is not receivable")
                self._emit(
                    ActionApplied(
                        on=now,
                        action="dishonor",
                        args={"return_id": action.return_id, "code": action.code},
                    )
                )
                self._emit(
                    DishonorSubmitted(
                        on=now,
                        return_id=action.return_id,
                        code=action.code,
                        settles_on=next_banking_day(now, self.calendar),
                    )
                )
            case Reinitiate():
                if not self.settled_returns():
                    return refuse(ActionError.NO_RETURN)
                if busy:
                    return refuse(ActionError.PENDING, "; ".join(busy))
                if open_ < a:
                    return refuse(ActionError.NOTHING_OPEN, "the entry is not receivable")
                attempt = self._st.attempts + 1
                settles = next_banking_day(now, self.calendar)
                code = self._last_code()
                if CODES[code].reinit is not Reinit.NSF:
                    violations.append("reinit_code")
                if attempt > REINIT_MAX:
                    violations.append("reinit_limit")
                if settles > reinit_deadline(self.case.settled_on):
                    violations.append("reinit_window")
                if action.company_entry_description != REINIT_DESCRIPTION:
                    violations.append("reinit_description")
                self._emit(
                    ActionApplied(
                        on=now,
                        action="reinitiate",
                        args={"company_entry_description": action.company_entry_description},
                    )
                )
                for v in violations:
                    self._emit(Violation(on=now, kind=v, detail=f"retry {attempt} after {code}"))
                self._emit(
                    RetrySubmitted(
                        on=now,
                        attempt=attempt,
                        description=action.company_entry_description,
                        settles_on=settles,
                    )
                )
        entries = tuple(e.entry_id for e in self.ledger.entries[before:])
        return ActionResult(
            ok=True, action=action.kind, entries=entries, violations=tuple(violations)
        )


__all__ = [
    "ACTION_KINDS",
    "BAD_DEBT",
    "CASH",
    "CONTRA",
    "FEES",
    "REVENUE",
    "AcceptReturn",
    "AchAction",
    "AchEngine",
    "AchEvent",
    "AchOutcome",
    "ActionError",
    "ActionResult",
    "Dishonor",
    "Reinitiate",
    "WriteOff",
]
