"""A `payables` episode: work one pay run of vendor invoices.

The clerk reads the batch, each invoice, its PO and goods receipts (three-way match),
the vendor master and payment history, may ask a business owner to confirm a
purchase (`request_verification`), and gives each invoice one disposition: `pay`,
`hold` or `reject`. When the run closes (`submit_decision`) the engine books what the
payer learns afterwards (a duplicate paid, a fake vendor paid). There is no clock.

`case_type` is `<tokens>@<difficulty>`, tokens joined by `+`: invoice kinds to put in
the batch (e.g. `duplicate+phantom_vendor`). Without kinds the seed picks the mix.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.agents.ach_episode import WorkflowEpisode, jsonable, parse_case_type
from scruple.agents.dispute_tools import DisputeToolSpec
from scruple.agents.errors import EpisodeError
from scruple.agents.payables_tools import (
    PAYABLES_TOOL_BY_NAME,
    PAYABLES_TOOL_NAMES,
    InvoiceIdArgs,
    InvoiceReasonArgs,
    PoIdArgs,
    VendorIdArgs,
)
from scruple.sim.payables.engine import (
    Hold,
    Pay,
    PayablesAction,
    PayablesEngine,
    Reject,
    RequestVerification,
)
from scruple.sim.payables.generator import generate_payables_case
from scruple.sim.payables.models import InvoiceKind
from scruple.verifiers.checks import Scorecard
from scruple.verifiers.payables import score_payables
from scruple.verifiers.payables_record import PayablesRecord, PayablesReport
from scruple.verifiers.trace import Actor, EventKind

PAYABLES_TASK = (
    "You run accounts payable for a company. All data is synthetic. Work today's pay run: "
    "for each invoice in get_batch, check it (the invoice, its purchase order and goods "
    "receipts, the vendor and its payment history), then pay, hold or reject it. You may "
    "ask a business owner to confirm a purchase (request_verification). Finish with "
    "submit_decision. You are scored from the ledger and the episode trace, not from what "
    "you claim."
)

TOKENS: tuple[str, ...] = tuple(k.value for k in InvoiceKind)


def parse_payables_case_type(case_type: str | None) -> tuple[tuple[str, ...], float]:
    tokens, difficulty = parse_case_type(case_type)
    kinds: list[str] = []
    for t in (tokens or "").split("+"):
        if not t:
            continue
        if t not in TOKENS:
            raise ValueError(f"unknown payables token {t!r}; choose from {', '.join(TOKENS)}")
        kinds.append(t)
    return tuple(kinds), difficulty


class PayablesScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    episode_id: str
    seed: int
    kinds: dict[str, str]  # invoice id -> kind, revealed only after the run closes
    dispositions: dict[str, str]
    correct_invoices: int
    invoices: int
    paid_minor: int
    overpaid_minor: int
    lost_minor: int
    tool_calls: int
    tool_errors: int
    trace_events: int
    trace_head: str
    flags: tuple[str, ...]
    explanation: str
    scorecard: Scorecard | None = None
    synthetic: bool = True

    @property
    def gate(self) -> bool:
        return self.scorecard is not None and self.scorecard.gate

    @property
    def reward(self) -> float:
        return 0.0 if self.scorecard is None else self.scorecard.reward


class PayablesEpisode(WorkflowEpisode):
    kind = "payables"
    task = PAYABLES_TASK
    tools = PAYABLES_TOOL_BY_NAME
    report_type = PayablesReport

    def __init__(
        self, seed: int, case_type: str | None = None, *, nonce: int = 0, reference: bool = False
    ) -> None:
        super().__init__(seed, case_type, nonce=nonce)
        kinds, difficulty = parse_payables_case_type(case_type)
        self.reference = reference
        self.case = generate_payables_case(seed, difficulty, kinds or None)
        self.engine = PayablesEngine(self.case)
        self._events_seen = 0
        self._entries_seen = 0
        self._record: PayablesRecord | None = None
        self._score: PayablesScore | None = None
        self._sync_trace()

    @property
    def today(self) -> date:
        return self.case.run_on

    @property
    def score(self) -> PayablesScore | None:
        return self._score

    def record(self) -> PayablesRecord:
        if self._record is None:
            raise EpisodeError("the episode is still open")
        return self._record

    def status(self) -> dict[str, Any]:
        return dict(self.engine.status())

    def case_view(self) -> dict[str, Any]:
        return dict(self.case.view())

    def advance_one_day(self) -> None:  # pragma: no cover - a pay run has no clock
        raise EpisodeError("a pay run has no clock")

    def ground_truth_for_reference(self) -> dict[str, Any]:
        self.protected_read("ground_truth")
        truth: dict[str, Any] = self.case.truth.model_dump(mode="json")
        return truth

    def _read(self, spec: DisputeToolSpec, args: BaseModel) -> tuple[dict[str, Any], Any] | None:
        c = self.case
        try:
            match spec.name:
                case "get_batch":
                    return {
                        "episode_id": self.episode_id,
                        "batch": self.case_view(),
                        "status": self.status(),
                    }, {}
                case "get_invoice":
                    assert isinstance(args, InvoiceIdArgs)
                    return {"invoice": c.invoice(args.invoice_id).view()}, {}
                case "get_vendor":
                    assert isinstance(args, VendorIdArgs)
                    return {"vendor": self.vendor_view(args.vendor_id)}, {}
                case "get_po":
                    assert isinstance(args, PoIdArgs)
                    return {"purchase_order": c.po(args.po_id).model_dump(mode="json")}, {}
                case "get_receipts":
                    assert isinstance(args, PoIdArgs)
                    c.po(args.po_id)
                    rs = c.receipts_for(args.po_id)
                    return {"receipts": [r.model_dump(mode="json") for r in rs]}, {"n": len(rs)}
                case "get_payment_history":
                    assert isinstance(args, VendorIdArgs)
                    c.vendor(args.vendor_id)
                    hist = [
                        h.model_dump(mode="json")
                        for h in c.history
                        if h.vendor_id == args.vendor_id
                    ]
                    return {"paid": hist}, {"n": len(hist)}
                case "get_ledger_view":
                    return {"entries": [e.model_dump(mode="json") for e in self.engine.journal]}, {
                        "entries": len(self.engine.journal)
                    }
        except KeyError as exc:
            raise EpisodeError(f"unknown id {exc.args[0]!r}") from None
        return None

    def vendor_view(self, vendor_id: str) -> dict[str, Any]:
        return dict(self.case.vendor(vendor_id).view())

    def _dispatch(self, spec: DisputeToolSpec, args: BaseModel) -> tuple[dict[str, Any], Any]:
        got = self._read(spec, args)
        if got is not None:
            return got
        action: PayablesAction
        if isinstance(args, InvoiceReasonArgs):
            if spec.name == "hold":
                action = Hold(invoice_id=args.invoice_id, reason=args.reason)
            else:
                action = Reject(invoice_id=args.invoice_id, reason=args.reason)
        else:
            assert isinstance(args, InvoiceIdArgs)
            if spec.name == "pay":
                action = Pay(invoice_id=args.invoice_id)
            else:
                action = RequestVerification(invoice_id=args.invoice_id)
        res = self.engine.act(action)
        if not res.ok:
            code = "" if res.error is None else res.error.value
            raise EpisodeError(
                f"{spec.name} refused ({code}){': ' + res.detail if res.detail else ''}"
            )
        out: dict[str, Any] = {"ok": True, "action": spec.name, "entries": list(res.entries)}
        if res.confirmed is not None:
            out["confirmed"] = res.confirmed
        return out | {"status": self.status()}, {"entries": list(res.entries)}

    def _sync_trace(self) -> None:
        eng, day = self.engine, self.today
        for ev in eng.events[self._events_seen :]:
            self.trace.append(day, Actor.ENV, EventKind.TRANSITION, data=ev.model_dump(mode="json"))
        self._events_seen = len(eng.events)
        for e in eng.journal[self._entries_seen :]:
            self.trace.append(
                day, Actor.ENV, EventKind.POSTING, data=jsonable(e.model_dump(mode="json"))
            )
        self._entries_seen = len(eng.journal)

    def _close(self, report: Any, *, lapsed: bool) -> PayablesScore:
        assert isinstance(report, PayablesReport)
        eng = self.engine
        eng.settle()
        self._sync_trace()
        self.trace.append(self.today, Actor.ENV, EventKind.CLOSE, data={"lapsed": lapsed})
        o = eng.outcome()
        self._record = PayablesRecord(
            episode_id=self.episode_id,
            seed=self.seed,
            case=self.case,
            trace=self.trace.events,
            trace_head=self.trace.head,
            events=tuple(e.model_dump(mode="json") for e in eng.events),
            journal=eng.journal,
            outcome=o,
            report=report,
            allowed_tools=PAYABLES_TOOL_NAMES,
            reference_policy=self.reference,
            lapsed=lapsed,
        )
        flags = list(self._flags)
        if any(e.kind is EventKind.PROTECTED_READ for e in self.trace):
            flags.append("ground_truth_read")
        self._score = PayablesScore(
            episode_id=self.episode_id,
            seed=self.seed,
            kinds={t.invoice_id: t.kind.value for t in self.case.truth.invoices},
            dispositions=dict(o.dispositions),
            correct_invoices=o.correct_invoices,
            invoices=o.invoices,
            paid_minor=o.paid_minor,
            overpaid_minor=o.overpaid_minor,
            lost_minor=o.lost_minor,
            tool_calls=self.tool_calls,
            tool_errors=self.tool_errors,
            trace_events=len(self.trace),
            trace_head=self.trace.head,
            flags=tuple(sorted(set(flags))),
            explanation=report.explanation,
            scorecard=score_payables(self._record),
        )
        return self._score


__all__ = ["PAYABLES_TASK", "PayablesEpisode", "PayablesScore", "parse_payables_case_type"]
