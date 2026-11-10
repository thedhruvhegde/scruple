"""A `recon` episode: reconcile one synthetic bank statement against the cash book.

The bank's file (camt.053 or BAI2) is parsed with the real parsers; the agent reads
the parsed lines, the cash book and the open invoices, then matches lines, posts
adjusting entries that cite statement lines, leaves timing items outstanding and
escalates what it cannot resolve. There is no clock. The scorecard
(`scruple.verifiers.recon`) is computed from the engine's ledger and the trace.

`case_type` is `<tokens>@<difficulty>`, tokens joined by `+`: break kinds to inject
(e.g. `fx+fee_net`) and optionally a statement format (`camt053` or `bai2`). Without
kinds the seed picks the breaks; without a format the seed picks one.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.agents.ach_episode import WorkflowEpisode, jsonable, parse_case_type
from scruple.agents.dispute_tools import DisputeToolSpec
from scruple.agents.errors import EpisodeError
from scruple.agents.recon_tools import (
    RECON_TOOL_BY_NAME,
    RECON_TOOL_NAMES,
    AdjustArgs,
    EscalateArgs,
    MatchArgs,
    TimingArgs,
    UnmatchArgs,
)
from scruple.sim.recon.engine import (
    Adjust,
    Escalate,
    MarkTiming,
    Match,
    ReconAction,
    ReconEngine,
    Unmatch,
)
from scruple.sim.recon.generator import generate_recon_case
from scruple.sim.recon.models import BreakKind
from scruple.verifiers.checks import Scorecard
from scruple.verifiers.recon import cleared, score_recon
from scruple.verifiers.recon_record import ReconRecord, ReconReport
from scruple.verifiers.trace import Actor, EventKind

RECON_TASK = (
    "You reconcile a company's bank account for one statement period. All data is "
    "synthetic. Compare the bank statement (get_statement) with the cash book "
    "(get_ledger): match lines, post adjusting entries only when a statement line "
    "justifies them (cite it), leave items in transit as timing, and escalate what you "
    "cannot resolve. Finish with submit_decision. You are scored from the ledger and "
    "the episode trace, not from what you claim."
)
FORMATS = ("camt053", "bai2")


def parse_recon_case_type(
    case_type: str | None,
) -> tuple[tuple[str, ...] | None, str | None, float]:
    tokens, difficulty = parse_case_type(case_type)
    fmt: str | None = None
    kinds: list[str] = []
    for t in (tokens or "").split("+"):
        if not t:
            continue
        if t in FORMATS:
            fmt = t
        elif t in {k.value for k in BreakKind}:
            kinds.append(t)
        else:
            raise ValueError(f"unknown recon break kind or format {t!r}")
    return (tuple(kinds) or None), fmt, difficulty


class ReconScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    episode_id: str
    seed: int
    breaks: tuple[str, ...]  # revealed only after the episode closes
    breaks_cleared: int
    difference_minor: int
    adjustments: int
    escalations: int
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


class ReconEpisode(WorkflowEpisode):
    kind = "recon"
    task = RECON_TASK
    tools = RECON_TOOL_BY_NAME
    report_type = ReconReport

    def __init__(
        self, seed: int, case_type: str | None = None, *, nonce: int = 0, reference: bool = False
    ) -> None:
        super().__init__(seed, case_type, nonce=nonce)
        kinds, fmt, difficulty = parse_recon_case_type(case_type)
        self.reference = reference
        self.case = generate_recon_case(seed, difficulty, fmt, kinds)
        self.engine = ReconEngine(self.case)
        self._events_seen = 0
        self._entries_seen = 0
        self._record: ReconRecord | None = None
        self._score: ReconScore | None = None
        self._sync_trace()

    @property
    def today(self) -> date:
        return self.case.period_end

    @property
    def score(self) -> ReconScore | None:
        return self._score

    def record(self) -> ReconRecord:
        if self._record is None:
            raise EpisodeError("the episode is still open")
        return self._record

    def status(self) -> dict[str, Any]:
        return dict(self.engine.status())

    def case_view(self) -> dict[str, Any]:
        return dict(self.case.view())

    def advance_one_day(self) -> None:  # pragma: no cover - recon has no clock tool
        raise EpisodeError("a reconciliation has no clock")

    def ground_truth_for_reference(self) -> dict[str, Any]:
        self.protected_read("ground_truth")
        truth: dict[str, Any] = self.case.truth.model_dump(mode="json")
        return truth

    def _dispatch(self, spec: DisputeToolSpec, args: BaseModel) -> tuple[dict[str, Any], Any]:
        eng = self.engine
        st = eng.statement
        match spec.name:
            case "get_task":
                return {
                    "episode_id": self.episode_id,
                    "task": self.case_view(),
                    "status": self.status(),
                    "synthetic": True,
                }, {}
            case "get_statement":
                return {
                    "format": st.format,
                    "account_id": st.account_id,
                    "currency": st.currency,
                    "opening_balance_minor": st.opening_balance_minor,
                    "closing_balance_minor": st.closing_balance_minor,
                    "lines": [x.model_dump(mode="json") for x in st.lines],
                    "synthetic": True,
                }, {"lines": len(st.lines)}
            case "get_statement_file":
                return {
                    "format": self.case.statement_format,
                    "file": self.case.statement_file,
                }, {"bytes": len(self.case.statement_file)}
            case "get_ledger":
                return {
                    "lines": [x.model_dump(mode="json") for x in self.case.ledger_lines],
                    "opening_balance_minor": self.case.opening_balance_minor,
                    "synthetic": True,
                }, {"lines": len(self.case.ledger_lines)}
            case "get_invoices":
                return {"invoices": [i.model_dump(mode="json") for i in self.case.invoices]}, {
                    "invoices": len(self.case.invoices)
                }
            case "get_ledger_view":
                return {
                    "entries": [e.model_dump(mode="json") for e in eng.journal],
                    "book_balance_minor": eng.book_balance(),
                }, {"entries": len(eng.journal)}
        action: ReconAction
        if isinstance(args, MatchArgs):
            action = Match(
                statement_line_ids=args.statement_line_ids, ledger_line_ids=args.ledger_line_ids
            )
        elif isinstance(args, UnmatchArgs):
            action = Unmatch(match_id=args.match_id)
        elif isinstance(args, AdjustArgs):
            action = Adjust(
                adjust_kind=args.kind,
                amount_minor=args.amount_minor,
                statement_line_ids=args.statement_line_ids,
                ledger_line_ids=args.ledger_line_ids,
                invoice_id=args.invoice_id,
                note=args.note,
            )
        elif isinstance(args, TimingArgs):
            action = MarkTiming(ledger_line_ids=args.ledger_line_ids)
        else:
            assert isinstance(args, EscalateArgs)
            action = Escalate(
                statement_line_ids=args.statement_line_ids,
                ledger_line_ids=args.ledger_line_ids,
                reason=args.reason,
            )
        res = eng.act(action)
        if not res.ok:
            code = "" if res.error is None else res.error.value
            raise EpisodeError(
                f"{spec.name} refused ({code}){': ' + res.detail if res.detail else ''}"
            )
        out = {"ok": True, "action": spec.name, "id": res.id, "entries": list(res.entries)}
        return out | {"status": self.status()}, out

    def _sync_trace(self) -> None:
        eng = self.engine
        day = self.today
        for ev in eng.events[self._events_seen :]:
            self.trace.append(day, Actor.ENV, EventKind.TRANSITION, data=ev.model_dump(mode="json"))
        self._events_seen = len(eng.events)
        for e in eng.journal[self._entries_seen :]:
            self.trace.append(
                day, Actor.ENV, EventKind.POSTING, data=jsonable(e.model_dump(mode="json"))
            )
        self._entries_seen = len(eng.journal)

    def _close(self, report: Any, *, lapsed: bool) -> ReconScore:
        assert isinstance(report, ReconReport)
        eng = self.engine
        self._sync_trace()
        self.trace.append(self.today, Actor.ENV, EventKind.CLOSE, data={"lapsed": lapsed})
        outcome = eng.outcome()
        self._record = ReconRecord(
            episode_id=self.episode_id,
            seed=self.seed,
            case=self.case,
            statement=eng.statement,
            trace=self.trace.events,
            trace_head=self.trace.head,
            events=tuple(e.model_dump(mode="json") for e in eng.events),
            journal=eng.journal,
            outcome=outcome,
            report=report,
            allowed_tools=RECON_TOOL_NAMES,
            reference_policy=self.reference,
            lapsed=lapsed,
        )
        flags = list(self._flags)
        if any(e.kind is EventKind.PROTECTED_READ for e in self.trace):
            flags.append("ground_truth_read")
        rec = self._record
        self._score = ReconScore(
            episode_id=self.episode_id,
            seed=self.seed,
            breaks=tuple(b.kind.value for b in self.case.truth.breaks),
            breaks_cleared=sum(cleared(rec, b) for b in self.case.truth.breaks),
            difference_minor=outcome.difference_minor,
            adjustments=len(outcome.adjustments),
            escalations=len(outcome.escalated_statement) + len(outcome.escalated_ledger),
            tool_calls=self.tool_calls,
            tool_errors=self.tool_errors,
            trace_events=len(self.trace),
            trace_head=self.trace.head,
            flags=tuple(sorted(set(flags))),
            explanation=report.explanation,
            scorecard=score_recon(rec),
        )
        return self._score


__all__ = ["RECON_TASK", "ReconEpisode", "ReconScore", "parse_recon_case_type"]
