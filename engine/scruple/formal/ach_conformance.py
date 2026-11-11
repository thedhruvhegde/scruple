"""Conformance: the real `AchEngine` against `scruple.formal.ach`, step by step.

As for disputes (`scruple.formal.conformance`): a random originator (every action,
legal or not) drives the engine; the event log is cut into steps (a return, dishonor
or retry settling, a contest resolving, an accepted action); each step must be an
enabled model command whose successor equals the engine replayed to that point
(`AchEngine.replay`). What is read from the engine to pick the command: its name, and
the facts that come from days or the receiver (was the dishonor timely, did the retry
find funds, was the retry late or mis-described). After every call the allowed actions
agree, and a refused action is one the model refuses too.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from scruple.formal import ach as am
from scruple.formal.conformance import InvariantBrokenError
from scruple.formal.ops import Inputs, ModelError, Params, PyOps, State
from scruple.sim.ach.engine import (
    BAD_DEBT,
    CASH,
    CONTRA,
    FEES,
    REVENUE,
    AcceptReturn,
    AchAction,
    AchEngine,
    AchEvent,
    ActionApplied,
    DebitSettled,
    Dishonor,
    DishonorContested,
    DishonorFinal,
    DishonorSettled,
    Reinitiate,
    ReturnScheduled,
    ReturnSettled,
    Violation,
    WriteOff,
)
from scruple.sim.ach.models import AchCase
from scruple.sim.ach.rules import (
    CODES,
    REINIT_DESCRIPTION,
    UNAUTHORIZED_ENTRY_FEE_MINOR,
    Reinit,
    dishonor_deadline,
)

STARTS = (ActionApplied, ReturnSettled, DishonorSettled, DishonorFinal, DishonorContested)
NO_INPUT: Inputs = {"r67": False, "dt": False, "funded": False, "late": False, "bad_desc": False}


def _slot(case: AchCase, rid: str) -> int:
    ids = [r.return_id for r in case.returns]
    if rid in ids:
        return ids.index(rid)
    if rid.startswith("RET-R"):
        return int(rid[5:]) + 1
    raise ModelError(f"unknown return {rid}")


def ach_params(eng: AchEngine) -> Params:
    c = eng.case
    if len(c.returns) > 2:
        raise ModelError("the model has two slots for a case's own returns")
    p: Params = {
        "a": c.amount_minor,
        "fee": UNAUTHORIZED_ENTRY_FEE_MINOR,
        "two": len(c.returns) == 2,
    }
    rs = list(eng._st.returns.values())
    for k in (0, 1):
        r = rs[k] if k < len(c.returns) else None
        code = CODES[r.code] if r is not None else None
        p[f"nsf{k}"] = code is not None and code.reinit is Reinit.NSF
        p[f"uf{k}"] = code is not None and code.unauthorized_fee
        p[f"tim{k}"] = r is not None and eng.is_timely(r)
        p[f"dup{k}"] = r is not None and eng.is_duplicate(r)
    return p


def abstract(eng: AchEngine) -> State:
    st = eng._st
    s: State = {}
    for k in range(am.N):
        s[f"st_{k}"], s[f"d_{k}"] = am.NONE, am.D_NONE
    for r in st.returns.values():
        k = _slot(eng.case, r.return_id)
        code = CODES[r.code]
        s[f"st_{k}"] = am.SETTLED if r.settled else am.SCHEDULED
        s[f"nsf_{k}"] = code.reinit is Reinit.NSF
        s[f"uf_{k}"] = code.unauthorized_fee
        s[f"tim_{k}"] = eng.is_timely(r)
        s[f"dup_{k}"] = eng.is_duplicate(r)
        if r.dishonor_code is not None:
            s[f"r67_{k}"] = r.dishonor_code == "R67"
            if r.dishonor_resolved == "final":
                s[f"d_{k}"] = am.D_FINAL
            elif r.dishonor_resolved is not None:
                s[f"d_{k}"] = am.D_CONTESTED
            else:
                s[f"d_{k}"] = am.D_SETTLED if r.dishonor_settled else am.D_SUBMITTED
            if r.dishonor_resolved is not None:
                assert r.dishonor_settles_on is not None
                s[f"dt_{k}"] = r.dishonor_settles_on <= dishonor_deadline(
                    r.settles_on, eng.calendar
                )
    if eng.settled_returns():
        last = CODES[eng._last_code()]
        s["last_nsf"], s["last_uf"] = last.reinit is Reinit.NSF, last.unauthorized_fee
    bal = eng.ledger.balance
    s.update(
        retry=st.retry is not None,
        attempts=st.attempts,
        viol=len(st.violations),
        rcv=eng.receivable(),
        cash=bal(CASH, "USD"),
        rev=bal(REVENUE, "USD"),
        contra=bal(CONTRA, "USD"),
        bad=bal(BAD_DEBT, "USD"),
        fees=bal(FEES, "USD"),
    )
    return s


def view(s: State) -> State:
    """Compared fields; per-slot facts only where the slot is in use."""
    keys = ["retry", "attempts", "viol", "rcv", "cash", "rev", "contra", "bad", "fees"]
    out = {k: s[k] for k in keys}
    any_settled = False
    for k in range(am.N):
        out[f"st_{k}"], out[f"d_{k}"] = s[f"st_{k}"], s[f"d_{k}"]
        if s[f"st_{k}"] != am.NONE:
            for f in ("nsf", "uf", "tim", "dup"):
                out[f"{f}_{k}"] = s[f"{f}_{k}"]
        any_settled = any_settled or s[f"st_{k}"] == am.SETTLED
        if s[f"d_{k}"] != am.D_NONE:
            out[f"r67_{k}"] = s[f"r67_{k}"]
        if s[f"d_{k}"] >= am.D_FINAL:
            out[f"dt_{k}"] = s[f"dt_{k}"]
    if any_settled:
        out["last_nsf"], out["last_uf"] = s["last_nsf"], s["last_uf"]
    return out


def _starts(ev: AchEvent) -> bool:
    return isinstance(ev, STARTS) or (isinstance(ev, DebitSettled) and ev.attempt > 0)


def label(case: AchCase, group: list[AchEvent]) -> tuple[str, Inputs]:
    first, inp = group[0], dict(NO_INPUT)
    violations = {e.kind for e in group if isinstance(e, Violation)}
    match first:
        case ReturnSettled(return_id=rid):
            return f"RETURN_{am.SLOTS[_slot(case, rid)]}", inp
        case DishonorSettled(return_id=rid):
            return f"DISHONOR_SETTLES_{am.SLOTS[_slot(case, rid)]}", inp
        case DishonorFinal(return_id=rid):
            return f"DISHONOR_FINAL_{am.SLOTS[_slot(case, rid)]}", {**inp, "dt": True}
        case DishonorContested(return_id=rid, code=code):
            name = f"DISHONOR_CONTESTED_{am.SLOTS[_slot(case, rid)]}"
            return name, {**inp, "dt": code != "R72"}
        case DebitSettled():
            if any(isinstance(e, ReturnScheduled) for e in group):
                return "RETRY_RETURNED", inp
            return "RETRY_CLEARS", {**inp, "funded": True}
        case ActionApplied(action="accept"):
            return "ACCEPT", inp
        case ActionApplied(action="write_off"):
            return "WRITE_OFF", inp
        case ActionApplied(action="dishonor", args=args):
            slot = am.SLOTS[_slot(case, args["return_id"])]
            return f"DISHONOR_{slot}", {**inp, "r67": args["code"] == "R67"}
        case ActionApplied(action="reinitiate"):
            late, desc = "reinit_window" in violations, "reinit_description" in violations
            return "REINITIATE", {**inp, "late": late, "bad_desc": desc}
    raise ModelError(f"no model command for the step starting with {first}")


def allowed(s: State, p: Params, ops: PyOps) -> set[str]:
    out: set[str] = set()
    for name, kind in (
        ("ACCEPT", "accept"),
        ("WRITE_OFF", "write_off"),
        ("REINITIATE", "reinitiate"),
    ):
        if am.MODEL.command(name).guard(s, p, NO_INPUT, ops):
            out.add(kind)
    if any(am.MODEL.command(f"DISHONOR_{x}").guard(s, p, NO_INPUT, ops) for x in am.SLOTS):
        out.add("dishonor")
    return out


@dataclass
class AchTrace:
    commands: list[str] = field(default_factory=list)
    refused: int = 0


class AchConformance:
    def __init__(self, case: AchCase, *, invariants: bool = True) -> None:
        self.case = case
        self.invariants = invariants
        self.engine = AchEngine(case)
        self.ops = PyOps()
        self.p = ach_params(self.engine)
        self.s = am.MODEL.init(self.p, dict(NO_INPUT), self.ops)
        self.seen = 2 + len(case.returns)  # invoice, debit, the scheduled returns
        self.trace = AchTrace()
        self._catch_up()

    def _check(self, eng: AchEngine, where: str) -> None:
        want, got = view(self.s), view(abstract(eng))
        if want != got:
            diff = {
                k: (want.get(k), got.get(k))
                for k in set(want) | set(got)
                if want.get(k) != got.get(k)
            }
            raise ModelError(f"{self.case.case_id} after {where}: model != engine {diff}")
        if self.invariants:
            broken = am.MODEL.check(self.s, self.p, self.ops)
            if broken:
                raise InvariantBrokenError(f"{self.case.case_id} after {where}: {broken}")

    def _catch_up(self) -> None:
        evs = self.engine.events
        groups: list[list[AchEvent]] = []
        for ev in evs[self.seen :]:
            if not groups or _starts(ev):
                groups.append([ev])
            else:
                groups[-1].append(ev)
        at = self.seen
        for g in groups:
            if not _starts(g[0]):
                raise ModelError(f"{self.case.case_id}: events {g} outside any step")
            name, inp = label(self.case, g)
            s2 = am.MODEL.step(self.s, self.p, name, inp, self.ops)
            if self.invariants:
                broken = am.MODEL.check_step(self.s, s2, self.p, name, inp, self.ops)
                if broken:
                    raise InvariantBrokenError(f"{self.case.case_id} step {name}: {broken}")
            self.s = s2
            at += len(g)
            self.trace.commands.append(name)
            self._check(AchEngine.replay(self.case, tuple(evs[:at])), name)
        self.seen = at
        if self.s["attempts"] < am.MAX_RETRIES:
            want, got = allowed(self.s, self.p, self.ops), set(self.engine.allowed_actions())
            if want != got:
                raise ModelError(f"{self.case.case_id}: allowed model {want} != engine {got}")

    def advance(self, days: int) -> None:
        self.engine.advance(days)
        self._catch_up()

    def act(self, action: AchAction) -> bool:
        res = self.engine.act(action)
        self._catch_up()
        if not res.ok:
            self.trace.refused += 1
            self._check_refusal(action)
        return res.ok

    def _check_refusal(self, action: AchAction) -> None:
        s, p, ops = self.s, self.p, self.ops
        match action:
            case AcceptReturn():
                ok = am.MODEL.command("ACCEPT").guard(s, p, NO_INPUT, ops)
            case WriteOff():
                ok = am.MODEL.command("WRITE_OFF").guard(s, p, NO_INPUT, ops)
            case Reinitiate():
                ok = am.MODEL.command("REINITIATE").guard(s, p, NO_INPUT, ops)
            case Dishonor(return_id=rid, code=code):
                if code not in ("R67", "R68"):
                    return
                try:
                    slot = am.SLOTS[_slot(self.case, rid)]
                except ModelError:
                    return
                ok = am.MODEL.command(f"DISHONOR_{slot}").guard(s, p, NO_INPUT, ops)
        if ok:
            raise ModelError(f"{self.case.case_id}: engine refused {action}, the model allows it")


def random_action(rng: random.Random, conf: AchConformance) -> AchAction:
    kind = rng.choice(("accept", "write_off", "dishonor", "dishonor", "reinitiate", "reinitiate"))
    match kind:
        case "accept":
            return AcceptReturn()
        case "write_off":
            return WriteOff()
        case "reinitiate":
            desc = REINIT_DESCRIPTION if rng.random() < 0.8 else "PAYMENT"
            return Reinitiate(company_entry_description=desc)
    ids = [r.return_id for r in conf.engine._st.returns.values()] + ["RET-NOPE"]
    code = rng.choice(("R67", "R68", "R68", "R67", "R99"))
    return Dishonor(return_id=rng.choice(ids), code=code)


def run_ach(case: AchCase, seed: int, steps: int = 25, *, invariants: bool = True) -> AchTrace:
    rng = random.Random(f"formal-ach:{seed}:{case.case_id}")
    conf = AchConformance(case, invariants=invariants)
    for _ in range(steps):
        if rng.random() < 0.4:
            conf.advance(rng.choice((1, 1, 2, 3, 5, 14, 30)))
        else:
            act = random_action(rng, conf)
            if isinstance(act, Reinitiate) and conf.s["attempts"] >= am.MAX_RETRIES:
                continue  # beyond the model's bound on retries
            conf.act(act)
    conf.engine.run_out()
    conf._catch_up()
    return conf.trace
