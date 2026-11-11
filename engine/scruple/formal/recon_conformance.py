"""Conformance: the real `ReconEngine` against `scruple.formal.recon`, action by action.

The model is built at the case's size (every statement and book line, enough match
and adjustment slots for the run). Every accepted action must be the enabled model
command it names, with the same successor (matches, adjustments and their citations,
timing marks, escalations, book cash, the other accounts, and the engine's reported
difference); every refused action (other than malformed ids, kinds or invoices, which
the abstraction leaves out) must be refused by the model too.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from scruple.formal import recon as rm
from scruple.formal.conformance import InvariantBrokenError
from scruple.formal.ops import Inputs, Model, ModelError, Params, PyOps, State
from scruple.sim.recon.engine import (
    CASH,
    Adjust,
    Escalate,
    MarkTiming,
    Match,
    ReconAction,
    ReconEngine,
    Unmatch,
)
from scruple.sim.recon.models import ADJUST_KINDS, ReconCase


@dataclass
class ReconTrace:
    commands: list[str] = field(default_factory=list)
    refused: int = 0


class ReconConformance:
    def __init__(self, case: ReconCase, slots: int, *, invariants: bool = True) -> None:
        self.case = case
        self.invariants = invariants
        self.engine = ReconEngine(case)
        self.s_ids = list(self.engine.lines)
        self.l_ids = [x.line_id for x in case.ledger_lines]
        self.model: Model = rm.recon_model(len(self.s_ids), len(self.l_ids), slots, slots)
        self.ops = PyOps()
        self.p: Params = {"o": case.opening_balance_minor, "close": self.engine.bank_closing()}
        self.p |= {f"s{i}": self.engine.lines[x].amount_minor for i, x in enumerate(self.s_ids)}
        self.p |= {f"l{j}": self.engine.book[x].amount_minor for j, x in enumerate(self.l_ids)}
        if not all(self.model.param_constraints(self.p, self.ops)):
            raise ModelError(f"{case.case_id}: the statement does not add up to its closing")
        self.s = self.model.init(self.p, self._inputs(), self.ops)
        self.trace = ReconTrace()
        self._check("opening")

    def _inputs(
        self, s_ids: tuple[str, ...] = (), l_ids: tuple[str, ...] = (), amt: int = 0
    ) -> Inputs:
        inp: Inputs = {"amt": amt}
        inp |= {f"ss{i}": x in s_ids for i, x in enumerate(self.s_ids)}
        inp |= {f"sl{j}": x in l_ids for j, x in enumerate(self.l_ids)}
        return inp

    def abstract(self) -> State:
        eng, st = self.engine, self.engine._st
        s: State = {k: (False if t is bool else 0) for k, t in self.model.state.items()}
        s["used"], s["nadj"] = st.n_match, len(st.adjustments)
        for mid, (a, b) in st.matches.items():
            x = int(mid.split("-")[1]) - 1
            s[f"mon_{x}"] = True
            s |= {f"ms_{x}_{i}": q in a for i, q in enumerate(self.s_ids)}
            s |= {f"ml_{x}_{j}": q in b for j, q in enumerate(self.l_ids)}
        for y, adj in enumerate(st.adjustments):
            s[f"aon_{y}"], s[f"amt_{y}"] = True, adj.amount_minor
            s |= {f"as_{y}_{i}": q in adj.statement_line_ids for i, q in enumerate(self.s_ids)}
            s |= {f"al_{y}_{j}": q in adj.ledger_line_ids for j, q in enumerate(self.l_ids)}
        es, el = eng._escalated()
        s |= {f"es_{i}": q in es for i, q in enumerate(self.s_ids)}
        s |= {f"el_{j}": q in el for j, q in enumerate(self.l_ids)}
        s |= {f"tm_{j}": q in st.timing for j, q in enumerate(self.l_ids)}
        ccy = self.case.currency
        accounts = {p.account for e in eng.journal for p in e.postings}
        s["cash"] = eng.book_balance()
        s["off"] = sum(eng.ledger.balance(a, ccy) for a in accounts if a != CASH)
        return s

    def view(self, s: State) -> State:
        out = dict(s)
        for x in self._slots():
            if not s[f"mon_{x}"]:
                for k in list(out):
                    if k.startswith((f"ms_{x}_", f"ml_{x}_")):
                        del out[k]
        return out

    def _slots(self) -> range:
        return range(sum(1 for k in self.model.state if k.startswith("mon_")))

    def _check(self, where: str) -> None:
        want, got = self.view(self.s), self.view(self.abstract())
        if want != got:
            diff = {k: (want.get(k), got.get(k)) for k in want if want.get(k) != got.get(k)}
            raise ModelError(f"{self.case.case_id} after {where}: model != engine {diff}")
        if rm.difference(self.model, self.s, self.p, self.ops) != self.engine.difference():
            raise ModelError(f"{self.case.case_id} after {where}: difference disagrees")
        if self.invariants:
            broken = self.model.check(self.s, self.p, self.ops)
            if broken:
                raise InvariantBrokenError(f"{self.case.case_id} after {where}: {broken}")

    def _command(self, action: ReconAction) -> tuple[str, Inputs] | None:
        """The model command an action names, or None when the abstraction leaves the
        action out (malformed ids, kinds, invoices)."""
        s_ids = tuple(getattr(action, "statement_line_ids", ()))
        l_ids = tuple(getattr(action, "ledger_line_ids", ()))
        if len(set(s_ids)) != len(s_ids) or len(set(l_ids)) != len(l_ids):
            return None
        if any(x not in self.s_ids for x in s_ids) or any(x not in self.l_ids for x in l_ids):
            return None
        match action:
            case Match():
                return f"MATCH_{self.s['used']}", self._inputs(s_ids, l_ids)
            case Unmatch(match_id=mid):
                if not mid.startswith("M-") or not mid[2:].isdigit():
                    return None
                x = int(mid[2:]) - 1
                if not 0 <= x < len(self._slots()):
                    return None
                return f"UNMATCH_{x}", self._inputs()
            case Adjust(adjust_kind=kind, invoice_id=inv):
                if kind not in ADJUST_KINDS or (
                    inv is not None and inv not in self.engine.invoices
                ):
                    return None
                if kind == "duplicate_reversal" and len(l_ids) != 1:
                    return None
                return f"ADJUST_{self.s['nadj']}", self._inputs(s_ids, l_ids, action.amount_minor)
            case MarkTiming():
                return "MARK_TIMING", self._inputs(s_ids, l_ids)
            case Escalate():
                return "ESCALATE", self._inputs(s_ids, l_ids)
        return None  # pragma: no cover

    def act(self, action: ReconAction) -> bool:
        named = self._command(action)
        res = self.engine.act(action)
        if named is None:
            if res.ok:
                raise ModelError(f"{self.case.case_id}: engine accepted {action} outside the model")
            return False
        name, inp = named
        enabled = self.model.command(name).guard(self.s, self.p, inp, self.ops)
        if not res.ok:
            self.trace.refused += 1
            if enabled:
                raise ModelError(
                    f"{self.case.case_id}: engine refused {action}, model allows {name}"
                )
            self._check("refusal")
            return False
        s2 = self.model.step(self.s, self.p, name, inp, self.ops)
        if self.invariants:
            broken = self.model.check_step(self.s, s2, self.p, name, inp, self.ops)
            if broken:
                raise InvariantBrokenError(f"{self.case.case_id} step {name}: {broken}")
        self.s = s2
        self.trace.commands.append(name)
        self._check(name)
        return True


def random_action(rng: random.Random, conf: ReconConformance) -> ReconAction:
    def pick(ids: list[str], p: float) -> tuple[str, ...]:
        out = tuple(x for x in ids if rng.random() < p)
        if rng.random() < 0.03 and ids:
            out = (*out, out[0] if out else ids[0])  # a duplicate (refused)
        return out

    s_ids, l_ids = conf.s_ids, conf.l_ids
    k = rng.random()
    if k < 0.35:
        return Match(
            statement_line_ids=pick(s_ids, 0.1) or (rng.choice(s_ids),),
            ledger_line_ids=pick(l_ids, 0.1) or (rng.choice(l_ids),),
        )
    if k < 0.45:
        n = conf.engine._st.n_match
        return Unmatch(match_id=f"M-{rng.randint(1, max(1, n + 1))}")
    if k < 0.75:
        amounts = [conf.engine.lines[x].amount_minor for x in s_ids] + [0, 1, -1]
        invoices = [None, None, *conf.engine.invoices, "INV-NOPE"]
        return Adjust(
            adjust_kind=rng.choice(ADJUST_KINDS),  # type: ignore[arg-type]
            amount_minor=rng.choice(amounts),
            statement_line_ids=pick(s_ids, 0.08),
            ledger_line_ids=pick(l_ids, 0.06),
            invoice_id=rng.choice(invoices),
        )
    if k < 0.87:
        return MarkTiming(ledger_line_ids=pick(l_ids, 0.08) or (rng.choice(l_ids),))
    return Escalate(statement_line_ids=pick(s_ids, 0.05), ledger_line_ids=pick(l_ids, 0.05))


def run_recon(
    case: ReconCase, seed: int, steps: int = 25, *, invariants: bool = True
) -> ReconTrace:
    rng = random.Random(f"formal-recon:{seed}:{case.case_id}")
    conf = ReconConformance(case, steps + 1, invariants=invariants)
    for _ in range(steps):
        conf.act(random_action(rng, conf))
    return conf.trace
