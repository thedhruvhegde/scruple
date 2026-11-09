"""Stateful ledger properties (roadmap 161) across every money path.

Three hypothesis state machines drive random action sequences and check the ledger
invariants after every single step:

- `CardLedgerMachine`: the merchant-side card ledger directly (captures, refunds,
  chargeback debits, reversals, fees, in one or two currencies).
- `SimulatorMachine`: the original batch `Simulator` (step, snapshot, restore, reset).
- `DisputeEngineMachine`: the event-sourced `DisputeEngine` (actions, time, rewinds,
  monitoring fines), including exact replay of the event log.

The invariants: every journal entry sums to zero; every currency sums to zero;
running balances equal a fresh recomputation from the journal; and no account goes
"negative silently": a cardholder can only end up holding more than was captured
when the ledger (or, for the batch simulator, the ledger verifier) has said so.
All data is synthetic.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from scruple.domain import Action
from scruple.sim.card_ledger import CardLedger, DoubleRefundError
from scruple.sim.deadlines import ManualClock, today
from scruple.sim.disputes import (
    DEFAULT_MIX,
    FULL_MIX,
    Accept,
    AgentAction,
    CardDispute,
    DisputeEngine,
    Escalate,
    Refund,
    Represent,
    RequestInfo,
    generate_cases,
)
from scruple.sim.engine import SimState, Simulator
from scruple.sim.generator import generate_cases as generate_batch
from scruple.sim.ledger import JournalEntry, UnbalancedEntryError
from scruple.sim.merchants import generate_merchants
from scruple.sim.models import Stage
from scruple.sim.policy import Policy
from scruple.verifiers.ledger import verify_ledger

MACHINE = settings(
    max_examples=25,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
DAY0 = date(2026, 3, 2)


def recomputed(entries: tuple[JournalEntry, ...]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = defaultdict(int)
    for e in entries:
        for p in e.postings:
            out[(p.account, e.currency)] += p.amount_minor
    return out


def assert_journal_sound(entries: tuple[JournalEntry, ...]) -> dict[tuple[str, str], int]:
    """Every entry balances, ids are 1..n in order, every currency nets to zero."""
    assert [e.entry_id for e in entries] == list(range(1, len(entries) + 1))
    for e in entries:
        assert e.postings, f"entry {e.entry_id} has no postings"
        assert sum(p.amount_minor for p in e.postings) == 0, e
    bal = recomputed(entries)
    per_ccy: dict[str, int] = defaultdict(int)
    for (_, ccy), v in bal.items():
        per_ccy[ccy] += v
    assert all(v == 0 for v in per_ccy.values()), dict(per_ccy)
    return bal


# --------------------------------------------------------------------- card ledger
class CardLedgerMachine(RuleBasedStateMachine):
    """Random postings straight into `CardLedger`, with a shadow model of the limits."""

    txns = Bundle("txns")

    def __init__(self) -> None:
        super().__init__()
        self.cl = CardLedger()
        self.n = 0
        self.day = DAY0
        self.model_returned: dict[str, int] = {}
        self.model_violations = 0

    @rule(
        target=txns,
        amount=st.integers(1, 10**8),
        settle=st.sampled_from(["USD", "USD", "EUR", "JPY"]),
    )
    def capture(self, amount: int, settle: str) -> str:
        self.n += 1
        txn = f"t{self.n}"
        self.cl.capture(txn, f"m{self.n % 3}", amount, "USD", settle, self.day)
        self.model_returned[txn] = 0
        return txn

    @rule(txn=txns, frac=st.floats(0.0, 1.5))
    def refund(self, txn: str, frac: float) -> None:
        amount = max(1, int(self.cl.captured(txn) * frac))
        before = len(self.cl.entries)
        if amount > self.cl.captured(txn) - self.model_returned[txn]:
            with pytest.raises(DoubleRefundError):
                self.cl.refund(txn, "c", amount, self.day)
            assert len(self.cl.entries) == before  # refused: nothing posted
            return
        self.cl.refund(txn, "c", amount, self.day)
        self.model_returned[txn] += amount

    @rule(txn=txns, frac=st.floats(0.01, 1.5))
    def chargeback(self, txn: str, frac: float) -> None:
        amount = max(1, int(self.cl.captured(txn) * frac))
        over = self.model_returned[txn] + amount > self.cl.captured(txn)
        self.cl.chargeback_debit(txn, "c", amount, self.day)
        self.model_returned[txn] += amount
        if over:
            self.model_violations += 1

    @rule(txn=txns, frac=st.floats(0.01, 1.5))
    def reversal(self, txn: str, frac: float) -> None:
        amount = max(1, int(self.cl.captured(txn) * frac))
        if amount > self.model_returned[txn]:
            with pytest.raises(ValueError, match="exceeds"):
                self.cl.to_merchant("chargeback_reversal", txn, "c", amount, self.day)
            return
        self.cl.to_merchant("chargeback_reversal", txn, "c", amount, self.day)
        self.model_returned[txn] -= amount

    @rule(txn=txns, amount=st.integers(0, 50_000))
    def fee(self, txn: str, amount: int) -> None:
        entry = self.cl.fee("fee:chargeback", txn, "c", amount, "processor:fees")
        assert (entry is None) == (amount == 0)

    @rule(txn=txns, amount=st.integers(-(10**6), 0))
    def bad_amounts_are_refused(self, txn: str, amount: int) -> None:
        before = len(self.cl.entries)
        with pytest.raises(ValueError):
            self.cl.to_cardholder("refund", txn, "c", amount, self.day)
        with pytest.raises(ValueError):
            self.cl.to_merchant("chargeback_reversal", txn, "c", amount, self.day)
        if amount < 0:
            with pytest.raises(ValueError):
                self.cl.fee("fee:x", txn, "c", amount, "network:fees")
        assert len(self.cl.entries) == before

    @rule(days=st.integers(1, 40))
    def advance(self, days: int) -> None:
        self.day += timedelta(days=days)

    @invariant()
    def journal_is_sound(self) -> None:
        bal = assert_journal_sound(self.cl.entries)
        for (acct, ccy), v in bal.items():
            assert self.cl.balance(acct, ccy) == v
        assert all(v == 0 for v in self.cl.currency_totals().values())

    @invariant()
    def returned_matches_the_model_and_is_never_negative(self) -> None:
        for txn, r in self.model_returned.items():
            assert self.cl.returned(txn) == r
            assert r >= 0
            assert self.cl.headroom(txn) == self.cl.captured(txn) - r

    @invariant()
    def over_credit_is_never_silent(self) -> None:
        over = {t: r - self.cl.captured(t) for t, r in self.model_returned.items()}
        assert self.cl.over_credited() == {t: v for t, v in over.items() if v > 0}
        assert len(self.cl.violations) == self.model_violations
        for t in self.cl.over_credited():
            assert any(v.txn_id == t for v in self.cl.violations)

    @invariant()
    def same_currency_cardholder_balance_tracks_returned(self) -> None:
        for txn in self.model_returned:
            if self.cl.settlement_currency(txn) == "USD":
                assert self.cl.balance(f"cardholder:{txn}", "USD") == (
                    self.cl.returned(txn) - self.cl.captured(txn)
                )


TestCardLedgerMachine = CardLedgerMachine.TestCase
TestCardLedgerMachine.settings = MACHINE


# ------------------------------------------------------------------ batch simulator
class SimulatorMachine(RuleBasedStateMachine):
    """The original `Simulator`: one policy decision per step, plus time travel."""

    def __init__(self) -> None:
        super().__init__()
        self.sim: Simulator | None = None
        self.saved: list[tuple[SimState, int]] = []

    @initialize(seed=st.integers(0, 2**32 - 1), n=st.integers(1, 25))
    def start(self, seed: int, n: int) -> None:
        self.sim = Simulator(generate_batch(seed, n))
        self.opening = len(self.sim.result().journal)

    @precondition(lambda self: self.sim is not None and not self.sim.done)
    @rule(action=st.sampled_from(list(Action)))
    def step(self, action: Action) -> None:
        assert self.sim is not None
        before = len(self.sim.result().journal)
        r = self.sim.step(Policy(rules=(), default_action=action))
        assert r.action is action
        posted = self.sim.result().journal[before:]
        assert all(e.case_id == r.case_id for e in posted)
        assert sum(
            p.amount_minor
            for e in posted
            if e.kind == "chargeback_debit"
            for p in e.postings
            if p.account.startswith("cardholder:")
        ) == (r.debit_txn_minor)
        assert sum(e.postings[1].amount_minor for e in posted if e.kind == "fee") == (
            r.fee_usd_minor
        )

    @precondition(lambda self: self.sim is not None)
    @rule()
    def snapshot(self) -> None:
        assert self.sim is not None
        self.saved.append((self.sim.snapshot(), len(self.sim.result().results)))

    @precondition(lambda self: bool(self.saved))
    @rule(data=st.data())
    def restore(self, data: st.DataObject) -> None:
        assert self.sim is not None
        state, n_results = data.draw(st.sampled_from(self.saved))
        self.sim.restore(state)
        assert len(self.sim.result().results) == n_results
        assert self.sim.snapshot() == state

    @precondition(lambda self: self.sim is not None)
    @rule()
    def reset(self) -> None:
        assert self.sim is not None
        self.sim.reset()
        assert len(self.sim.result().journal) == self.opening
        assert self.sim.result().results == ()

    @precondition(lambda self: self.sim is not None)
    @rule(acct=st.sampled_from(["merchant:x", "network:fees"]), skew=st.integers(1, 10**6))
    def unbalanced_entry_is_refused(self, acct: str, skew: int) -> None:
        assert self.sim is not None
        before = self.sim.snapshot()
        with pytest.raises(UnbalancedEntryError):
            self.sim._ledger.post("fee", "t", None, "USD", [(acct, skew), ("cardholder:t", 0)])
        assert self.sim.snapshot() == before

    @invariant()
    def journal_is_sound(self) -> None:
        if self.sim is None:
            return
        run = self.sim.result()
        bal = assert_journal_sound(run.journal)
        for (acct, ccy), v in bal.items():
            assert self.sim._ledger.balance(acct, ccy) == v

    @invariant()
    def results_are_consistent_and_regret_is_never_negative(self) -> None:
        if self.sim is None:
            return
        run = self.sim.result()
        assert [r.case_id for r in run.results] == [
            c.case_id for c in self.sim.cases[: len(run.results)]
        ]
        for r in run.results:
            assert r.regret_usd_minor >= 0
            assert r.cost_usd_minor == r.oracle_cost_usd_minor + r.regret_usd_minor
            assert r.debit_txn_minor >= 0 and r.fee_usd_minor >= 0 and r.ops_usd_minor >= 0

    @invariant()
    def over_credit_is_never_silent(self) -> None:
        """The batch ledger does not refuse an over-credit, so the ledger verifier must
        report every cardholder left holding more than they paid."""
        if self.sim is None:
            return
        run = self.sim.result()
        bal = recomputed(run.journal)
        processed = {r.case_id for r in run.results}
        over = {
            c.case_id
            for c in self.sim.cases
            if c.case_id in processed and bal[(f"cardholder:{c.txn_id}", c.txn_currency)] > 0
        }
        flagged_txns = {
            c.txn_id
            for c in self.sim.cases
            for f in verify_ledger(self.sim.cases, run).findings
            if f.case_id == c.case_id
        }
        for cid in over:
            txn = next(c.txn_id for c in self.sim.cases if c.case_id == cid)
            assert txn in flagged_txns, f"{cid} over-credited without a finding"


TestSimulatorMachine = SimulatorMachine.TestCase
TestSimulatorMachine.settings = MACHINE


# ------------------------------------------------------------------- dispute engine
ACTIONS = st.one_of(
    st.just(Accept()),
    st.just(Escalate()),
    st.just(RequestInfo()),
    st.integers(0, 3).map(lambda k: ("represent", k)),
    st.floats(0.01, 1.3).map(lambda f: ("refund", f)),
)


class DisputeEngineMachine(RuleBasedStateMachine):
    """The event-sourced engine under random agents, time, rewinds and fines."""

    def __init__(self) -> None:
        super().__init__()
        self.e: DisputeEngine | None = None
        self.clock = ManualClock()
        self.cases: list[CardDispute] = []

    @initialize(seed=st.integers(0, 2**32 - 1), n=st.integers(1, 8), full=st.booleans())
    def start(self, seed: int, n: int, full: bool) -> None:
        self.cases = generate_cases(seed, n, FULL_MIX if full else DEFAULT_MIX)
        self.clock.set_date(min(c.opened_on for c in self.cases))
        merchants = generate_merchants(seed)
        self.e = DisputeEngine(self.cases, self.clock, merchants=merchants)

    def _concrete(self, cid: str, raw: object) -> AgentAction:
        assert self.e is not None
        if isinstance(raw, tuple):
            kind, x = raw
            if kind == "represent":
                ev = self.e.observe(cid).evidence
                k = int(x)
                ids = tuple(d.doc_id for d in ev[:k]) if k else tuple(d.doc_id for d in ev)
                return Represent(evidence_ids=ids)
            amount = max(1, int(self.e.case(cid).captured_minor * float(x)))
            return Refund(amount_minor=amount)
        assert isinstance(raw, Accept | Escalate | RequestInfo)
        return raw

    @precondition(lambda self: self.e is not None)
    @rule(i=st.integers(0, 7), raw=ACTIONS)
    def act(self, i: int, raw: object) -> None:
        assert self.e is not None
        cid = self.cases[i % len(self.cases)].case_id
        action = self._concrete(cid, raw)
        self.e.observe(cid)  # brings the case up to date; may emit events
        digest, n_events = self.e.state_digest(), len(self.e.events)
        res = self.e.act(cid, action)
        if not res.ok:
            assert res.error is not None
            assert self.e.state_digest() == digest
            assert len(self.e.events) == n_events
        else:
            assert res.stage_before is not None and res.stage_after is not None
            assert all(1 <= x <= len(self.e.ledger.entries) for x in res.entries)

    @precondition(lambda self: self.e is not None)
    @rule(days=st.sampled_from([1, 2, 5, 9, 11, 31, 45]))
    def advance(self, days: int) -> None:
        assert self.e is not None
        self.clock.advance(days=days)
        self.e.sync()

    @precondition(lambda self: self.e is not None)
    @rule(i=st.integers(0, 7), data=st.data())
    def rewind(self, i: int, data: st.DataObject) -> None:
        assert self.e is not None
        cid = self.cases[i % len(self.cases)].case_id
        hi = len(self.e.case_events(cid))
        to = data.draw(st.sampled_from(self.e.step_boundaries(cid)))
        others = {k: self.e.case_digest(k) for k in self.e.case_ids if k != cid}
        removed = self.e.rewind(cid, to)
        assert len(removed) == hi - to
        assert len(self.e.case_events(cid)) == to
        assert {k: self.e.case_digest(k) for k in others} == others

    @precondition(lambda self: self.e is not None)
    @rule()
    def fines(self) -> None:
        assert self.e is not None
        before = len(self.e.ledger.entries)
        self.e.post_monitoring_fines()
        after = len(self.e.ledger.entries)
        self.e.post_monitoring_fines()  # at most once
        assert len(self.e.ledger.entries) == after >= before

    @invariant()
    def journal_is_sound(self) -> None:
        if self.e is None:
            return
        bal = assert_journal_sound(self.e.ledger.entries)
        for (acct, ccy), v in bal.items():
            assert self.e.ledger.balance(acct, ccy) == v

    @invariant()
    def no_silent_over_credit(self) -> None:
        if self.e is None:
            return
        led = self.e.ledger
        incidents = {i.case_id for i in self.e.incidents if i.kind == "double_refund"}
        for c in self.cases:
            r = led.returned(c.txn_id)
            assert r >= 0
            if r > led.captured(c.txn_id):
                assert c.txn_id in led.over_credited()
                assert any(v.txn_id == c.txn_id for v in led.violations)
                assert c.case_id in incidents
        assert len(led.violations) == len(incidents)

    @invariant()
    def resolved_cases_are_terminal_and_scored_from_the_ledger(self) -> None:
        if self.e is None:
            return
        for c in self.cases:
            out = self.e.outcome(c.case_id)
            if out is None:
                assert c.case_id in self.e.open_cases()
                continue
            assert out.path[-1] is Stage.RESOLVED
            assert out.cardholder_net_minor == self.e.ledger.returned(c.txn_id)
            assert out.fees_usd_minor >= 0
            assert self.e.allowed_actions(c.case_id) == ()

    @invariant()
    def the_log_replays_exactly(self) -> None:
        if self.e is None:
            return
        raw = self.e.events_json()
        again = DisputeEngine.replay(
            self.cases,
            DisputeEngine.events_from_json(raw),
            self.clock,
            merchants=tuple(self.e.merchants.values()),
        )
        assert again.state_digest() == self.e.state_digest()
        assert again.events_json() == raw
        assert today(self.clock) >= min(c.opened_on for c in self.cases)


TestDisputeEngineMachine = DisputeEngineMachine.TestCase
TestDisputeEngineMachine.settings = MACHINE
