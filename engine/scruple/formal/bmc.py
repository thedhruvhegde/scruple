"""Bounded model checking with z3 for models written with `scruple.formal.ops`.

`check(model, depth)` unrolls the model's transition relation `depth` steps with
symbolic parameters (every amount, fee, day and window the parameter constraints
allow, not samples) and symbolic per-step inputs. At each depth j and for each
invariant separately it asks z3 for a path of length j that breaks the invariant (a
state invariant at state j, a step invariant on step j). `unsat` at every j <= depth
means: no execution of at most `depth` steps breaks it, under the abstraction. `sat`
gives a concrete counterexample trace (parameters, commands, inputs, states).

Cover checks guard against a vacuous model: each command must be reachable within the
depth, or a proof could hold only because nothing ever happens.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import z3

from scruple.formal.ops import Inputs, Model, Params, PyOps, State


class Z3Ops:
    """Symbolic backend: z3 terms; `require` collects side conditions of a step."""

    def __init__(self) -> None:
        self.pending: list[Any] = []

    def ite(self, c: Any, a: Any, b: Any) -> Any:
        if isinstance(c, bool):
            return a if c else b
        return z3.If(c, a, b)

    def and_(self, *xs: Any) -> Any:
        return z3.And(*xs)

    def or_(self, *xs: Any) -> Any:
        return z3.Or(*xs)

    def not_(self, x: Any) -> Any:
        return z3.Not(x)

    def require(self, c: Any, why: str = "") -> None:
        self.pending.append(c)

    def deadline(self, inp: Inputs, stage: Any, start: Any, declined: Any) -> Any:
        self.require(inp["dl"] > start, "a deadline is after its start")
        return inp["dl"]

    def post(self, amount: Any, day: Any) -> Any:
        return amount

    def take(self) -> list[Any]:
        out, self.pending = self.pending, []
        return out


def _var(name: str, t: type) -> Any:
    return z3.Bool(name) if t is bool else z3.Int(name)


def _norm(t: type, v: Any) -> Any:
    """z3 cannot mix Python bools into Int-typed ites; coerce constants."""
    if t is bool and isinstance(v, bool):
        return z3.BoolVal(v)
    if t is int and isinstance(v, int) and not isinstance(v, bool):
        return z3.IntVal(v)
    return v


@dataclass
class Counterexample:
    params: dict[str, Any]
    steps: list[tuple[str, dict[str, Any]]]  # (command, inputs)
    states: list[dict[str, Any]]
    init_inputs: dict[str, Any] = field(default_factory=dict)

    def replay(self, model: Model) -> tuple[list[State], list[str]]:
        """Run the trace on the concrete (Python) model: its states, and the
        invariants the last state or step breaks. A real counterexample replays."""
        ops = PyOps()
        s = model.init(self.params, self.init_inputs, ops)
        states, broken = [s], model.check(s, self.params, ops)
        for name, inp in self.steps:
            s2 = model.step(s, self.params, name, inp, ops)
            broken = model.check(s2, self.params, ops)
            broken += model.check_step(s, s2, self.params, name, inp, ops)
            states.append(s2)
            s = s2
        return states, broken

    def text(self) -> str:
        lines = [f"params: {self.params}", f"state 0: {self.states[0]}"]
        for j, (cmd, inp) in enumerate(self.steps):
            lines.append(f"step {j + 1}: {cmd} {inp}")
            lines.append(f"state {j + 1}: {self.states[j + 1]}")
        return "\n".join(lines)


@dataclass
class Result:
    invariant: str
    text: str
    depth: int  # every depth up to this one was checked
    holds: bool
    seconds: float
    counterexample: Counterexample | None = None


@dataclass
class Report:
    model: str
    depth: int
    results: list[Result]
    covered: dict[str, int | None]  # command -> first depth it fires at (None: never)
    seconds: float
    unroll_seconds: float
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.holds for r in self.results)


class Unrolling:
    """The model's states s_0..s_k, with every step's constraints in one solver."""

    def __init__(self, model: Model, extra: Callable[[Params], list[Any]] | None = None) -> None:
        self.model = model
        self.ops = Z3Ops()
        self.solver = z3.Solver()
        self.p: Params = {k: _var(f"p_{k}", t) for k, t in model.params.items()}
        self.solver.add(*model.param_constraints(self.p, self.ops))
        if extra is not None:
            self.solver.add(*extra(self.p))
        i0 = self._inputs(0)
        s0 = model.init(self.p, i0, self.ops)
        self.solver.add(*self.ops.take())
        self.states: list[State] = [self._fresh_state(0, s0)]
        self.cmds: list[Any] = []
        self.inputs: list[Inputs] = [i0]

    def _inputs(self, j: int) -> Inputs:
        return {k: _var(f"i{j}_{k}", t) for k, t in self.model.inputs.items()}

    def _fresh_state(self, j: int, values: State) -> State:
        """Name state j's fields so models and counterexamples stay readable."""
        out: State = {}
        for k, t in self.model.state.items():
            v = _var(f"s{j}_{k}", t)
            self.solver.add(v == _norm(t, values[k]))
            out[k] = v
        return out

    def extend(self) -> None:
        j = len(self.states) - 1
        s, inp = self.states[j], self._inputs(j + 1)
        cmd = z3.Int(f"c{j}")
        n = len(self.model.commands)
        self.solver.add(cmd >= 0, cmd < n)
        self.solver.add(*self.model.input_constraints(inp, self.p, self.ops))
        nxt: State = {}
        for idx, c in enumerate(self.model.commands):
            g = c.guard(s, self.p, inp, self.ops)
            u = c.update(s, self.p, inp, self.ops)
            side = self.ops.take()
            self.solver.add(z3.Implies(cmd == idx, z3.And(g, *side)))
            for k, t in self.model.state.items():
                v = _norm(t, u[k])
                nxt[k] = v if idx == 0 else z3.If(cmd == idx, v, nxt[k])
        self.states.append(self._fresh_state(j + 1, nxt))
        self.cmds.append(cmd)
        self.inputs.append(inp)

    def took(self, j: int) -> Callable[[str], Any]:
        names = [c.name for c in self.model.commands]

        def f(name: str) -> Any:
            return self.cmds[j] == names.index(name)

        return f

    def counterexample(self, upto: int) -> Counterexample:
        mdl = self.solver.model()

        def val(x: Any) -> Any:
            v = mdl.eval(x, model_completion=True)
            if z3.is_bool(v):
                return z3.is_true(v)
            return v.as_long()

        names = [c.name for c in self.model.commands]
        return Counterexample(
            params={k: val(v) for k, v in self.p.items()},
            steps=[
                (names[val(self.cmds[j])], {k: val(v) for k, v in self.inputs[j + 1].items()})
                for j in range(upto)
            ],
            states=[{k: val(v) for k, v in self.states[j].items()} for j in range(upto + 1)],
            init_inputs={k: val(v) for k, v in self.inputs[0].items()},
        )


def check(
    model: Model,
    depth: int,
    *,
    only: tuple[str, ...] | None = None,
    extra: Callable[[Params], list[Any]] | None = None,
    timeout_ms: int = 600_000,
) -> Report:
    """Check every invariant of `model` on all paths of at most `depth` steps.

    A state invariant proved at depth j (no path of j steps breaks it) is then asserted
    on state j: it holds on every reachable state there, so this only prunes paths that
    cannot happen, and later depths stay cheap."""
    t0 = time.perf_counter()
    u = Unrolling(model, extra)
    u.solver.set("timeout", timeout_ms)
    invs = [i for i in model.invariants if only is None or i.name in only]
    lemmas = [i for i in model.invariants if i.state is not None]
    spent = dict.fromkeys((i.name for i in model.invariants), 0.0)
    found: dict[str, Result] = {}
    covered: dict[str, int | None] = dict.fromkeys(c.name for c in model.commands)
    unroll = 0.0

    def fails(name: str, bad: Any, j: int) -> bool:
        t = time.perf_counter()
        u.solver.push()
        u.solver.add(bad)
        r = u.solver.check()
        if r == z3.sat and name not in found:
            inv = next(i for i in model.invariants if i.name == name)
            found[name] = Result(name, inv.text, j, False, 0.0, u.counterexample(j))
        u.solver.pop()
        spent[name] += time.perf_counter() - t
        if r == z3.unknown:
            raise RuntimeError(f"z3 gave up on {name} at depth {j}")
        return bool(r == z3.sat)

    for j in range(depth + 1):
        if j > 0:
            t = time.perf_counter()
            u.extend()
            unroll += time.perf_counter() - t
            took = u.took(j - 1)
            for c in model.commands:
                if covered[c.name] is None:
                    u.solver.push()
                    u.solver.add(took(c.name))
                    if u.solver.check() == z3.sat:
                        covered[c.name] = j
                    u.solver.pop()
            for inv in invs:
                if inv.step is not None and inv.name not in found:
                    ok = inv.step(u.states[j - 1], u.states[j], u.p, u.inputs[j], u.ops, took)
                    fails(inv.name, z3.Not(ok), j)
        held = []
        for inv in lemmas:
            assert inv.state is not None
            if inv.name in found:
                continue
            ok = inv.state(u.states[j], u.p, u.ops)
            if not fails(inv.name, z3.Not(ok), j):
                held.append(ok)
        u.solver.add(*held)
    results = [found.get(i.name, Result(i.name, i.text, depth, True, 0.0)) for i in invs]
    for r in results:
        r.seconds = round(spent[r.invariant], 3)
    return Report(
        model=model.name,
        depth=depth,
        results=results,
        covered=covered,
        seconds=round(time.perf_counter() - t0, 3),
        unroll_seconds=round(unroll, 3),
    )


@dataclass
class Induction:
    """Houdini: the largest subset of the invariants that is jointly inductive."""

    inductive: tuple[str, ...]  # hold in every reachable state, at any depth
    dropped: tuple[str, ...]  # not preserved by one step from the others (bounded only)
    seconds: float


def induction(
    model: Model,
    *,
    extra: Callable[[Params], list[Any]] | None = None,
    candidates: tuple[str, ...] | None = None,
) -> Induction:
    """One step from any state satisfying the candidate invariants keeps them all.

    Starts from the invariants that hold initially (checked here too) and drops any
    that one step from a state satisfying the rest can break, until nothing changes.
    What is left holds on every reachable state: an unbounded proof under the
    abstraction. Step invariants are kept if they hold on every step from such a
    state."""
    t0 = time.perf_counter()
    ops = Z3Ops()
    base = z3.Solver()
    p: Params = {k: _var(f"p_{k}", t) for k, t in model.params.items()}
    base.add(*model.param_constraints(p, ops))
    if extra is not None:
        base.add(*extra(p))
    s: State = {k: _var(f"a_{k}", t) for k, t in model.state.items()}
    inp: Inputs = {k: _var(f"i_{k}", t) for k, t in model.inputs.items()}
    i0: Inputs = {k: _var(f"i0_{k}", t) for k, t in model.inputs.items()}
    first = model.init(p, i0, ops)
    s0 = {k: _norm(t, first[k]) for k, t in model.state.items()}
    init_side = ops.take()
    base.add(*model.input_constraints(inp, p, ops))
    cmd = z3.Int("c")
    base.add(cmd >= 0, cmd < len(model.commands))
    nxt: State = {}
    for idx, c in enumerate(model.commands):
        g = c.guard(s, p, inp, ops)
        upd = c.update(s, p, inp, ops)
        base.add(z3.Implies(cmd == idx, z3.And(g, *ops.take())))
        for k, t in model.state.items():
            v = _norm(t, upd[k])
            nxt[k] = v if idx == 0 else z3.If(cmd == idx, v, nxt[k])
    names = [c.name for c in model.commands]

    def took(n: str) -> Any:
        return cmd == names.index(n)

    pool = [i for i in model.invariants if candidates is None or i.name in candidates]
    alive = []
    for inv in pool:
        if inv.state is None:
            alive.append(inv)
            continue
        z = z3.Solver()
        z.add(*model.param_constraints(p, ops), *init_side, z3.Not(inv.state(s0, p, ops)))
        if extra is not None:
            z.add(*extra(p))
        if z.check() == z3.unsat:
            alive.append(inv)
    changed = True
    while changed:
        changed = False
        assume = [i.state(s, p, ops) for i in alive if i.state is not None]
        for inv in list(alive):
            goals = []
            if inv.state is not None:
                goals.append(inv.state(nxt, p, ops))
            if inv.step is not None:
                goals.append(inv.step(s, nxt, p, inp, ops, took))
            base.push()
            base.add(*assume, z3.Not(z3.And(*goals)))
            r = base.check()
            base.pop()
            if r != z3.unsat:
                alive.remove(inv)
                changed = True
                break
    kept = tuple(i.name for i in alive)
    return Induction(
        inductive=kept,
        dropped=tuple(i.name for i in model.invariants if i.name not in kept),
        seconds=round(time.perf_counter() - t0, 3),
    )
