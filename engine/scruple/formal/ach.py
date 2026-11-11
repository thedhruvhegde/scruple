"""Abstract model of one ACH return case: returns, dishonors, contests, retries, ledger.

It restates `scruple.sim.ach.engine.AchEngine` as guarded commands (`scruple.formal.
ops`). The invoice amount `a` and the Unauthorized Entry Fee `fee` are symbolic.
Returns live in five slots: the case's (up to two) scheduled returns `O0`, `O1`, and
the returns of retries 1 to 3 (`R1`..`R3`; the bound on retries is part of the
abstraction, see docs/FORMAL.md). Time is abstract: the environment may settle
anything that is pending in any order (a superset of the engine's date order, which
the conformance test checks), except that a case's second return settles after its
first. What depends on days enters as data: whether each scheduled return was timely
(a parameter), and whether a dishonor settled within its window (an input at the
contest). Whether a retry finds the receiver's funds is an input too.

Per slot k: `st_k` (0 none, 1 scheduled, 2 settled), `d_k` (dishonor: 0 none,
1 submitted, 2 settled, 3 final, 4 contested), the return code's class `nsf_k` (R01 or
R09) and `uf_k` (carries the Unauthorized Entry Fee), `tim_k` (timely), `dup_k` (a
duplicate), `r67_k` (dishonored as R67, else R68), `dt_k` (the dishonor was timely).
Case-wide: `retry` (a retry is in flight), `attempts`, `last_nsf` / `last_uf` (the
code class of the latest settled return, which a retry's return reuses), `viol`
(recorded rule violations), and the ledger: `rcv` (receivable), `cash`, `rev`
(revenue), `contra` (returns accepted), `bad` (bad debt), `fees`.
"""

from __future__ import annotations

from typing import Any

from scruple.formal.ops import Command, Inputs, Invariant, Model, Params, State, b2i

SLOTS = ("O0", "O1", "R1", "R2", "R3")
N = len(SLOTS)
RETRY_SLOTS = (2, 3, 4)  # slot of retry n is n + 1
MAX_RETRIES = 3
NONE, SCHEDULED, SETTLED = 0, 1, 2
D_NONE, D_SUBMITTED, D_SETTLED, D_FINAL, D_CONTESTED = range(5)

PARAMS: dict[str, type] = {
    "a": int,
    "fee": int,
    "two": bool,  # the case has a second (duplicate) return
    **{f"{f}{k}": bool for k in (0, 1) for f in ("nsf", "uf", "tim", "dup")},
}
STATE: dict[str, type] = {
    **{f"{f}_{k}": int for k in range(N) for f in ("st", "d")},
    **{f"{f}_{k}": bool for k in range(N) for f in ("nsf", "uf", "tim", "dup", "r67", "dt")},
    "retry": bool,
    "attempts": int,
    "last_nsf": bool,
    "last_uf": bool,
    "viol": int,
    "improper": int,  # ghost: retries the rules forbid (count)
    "rcv": int,
    "cash": int,
    "rev": int,
    "contra": int,
    "bad": int,
    "fees": int,
}
# r67: dishonor code; dt: dishonor timely; funded: the retry clears; late, bad_desc:
# the retry settles after the 180-day window / without the RETRY PYMT description
INPUTS: dict[str, type] = {
    "r67": bool,
    "dt": bool,
    "funded": bool,
    "late": bool,
    "bad_desc": bool,
}


def param_constraints(p: Params, ops: Any) -> list[Any]:
    return [
        p["a"] >= 1,
        p["fee"] >= 0,
        ops.not_(p["dup0"]),  # the first return is never a duplicate of the second
        p["dup1"] == p["two"],  # the second is, by date order (AchCase validates it)
        # R01/R09 carry no Unauthorized Entry Fee (scruple.sim.ach.rules.CODES)
        ops.not_(ops.and_(p["nsf0"], p["uf0"])),
        ops.not_(ops.and_(p["nsf1"], p["uf1"])),
    ]


def input_constraints(i: Inputs, p: Params, ops: Any) -> list[Any]:
    return []


def _set(s: State, **kw: Any) -> State:
    return {**s, **kw}


def init(p: Params, i: Inputs, ops: Any) -> State:
    """Invoice, the original debit settles, the case's returns are scheduled."""
    a = p["a"]
    s: State = {k: (False if t is bool else 0) for k, t in STATE.items()}
    s.update(rcv=0, cash=a, rev=-a)  # invoice (rcv +a, rev -a) then debit (cash +a, rcv -a)
    for k in (0, 1):
        s[f"nsf_{k}"], s[f"uf_{k}"] = p[f"nsf{k}"], p[f"uf{k}"]
        s[f"tim_{k}"], s[f"dup_{k}"] = p[f"tim{k}"], p[f"dup{k}"]
    s["st_0"] = SCHEDULED
    s["st_1"] = ops.ite(p["two"], SCHEDULED, NONE)
    return s


def busy(s: State, ops: Any) -> Any:
    """`AchEngine.pending()`: a retry, a retry's return, or a dishonor unresolved."""
    return ops.or_(
        s["retry"],
        *(s[f"st_{k}"] == SCHEDULED for k in RETRY_SLOTS),
        *(ops.or_(s[f"d_{k}"] == D_SUBMITTED, s[f"d_{k}"] == D_SETTLED) for k in range(N)),
    )


def quiet(s: State, ops: Any) -> Any:
    """Nothing pending and nothing scheduled (`AchEngine.quiet`)."""
    return ops.and_(
        ops.not_(s["retry"]),
        *(s[f"st_{k}"] != SCHEDULED for k in range(N)),
        *(ops.or_(s[f"d_{k}"] == D_NONE, s[f"d_{k}"] >= D_FINAL) for k in range(N)),
    )


def resolved(s: State, ops: Any) -> Any:
    return ops.and_(quiet(s, ops), s["rcv"] == 0)


def any_settled(s: State, ops: Any) -> Any:
    return ops.or_(*(s[f"st_{k}"] == SETTLED for k in range(N)))


# --- commands -------------------------------------------------------------------------
def _ret_settle(k: int) -> Command:
    def g(s: State, p: Params, i: Inputs, ops: Any) -> Any:
        order = s["st_0"] == SETTLED if k == 1 else True  # date order of the case's returns
        return ops.and_(s[f"st_{k}"] == SCHEDULED, order)

    def u(s: State, p: Params, i: Inputs, ops: Any) -> State:
        a, f = p["a"], ops.ite(s[f"uf_{k}"], p["fee"], 0)
        return _set(
            s,
            **{f"st_{k}": SETTLED},
            cash=s["cash"] - a - f,
            rcv=s["rcv"] + a,
            fees=s["fees"] + f,
            last_nsf=s[f"nsf_{k}"],
            last_uf=s[f"uf_{k}"],
        )

    return Command(f"RETURN_{SLOTS[k]}", g, u)


def _dis_settle(k: int) -> Command:
    def g(s: State, p: Params, i: Inputs, ops: Any) -> Any:
        return s[f"d_{k}"] == D_SUBMITTED

    def u(s: State, p: Params, i: Inputs, ops: Any) -> State:
        a = p["a"]
        return _set(s, **{f"d_{k}": D_SETTLED}, cash=s["cash"] + a, rcv=s["rcv"] - a)

    return Command(f"DISHONOR_SETTLES_{SLOTS[k]}", g, u)


def improper_return(s: State, k: int, ops: Any) -> Any:
    """What a dishonor may claim: R67 against a duplicate, R68 against an untimely return."""
    return ops.or_(
        ops.and_(s[f"r67_{k}"], s[f"dup_{k}"]),
        ops.and_(ops.not_(s[f"r67_{k}"]), ops.not_(s[f"tim_{k}"])),
    )


def _contest(k: int, contested: bool) -> Command:
    def g(s: State, p: Params, i: Inputs, ops: Any) -> Any:
        stands = ops.and_(i["dt"], improper_return(s, k, ops))  # else R72, R75 or R73
        return ops.and_(s[f"d_{k}"] == D_SETTLED, ops.not_(stands) if contested else stands)

    def u(s: State, p: Params, i: Inputs, ops: Any) -> State:
        a = p["a"]
        if not contested:
            return _set(s, **{f"d_{k}": D_FINAL, f"dt_{k}": i["dt"]})
        return _set(
            s,
            **{f"d_{k}": D_CONTESTED, f"dt_{k}": i["dt"]},
            cash=s["cash"] - a,
            rcv=s["rcv"] + a,
        )

    return Command(f"DISHONOR_{'CONTESTED' if contested else 'FINAL'}_{SLOTS[k]}", g, u)


def _retry_settle(paid: bool) -> Command:
    def g(s: State, p: Params, i: Inputs, ops: Any) -> Any:
        clears = ops.and_(s["last_nsf"], i["funded"])
        return ops.and_(s["retry"], clears if paid else ops.not_(clears))

    def u(s: State, p: Params, i: Inputs, ops: Any) -> State:
        a = p["a"]
        s = _set(s, retry=False, cash=s["cash"] + a, rcv=s["rcv"] - a)
        if paid:
            return s
        out = dict(s)
        for n in range(1, MAX_RETRIES + 1):
            k = n + 1
            here = s["attempts"] == n
            out[f"st_{k}"] = ops.ite(here, SCHEDULED, s[f"st_{k}"])
            # R01 when the last return was R01/R09, else the same code (and fee)
            out[f"nsf_{k}"] = ops.ite(here, s["last_nsf"], s[f"nsf_{k}"])
            out[f"uf_{k}"] = ops.ite(
                here, ops.and_(ops.not_(s["last_nsf"]), s["last_uf"]), s[f"uf_{k}"]
            )
            out[f"tim_{k}"] = ops.ite(here, True, s[f"tim_{k}"])  # sent on its last day
            out[f"dup_{k}"] = ops.ite(here, False, s[f"dup_{k}"])
        return out

    return Command("RETRY_CLEARS" if paid else "RETRY_RETURNED", g, u)


def _close(kind: str) -> Command:
    def g(s: State, p: Params, i: Inputs, ops: Any) -> Any:
        return ops.and_(s["rcv"] > 0, ops.not_(busy(s, ops)))

    def u(s: State, p: Params, i: Inputs, ops: Any) -> State:
        acct = "contra" if kind == "ACCEPT" else "bad"
        return _set(s, **{acct: s[acct] + s["rcv"]}, rcv=0)

    return Command(kind, g, u, agent=True)


def _dishonor(k: int) -> Command:
    def g(s: State, p: Params, i: Inputs, ops: Any) -> Any:
        # the engine does not wait for a retry in flight here (see docs/FORMAL.md)
        return ops.and_(
            s[f"st_{k}"] == SETTLED,
            s[f"d_{k}"] == D_NONE,
            s["rcv"] >= p["a"],
        )

    def u(s: State, p: Params, i: Inputs, ops: Any) -> State:
        return _set(s, **{f"d_{k}": D_SUBMITTED, f"r67_{k}": i["r67"]})

    return Command(f"DISHONOR_{SLOTS[k]}", g, u, agent=True)


def _g_reinit(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(
        any_settled(s, ops),
        ops.not_(busy(s, ops)),
        s["rcv"] >= p["a"],
        s["attempts"] < MAX_RETRIES,
    )


def _u_reinit(s: State, p: Params, i: Inputs, ops: Any) -> State:
    n = s["attempts"] + 1
    bad = ops.or_(ops.not_(s["last_nsf"]), n > 2, i["late"])
    v = (
        b2i(ops, ops.not_(s["last_nsf"]))
        + b2i(ops, n > 2)
        + b2i(ops, i["late"])
        + b2i(ops, i["bad_desc"])
    )
    return _set(
        s,
        attempts=n,
        retry=True,
        viol=s["viol"] + v,
        improper=s["improper"] + b2i(ops, ops.or_(bad, i["bad_desc"])),
    )


COMMANDS = (
    *(_ret_settle(k) for k in range(N)),
    *(_dis_settle(k) for k in range(N)),
    *(_contest(k, c) for k in range(N) for c in (False, True)),
    _retry_settle(True),
    _retry_settle(False),
    _close("ACCEPT"),
    _close("WRITE_OFF"),
    *(_dishonor(k) for k in range(N)),
    Command("REINITIATE", _g_reinit, _u_reinit, agent=True),
)


# --- invariants -----------------------------------------------------------------------
def _total(s: State) -> Any:
    return s["rcv"] + s["cash"] + s["rev"] + s["contra"] + s["bad"] + s["fees"]


def collected(s: State) -> Any:
    """Cash kept from the receiver (principal only: cash plus the fees it paid)."""
    return s["cash"] + s["fees"]


def _inv_balance(s: State, p: Params, ops: Any) -> Any:
    return _total(s) == 0


def _step_balance(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
    return _total(s2) - _total(s) == 0


def _inv_allocation(s: State, p: Params, ops: Any) -> Any:
    return ops.and_(s["rev"] == -p["a"], collected(s) + s["bad"] + s["contra"] + s["rcv"] == p["a"])


def _inv_invented(s: State, p: Params, ops: Any) -> Any:
    """The originator never collects more than it invoiced."""
    return collected(s) <= p["a"]


def _inv_negative(s: State, p: Params, ops: Any) -> Any:
    """Never below zero, even counting what dishonors and retries in flight will collect."""
    return ops.and_(
        s["rcv"] - in_flight(s, p, ops) >= 0,
        s["rcv"] >= 0,
        s["contra"] >= 0,
        s["bad"] >= 0,
        s["fees"] >= 0,
    )


def _inv_dishonor(s: State, p: Params, ops: Any) -> Any:
    """A dishonor stands only against an improper return and only when timely; it is
    only ever of a settled return."""
    out = []
    for k in range(N):
        d = s[f"d_{k}"]
        out += [
            d >= D_NONE,
            d <= D_CONTESTED,
            ops.or_(d == D_NONE, s[f"st_{k}"] == SETTLED),
            ops.or_(d != D_FINAL, ops.and_(s[f"dt_{k}"], improper_return(s, k, ops))),
        ]
    return ops.and_(*out)


def _step_dishonor(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
    """Dishonor states only move forward; a contest moves exactly the dishonored amount
    back; a return slot is never scheduled or settled twice."""
    out = []
    for k in range(N):
        d, d2 = s[f"d_{k}"], s2[f"d_{k}"]
        out += [
            d2 >= d,
            s2[f"st_{k}"] >= s[f"st_{k}"],
            ops.or_(
                ops.not_(took(f"DISHONOR_CONTESTED_{SLOTS[k]}")), s2["rcv"] - s["rcv"] == p["a"]
            ),
            ops.or_(d != D_FINAL, d2 == D_FINAL),
            ops.or_(d != D_CONTESTED, d2 == D_CONTESTED),
        ]
    return ops.and_(*out)


def _inv_reinit(s: State, p: Params, ops: Any) -> Any:
    """Every retry the rules forbid is recorded as a violation; at most two are proper."""
    return ops.and_(
        s["viol"] >= s["improper"],
        s["attempts"] - s["improper"] <= 2,
        s["attempts"] >= 0,
        s["attempts"] <= MAX_RETRIES,
    )


def _step_terminal(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
    """A resolved case (nothing pending, nothing receivable) never moves again."""
    return ops.not_(resolved(s, ops))


def _times(ops: Any, conds: list[Any], a: Any) -> Any:
    """`a` times the number of true conditions, kept linear for the solver."""
    out: Any = 0
    for c in conds:
        out = out + ops.ite(c, a, 0)
    return out


def in_flight(s: State, p: Params, ops: Any) -> Any:
    """Receivable already on its way back: dishonors submitted and a retry in flight."""
    return _times(ops, [s[f"d_{k}"] == D_SUBMITTED for k in range(N)] + [s["retry"]], p["a"])


def _inv_well_formed(s: State, p: Params, ops: Any) -> Any:
    """The shape of a reachable state: which slots are in use, and the cash a count of
    settled returns, dishonors, contests and retries implies."""
    a = p["a"]
    returned = _times(ops, [s[f"st_{k}"] == SETTLED for k in range(N)], a)
    dishonored = _times(ops, [s[f"d_{k}"] >= D_SETTLED for k in range(N)], a)
    contested = _times(ops, [s[f"d_{k}"] == D_CONTESTED for k in range(N)], a)
    retried = _times(
        ops,
        [
            ops.and_(s["attempts"] >= n, ops.or_(s["attempts"] > n, ops.not_(s["retry"])))
            for n in range(1, MAX_RETRIES + 1)
        ],
        a,
    )
    out = [
        collected(s) == a - returned + dishonored - contested + retried,
        s["st_0"] >= SCHEDULED,
        (s["st_1"] >= SCHEDULED) == p["two"],
        s["fees"] >= 0,
        s["contra"] >= 0,
        s["bad"] >= 0,
        s["attempts"] >= 0,
        s["attempts"] <= MAX_RETRIES,
        s["improper"] >= 0,
        ops.or_(ops.not_(s["retry"]), s["attempts"] >= 1),
    ]
    for k in range(N):
        out += [s[f"st_{k}"] >= NONE, s[f"st_{k}"] <= SETTLED]
    for n in range(1, MAX_RETRIES + 1):
        k = n + 1
        used = s[f"st_{k}"] != NONE
        out += [
            ops.or_(
                ops.not_(used),
                s["attempts"] > n,
                ops.and_(s["attempts"] == n, ops.not_(s["retry"])),
            ),
            ops.or_(ops.not_(used), s[f"tim_{k}"]),
            ops.or_(ops.not_(used), ops.not_(s[f"dup_{k}"])),
            ops.or_(ops.not_(used), ops.not_(ops.and_(s[f"nsf_{k}"], s[f"uf_{k}"]))),
            # a retry's return is pending only while it is the latest retry
            ops.or_(s[f"st_{k}"] != SCHEDULED, s["attempts"] == n),
        ]
    out.append(ops.not_(ops.and_(s["last_nsf"], s["last_uf"])))
    out += [ops.not_(ops.and_(s[f"nsf_{k}"], s[f"uf_{k}"])) for k in (0, 1)]
    return ops.and_(*out)


INVARIANTS = (
    Invariant(
        "well_formed",
        "slots in use agree with the retries made; cash equals the invoice less settled "
        "returns plus dishonors not contested plus retry debits",
        _inv_well_formed,
    ),
    Invariant(
        "double_entry",
        "every step's postings sum to zero and all balances net to zero",
        _inv_balance,
        _step_balance,
    ),
    Invariant(
        "allocation",
        "collected + written off + voided + open receivable = the invoice, always",
        _inv_allocation,
    ),
    Invariant(
        "no_money_invented",
        "the originator never collects more than it invoiced",
        _inv_invented,
    ),
    Invariant(
        "no_negative_balance",
        "receivable (also net of dishonors and retries in flight), accepted returns, "
        "bad debt and fees never below zero",
        _inv_negative,
    ),
    Invariant(
        "dishonor_rules",
        "a dishonor stands only against an improper return (R67 duplicate, R68 "
        "untimely) and only if timely; dishonor states only move forward and a contest "
        "moves the dishonored amount back",
        _inv_dishonor,
        _step_dishonor,
    ),
    Invariant(
        "reinit_rules",
        "every retry the rules forbid is recorded; at most two retries are proper",
        _inv_reinit,
    ),
    Invariant(
        "terminal_absorbing",
        "a resolved case (quiet, nothing receivable) has no further step",
        None,
        _step_terminal,
    ),
)

MODEL = Model(
    name="ach",
    params=PARAMS,
    state=STATE,
    inputs=INPUTS,
    param_constraints=param_constraints,
    input_constraints=input_constraints,
    init=init,
    commands=COMMANDS,
    invariants=INVARIANTS,
)
