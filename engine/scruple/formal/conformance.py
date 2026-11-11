"""Conformance: the real `DisputeEngine` against the model, step by step.

`run_dispute(case, seed, steps)` drives a one-case engine with a random agent (every
action kind, legal or not, random refund amounts and evidence, random clock moves)
and checks that every engine step is a step of the model:

1. The engine's event log is cut into steps where the engine itself starts one (an
   accepted action, a refund settling, a missed deadline, an issuer reply, a ruling).
   Each step names one model command; the only things read from the engine to choose
   it are the command's name and the issuer's award `w` (the model allows any).
2. The command must be enabled in the model state (its guard holds), and the model's
   successor must equal the abstraction of the engine replayed to the end of that
   step (`DisputeEngine.at`): stage, who acts, resolution, dates, deadline, refund in
   flight, returned, every ledger balance, fees, violations and incidents.
3. After every engine call the model is quiescent at the clock's day (nothing due the
   engine has not done), its allowed actions equal `allowed_actions`, and a refused
   action is one the model also refuses.
4. Every invariant of `scruple.formal.dispute` holds on every model state and step.

The model's own pieces are the deadlines (from the network rules data) and FX
(`scruple.formal.fx`); they are compared through the engine's postings and deadline.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from scruple.formal import dispute as dm
from scruple.formal.fx import FxModel
from scruple.formal.ops import INF, Inputs, ModelError, Params, PyOps, State
from scruple.sim.card_ledger import FEE_CURRENCY
from scruple.sim.deadlines import (
    DEFAULT_CALENDAR,
    STAGE_WINDOW,
    BusinessCalendar,
    ManualClock,
    window_end,
)
from scruple.sim.disputes import (
    Accept,
    ActionApplied,
    AgentAction,
    AwaitingSet,
    CardDispute,
    CaseResolved,
    DisputeEngine,
    EngineEvent,
    Escalate,
    PendingRefundCleared,
    Refund,
    RefundPosted,
    Represent,
    RequestInfo,
    ReversalPosted,
    StageFired,
)
from scruple.sim.fees import DEFAULT_FEES, FeeSchedule
from scruple.sim.lifecycle import Event
from scruple.sim.models import Stage
from scruple.sim.networks import WindowId, Workflow

STAGE_ID = {Stage(n): i for i, n in enumerate(dm.STAGES)}
AW_ID = {n: i for i, n in enumerate(dm.AWAITING)}
RES_ID = {n: i for i, n in enumerate(dm.RESOLUTIONS)}
COMPARED = (
    "stage",
    "aw",
    "res",
    "entered",
    "due",
    "dl",
    "declined",
    "pending",
    "returned",
    "ch",
    "m",
    "pf",
    "nf",
    "fees",
    "viol",
    "inc_dbl",
    "inc_blk",
)


def _o(d: date) -> int:
    return d.toordinal()


def dispute_params(c: CardDispute, fees: FeeSchedule, issuer_days: int, arb_days: int) -> Params:
    rf = c.refund_in_flight
    return {
        "C": c.captured_minor,
        "A": c.amount_minor,
        "alloc": c.workflow is Workflow.ALLOCATION,
        "opens_cb": c.opening_stage is Stage.CHARGEBACK,
        "race": rf is not None,
        "R": rf.amount_minor if rf is not None else 1,
        "race_day": _o(rf.settles_on) if rf is not None else _o(c.processed_on),
        "processed": _o(c.processed_on),
        "opened": _o(c.opened_on),
        "f_cb": fees.entry_fee(Stage.CHARGEBACK),
        "f_rep": fees.entry_fee(Stage.REPRESENTMENT),
        "f_pre": fees.entry_fee(Stage.PRE_ARBITRATION),
        "f_arb": fees.entry_fee(Stage.ARBITRATION),
        "f_loss": fees.arbitration_loss,
        "iss_days": issuer_days,
        "arb_days": arb_days,
        "single": c.currency == c.settlement_currency == FEE_CURRENCY,
    }


def dispute_ops(c: CardDispute, fx: FxModel, calendar: BusinessCalendar) -> PyOps:
    """The model's concrete hooks: network deadlines and integer FX."""
    rules = c.rules
    stages = {i: Stage(n) for i, n in enumerate(dm.STAGES)}

    def deadline(stage: int, start: int, declined: bool) -> int:
        day = date.fromordinal(start)
        if declined:
            return _o(day + timedelta(days=rules.window(WindowId.ARBITRATION_FILING).days))
        w = rules.window(STAGE_WINDOW[stages[stage]])
        return _o(window_end(day, w, calendar))

    def post(amount: int, day: int) -> int:
        return fx.convert(amount, c.currency, c.settlement_currency, day)

    return PyOps(deadline_fn=deadline, post_fn=post)


def abstract(eng: DisputeEngine, case_id: str) -> State:
    """The engine's state of one case, in the model's terms (one case per engine)."""
    st = eng._cases[case_id]
    c = st.case
    led = eng.ledger
    resolved = st.resolution is not None
    dl = eng.deadline(case_id)
    fee_entries = [e for e in led.entries if e.case_id == case_id and e.kind.startswith("fee:")]
    return {
        "stage": STAGE_ID[st.life.stage],
        "aw": AW_ID[st.awaiting],
        "res": RES_ID[None if st.resolution is None else st.resolution.value],
        "entered": _o(st.life.entered_on),
        "due": INF if st.due is None else _o(st.due),
        "dl": INF if dl is None or resolved else _o(dl.due),
        "declined": st.declined_on is not None,
        "pending": st.pending_refund is not None,
        "returned": led.returned(c.txn_id),
        "ch": led.balance(f"cardholder:{c.txn_id}", c.settlement_currency),
        "m": led.balance(f"merchant:{c.merchant_id}", FEE_CURRENCY),
        "pf": led.balance("processor:fees", FEE_CURRENCY),
        "nf": led.balance("network:fees", FEE_CURRENCY),
        "fees": sum(e.postings[1].amount_minor for e in fee_entries),
        "viol": len(led.violations),
        "inc_dbl": sum(1 for i in eng.incidents if i.kind == "double_refund"),
        "inc_blk": sum(1 for i in eng.incidents if i.kind == "refund_blocked"),
    }


def view(s: State) -> State:
    """The compared fields, with the ones that mean nothing in this state blanked."""
    out = {k: s[k] for k in COMPARED}
    if s["aw"] not in (dm.ISS, dm.NET) or s["res"] != dm.NONE:
        out["due"] = None
    if s["aw"] != dm.MER or s["res"] != dm.NONE:
        out["dl"] = None
    return out


def _starts_step(ev: EngineEvent) -> bool:
    match ev:
        case ActionApplied() | PendingRefundCleared():
            return True
        case StageFired(event=Event.DEADLINE_MISSED):
            return True
        case AwaitingSet(awaiting="issuer" | "network", due=None):
            return True
    return False


def label(group: list[EngineEvent], s: State, p: Params) -> tuple[str, Inputs]:
    """The model command one engine step is, with its inputs."""
    first = group[0]
    inp: Inputs = {"d": 0, "x": 0, "w": 0, "dl": 0}
    resolved = next((e.resolution.value for e in group if isinstance(e, CaseResolved)), None)
    reversal = sum(e.amount_minor for e in group if isinstance(e, ReversalPosted))
    match first:
        case ActionApplied(action=action, on=on):
            inp["d"] = _o(on)
            match action:
                case Accept():
                    return "ACCEPT", inp
                case Represent() if s["stage"] == dm.INQ:
                    if resolved == "won":
                        return "REP_INQ_WIN", {**inp, "w": p["A"]}
                    return "REP_INQ_CB", inp
                case Represent():
                    return "REP_CB", inp
                case Escalate():
                    return "ESCALATE", inp
                case Refund(amount_minor=x):
                    return "REFUND", {**inp, "x": x}
                case RequestInfo():
                    return "NOOP", inp
        case PendingRefundCleared():
            posted = any(isinstance(e, RefundPosted) for e in group)
            return ("RACE_POST" if posted else "RACE_BLOCK"), inp
        case StageFired(event=Event.DEADLINE_MISSED):
            return ("MISS_INQ" if s["stage"] == dm.INQ else "MISS_LOSE"), inp
        case AwaitingSet(awaiting="issuer"):
            if reversal:
                return "ISS_WIN", {**inp, "w": reversal}
            return ("ISS_PREARB" if s["stage"] == dm.REP else "ISS_DECLINE"), inp
        case AwaitingSet(awaiting="network"):
            if reversal:
                return "RULE_WIN", {**inp, "w": reversal}
            return "RULE_LOSE", inp
    raise ModelError(f"no model command for the step starting with {first}")


@dataclass
class Trace:
    """What one conformance run saw (for coverage reports)."""

    commands: list[str] = field(default_factory=list)
    refused: int = 0
    checked_states: int = 0


class DisputeConformance:
    """A one-case engine and the model, stepped together."""

    def __init__(
        self,
        case: CardDispute,
        *,
        fees: dict[Any, FeeSchedule] | None = None,
        calendar: BusinessCalendar = DEFAULT_CALENDAR,
        invariants: bool = True,
    ) -> None:
        self.case = case
        self.invariants = invariants
        self.clock = ManualClock()
        self.clock.set_date(case.opened_on)
        self.fees = fees if fees is not None else DEFAULT_FEES
        self.engine = DisputeEngine([case], self.clock, fees=self.fees, calendar=calendar)
        e = self.engine.params
        self.p = dispute_params(
            case, self.fees[case.network], e.issuer_response_days, e.arbitration_ruling_days
        )
        self.fx = FxModel(self.engine.ledger.fx)
        self.ops = dispute_ops(case, self.fx, calendar)
        self.model = dm.MODEL
        self.s = self.model.init(self.p, {"d": 0, "x": 0, "w": 0, "dl": 0}, self.ops)
        self.seen = self.engine.opening_events(case.case_id)
        self.trace = Trace()
        self._check_state(self.engine, "opening")

    @property
    def cid(self) -> str:
        return self.case.case_id

    def _check_state(self, eng: DisputeEngine, where: str) -> None:
        want, got = view(self.s), view(abstract(eng, self.cid))
        if want != got:
            diff = {k: (want[k], got[k]) for k in want if want[k] != got[k]}
            raise ModelError(f"{self.cid} after {where}: model != engine {diff}")
        broken = self.model.check(self.s, self.p, self.ops) if self.invariants else []
        if broken:
            raise InvariantBrokenError(f"{self.cid} after {where}: {broken} in {self.s}")
        self.trace.checked_states += 1

    def _catch_up(self) -> None:
        """Fold every new engine step into the model, checking each one."""
        evs = self.engine.events
        new = list(evs[self.seen :])
        groups: list[list[EngineEvent]] = []
        for ev in new:
            if not groups or _starts_step(ev):
                groups.append([ev])
            else:
                groups[-1].append(ev)
        at = self.seen
        for g in groups:
            if not _starts_step(g[0]):
                raise ModelError(f"{self.cid}: events {g} outside any step")
            name, inp = label(g, self.s, self.p)
            s2 = self.model.step(self.s, self.p, name, inp, self.ops)
            broken = (
                self.model.check_step(self.s, s2, self.p, name, inp, self.ops)
                if self.invariants
                else []
            )
            if broken:
                raise InvariantBrokenError(f"{self.cid} step {name}: {broken}")
            self.s = s2
            at += len(g)
            self.trace.commands.append(name)
            self._check_state(self.engine.at(at), name)
        self.seen = at

    def _check_quiet(self) -> None:
        now = _o(self.clock.now().date())
        if not dm.quiet_by(self.s, self.p, self.ops, now):
            raise ModelError(f"{self.cid}: the model has a step due by {now} the engine skipped")
        want = set(dm.allowed(self.s))
        got = set(self.engine.allowed_actions(self.cid))
        if want != got:
            raise ModelError(f"{self.cid}: allowed actions model {want} != engine {got}")
        self._check_state(self.engine, "sync")

    def advance(self, days: int) -> None:
        self.clock.advance(days=days)
        self.engine.sync()
        self._catch_up()
        self._check_quiet()

    def act(self, action: AgentAction) -> bool:
        res = self.engine.act(self.cid, action)
        self._catch_up()
        self._check_quiet()
        if not res.ok:
            self.trace.refused += 1
            self._check_refusal(action, res.error.value if res.error else "")
        return res.ok

    def _check_refusal(self, action: AgentAction, error: str) -> None:
        if error == "bad_evidence":
            return  # evidence ids are outside the abstraction
        if action.kind not in dm.allowed(self.s):
            return
        if isinstance(action, Refund) and action.amount_minor > self.p["C"] - self.s["returned"]:
            return
        raise ModelError(f"{self.cid}: engine refused {action.kind} ({error}), the model allows it")


class InvariantBrokenError(AssertionError):
    """An invariant failed on a state the real engine reached."""


def random_action(rng: random.Random, conf: DisputeConformance) -> AgentAction:
    s, p = conf.s, conf.p
    kind = rng.choice(("accept", "represent", "escalate", "request_info", "refund"))
    match kind:
        case "accept":
            return Accept()
        case "escalate":
            return Escalate()
        case "request_info":
            return RequestInfo()
        case "refund":
            head = p["C"] - s["returned"]
            amounts = [1, max(1, p["A"] - s["returned"]), max(1, head), head + 1, p["A"]]
            return Refund(amount_minor=max(1, rng.choice([*amounts, rng.randint(1, p["C"])])))
    ids = [d.doc_id for d in conf.engine.vault.for_case(conf.cid)]
    pick = tuple(x for x in ids if rng.random() < 0.6)
    if rng.random() < 0.05:
        pick = (*pick, "no-such-doc")
    return Represent(evidence_ids=pick)


def run_dispute(case: CardDispute, seed: int, steps: int = 30, *, invariants: bool = True) -> Trace:
    """Random agent against engine and model; raises on the first disagreement."""
    rng = random.Random(f"formal:{seed}:{case.case_id}")
    conf = DisputeConformance(case, invariants=invariants)
    for _ in range(steps):
        if rng.random() < 0.45:
            conf.advance(rng.choice((0, 1, 2, 5, 9, 11, 20, 31, 45)))
        else:
            conf.act(random_action(rng, conf))
    conf.advance(400)  # let every pending step happen
    return conf.trace
