"""Event-sourced engine for one AML alert: requests for information and one decision.

State is the fold of an append-only event log (`AmlEngine.events`), like the ACH and
dispute engines: `AmlEngine.replay(case, events)` rebuilds it, `state_digest()`
hashes it. The transaction history is fixed (the alert is about the past), so the
engine moves no money and has no ledger.

Actions:

- `RequestInfo`: ask the relationship manager to get an explanation from the
  customer. The reply (the case's `rfi`) arrives after `RFI_DAYS` simulated days
  (ASSUMPTION AM-8); at most `RFI_MAX` requests per alert.
- `CloseAlert`: close as a false positive.
- `Escalate`: hand the alert to a second-level investigator, citing indicators and
  transfers.
- `FileSar`: file a (synthetic) suspicious activity report, citing indicators and
  transfers and stating the amount involved.

One decision ends the alert; anything after it is refused. Cited transfers must
exist in the case (unknown ids are refused); whether the citations support the
decision is the verifier's question, not the engine's.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from scruple.hashing import content_hash
from scruple.sim.aml.models import AmlCase, Decision

RFI_DAYS = 2  # AM-8
RFI_MAX = 2  # AM-8
COST_UNITS = 100  # the cost of missing a typology; see `decision_cost`

_EV = ConfigDict(frozen=True, extra="forbid")


class _Ev(BaseModel):
    model_config = _EV
    on: date


class AlertOpened(_Ev):
    type: Literal["alert_opened"] = "alert_opened"
    alert_id: str


class InfoRequested(_Ev):
    type: Literal["info_requested"] = "info_requested"
    question: str
    replied_on: date
    responded: bool
    explanation: str = ""
    documents: tuple[str, ...] = ()


class Decided(_Ev):
    type: Literal["decided"] = "decided"
    decision: Decision
    indicators: tuple[str, ...] = ()
    txn_ids: tuple[str, ...] = ()
    amount_minor: int | None = None  # SARs only
    narrative: str = ""


AmlEvent = Annotated[AlertOpened | InfoRequested | Decided, Field(discriminator="type")]
_EVENTS: TypeAdapter[tuple[AmlEvent, ...]] = TypeAdapter(tuple[AmlEvent, ...])


class _Act(BaseModel):
    model_config = _EV


class RequestInfo(_Act):
    kind: Literal["request_info"] = "request_info"
    question: str = Field(min_length=1, max_length=500)


class CloseAlert(_Act):
    kind: Literal["close"] = "close"
    reason: str = ""


class Escalate(_Act):
    kind: Literal["escalate"] = "escalate"
    indicators: tuple[str, ...] = ()
    txn_ids: tuple[str, ...] = ()
    reason: str = ""


class FileSar(_Act):
    kind: Literal["file_sar"] = "file_sar"
    indicators: tuple[str, ...] = ()
    txn_ids: tuple[str, ...] = ()
    amount_minor: int = Field(ge=0)
    narrative: str = ""


AmlAction = Annotated[RequestInfo | CloseAlert | Escalate | FileSar, Field(discriminator="kind")]
ACTION_KINDS = ("request_info", "close", "escalate", "file_sar")


class ActionError(StrEnum):
    DECIDED = "decided"  # the alert already has a decision
    RFI_LIMIT = "rfi_limit"
    UNKNOWN_TXN = "unknown_txn"


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    action: str
    error: ActionError | None = None
    detail: str = ""


class AmlOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision | None  # None: the alert was left open
    indicators: tuple[str, ...]
    txn_ids: tuple[str, ...]
    sar_amount_minor: int | None
    info_requests: int
    correct: bool  # the decision equals the truth's
    cost: int  # decision_cost, in units of COST_UNITS


def decision_cost(suspicious: bool, decision: Decision | None) -> int:
    """ASSUMPTION AM-9 (ours; no public source prices these): a missed typology costs
    100; escalating one instead of filing costs 10 (an investigator redoes the work);
    filing a SAR on a false positive costs 20 (a defensive filing); escalating a false
    positive costs 5 (an investigator's time). Leaving an alert open costs as much as
    the worst decision for its kind."""
    if suspicious:
        return {"file_sar": 0, "escalate": 10, "close": COST_UNITS, None: COST_UNITS}[decision]
    return {"close": 0, "escalate": 5, "file_sar": 20, None: 20}[decision]


class AmlEngine:
    def __init__(self, case: AmlCase) -> None:
        self.case = case
        self._today = case.alert.raised_on
        self._events: list[AmlEvent] = []
        self._decided: Decided | None = None
        self._rfis = 0
        self._emit(AlertOpened(on=self._today, alert_id=case.alert.alert_id))

    @classmethod
    def replay(cls, case: AmlCase, events: tuple[AmlEvent, ...]) -> AmlEngine:
        eng = cls.__new__(cls)
        eng.case = case
        eng._today = case.alert.raised_on
        eng._events = []
        eng._decided = None
        eng._rfis = 0
        for ev in events:
            eng._emit(ev)
        return eng

    def _emit(self, ev: AmlEvent) -> None:
        self._events.append(ev)
        match ev:
            case InfoRequested():
                self._rfis += 1
                self._today = max(self._today, ev.replied_on)
            case Decided():
                self._decided = ev
            case AlertOpened():
                pass

    @property
    def events(self) -> tuple[AmlEvent, ...]:
        return tuple(self._events)

    def events_json(self) -> str:
        return json.dumps([e.model_dump(mode="json") for e in self._events], sort_keys=True)

    @staticmethod
    def events_from_json(raw: str | bytes) -> tuple[AmlEvent, ...]:
        return _EVENTS.validate_json(raw)

    def state_digest(self) -> str:
        return content_hash([e.model_dump(mode="json") for e in self._events])

    @property
    def today(self) -> date:
        return self._today

    @property
    def decided(self) -> bool:
        return self._decided is not None

    def allowed_actions(self) -> tuple[str, ...]:
        if self.decided:
            return ()
        out = ["close", "escalate", "file_sar"]
        if self._rfis < RFI_MAX:
            out.insert(0, "request_info")
        return tuple(out)

    def info_replies(self) -> list[dict[str, object]]:
        return [
            {
                "question": e.question,
                "replied_on": e.replied_on.isoformat(),
                "responded": e.responded,
                "explanation": e.explanation,
                "documents": list(e.documents),
            }
            for e in self._events
            if isinstance(e, InfoRequested)
        ]

    def status(self) -> dict[str, object]:
        d = self._decided
        return {
            "today": self._today.isoformat(),
            "decided": d is not None,
            "decision": None if d is None else d.decision,
            "info_requests": self._rfis,
            "allowed_actions": list(self.allowed_actions()),
        }

    def outcome(self) -> AmlOutcome:
        d = self._decided
        decision = None if d is None else d.decision
        suspicious = self.case.truth.suspicious
        return AmlOutcome(
            decision=decision,
            indicators=() if d is None else d.indicators,
            txn_ids=() if d is None else d.txn_ids,
            sar_amount_minor=None if d is None else d.amount_minor,
            info_requests=self._rfis,
            correct=decision == self.case.truth.expected,
            cost=decision_cost(suspicious, decision),
        )

    def act(self, action: AmlAction) -> ActionResult:
        def refuse(err: ActionError, detail: str = "") -> ActionResult:
            return ActionResult(ok=False, action=action.kind, error=err, detail=detail)

        if self.decided:
            return refuse(ActionError.DECIDED, "the alert already has a decision")
        now = self._today
        match action:
            case RequestInfo():
                if self._rfis >= RFI_MAX:
                    return refuse(ActionError.RFI_LIMIT, f"at most {RFI_MAX} requests")
                r = self.case.rfi
                self._emit(
                    InfoRequested(
                        on=now,
                        question=action.question,
                        replied_on=now + timedelta(days=RFI_DAYS),
                        responded=r.responded,
                        explanation=r.explanation,
                        documents=r.documents,
                    )
                )
            case CloseAlert():
                self._emit(Decided(on=now, decision="close", narrative=action.reason))
            case Escalate() | FileSar():
                known = {t.txn_id for t in self.case.transfers}
                unknown = [x for x in action.txn_ids if x not in known]
                if unknown:
                    return refuse(ActionError.UNKNOWN_TXN, ", ".join(unknown))
                if isinstance(action, Escalate):
                    ev = Decided(
                        on=now,
                        decision="escalate",
                        indicators=action.indicators,
                        txn_ids=action.txn_ids,
                        narrative=action.reason,
                    )
                else:
                    ev = Decided(
                        on=now,
                        decision="file_sar",
                        indicators=action.indicators,
                        txn_ids=action.txn_ids,
                        amount_minor=action.amount_minor,
                        narrative=action.narrative,
                    )
                self._emit(ev)
        return ActionResult(ok=True, action=action.kind)


__all__ = [
    "ACTION_KINDS",
    "COST_UNITS",
    "RFI_DAYS",
    "RFI_MAX",
    "ActionError",
    "ActionResult",
    "AlertOpened",
    "AmlAction",
    "AmlEngine",
    "AmlEvent",
    "AmlOutcome",
    "CloseAlert",
    "Decided",
    "Escalate",
    "FileSar",
    "InfoRequested",
    "RequestInfo",
    "decision_cost",
]
