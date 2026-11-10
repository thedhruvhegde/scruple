"""An `ach` episode: one returned ACH debit, worked on the originator's behalf.

The agent reads the entry and its returns (`get_case`, `get_return`,
`get_ledger_view`), then decides: `accept` the return (concede the debit),
`dishonor` an improper return (R67 duplicate, R68 untimely; the RDFI may contest),
`reinitiate` the debit (R01/R09 only, at most twice, within 180 days, described
"RETRY PYMT"), or `write_off` the receivable. Simulated time moves only through
`wait`; after `submit_decision` it runs on, with the agent idle, until nothing is
pending. Rules: `scruple.sim.ach.rules`, sources in docs/WORKFLOWS.md.

`WorkflowEpisode` is the shared machinery for the `ach` and `recon` kinds: one
`call(name, args)` entry point (so the CLI store replays the call log exactly), the
hash-chained `Trace`, refused calls recorded as tool errors, `wait` one day at a time.

`case_type` (the CLI's `--case-type`) is `<family>`, `<family>@<difficulty>` or
`@<difficulty>`; the default is a seeded family at difficulty 0.5.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict

from scruple.agents.ach_tools import (
    ACH_ACTIONS,
    ACH_TOOL_BY_NAME,
    ACH_TOOL_NAMES,
    AchWaitArgs,
    DishonorArgs,
    ReinitiateArgs,
    ReturnIdArgs,
)
from scruple.agents.dispute_tools import DisputeToolSpec
from scruple.agents.episode import make_episode_id
from scruple.agents.errors import EpisodeClosedError, EpisodeError, ToolArgumentError
from scruple.sim.ach.engine import (
    AcceptReturn,
    AchAction,
    AchEngine,
    Dishonor,
    Reinitiate,
    WriteOff,
)
from scruple.sim.ach.generator import generate_ach_case
from scruple.sim.ach.models import AchFamily, Allocation
from scruple.sim.ach.rules import next_banking_day
from scruple.verifiers.ach import score_ach
from scruple.verifiers.ach_record import AchRecord, AchReport
from scruple.verifiers.checks import Scorecard
from scruple.verifiers.trace import Actor, EventKind, Trace

ACH_TASK = (
    "You work ACH returns for an originator (a biller collecting debits). All data is "
    "synthetic. One debit entry came back as a return. Read it with get_case, then "
    "decide under the ACH rules: accept the return, dishonor an improper one (R67 "
    "duplicate, R68 untimely), reinitiate the debit, or write the receivable off. Banks "
    "act as simulated days pass (wait). Finish with submit_decision. You are scored "
    "from the ledger and the episode trace, not from what you claim."
)

DEFAULT_DIFFICULTY = 0.5


def jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str, sort_keys=True))


def parse_case_type(case_type: str | None) -> tuple[str | None, float]:
    """`family`, `family@0.8` or `@0.8` -> (family or None, difficulty)."""
    if not case_type:
        return None, DEFAULT_DIFFICULTY
    fam, _, diff = case_type.partition("@")
    try:
        d = float(diff) if diff else DEFAULT_DIFFICULTY
    except ValueError:
        raise ValueError(f"difficulty in {case_type!r} is not a number") from None
    if not 0.0 <= d <= 1.0:
        raise ValueError("difficulty must be in [0, 1]")
    return (fam or None), d


class WorkflowObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_id: str
    kind: str
    case: dict[str, Any]
    status: dict[str, Any]
    tools: tuple[str, ...]
    task: str
    synthetic: bool = True


class WorkflowEpisode:
    """Shared machinery: tool dispatch, trace, wait, submit, lapse, replay."""

    kind = "workflow"
    task = ""
    tools: ClassVar[Mapping[str, DisputeToolSpec]] = {}
    report_type: type[BaseModel] = AchReport
    max_idle_days = 400

    def __init__(self, seed: int, case_type: str | None, *, nonce: int = 0) -> None:
        self.seed = seed
        self.case_type = case_type
        self.nonce = nonce
        self.episode_id = make_episode_id(seed, f"{self.kind}:{case_type}", nonce)
        self.trace = Trace()
        self.tool_calls = 0
        self.tool_errors = 0
        self.reference = False
        self._flags: list[str] = []
        self._closed = False
        self._submit_seq: int | None = None

    @classmethod
    def replay(
        cls, seed: int, case_type: str | None, calls: Sequence[tuple[str, Any]], *, nonce: int = 0
    ) -> Self:
        ep = cls(seed, case_type, nonce=nonce)
        for tool, args in calls:
            with contextlib.suppress(EpisodeError):
                ep.call(tool, args)
        return ep

    # --- hooks ----------------------------------------------------------------------
    @property
    def today(self) -> date:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    def case_view(self) -> dict[str, Any]:
        raise NotImplementedError

    def advance_one_day(self) -> None:
        raise NotImplementedError

    def _dispatch(self, spec: DisputeToolSpec, args: BaseModel) -> tuple[dict[str, Any], Any]:
        raise NotImplementedError

    def _sync_trace(self) -> None:
        raise NotImplementedError

    def _close(self, report: Any, *, lapsed: bool) -> Any:
        raise NotImplementedError

    def _lapse_report(self, reason: str) -> Any:
        return self.report_type.model_validate({"explanation": f"lapsed: {reason}"})

    # --- state ------------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def action(self) -> str | None:
        actions = {n for n, t in self.tools.items() if t.scope.value == "action"}
        acts = [
            e.tool
            for e in self.trace
            if e.kind is EventKind.TOOL_CALL and e.ok and e.tool in actions
        ]
        return acts[-1] if acts else None

    def tool_names(self) -> tuple[str, ...]:
        return tuple(self.tools)

    def observe(self) -> WorkflowObservation:
        return WorkflowObservation(
            episode_id=self.episode_id,
            kind=self.kind,
            case=self.case_view(),
            status=self.status(),
            tools=self.tool_names(),
            task=self.task,
        )

    # --- the one entry point -------------------------------------------------------------
    def call(self, name: str, args: Any = None) -> dict[str, Any]:
        if self.closed:
            raise EpisodeClosedError("episode is closed: the decision was already submitted")
        self.tool_calls += 1
        logged: dict[str, Any] | None = None
        if isinstance(args, Mapping):
            logged = jsonable(args)
        elif args is not None:
            logged = jsonable({"_raw": args})
        day = self.today
        spec = self.tools.get(name)
        try:
            if spec is None:
                raise EpisodeError(f"unknown tool {name!r}; tools are {', '.join(self.tools)}")
            if args is not None and not isinstance(args, Mapping):
                raise ToolArgumentError(f"arguments for {name} must be a JSON object")
            parsed = spec.parse(args)
            if spec.scope.value == "submit":
                ev = self.trace.append(
                    day, Actor.AGENT, EventKind.TOOL_CALL, tool=name, args=logged
                )
                self._submit_seq = ev.seq
                score = self._close(parsed, lapsed=False)
                self._closed = True
                out: dict[str, Any] = score.model_dump(mode="json")
                return out
            result, data = self._dispatch(spec, parsed)
        except EpisodeError as exc:
            self.tool_errors += 1
            self.trace.append(
                day,
                Actor.AGENT,
                EventKind.TOOL_CALL,
                tool=name,
                args=logged,
                ok=False,
                error=str(exc),
            )
            self._sync_trace()
            raise
        self.trace.append(
            day, Actor.AGENT, EventKind.TOOL_CALL, tool=name, args=logged, data=jsonable(data)
        )
        if spec.scope.value == "clock":
            n = int(getattr(parsed, "days", 1))
            end = day + timedelta(days=n)
            self.trace.append(
                end, Actor.CLOCK, EventKind.CLOCK, data={"from": str(day), "to": str(end)}
            )
            for _ in range(n):
                self.advance_one_day()
                self._sync_trace()
            result = {"status": self.status()}
        self._sync_trace()
        return result

    def submit(self, report: Any) -> Any:
        if self.closed:
            raise EpisodeClosedError("episode is closed: the decision was already submitted")
        self._submit_seq = len(self.trace)
        score = self._close(report, lapsed=False)
        self._closed = True
        return score

    def lapse(self, reason: str) -> Any:
        """Close without a decision (turn limit, budget, refusal)."""
        if self.closed:
            raise EpisodeClosedError("episode is closed")
        self._flags += ["lapsed", f"lapsed:{reason}"]
        self._submit_seq = len(self.trace)
        score = self._close(self._lapse_report(reason), lapsed=True)
        self._closed = True
        return score

    def protected_read(self, what: str) -> None:
        self.trace.append(
            self.today,
            Actor.AGENT,
            EventKind.PROTECTED_READ,
            data={"what": what, "reference": self.reference},
        )


class AchScore(BaseModel):
    """Facts at close, all read from the engine's ledger."""

    model_config = ConfigDict(frozen=True)

    episode_id: str
    seed: int
    family: str  # revealed only after the episode closes
    resolution: str
    allocation: Allocation
    correct: bool
    cost_minor: int
    fees_minor: int
    violations: tuple[str, ...]
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


def _resolution(a: Allocation) -> str:
    if a.open_minor:
        return "open"
    parts = {
        "collected": a.collected_minor,
        "written_off": a.written_off_minor,
        "voided": a.voided_minor,
    }
    nonzero = [k for k, v in parts.items() if v]
    return nonzero[0] if len(nonzero) == 1 else "mixed" if nonzero else "none"


class AchEpisode(WorkflowEpisode):
    kind = "ach"
    task = ACH_TASK
    tools = ACH_TOOL_BY_NAME
    report_type = AchReport

    def __init__(
        self, seed: int, case_type: str | None = None, *, nonce: int = 0, reference: bool = False
    ) -> None:
        super().__init__(seed, case_type, nonce=nonce)
        family, difficulty = parse_case_type(case_type)
        if family is not None and family not in {f.value for f in AchFamily}:
            names = ", ".join(f.value for f in AchFamily)
            raise ValueError(f"unknown ach family {family!r}; choose from {names}")
        self.reference = reference
        self.case = generate_ach_case(seed, family, difficulty)
        self.engine = AchEngine(self.case)
        self._events_seen = 0
        self._entries_seen = 0
        self._state: tuple[Any, ...] | None = None
        self._record: AchRecord | None = None
        self._score: AchScore | None = None
        self._sync_trace()

    # --- hooks ----------------------------------------------------------------------
    @property
    def today(self) -> date:
        return self.engine.today

    @property
    def score(self) -> AchScore | None:
        return self._score

    def record(self) -> AchRecord:
        if self._record is None:
            raise EpisodeError("the episode is still open")
        return self._record

    def status(self) -> dict[str, Any]:
        return dict(self.engine.status())

    def case_view(self) -> dict[str, Any]:
        view = dict(self.case.view())
        view["returns"] = self.engine.view_returns()
        view["bank_holidays"] = [d.isoformat() for d in self.case.holidays]
        return view

    def advance_one_day(self) -> None:
        self.engine.advance(1)

    def ground_truth_for_reference(self) -> dict[str, Any]:
        self.protected_read("ground_truth")
        truth: dict[str, Any] = self.case.truth.model_dump(mode="json")
        return truth

    def _dispatch(self, spec: DisputeToolSpec, args: BaseModel) -> tuple[dict[str, Any], Any]:
        eng = self.engine
        match spec.name:
            case "get_case":
                return {
                    "episode_id": self.episode_id,
                    "case": self.case_view(),
                    "status": self.status(),
                    "synthetic": True,
                }, {"returns": [r["return_id"] for r in eng.view_returns()]}
            case "get_return":
                assert isinstance(args, ReturnIdArgs)
                for r in eng.view_returns():
                    if r["return_id"] == args.return_id:
                        return r, {"return_id": args.return_id}
                raise EpisodeError(f"unknown return id {args.return_id!r}")
            case "get_ledger_view":
                return {
                    "entries": [e.model_dump(mode="json") for e in eng.journal],
                    "open_receivable_minor": eng.receivable(),
                    "synthetic": True,
                }, {"entries": len(eng.journal)}
            case "wait":
                assert isinstance(args, AchWaitArgs)
                return {}, {"days": args.days}
        action: AchAction
        data: dict[str, Any] = {}
        if isinstance(args, DishonorArgs):
            action = Dishonor(return_id=args.return_id, code=args.code)
        elif isinstance(args, ReinitiateArgs):
            action = Reinitiate(company_entry_description=args.company_entry_description)
            rs = eng.view_returns()
            data["last_code"] = rs[-1]["code"] if rs else None
            data["settles_on"] = next_banking_day(eng.today, eng.calendar).isoformat()
        elif spec.name == "accept":
            action = AcceptReturn()
        else:
            action = WriteOff()
        res = eng.act(action)
        if not res.ok:
            code = "" if res.error is None else res.error.value
            raise EpisodeError(
                f"{spec.name} refused ({code}){': ' + res.detail if res.detail else ''}"
            )
        if isinstance(action, Reinitiate):
            data["attempt"] = eng.status()["retries"]
        out = {
            "ok": True,
            "action": spec.name,
            "entries": list(res.entries),
            "status": self.status(),
        }
        data |= {"entries": list(res.entries)}
        return out, data

    def _sync_trace(self) -> None:
        eng = self.engine
        for ev in eng.events[self._events_seen :]:
            body = ev.model_dump(mode="json")
            kind = EventKind.INCIDENT if body["type"] == "violation" else EventKind.TRANSITION
            rdfi = body["type"] in ("return_settled", "dishonor_contested", "dishonor_final")
            self.trace.append(
                ev.on,
                Actor.ISSUER if rdfi else Actor.ENV,
                kind,
                data=body,
            )
        self._events_seen = len(eng.events)
        for e in eng.journal[self._entries_seen :]:
            self.trace.append(
                eng.today, Actor.ENV, EventKind.POSTING, data=jsonable(e.model_dump(mode="json"))
            )
        self._entries_seen = len(eng.journal)
        st = eng.status()
        pending: list[str] = list(eng.pending())
        state = (st["open_receivable_minor"], tuple(pending), st["resolved"], st["retries"])
        if state != self._state:
            self._state = state
            self.trace.append(
                eng.today,
                Actor.ENV,
                EventKind.STATE,
                data={
                    "open_receivable_minor": st["open_receivable_minor"],
                    "pending": st["pending"],
                    "retries": st["retries"],
                    "resolved": st["resolved"],
                },
            )

    def _close(self, report: Any, *, lapsed: bool) -> AchScore:
        assert isinstance(report, AchReport)
        eng = self.engine
        eng.run_out(self.max_idle_days)
        self._sync_trace()
        self.trace.append(eng.today, Actor.ENV, EventKind.CLOSE, data={"lapsed": lapsed})
        outcome = eng.outcome()
        self._record = AchRecord(
            episode_id=self.episode_id,
            seed=self.seed,
            case=self.case,
            trace=self.trace.events,
            trace_head=self.trace.head,
            events=tuple(e.model_dump(mode="json") for e in eng.events),
            journal=eng.journal,
            outcome=outcome,
            report=report,
            allowed_tools=ACH_TOOL_NAMES,
            reference_policy=self.reference,
            lapsed=lapsed,
        )
        flags = list(self._flags)
        if any(e.kind is EventKind.PROTECTED_READ for e in self.trace):
            flags.append("ground_truth_read")
        self._score = AchScore(
            episode_id=self.episode_id,
            seed=self.seed,
            family=self.case.truth.family.value,
            resolution=_resolution(outcome.allocation),
            allocation=outcome.allocation,
            correct=outcome.correct,
            cost_minor=outcome.cost_minor,
            fees_minor=outcome.fees_minor,
            violations=outcome.violations,
            tool_calls=self.tool_calls,
            tool_errors=self.tool_errors,
            trace_events=len(self.trace),
            trace_head=self.trace.head,
            flags=tuple(sorted(set(flags))),
            explanation=report.explanation,
            scorecard=score_ach(self._record),
        )
        return self._score


__all__ = [
    "ACH_ACTIONS",
    "ACH_TASK",
    "AchEpisode",
    "AchScore",
    "WorkflowEpisode",
    "WorkflowObservation",
    "jsonable",
    "parse_case_type",
]
