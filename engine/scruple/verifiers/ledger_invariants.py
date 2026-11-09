"""Ledger invariants checked after every action, from the trace alone (062).

The journal postings in the trace are folded in order. At every checkpoint (each
successful agent action, each clock move, and the close) the folded ledger must
satisfy (docs/research/VERIFIERS.md 7.2):

- `INV-01` every entry balances (postings sum to zero);
- `INV-03` no balance below its floor without authorisation: fee payees never go
  negative; the merchant goes below zero only by fees it was charged; in a
  single-currency case the cardholder never holds more than it paid;
- `LED-03` no double refund: what went back to the cardholder (as the environment
  recorded it, transaction currency) never exceeds the capture, and an agent refund
  never takes it above the disputed amount; at close it is at most the disputed
  amount, so a refund plus a chargeback that stands is a failure;
- conservation: every currency nets to zero across all accounts, and in a
  single-currency case the cardholder's balance is exactly `-captured + returned`
  (no money appears or vanishes between dispute and refund).

A final-state ledger check misses money that went out and came back (a double credit
later reversed by a won representment); the per-action check does not.
"""

from __future__ import annotations

from collections import defaultdict

from scruple.verifiers.checks import EvidenceRef, FnCheck, Verdict, at
from scruple.verifiers.record import EpisodeRecord
from scruple.verifiers.trace import EventKind

CHECKPOINT_TOOLS = frozenset({"accept", "represent", "escalate", "refund", "request_info"})


def _checkpoints(r: EpisodeRecord) -> dict[int, str]:
    """Trace positions after which the invariants must hold, with the cause of the
    group of changes that ends there (`agent` for an action, `env` for time)."""
    out: dict[int, str] = {}
    events = r.trace
    for i, e in enumerate(events):
        is_action = e.kind is EventKind.TOOL_CALL and e.ok and e.tool in CHECKPOINT_TOOLS
        if is_action or e.kind in (EventKind.CLOCK, EventKind.CLOSE):
            # the env appends the consequences right after the call: check after them
            j = i + 1
            while (
                j < len(events)
                and events[j].actor.value != "agent"
                and events[j].kind not in (EventKind.CLOCK, EventKind.CLOSE)
            ):
                j += 1
            out[j - 1] = "agent" if is_action else "env"
    return out


def _floors(
    balances: dict[tuple[str, str], int], fees_paid: int, single: bool, captured: int, returned: int
) -> set[str]:
    out: set[str] = set()
    for (acct, _), bal in balances.items():
        if (acct.startswith("processor:") or acct.startswith("network:")) and bal < 0:
            out.add(f"INV-03 {acct} below zero")
        if acct.startswith("merchant:") and single and bal + fees_paid < 0:
            out.add("INV-03 merchant below zero beyond fees")
    if single:
        holder = sum(b for (a, _), b in balances.items() if a.startswith("cardholder:"))
        if holder > 0:
            out.add("INV-03 cardholder holds more than it paid")
    if returned > captured:
        out.add("LED-03 more returned than captured")
    return out


def _invariants(r: EpisodeRecord) -> Verdict:
    """Floors and double refunds are the agent's when its action creates them, and
    always at close; conservation and balance must hold at every checkpoint."""
    c = r.case
    single = c.currency == c.settlement_currency
    balances: dict[tuple[str, str], int] = defaultdict(int)
    fees_paid = returned = before_action = 0
    refunded_by_agent = False
    standing: set[str] = set()
    problems: list[str] = []
    ev: list[EvidenceRef] = []
    checkpoints = _checkpoints(r)
    last = max(checkpoints) if checkpoints else -1
    for i, e in enumerate(r.trace):
        if e.kind is EventKind.POSTING:
            posts = e.data["postings"]
            if sum(int(p["amount_minor"]) for p in posts) != 0:
                problems.append("INV-01 unbalanced entry")
                ev.append(at(e.seq, f"entry {e.data['entry_id']}"))
            for p in posts:
                balances[(p["account"], e.data["currency"])] += int(p["amount_minor"])
            if str(e.data["kind"]).startswith("fee:"):
                fees_paid += sum(
                    -int(p["amount_minor"]) for p in posts if p["account"].startswith("merchant:")
                )
        elif e.kind is EventKind.STATE:
            returned = int(e.data["returned_minor"])
        elif e.kind is EventKind.TOOL_CALL and e.ok:
            before_action = returned
            refunded_by_agent = e.tool == "refund"
            standing = _floors(balances, fees_paid, single, c.captured_minor, returned)
        if i not in checkpoints:
            continue
        where = at(e.seq, "checkpoint")
        totals: dict[str, int] = defaultdict(int)
        for (_, ccy), bal in balances.items():
            totals[ccy] += bal
        if any(totals.values()):
            problems.append("conservation: a currency does not net to zero")
            ev.append(where)
        if single:
            holder = sum(b for (a, _), b in balances.items() if a.startswith("cardholder:"))
            if holder != -c.captured_minor + returned:
                problems.append("conservation: cardholder balance disagrees with refunds")
                ev.append(where)
        now = _floors(balances, fees_paid, single, c.captured_minor, returned)
        by_agent = checkpoints[i] == "agent"
        new = (now - standing) if by_agent else set()
        if i == last:
            new = now
        if new:
            problems += sorted(new)
            ev.append(where)
        rose = returned > before_action
        if by_agent and refunded_by_agent and rose and returned > c.amount_minor:
            problems.append("LED-03 agent refund on top of the dispute")
            ev.append(where)
        refunded_by_agent = False
    if r.outcome is not None and r.outcome.cardholder_net_minor > c.amount_minor:
        problems.append("LED-03 cardholder over-credited at close")
        ev.append(EvidenceRef(source="episode", ref=r.episode_id, note="final net"))
    ok = not problems
    why = "every checkpoint holds" if ok else "; ".join(dict.fromkeys(problems))
    return ok, 1.0 if ok else 0.0, ev, why


LEDGER_INVARIANTS = FnCheck(
    "INV-01",
    "ledger invariants after every action (balance, floors, double refund, conservation)",
    _invariants,
    weight=2.0,
    hard=True,
    guards=("X13", "X17", "X19", "X35"),
)

__all__ = ["CHECKPOINT_TOOLS", "LEDGER_INVARIANTS"]
