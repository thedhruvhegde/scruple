"""Abstract model of one bank reconciliation: matches, adjustments, timing, escalations.

It restates `scruple.sim.recon.engine.ReconEngine` as guarded commands over `n`
statement lines and `m` cash-book lines with symbolic signed amounts (`s0..`,
`l0..`), a symbolic opening balance and the statement's closing balance. Match groups
and adjustments live in slots (`G` and `K` of them, in creation order, as the
engine numbers them); `recon_model(n, m, G, K)` builds a model of any size. The z3
checks use a small one (see `CHECK_SIZE`); the conformance test builds one per case,
as large as the case.

State: per match slot `mon_g` (active) and its lines `ms_g_i`, `ml_g_j`; per
adjustment slot `aon_k`, `amt_k` and the lines it cites `as_k_i`, `al_k_j`; per line
`es_i` / `el_j` (escalated) and `tm_j` (marked as timing); counters `used` (match
ids issued) and `nadj`; and the ledger's `cash` and `off` (every other account).

The engine does not judge adjustments (that is the verifier's REC-EVID check), so
"no money invented" is proved in the form the engine can promise: whenever the
reconciliation closes with every line handled once and every match balanced, the
adjustments add up to exactly the statement lines they cite less the book lines they
correct. `WITNESSES` are the cases the engine allows by design (an adjustment that
cites no statement line, or a line already matched), each shown reachable.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from scruple.formal.ops import Command, Inputs, Invariant, Model, Params, State

CHECK_SIZE = (2, 2, 2, 2)  # statement lines, book lines, match slots, adjustment slots


def _sum(ops: Any, terms: list[tuple[Any, Any]]) -> Any:
    """Sum of `amount` over the terms whose condition holds (linear for the solver)."""
    out: Any = 0
    for cond, amount in terms:
        out = out + ops.ite(cond, amount, 0)
    return out


def _nonzero(ops: Any, conds: list[Any]) -> Any:
    return ops.or_(*conds) if conds else False


@cache
def recon_model(n: int, m: int, g: int, k: int) -> Model:
    ix_s, ix_l, ix_g, ix_k = range(n), range(m), range(g), range(k)
    params: dict[str, type] = {"o": int, "close": int}
    params |= {f"s{i}": int for i in ix_s} | {f"l{j}": int for j in ix_l}
    state: dict[str, type] = {"used": int, "nadj": int, "cash": int, "off": int}
    for x in ix_g:
        state |= {f"mon_{x}": bool}
        state |= {f"ms_{x}_{i}": bool for i in ix_s} | {f"ml_{x}_{j}": bool for j in ix_l}
    for y in ix_k:
        state |= {f"aon_{y}": bool, f"amt_{y}": int}
        state |= {f"as_{y}_{i}": bool for i in ix_s} | {f"al_{y}_{j}": bool for j in ix_l}
    state |= {f"es_{i}": bool for i in ix_s} | {f"el_{j}": bool for j in ix_l}
    state |= {f"tm_{j}": bool for j in ix_l}
    inputs: dict[str, type] = {"amt": int}
    inputs |= {f"ss{i}": bool for i in ix_s} | {f"sl{j}": bool for j in ix_l}

    def param_constraints(p: Params, ops: Any) -> list[Any]:
        # the statement file's closing balance is its opening plus its lines
        return [p["close"] == p["o"] + _sum(ops, [(True, p[f"s{i}"]) for i in ix_s])]

    def input_constraints(i: Inputs, p: Params, ops: Any) -> list[Any]:
        return []

    def init(p: Params, i: Inputs, ops: Any) -> State:
        s: State = {key: (False if t is bool else 0) for key, t in state.items()}
        book = p["o"] + _sum(ops, [(True, p[f"l{j}"]) for j in ix_l])
        s.update(cash=book, off=-book)  # opening + every book line, against their offsets
        return s

    def matched_s(s: State, ops: Any, i: int) -> Any:
        return ops.or_(*(ops.and_(s[f"mon_{x}"], s[f"ms_{x}_{i}"]) for x in ix_g)) if g else False

    def matched_l(s: State, ops: Any, j: int) -> Any:
        return ops.or_(*(ops.and_(s[f"mon_{x}"], s[f"ml_{x}_{j}"]) for x in ix_g)) if g else False

    def free_s(s: State, ops: Any, inp: Inputs) -> Any:
        """No selected statement line is matched or escalated."""
        return ops.and_(
            True,
            *(
                ops.not_(ops.and_(inp[f"ss{i}"], ops.or_(matched_s(s, ops, i), s[f"es_{i}"])))
                for i in ix_s
            ),
        )

    def free_l(s: State, ops: Any, inp: Inputs) -> Any:
        """No selected book line is matched, escalated or marked as timing."""
        taken = [ops.or_(matched_l(s, ops, j), s[f"el_{j}"], s[f"tm_{j}"]) for j in ix_l]
        return ops.and_(True, *(ops.not_(ops.and_(inp[f"sl{j}"], taken[j])) for j in ix_l))

    def any_s(inp: Inputs, ops: Any) -> Any:
        return _nonzero(ops, [inp[f"ss{i}"] for i in ix_s])

    def any_l(inp: Inputs, ops: Any) -> Any:
        return _nonzero(ops, [inp[f"sl{j}"] for j in ix_l])

    cmds: list[Command] = []
    for x in ix_g:

        def g_match(s: State, p: Params, i: Inputs, ops: Any, x: int = x) -> Any:
            return ops.and_(
                s["used"] == x, any_s(i, ops), any_l(i, ops), free_s(s, ops, i), free_l(s, ops, i)
            )

        def u_match(s: State, p: Params, i: Inputs, ops: Any, x: int = x) -> State:
            out = {**s, "used": s["used"] + 1, f"mon_{x}": True}
            out |= {f"ms_{x}_{q}": i[f"ss{q}"] for q in ix_s}
            out |= {f"ml_{x}_{q}": i[f"sl{q}"] for q in ix_l}
            return out

        def g_unmatch(s: State, p: Params, i: Inputs, ops: Any, x: int = x) -> Any:
            return s[f"mon_{x}"]

        def u_unmatch(s: State, p: Params, i: Inputs, ops: Any, x: int = x) -> State:
            return {**s, f"mon_{x}": False}

        cmds += [
            Command(f"MATCH_{x}", g_match, u_match, agent=True),
            Command(f"UNMATCH_{x}", g_unmatch, u_unmatch, agent=True),
        ]
    for y in ix_k:

        def g_adjust(s: State, p: Params, i: Inputs, ops: Any, y: int = y) -> Any:
            no_esc = [ops.not_(ops.and_(i[f"ss{q}"], s[f"es_{q}"])) for q in ix_s]
            return ops.and_(s["nadj"] == y, i["amt"] != 0, *no_esc)

        def u_adjust(s: State, p: Params, i: Inputs, ops: Any, y: int = y) -> State:
            out = {**s, "nadj": s["nadj"] + 1, f"aon_{y}": True, f"amt_{y}": i["amt"]}
            out |= {f"as_{y}_{q}": i[f"ss{q}"] for q in ix_s}
            out |= {f"al_{y}_{q}": i[f"sl{q}"] for q in ix_l}
            out |= {"cash": s["cash"] + i["amt"], "off": s["off"] - i["amt"]}
            return out

        cmds.append(Command(f"ADJUST_{y}", g_adjust, u_adjust, agent=True))

    def g_timing(s: State, p: Params, i: Inputs, ops: Any) -> Any:
        return ops.and_(any_l(i, ops), free_l(s, ops, i))

    def u_timing(s: State, p: Params, i: Inputs, ops: Any) -> State:
        return {**s, **{f"tm_{q}": ops.or_(s[f"tm_{q}"], i[f"sl{q}"]) for q in ix_l}}

    def g_escalate(s: State, p: Params, i: Inputs, ops: Any) -> Any:
        return ops.and_(ops.or_(any_s(i, ops), any_l(i, ops)), free_s(s, ops, i), free_l(s, ops, i))

    def u_escalate(s: State, p: Params, i: Inputs, ops: Any) -> State:
        out = {**s, **{f"es_{q}": ops.or_(s[f"es_{q}"], i[f"ss{q}"]) for q in ix_s}}
        return out | {f"el_{q}": ops.or_(s[f"el_{q}"], i[f"sl{q}"]) for q in ix_l}

    cmds += [
        Command("MARK_TIMING", g_timing, u_timing, agent=True),
        Command("ESCALATE", g_escalate, u_escalate, agent=True),
    ]

    # --- derived quantities -----------------------------------------------------------
    def adj_total(s: State, ops: Any) -> Any:
        return _sum(ops, [(s[f"aon_{y}"], s[f"amt_{y}"]) for y in ix_k])

    def difference(s: State, p: Params, ops: Any) -> Any:
        """`ReconEngine.difference`: bank closing minus the adjusted book."""
        adjusted = (
            s["cash"]
            - _sum(ops, [(s[f"tm_{j}"], p[f"l{j}"]) for j in ix_l])
            - _sum(ops, [(s[f"el_{j}"], p[f"l{j}"]) for j in ix_l])
            + _sum(ops, [(s[f"es_{i}"], p[f"s{i}"]) for i in ix_s])
        )
        return p["close"] - adjusted

    def cited_s(s: State, ops: Any, i: int) -> Any:
        return ops.or_(*(ops.and_(s[f"aon_{y}"], s[f"as_{y}_{i}"]) for y in ix_k)) if k else False

    def cited_l(s: State, ops: Any, j: int) -> Any:
        return ops.or_(*(ops.and_(s[f"aon_{y}"], s[f"al_{y}_{j}"]) for y in ix_k)) if k else False

    def exactly_one(ops: Any, conds: list[Any]) -> Any:
        out = []
        for a in range(len(conds)):
            others = [ops.not_(conds[b]) for b in range(len(conds)) if b != a]
            out.append(ops.or_(ops.not_(conds[a]), ops.and_(True, *others)))
        return ops.and_(ops.or_(*conds), *out)

    def handled_once(s: State, p: Params, ops: Any) -> Any:
        """Every line handled exactly once; no line cited by two adjustments or in two
        match groups; every match group balances (statement = book)."""
        out = []
        for i in ix_s:
            in_groups = [ops.and_(s[f"mon_{x}"], s[f"ms_{x}_{i}"]) for x in ix_g]
            cites = [ops.and_(s[f"aon_{y}"], s[f"as_{y}_{i}"]) for y in ix_k]
            out.append(exactly_one(ops, [*in_groups, s[f"es_{i}"], *cites]))
        for j in ix_l:
            in_groups = [ops.and_(s[f"mon_{x}"], s[f"ml_{x}_{j}"]) for x in ix_g]
            cites = [ops.and_(s[f"aon_{y}"], s[f"al_{y}_{j}"]) for y in ix_k]
            out.append(exactly_one(ops, [*in_groups, s[f"el_{j}"], s[f"tm_{j}"], *cites]))
        for x in ix_g:
            ss = _sum(ops, [(s[f"ms_{x}_{i}"], p[f"s{i}"]) for i in ix_s])
            ll = _sum(ops, [(s[f"ml_{x}_{j}"], p[f"l{j}"]) for j in ix_l])
            out.append(ops.or_(ops.not_(s[f"mon_{x}"]), ss == ll))
        return ops.and_(*out)

    # --- invariants -------------------------------------------------------------------
    def inv_balance(s: State, p: Params, ops: Any) -> Any:
        return s["cash"] + s["off"] == 0

    def inv_cash(s: State, p: Params, ops: Any) -> Any:
        book = p["o"] + _sum(ops, [(True, p[f"l{j}"]) for j in ix_l])
        return s["cash"] == book + adj_total(s, ops)

    def inv_tie_out(s: State, p: Params, ops: Any) -> Any:
        open_s = _sum(ops, [(ops.not_(s[f"es_{i}"]), p[f"s{i}"]) for i in ix_s])
        open_l = _sum(
            ops,
            [(ops.and_(ops.not_(s[f"tm_{j}"]), ops.not_(s[f"el_{j}"])), p[f"l{j}"]) for j in ix_l],
        )
        return difference(s, p, ops) == open_s - open_l - adj_total(s, ops)

    def inv_exclusive(s: State, p: Params, ops: Any) -> Any:
        out = []
        for i in ix_s:
            groups = [ops.and_(s[f"mon_{x}"], s[f"ms_{x}_{i}"]) for x in ix_g]
            out.append(ops.not_(ops.and_(matched_s(s, ops, i), s[f"es_{i}"])))
            out += [ops.not_(ops.and_(groups[a], groups[b])) for a in ix_g for b in ix_g if a < b]
        for j in ix_l:
            groups = [ops.and_(s[f"mon_{x}"], s[f"ml_{x}_{j}"]) for x in ix_g]
            ml = matched_l(s, ops, j)
            out += [
                ops.not_(ops.and_(ml, s[f"el_{j}"])),
                ops.not_(ops.and_(ml, s[f"tm_{j}"])),
                ops.not_(ops.and_(s[f"el_{j}"], s[f"tm_{j}"])),
            ]
            out += [ops.not_(ops.and_(groups[a], groups[b])) for a in ix_g for b in ix_g if a < b]
        out += [s["used"] >= 0, s["used"] <= g, s["nadj"] >= 0, s["nadj"] <= k]
        out += [s[f"aon_{y}"] == (s["nadj"] > y) for y in ix_k]
        out += [ops.or_(ops.not_(s[f"aon_{y}"]), s[f"amt_{y}"] != 0) for y in ix_k]
        out += [ops.or_(ops.not_(s[f"mon_{x}"]), s["used"] > x) for x in ix_g]
        return ops.and_(*out)

    def step_append_only(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
        out = []
        for y in ix_k:
            keep = [s2[f"amt_{y}"] == s[f"amt_{y}"]]
            keep += [s2[f"as_{y}_{q}"] == s[f"as_{y}_{q}"] for q in ix_s]
            keep += [s2[f"al_{y}_{q}"] == s[f"al_{y}_{q}"] for q in ix_l]
            out.append(ops.or_(ops.not_(s[f"aon_{y}"]), ops.and_(s2[f"aon_{y}"], *keep)))
        out += [ops.or_(ops.not_(s[f"es_{q}"]), s2[f"es_{q}"]) for q in ix_s]
        out += [ops.or_(ops.not_(s[f"el_{q}"]), s2[f"el_{q}"]) for q in ix_l]
        out += [ops.or_(ops.not_(s[f"tm_{q}"]), s2[f"tm_{q}"]) for q in ix_l]
        return ops.and_(*out)

    def inv_justified(s: State, p: Params, ops: Any) -> Any:
        """Closed (difference zero, every line handled once, matches balanced) implies
        the adjustments are exactly the cited statement lines less the cited book lines."""
        closed = ops.and_(difference(s, p, ops) == 0, handled_once(s, p, ops))
        evidence = _sum(ops, [(cited_s(s, ops, i), p[f"s{i}"]) for i in ix_s]) - _sum(
            ops, [(cited_l(s, ops, j), p[f"l{j}"]) for j in ix_l]
        )
        return ops.or_(ops.not_(closed), adj_total(s, ops) == evidence)

    invariants = (
        Invariant(
            "double_entry", "every entry balances; cash and the rest net to zero", inv_balance
        ),
        Invariant(
            "book_cash",
            "book cash = opening + every book line + every adjustment (nothing else posts)",
            inv_cash,
        ),
        Invariant(
            "tie_out",
            "difference = statement lines not escalated - book lines neither timing nor "
            "escalated - adjustments",
            inv_tie_out,
        ),
        Invariant(
            "handled_at_most_once",
            "no line both matched and escalated (or timing), none in two match groups; "
            "adjustment and match ids issued in order",
            inv_exclusive,
        ),
        Invariant(
            "append_only",
            "adjustments, escalations and timing marks are never undone or changed",
            None,
            step_append_only,
        ),
        Invariant(
            "no_money_invented",
            "when closed (difference zero, every line handled once, matches balanced) the "
            "adjustments equal the statement lines they cite less the book lines they correct",
            inv_justified,
        ),
    )

    def wit_uncited(s: State, p: Params, ops: Any) -> Any:
        """Never: a zero difference reached with an adjustment that cites no statement line."""
        bad = [
            ops.and_(s[f"aon_{y}"], ops.not_(_nonzero(ops, [s[f"as_{y}_{q}"] for q in ix_s])))
            for y in ix_k
        ]
        return ops.not_(ops.and_(difference(s, p, ops) == 0, ops.or_(False, *bad)))

    def wit_matched_and_cited(s: State, p: Params, ops: Any) -> Any:
        """Never: a statement line both matched and cited by an adjustment."""
        return ops.and_(
            True, *(ops.not_(ops.and_(matched_s(s, ops, i), cited_s(s, ops, i))) for i in ix_s)
        )

    witnesses = (
        Invariant("uncited_adjustment_closes", "allowed by design: REC-EVID", wit_uncited),
        Invariant(
            "matched_line_also_adjusted", "allowed by design: REC-EVID", wit_matched_and_cited
        ),
    )
    model = Model(
        name="recon",
        params=params,
        state=state,
        inputs=inputs,
        param_constraints=param_constraints,
        input_constraints=input_constraints,
        init=init,
        commands=tuple(cmds),
        invariants=invariants,
    )
    _WITNESSES[id(model)] = witnesses
    _DIFF[id(model)] = difference
    return model


_WITNESSES: dict[int, tuple[Invariant, ...]] = {}
_DIFF: dict[int, Any] = {}


def witnesses(model: Model) -> Model:
    """The same model with the by-design properties as its invariants (expected to fail)."""
    from dataclasses import replace

    return replace(model, name=f"{model.name}-witnesses", invariants=_WITNESSES[id(model)])


def difference(model: Model, s: State, p: Params, ops: Any) -> Any:
    return _DIFF[id(model)](s, p, ops)


MODEL = recon_model(*CHECK_SIZE)
