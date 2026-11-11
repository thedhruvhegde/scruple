"""Abstract model of one card dispute: lifecycle, deadlines, refunds, fees and ledger.

It restates `scruple.sim.disputes.DisputeEngine` for a single case as guarded
commands (`scruple.formal.ops`). Amounts are integers in minor units, days are
`date.toordinal()` ints. The issuer is fully nondeterministic: at every decision it
may award the merchant any amount `w` in [0, disputed] (0: the merchant loses), which
covers the seeded issuer, the truth oracle, moot refunds and split-shipment partial
wins. Deadlines are an input after the stage starts (see `ops.deadline`).

State (one case): `stage`, `aw` (who acts next), `res` (resolution, 0 while open),
`entered` (day the stage was entered), `due` (when the issuer or network answers),
`dl` (the merchant's last day to act), `declined` (allocation pre-arbitration
declined), `pending` (a refund in flight), `debited` (a chargeback debit posted),
`returned` (net returned to the cardholder, transaction currency, exact), the ledger
balances `ch` (cardholder), `m` (merchant), `pf` (processor fees), `nf` (network
fees), `fees` (fees charged to the merchant), `viol` (double-refund violations the
ledger recorded), `inc_dbl` / `inc_blk` (incidents: over-credit flagged, refund
refused). Ghost variables for the properties: `last` (latest event day), `mono`
(event days never went backwards), `agent_ref` (the agent refunded), `crash` (a step
the engine would raise on: a reversal above what was returned, a stage fired before
it was entered).

Commands mirror the engine's steps (an agent action, or one iteration of
`_sync_case`): ACCEPT, REP_INQ_WIN, REP_INQ_CB, REP_CB, ESCALATE, REFUND, NOOP
(request_info), RACE_POST, RACE_BLOCK (a refund in flight settles or is refused),
MISS_INQ, MISS_LOSE (a missed merchant deadline), ISS_WIN, ISS_PREARB, ISS_DECLINE
(the issuer answers), RULE_WIN, RULE_LOSE (the network rules). The environment fires
in the engine's order: a settling refund first when it is due no later than the next
deadline, and an agent may act on day `d` only when nothing is due by `d` (the engine
syncs before every action).
"""

from __future__ import annotations

from typing import Any

from scruple.formal.ops import (
    INF,
    Command,
    Inputs,
    Invariant,
    Model,
    Params,
    State,
    b2i,
    imax,
    merge,
)

INQ, CB, REP, PRE, ARB, RES = range(6)
STAGES = ("inquiry", "chargeback", "representment", "pre_arbitration", "arbitration", "resolved")
MER, ISS, NET, NOB = range(4)
AWAITING = ("merchant", "issuer", "network", "nobody")
NONE, ACCEPTED, WON, PARTIAL, LOST = range(5)
RESOLUTIONS = (None, "accepted", "won", "partial", "lost")
MAX_DAY = 3_652_059  # date.max.toordinal()

PARAMS: dict[str, type] = {
    "C": int,  # captured
    "A": int,  # disputed
    "alloc": bool,  # allocation workflow (Visa fraud and authorization)
    "opens_cb": bool,  # opens as a chargeback (else an inquiry)
    "race": bool,  # a refund is in flight
    "R": int,  # its amount
    "race_day": int,  # the day it settles
    "processed": int,
    "opened": int,
    "f_cb": int,  # fee on entering each stage (USD minor units), and on losing arbitration
    "f_rep": int,
    "f_pre": int,
    "f_arb": int,
    "f_loss": int,
    "iss_days": int,  # the issuer answers after this many days
    "arb_days": int,  # the network rules after this many days
    "single": bool,  # transaction currency is the settlement currency
}
STATE: dict[str, type] = {
    "stage": int,
    "aw": int,
    "res": int,
    "entered": int,
    "due": int,
    "dl": int,
    "declined": bool,
    "pending": bool,
    "debited": bool,
    "returned": int,
    "ch": int,
    "m": int,
    "pf": int,
    "nf": int,
    "fees": int,
    "viol": int,
    "inc_dbl": int,
    "inc_blk": int,
    "last": int,
    "mono": bool,
    "agent_ref": bool,
    "refd": int,
    "rev": int,
    "crash": bool,
}
INPUTS: dict[str, type] = {"d": int, "x": int, "w": int, "dl": int}


def param_constraints(p: Params, ops: Any) -> list[Any]:
    """What `CardDispute` and `FeeSchedule` validate, nothing more."""
    return [
        p["C"] >= 1,
        p["A"] >= 1,
        p["A"] <= p["C"],
        p["R"] >= 1,
        p["R"] <= p["C"],
        p["processed"] <= p["opened"],
        p["race_day"] >= p["processed"],
        p["f_cb"] >= 0,
        p["f_rep"] >= 0,
        p["f_pre"] >= 0,
        p["f_arb"] >= 0,
        p["f_loss"] >= 0,
        p["iss_days"] >= 1,
        p["arb_days"] >= 1,
        p["processed"] >= 1,  # days are real dates: date.min .. date.max ordinals
        p["opened"] <= MAX_DAY,
        p["race_day"] <= MAX_DAY,
    ]


def input_constraints(i: Inputs, p: Params, ops: Any) -> list[Any]:
    return [i["w"] >= 0, i["w"] <= p["A"]]


# --- helpers (all pure: they return a new state) ------------------------------------
def _set(s: State, **kw: Any) -> State:
    return {**s, **kw}


def _at(s: State, ops: Any, day: Any) -> State:
    """An event on `day`: time must not run backwards."""
    return _set(s, mono=ops.and_(s["mono"], day >= s["last"]), last=imax(ops, s["last"], day))


def _fire(s: State, ops: Any, to: Any, day: Any) -> State:
    """`Lifecycle.fire`: raises (a crash) when `day` is before the stage was entered."""
    s = _at(s, ops, day)
    return _set(s, crash=ops.or_(s["crash"], day < s["entered"]), stage=to, entered=day)


def _fee(s: State, ops: Any, amount: Any, processor: Any) -> State:
    return _set(
        s,
        m=s["m"] - amount,
        fees=s["fees"] + amount,
        pf=s["pf"] + ops.ite(processor, amount, 0),
        nf=s["nf"] + ops.ite(processor, 0, amount),
    )


def _to_cardholder(s: State, ops: Any, amount: Any, day: Any) -> State:
    posted = ops.post(amount, day)
    return _set(s, returned=s["returned"] + amount, ch=s["ch"] + posted, m=s["m"] - posted)


def _to_merchant(s: State, ops: Any, amount: Any, day: Any) -> State:
    """`CardLedger.to_merchant` raises when reversing more than was returned."""
    posted = ops.post(amount, day)
    return _set(
        s,
        crash=ops.or_(s["crash"], amount > s["returned"]),
        returned=s["returned"] - amount,
        rev=s["rev"] + amount,
        ch=s["ch"] - posted,
        m=s["m"] + posted,
    )


def _resolve(s: State, ops: Any, day: Any, res: Any, reversal: Any = 0) -> State:
    s = _fire(s, ops, RES, day)
    s = _set(s, aw=NOB, res=res)
    rev = _to_merchant(s, ops, reversal, day)
    return merge(ops, reversal > 0, rev, s)


def _debit(s: State, p: Params, ops: Any, day: Any) -> State:
    """`_on_chargeback`'s debit: always posts; over the capture it is a violation,
    and above the disputed amount an incident."""
    over = p["A"] - imax(ops, 0, p["C"] - s["returned"])
    s = _to_cardholder(s, ops, p["A"], day)
    # over the capture the ledger records a violation; within it (a partial dispute)
    # but on top of a refund, the engine still flags an incident
    flagged = ops.or_(over > 0, s["returned"] > p["A"])
    return _set(
        s,
        debited=True,
        viol=s["viol"] + b2i(ops, over > 0),
        inc_dbl=s["inc_dbl"] + b2i(ops, flagged),
    )


def _on_chargeback(s: State, p: Params, ops: Any, inp: Inputs) -> State:
    """Debit, chargeback fee, then the merchant's response window."""
    day = s["entered"]
    s = _at(s, ops, day)
    s = _debit(s, p, ops, day)
    s = _fee(s, ops, p["f_cb"], True)
    return _set(s, aw=MER, dl=ops.deadline(inp, CB, day, False))


def _maybe_close_inquiry(s: State, p: Params, ops: Any, day: Any) -> State:
    """A refund that returns the disputed amount answers an open inquiry."""
    c = ops.and_(s["res"] == NONE, s["stage"] == INQ, s["returned"] >= p["A"])
    closed = _resolve(s, ops, imax(ops, day, s["entered"]), ACCEPTED)
    return merge(ops, c, closed, s)


def _refund(s: State, p: Params, ops: Any, amount: Any, day: Any) -> State:
    """`_post_refund` once the ledger has accepted it."""
    s = _at(s, ops, day)
    s = _to_cardholder(s, ops, amount, day)
    s = _set(s, refd=s["refd"] + amount)
    return _maybe_close_inquiry(s, p, ops, day)


def next_due(s: State, p: Params, ops: Any) -> Any:
    return ops.ite(s["res"] != NONE, INF, ops.ite(s["aw"] == MER, s["dl"] + 1, s["due"]))


def race_first(s: State, p: Params, ops: Any) -> Any:
    return ops.and_(s["pending"], p["race_day"] <= next_due(s, p, ops))


def step_due_by(s: State, p: Params, ops: Any, d: Any) -> Any:
    return ops.and_(
        s["res"] == NONE,
        ops.or_(
            ops.and_(s["aw"] == MER, d > s["dl"]),
            ops.and_(s["aw"] != MER, d >= s["due"]),
        ),
    )


def quiet_by(s: State, p: Params, ops: Any, d: Any) -> Any:
    """Nothing falls due by day `d` (so an action on `d` meets no pending step)."""
    return ops.and_(
        ops.not_(ops.and_(s["pending"], p["race_day"] <= d)),
        ops.not_(step_due_by(s, p, ops, d)),
    )


def race_blocked(s: State, p: Params, ops: Any) -> Any:
    """`_settle_race`: refused after the debit returned the dispute, or by the ledger."""
    return ops.or_(s["returned"] >= p["A"], p["R"] > p["C"] - s["returned"])


def env_ready(s: State, p: Params, ops: Any) -> Any:
    return ops.and_(ops.not_(race_first(s, p, ops)), s["res"] == NONE)


def init(p: Params, i: Inputs, ops: Any) -> State:
    """`DisputeEngine._open`: capture, a refund settled before filing, then the debit."""
    capture = ops.post(p["C"], p["processed"])
    s: State = {
        "stage": ops.ite(p["opens_cb"], CB, INQ),
        "aw": MER,
        "res": NONE,
        "entered": p["opened"],
        "due": INF,
        "dl": INF,
        "declined": False,
        "pending": ops.and_(p["race"], p["race_day"] > p["opened"]),
        "debited": False,
        "returned": 0,
        "ch": -capture,
        "m": capture,
        "pf": 0,
        "nf": 0,
        "fees": 0,
        "viol": 0,
        "inc_dbl": 0,
        "inc_blk": 0,
        "last": p["processed"],
        "mono": True,
        "agent_ref": False,
        "refd": 0,
        "rev": 0,
        "crash": False,
    }
    pre = ops.and_(p["race"], p["race_day"] <= p["opened"])
    s = merge(ops, pre, _refund(s, p, ops, p["R"], p["race_day"]), s)
    s = _at(s, ops, p["opened"])  # the dispute is filed
    as_cb = _on_chargeback(s, p, ops, i)
    as_inq = merge(ops, s["res"] == NONE, _set(s, dl=ops.deadline(i, INQ, p["opened"], False)), s)
    return merge(ops, p["opens_cb"], as_cb, as_inq)


# --- commands -----------------------------------------------------------------------
def _agent(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    d = i["d"]
    return ops.and_(
        s["res"] == NONE,
        s["aw"] == MER,
        d >= s["last"],
        d >= p["opened"],
        quiet_by(s, p, ops, d),
    )


def _g_accept(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_agent(s, p, i, ops), ops.or_(s["stage"] == CB, s["stage"] == PRE))


def _u_accept(s: State, p: Params, i: Inputs, ops: Any) -> State:
    return _resolve(s, ops, i["d"], ACCEPTED)


def _g_rep_inq_win(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_agent(s, p, i, ops), s["stage"] == INQ, i["w"] == p["A"])


def _u_rep_inq_win(s: State, p: Params, i: Inputs, ops: Any) -> State:
    return _resolve(s, ops, i["d"], WON)


def _g_rep_inq_cb(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_agent(s, p, i, ops), s["stage"] == INQ, i["w"] < p["A"])


def _file_chargeback(s: State, p: Params, i: Inputs, ops: Any, day: Any) -> State:
    s = _fire(s, ops, CB, day)
    return _on_chargeback(s, p, ops, i)


def _u_rep_inq_cb(s: State, p: Params, i: Inputs, ops: Any) -> State:
    return _file_chargeback(s, p, i, ops, i["d"])


def _g_rep_cb(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_agent(s, p, i, ops), s["stage"] == CB)


def _u_rep_cb(s: State, p: Params, i: Inputs, ops: Any) -> State:
    d = i["d"]
    s = _fire(s, ops, ops.ite(p["alloc"], PRE, REP), d)
    s = _fee(s, ops, ops.ite(p["alloc"], p["f_pre"], p["f_rep"]), False)
    return _set(s, aw=ISS, due=d + p["iss_days"])


def _g_escalate(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_agent(s, p, i, ops), s["stage"] == PRE)


def _u_escalate(s: State, p: Params, i: Inputs, ops: Any) -> State:
    d = i["d"]
    s = _fire(s, ops, ARB, d)
    s = _fee(s, ops, p["f_arb"], False)
    return _set(s, aw=NET, due=d + p["arb_days"])


def _g_refund(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_agent(s, p, i, ops), i["x"] >= 1, i["x"] <= p["C"] - s["returned"])


def _u_refund(s: State, p: Params, i: Inputs, ops: Any) -> State:
    return _set(_refund(s, p, ops, i["x"], i["d"]), agent_ref=True)


def _g_noop(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return _agent(s, p, i, ops)


def _u_noop(s: State, p: Params, i: Inputs, ops: Any) -> State:
    return _at(s, ops, i["d"])


def _g_race_post(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(race_first(s, p, ops), ops.not_(race_blocked(s, p, ops)))


def _u_race_post(s: State, p: Params, i: Inputs, ops: Any) -> State:
    return _refund(_set(s, pending=False), p, ops, p["R"], p["race_day"])


def _g_race_block(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(race_first(s, p, ops), race_blocked(s, p, ops))


def _u_race_block(s: State, p: Params, i: Inputs, ops: Any) -> State:
    s = _at(s, ops, p["race_day"])
    return _set(s, pending=False, inc_blk=s["inc_blk"] + 1)


def _g_miss_inq(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(env_ready(s, p, ops), s["aw"] == MER, s["stage"] == INQ)


def _u_miss_inq(s: State, p: Params, i: Inputs, ops: Any) -> State:
    return _file_chargeback(s, p, i, ops, s["dl"] + 1)


def _g_miss_lose(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(env_ready(s, p, ops), s["aw"] == MER, s["stage"] != INQ)


def _u_miss_lose(s: State, p: Params, i: Inputs, ops: Any) -> State:
    return _resolve(s, ops, s["dl"] + 1, LOST)


def _g_iss(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(env_ready(s, p, ops), s["aw"] == ISS)


def _won(s: State, p: Params, i: Inputs, ops: Any) -> State:
    """The merchant is awarded `w` > 0 on the day the answer is due: reverse it."""
    day = s["due"]
    s = _at(_set(s, due=INF), ops, day)
    return _resolve(s, ops, day, ops.ite(i["w"] >= p["A"], WON, PARTIAL), i["w"])


def _g_iss_win(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_g_iss(s, p, i, ops), i["w"] > 0)


def _g_iss_prearb(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_g_iss(s, p, i, ops), i["w"] == 0, s["stage"] == REP)


def _u_iss_prearb(s: State, p: Params, i: Inputs, ops: Any) -> State:
    day = s["due"]
    s = _at(_set(s, due=INF), ops, day)
    s = _fire(s, ops, PRE, day)
    s = _fee(s, ops, p["f_pre"], False)
    return _set(s, aw=MER, dl=ops.deadline(i, PRE, day, False))


def _g_iss_decline(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(_g_iss(s, p, i, ops), i["w"] == 0, s["stage"] == PRE)


def _u_iss_decline(s: State, p: Params, i: Inputs, ops: Any) -> State:
    day = s["due"]
    s = _at(_set(s, due=INF), ops, day)
    return _set(s, declined=True, aw=MER, dl=ops.deadline(i, PRE, day, True))


def _g_rule_win(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(env_ready(s, p, ops), s["aw"] == NET, i["w"] > 0)


def _g_rule_lose(s: State, p: Params, i: Inputs, ops: Any) -> Any:
    return ops.and_(env_ready(s, p, ops), s["aw"] == NET, i["w"] == 0)


def _u_rule_lose(s: State, p: Params, i: Inputs, ops: Any) -> State:
    day = s["due"]
    s = _at(_set(s, due=INF), ops, day)
    s = _resolve(s, ops, day, LOST)
    return _fee(s, ops, p["f_loss"], False)


COMMANDS = (
    Command("ACCEPT", _g_accept, _u_accept, agent=True),
    Command("REP_INQ_WIN", _g_rep_inq_win, _u_rep_inq_win, agent=True),
    Command("REP_INQ_CB", _g_rep_inq_cb, _u_rep_inq_cb, agent=True),
    Command("REP_CB", _g_rep_cb, _u_rep_cb, agent=True),
    Command("ESCALATE", _g_escalate, _u_escalate, agent=True),
    Command("REFUND", _g_refund, _u_refund, agent=True),
    Command("NOOP", _g_noop, _u_noop, agent=True),
    Command("RACE_POST", _g_race_post, _u_race_post),
    Command("RACE_BLOCK", _g_race_block, _u_race_block),
    Command("MISS_INQ", _g_miss_inq, _u_miss_inq),
    Command("MISS_LOSE", _g_miss_lose, _u_miss_lose),
    Command("ISS_WIN", _g_iss_win, _won),
    Command("ISS_PREARB", _g_iss_prearb, _u_iss_prearb),
    Command("ISS_DECLINE", _g_iss_decline, _u_iss_decline),
    Command("RULE_WIN", _g_rule_win, _won),
    Command("RULE_LOSE", _g_rule_lose, _u_rule_lose),
)
AGENT_COMMANDS = tuple(c.name for c in COMMANDS if c.agent)


def allowed(s: State) -> tuple[str, ...]:
    """The agent action kinds the model allows now (concrete states only)."""
    if s["res"] != NONE or s["aw"] != MER:
        return ()
    out: list[str] = []
    if s["stage"] in (CB, PRE):
        out.append("accept")
    if s["stage"] in (INQ, CB):
        out.append("represent")
    if s["stage"] == PRE:
        out.append("escalate")
    return (*out, "request_info", "refund")


# --- invariants ---------------------------------------------------------------------
def _total(s: State) -> Any:
    return s["ch"] + s["m"] + s["pf"] + s["nf"]


def _inv_balance(s: State, p: Params, ops: Any) -> Any:
    return ops.or_(ops.not_(p["single"]), _total(s) == 0)


def _step_balance(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
    return ops.or_(ops.not_(p["single"]), _total(s2) - _total(s) == 0)


def _inv_conservation(s: State, p: Params, ops: Any) -> Any:
    return ops.or_(
        ops.not_(p["single"]),
        ops.and_(
            s["ch"] == s["returned"] - p["C"],
            s["m"] == p["C"] - s["returned"] - s["fees"],
            s["pf"] + s["nf"] == s["fees"],
        ),
    )


def _inv_no_double_refund(s: State, p: Params, ops: Any) -> Any:
    return ops.or_(s["returned"] <= p["C"], s["viol"] > 0)


def _step_no_double_refund(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
    refunded = ops.or_(took("REFUND"), took("RACE_POST"))
    return ops.or_(ops.not_(refunded), s2["returned"] <= p["C"])


def _inv_no_negative(s: State, p: Params, ops: Any) -> Any:
    over = s["viol"] > 0
    return ops.and_(
        s["pf"] >= 0,
        s["nf"] >= 0,
        s["returned"] >= 0,
        ops.or_(over, ops.not_(p["single"]), ops.and_(s["m"] + s["fees"] >= 0, s["ch"] <= 0)),
    )


def _inv_deadlines(s: State, p: Params, ops: Any) -> Any:
    waiting = ops.and_(s["res"] == NONE, s["aw"] == MER)
    answering = ops.and_(s["res"] == NONE, ops.or_(s["aw"] == ISS, s["aw"] == NET))
    return ops.and_(
        s["mono"],
        ops.or_(ops.not_(waiting), s["dl"] > s["entered"]),
        ops.or_(ops.not_(answering), s["due"] > s["entered"]),
    )


def _step_deadlines(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
    acted = ops.or_(*(took(n) for n in AGENT_COMMANDS))
    same_window = ops.and_(
        s["res"] == NONE,
        s2["res"] == NONE,
        s["aw"] == MER,
        s2["aw"] == MER,
        s["stage"] == s2["stage"],
        s["declined"] == s2["declined"],
    )
    return ops.and_(
        ops.or_(ops.not_(acted), i["d"] <= s["dl"]),  # never act after the deadline
        ops.or_(ops.not_(same_window), s2["dl"] >= s["dl"]),  # never pulled earlier
        s2["last"] >= s["last"],
    )


def _inv_terminal(s: State, p: Params, ops: Any) -> Any:
    done = s["res"] != NONE
    return ops.and_(done == (s["stage"] == RES), ops.or_(ops.not_(done), s["aw"] == NOB))


def _step_terminal(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
    return ops.or_(
        s["res"] == NONE,
        ops.and_(
            s2["res"] == s["res"],
            s2["stage"] == RES,
            s2["aw"] == NOB,
            s2["entered"] == s["entered"],
            s2["fees"] == s["fees"],
            # only a refund already in flight may still settle (and nothing else moves)
            ops.or_(s2["returned"] == s["returned"], took("RACE_POST")),
        ),
    )


def _inv_race(s: State, p: Params, ops: Any) -> Any:
    """Without agent refunds the cardholder holds the merchant's refunds or the
    chargeback, never both, unless the over-credit was flagged as an incident."""
    return ops.or_(s["agent_ref"], s["returned"] <= imax(ops, s["refd"], p["A"]), s["inc_dbl"] > 0)


def _step_race(s: State, s2: State, p: Params, i: Inputs, ops: Any, took: Any) -> Any:
    """A refund settling after the chargeback debit never pays the dispute twice."""
    return ops.or_(
        ops.not_(took("RACE_POST")),
        ops.not_(s["debited"]),
        s2["returned"] <= imax(ops, p["A"], s["returned"]),
    )


def _inv_well_formed(s: State, p: Params, ops: Any) -> Any:
    """The shape of a reachable state (what the other invariants lean on)."""
    st, aw, open_ = s["stage"], s["aw"], s["res"] == NONE
    live = ops.or_(st == CB, st == REP, st == PRE, st == ARB)
    return ops.and_(
        st >= INQ,
        st <= RES,
        aw >= MER,
        aw <= NOB,
        s["res"] >= NONE,
        s["res"] <= LOST,
        ops.or_(ops.not_(live), s["debited"]),
        ops.or_(st != INQ, ops.not_(s["debited"])),
        ops.or_(ops.not_(open_), ops.not_(s["debited"]), s["returned"] >= p["A"]),
        ops.or_(aw != ISS, st == REP, st == PRE),
        ops.or_(aw != NET, st == ARB),
        ops.or_(aw != MER, ops.not_(open_), st == INQ, st == CB, st == PRE),
        ops.or_(ops.not_(s["declined"]), st == PRE, st == ARB, st == RES),
        ops.or_(ops.not_(s["pending"]), ops.and_(p["race"], p["race_day"] > s["last"])),
        ops.or_(ops.not_(open_), aw == MER, s["due"] >= s["last"]),
        ops.or_(ops.not_(open_), aw != MER, s["dl"] + 1 >= s["last"]),
        s["entered"] <= s["last"],
        s["entered"] >= p["opened"],
        s["last"] >= p["processed"],
        s["fees"] >= 0,
        s["refd"] >= 0,
        s["returned"] == s["refd"] + ops.ite(s["debited"], p["A"], 0) - s["rev"],
        s["rev"] >= 0,
        s["rev"] <= ops.ite(s["debited"], p["A"], 0),
        ops.or_(s["rev"] == 0, s["res"] == WON, s["res"] == PARTIAL),
        s["viol"] >= 0,
        s["inc_dbl"] >= s["viol"],
        s["inc_blk"] >= 0,
    )


def _inv_no_crash(s: State, p: Params, ops: Any) -> Any:
    return ops.not_(s["crash"])


INVARIANTS = (
    Invariant(
        "well_formed",
        "stage, who acts and resolution agree; a debit precedes every later stage and "
        "keeps the disputed amount returned until resolution; pending steps lie ahead",
        _inv_well_formed,
    ),
    Invariant(
        "double_entry",
        "every step's postings sum to zero and all balances net to zero",
        _inv_balance,
        _step_balance,
    ),
    Invariant(
        "conservation",
        "cardholder = returned - captured, merchant = captured - returned - fees, "
        "fee payees = fees (cardholder + merchant + fees = 0)",
        _inv_conservation,
    ),
    Invariant(
        "no_double_refund",
        "a refund never returns more than the capture; more than the capture only "
        "through a network debit the ledger records as a violation",
        _inv_no_double_refund,
        _step_no_double_refund,
    ),
    Invariant(
        "no_negative_balance",
        "fee payees and returned never below zero; merchant below zero only by fees, "
        "cardholder above zero never, unless a violation was recorded",
        _inv_no_negative,
    ),
    Invariant(
        "deadline_monotonicity",
        "event days never go backwards, every deadline is after its stage started and "
        "is never pulled earlier, and no agent action is accepted after it",
        _inv_deadlines,
        _step_deadlines,
    ),
    Invariant(
        "terminal_absorbing",
        "resolved iff stage resolved; once resolved, stage, resolution, dates and fees "
        "never change and money moves only by a refund already in flight",
        _inv_terminal,
        _step_terminal,
    ),
    Invariant(
        "refund_race_safety",
        "with no agent refund the cardholder never silently holds both a refund and "
        "the chargeback, and a refund after the debit never pays the dispute twice",
        _inv_race,
        _step_race,
    ),
    Invariant(
        "no_crash",
        "no reversal above what was returned, no stage fired before it was entered",
        _inv_no_crash,
    ),
)

MODEL = Model(
    name="dispute",
    params=PARAMS,
    state=STATE,
    inputs=INPUTS,
    param_constraints=param_constraints,
    input_constraints=input_constraints,
    init=init,
    commands=COMMANDS,
    invariants=INVARIANTS,
)
