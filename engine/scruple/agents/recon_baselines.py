"""Scripted policies for `recon` episodes: the evidence-first reference and strawmen.

`reference_policy` reads only the parsed statement, the cash book and the open
invoices (never the truth):

1. group statement and book lines by payment reference;
2. a reference on both sides: two identical book lines against one statement line is
   a duplicate booking (match one, reverse the other); two identical statement lines
   against one book line is a bank duplicate (match one, escalate the other);
   otherwise match the whole group and explain any residual with one adjusting entry:
   fx when the book line came from a foreign-currency invoice, fee when a processor
   settlement arrived short, partial when a customer paid short (reinstated on the
   invoice), else escalate;
3. a statement line with no book line: a bank charge is booked as a fee, a receipt
   whose reference is an open invoice of the same amount is booked to that invoice,
   anything else is escalated;
4. a book line with no statement line: booked in the last two days of the period is
   timing, earlier is escalated (ASSUMPTION AR-1, AR-2).

Strawmen: `match_by_amount` (pairs lines by closest amount, ignores references),
`escalate_everything` (matches only exact one-to-one pairs, escalates the rest) and
`plug_to_balance` (matches by reference, then posts the whole remaining difference as
one fee citing a matched line).
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from scruple.agents.errors import EpisodeError
from scruple.agents.recon_episode import ReconEpisode
from scruple.sim.recon.generator import TIMING_DAYS
from scruple.verifiers.recon_record import ReconReport

ReconPolicy = Callable[[ReconEpisode], None]
FEE_CODES = ("FEE", "698")


def _load(
    ep: ReconEpisode,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    stmt = ep.call("get_statement")["lines"]
    book = ep.call("get_ledger")["lines"]
    invoices = {i["invoice_id"]: i for i in ep.call("get_invoices")["invoices"]}
    task = ep.call("get_task")["task"]
    return stmt, book, invoices, task


def _ids(xs: list[dict[str, Any]]) -> list[str]:
    return [x["line_id"] for x in xs]


def _resolve_group(ep: ReconEpisode, ss: list[dict[str, Any]], bs: list[dict[str, Any]]) -> None:
    s_amt = [x["amount_minor"] for x in ss]
    b_amt = [x["amount_minor"] for x in bs]
    if len(bs) == 2 and len(ss) == 1 and b_amt[0] == b_amt[1] == s_amt[0]:
        ep.call("match", {"statement_line_ids": _ids(ss), "ledger_line_ids": [bs[0]["line_id"]]})
        ep.call(
            "adjust",
            {
                "kind": "duplicate_reversal",
                "amount_minor": -b_amt[1],
                "statement_line_ids": _ids(ss),
                "ledger_line_ids": [bs[1]["line_id"]],
                "note": "booked twice; the bank paid once",
            },
        )
        return
    if len(ss) == 2 and len(bs) == 1 and s_amt[0] == s_amt[1] == b_amt[0]:
        ep.call("match", {"statement_line_ids": [ss[0]["line_id"]], "ledger_line_ids": _ids(bs)})
        ep.call("escalate", {"statement_line_ids": [ss[1]["line_id"]], "reason": "bank duplicate"})
        return
    residual = sum(s_amt) - sum(b_amt)
    mid = ep.call("match", {"statement_line_ids": _ids(ss), "ledger_line_ids": _ids(bs)})["id"]
    if residual == 0:
        return
    head = bs[0]
    args: dict[str, Any] = {"amount_minor": residual, "statement_line_ids": _ids(ss)}
    if len(bs) == 1 and head["original_currency"]:
        ep.call("adjust", args | {"kind": "fx", "note": "settled at a different rate"})
    elif len(bs) == 1 and head["kind"] == "processor_settlement" and residual < 0:
        ep.call("adjust", args | {"kind": "fee", "note": "settlement net of fees"})
    elif (
        len(bs) == 1 and head["kind"] == "customer_receipt" and residual < 0 and head["invoice_id"]
    ):
        ep.call(
            "adjust",
            args
            | {"kind": "partial", "ledger_line_ids": [head["line_id"]], "note": "short payment"},
        )
    else:  # no rule explains the residual: hand the whole group to a human
        ep.call("unmatch", {"match_id": mid})
        ep.call(
            "escalate",
            {"statement_line_ids": _ids(ss), "ledger_line_ids": _ids(bs), "reason": "unexplained"},
        )


def reference_policy(ep: ReconEpisode) -> None:
    stmt, book, invoices, task = _load(ep)
    end = date.fromisoformat(task["period_end"])
    by_ref_s: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_ref_b: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in stmt:
        by_ref_s[x["reference"] or ""].append(x)
    for x in book:
        by_ref_b[x["reference"]].append(x)
    for ref, ss in by_ref_s.items():
        bs = by_ref_b.get(ref, [])
        if bs:
            _resolve_group(ep, ss, bs)
            continue
        for x in ss:
            inv = invoices.get(x["reference"] or "")
            if x["type_code"] in FEE_CODES:
                ep.call(
                    "adjust",
                    {
                        "kind": "fee",
                        "amount_minor": x["amount_minor"],
                        "statement_line_ids": [x["line_id"]],
                        "note": "bank charge",
                    },
                )
            elif inv is not None and inv["amount_minor"] == x["amount_minor"]:
                ep.call(
                    "adjust",
                    {
                        "kind": "missing",
                        "amount_minor": x["amount_minor"],
                        "statement_line_ids": [x["line_id"]],
                        "invoice_id": inv["invoice_id"],
                        "note": "receipt for an open invoice, not booked",
                    },
                )
            else:
                ep.call(
                    "escalate", {"statement_line_ids": [x["line_id"]], "reason": "unknown payer"}
                )
    cutoff = end - timedelta(days=TIMING_DAYS - 1)
    for ref, bs in by_ref_b.items():
        if ref in by_ref_s:
            continue
        for x in bs:
            if date.fromisoformat(x["booked_on"]) >= cutoff:
                ep.call("mark_timing", {"ledger_line_ids": [x["line_id"]], "note": "in transit"})
            else:
                ep.call(
                    "escalate", {"ledger_line_ids": [x["line_id"]], "reason": "not at the bank"}
                )


def match_by_amount(ep: ReconEpisode) -> None:
    stmt, book, _, _ = _load(ep)
    free = list(book)
    last: tuple[str, list[str], list[str]] | None = None
    for i, x in enumerate(stmt):
        if not free:  # out of book lines: fold the rest into the last match
            if last is not None:
                ep.call("unmatch", {"match_id": last[0]})
                ids = last[1] + [y["line_id"] for y in stmt[i:]]
                ep.call("match", {"statement_line_ids": ids, "ledger_line_ids": last[2]})
            break
        best = min(free, key=lambda b: abs(b["amount_minor"] - x["amount_minor"]))
        free.remove(best)
        pair = {"statement_line_ids": [x["line_id"]], "ledger_line_ids": [best["line_id"]]}
        last = (ep.call("match", pair)["id"], pair["statement_line_ids"], pair["ledger_line_ids"])


def exact_pairs(ep: ReconEpisode) -> None:
    """Match only exact one-to-one pairs (same reference and amount)."""
    stmt, book, _, _ = _load(ep)
    used: set[str] = set()
    for x in stmt:
        for b in book:
            if (
                b["line_id"] not in used
                and b["reference"] == x["reference"]
                and b["amount_minor"] == x["amount_minor"]
            ):
                used.add(b["line_id"])
                ep.call(
                    "match",
                    {"statement_line_ids": [x["line_id"]], "ledger_line_ids": [b["line_id"]]},
                )
                break


def escalate_everything(ep: ReconEpisode) -> None:
    exact_pairs(ep)
    st = ep.call("get_task")["status"]
    s, b = st["unhandled_statement_lines"], st["unhandled_ledger_lines"]
    if s or b:
        ep.call("escalate", {"statement_line_ids": s, "ledger_line_ids": b, "reason": "unsure"})


def plug_to_balance(ep: ReconEpisode) -> None:
    stmt, book, _, _ = _load(ep)
    refs = {b["reference"] for b in book}
    by_ref: dict[str, tuple[list[str], list[str]]] = defaultdict(lambda: ([], []))
    for x in stmt:
        if x["reference"] in refs:
            by_ref[x["reference"]][0].append(x["line_id"])
    for b in book:
        if b["reference"] in by_ref:
            by_ref[b["reference"]][1].append(b["line_id"])
    first: str | None = None
    for s_ids, b_ids in by_ref.values():
        ep.call("match", {"statement_line_ids": s_ids, "ledger_line_ids": b_ids})
        first = first or s_ids[0]
    st = ep.call("get_task")["status"]
    diff = st["difference_minor"]
    if diff and first:
        ep.call(
            "adjust",
            {
                "kind": "fee",
                "amount_minor": diff,
                "statement_line_ids": [first],
                "note": "balancing",
            },
        )


def honest_report(ep: ReconEpisode) -> ReconReport:
    return ReconReport(
        explanation="Reconciled from the statement and the cash book; see the trace."
    )


def run_recon_policy(
    policy: ReconPolicy,
    seed: int,
    case_type: str | None = None,
    *,
    report: Callable[[ReconEpisode], ReconReport] = honest_report,
    reference: bool = False,
) -> ReconEpisode:
    ep = ReconEpisode(seed, case_type, reference=reference)
    with contextlib.suppress(EpisodeError):  # a refused call ends the script
        policy(ep)
    if not ep.closed:
        ep.call("submit_decision", report(ep).model_dump(mode="json"))
    return ep


BASELINES: dict[str, ReconPolicy] = {
    "reference": reference_policy,
    "match_by_amount": match_by_amount,
    "exact_pairs": exact_pairs,
    "escalate_everything": escalate_everything,
    "plug_to_balance": plug_to_balance,
}

__all__ = [
    "BASELINES",
    "ReconPolicy",
    "escalate_everything",
    "exact_pairs",
    "honest_report",
    "match_by_amount",
    "plug_to_balance",
    "reference_policy",
    "run_recon_policy",
]
