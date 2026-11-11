"""One model, two backends: concrete Python values, or z3 terms.

A model in `scruple.formal` is written once, as guarded commands over a state dict,
using only arithmetic, comparisons and the operations of an `Ops` object. With
`PyOps` the same code runs on ints and bools (the conformance tests step it next to
the real engine); with `scruple.formal.bmc.Z3Ops` it builds z3 terms (bounded model
checking with symbolic amounts and days). So the model that is checked is the model
that is tested against the code: there is no second copy to drift.

Hooks for what the abstraction leaves open:

- `deadline(inp, stage, start, declined)`: the merchant's last day to act. Python:
  the network rules (`deadline_fn`). z3: any day after `start` (a fresh input), which
  over-approximates every network's windows and calendar.
- `post(amount, day)`: a transaction-currency amount in the settlement currency on a
  day. Python: the integer FX model (`scruple.formal.fx`). z3: the identity (one
  currency; FX is checked on its own).
- `require(cond)`: a side condition of the step being built (z3: asserted under the
  command's guard; Python: recorded, and a conformance failure if false).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

State = dict[str, Any]
Inputs = dict[str, Any]
Params = dict[str, Any]

# a day after every real one: "no due date" (date(9999, 12, 31).toordinal() < INF)
INF = 10**7


class PyOps:
    """Concrete backend: plain Python ints and bools."""

    def __init__(
        self,
        deadline_fn: Callable[[int, int, bool], int] | None = None,
        post_fn: Callable[[int, int], int] | None = None,
    ) -> None:
        self._deadline = deadline_fn
        self._post = post_fn
        self.broken: list[str] = []

    def ite(self, c: Any, a: Any, b: Any) -> Any:
        return a if c else b

    def and_(self, *xs: Any) -> Any:
        return all(xs)

    def or_(self, *xs: Any) -> Any:
        return any(xs)

    def not_(self, x: Any) -> Any:
        return not x

    def require(self, c: Any, why: str = "") -> None:
        if not c:
            self.broken.append(why or "side condition")

    def deadline(self, inp: Inputs, stage: Any, start: Any, declined: Any) -> Any:
        if self._deadline is None:
            return inp["dl"]
        return self._deadline(int(stage), int(start), bool(declined))

    def post(self, amount: Any, day: Any) -> Any:
        return amount if self._post is None else self._post(int(amount), int(day))


def imax(ops: Any, a: Any, b: Any) -> Any:
    return ops.ite(a >= b, a, b)


def b2i(ops: Any, c: Any) -> Any:
    return ops.ite(c, 1, 0)


def merge(ops: Any, c: Any, a: State, b: State) -> State:
    """Field by field: `a` where `c` holds, else `b`."""
    return {k: ops.ite(c, a[k], b[k]) for k in b}


Guard = Callable[[State, Params, Inputs, Any], Any]
Update = Callable[[State, Params, Inputs, Any], State]
StateInv = Callable[[State, Params, Any], Any]
# (before, after, params, inputs, ops, took): `took(name)` is true when that command fired
StepInv = Callable[[State, State, Params, Inputs, Any, Callable[[str], Any]], Any]


@dataclass(frozen=True)
class Command:
    name: str
    guard: Guard
    update: Update
    agent: bool = False  # an agent action (as opposed to the environment moving)


@dataclass(frozen=True)
class Invariant:
    name: str
    text: str
    state: StateInv | None = None
    step: StepInv | None = None


@dataclass(frozen=True)
class Model:
    """A guarded-command model: typed params, state and per-step inputs."""

    name: str
    params: dict[str, type]
    state: dict[str, type]
    inputs: dict[str, type]
    param_constraints: Callable[[Params, Any], list[Any]]
    input_constraints: Callable[[Inputs, Params, Any], list[Any]]
    init: Callable[[Params, Inputs, Any], State]
    commands: tuple[Command, ...]
    invariants: tuple[Invariant, ...] = field(default=())

    def command(self, name: str) -> Command:
        for c in self.commands:
            if c.name == name:
                return c
        raise KeyError(name)

    def step(self, s: State, p: Params, name: str, inp: Inputs, ops: PyOps) -> State:
        """Fire one command concretely. Raises `ModelError` if it is not enabled."""
        cmd = self.command(name)
        if not all(self.input_constraints(inp, p, ops)):
            raise ModelError(f"{name}: inputs {inp} out of range")
        if not cmd.guard(s, p, inp, ops):
            raise ModelError(f"{name} is not enabled in {s}")
        before = len(ops.broken)
        out = cmd.update(s, p, inp, ops)
        if len(ops.broken) > before:
            raise ModelError(f"{name}: {ops.broken[before:]}")
        return out

    def check(self, s: State, p: Params, ops: PyOps) -> list[str]:
        """Names of the state invariants `s` breaks."""
        return [i.name for i in self.invariants if i.state is not None and not i.state(s, p, ops)]

    def check_step(
        self, s: State, s2: State, p: Params, name: str, inp: Inputs, ops: PyOps
    ) -> list[str]:
        def took(n: str) -> bool:
            return n == name

        return [
            i.name
            for i in self.invariants
            if i.step is not None and not i.step(s, s2, p, inp, ops, took)
        ]


class ModelError(AssertionError):
    """The engine did something the model does not allow (a conformance failure)."""
