"""Ledger correctness verifier.

Anchored to the journal itself, not to the simulator's per-case results: it re-derives
balances from postings and reconciles them with what each case result claims. A
component that misreports its results (see the tampering attacks) shows up here as
a mismatch.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from agent_gate.sim.engine import RunResult
from agent_gate.sim.models import DisputeCase
from agent_gate.verifiers.base import Finding, VerifierReport

NAME = "ledger_correctness"


def verify_ledger(cases: Sequence[DisputeCase], run: RunResult) -> VerifierReport:
    by_case = {c.case_id: c for c in cases}
    findings: list[Finding] = []
    balances: dict[tuple[str, str], int] = defaultdict(int)
    debit_by_case: dict[str, int] = defaultdict(int)
    fee_by_case: dict[str, int] = defaultdict(int)
    over_refunded: set[str] = set()
    for e in run.journal:
        total = sum(p.amount_minor for p in e.postings)
        if total != 0:
            findings.append(
                Finding(
                    case_id=e.case_id or e.txn_id,
                    kind="unbalanced_entry",
                    detail=f"entry {e.entry_id} sums to {total}",
                )
            )
        for p in e.postings:
            balances[(p.account, e.currency)] += p.amount_minor
        if e.case_id is None:
            continue
        case = by_case.get(e.case_id)
        if case is None:
            findings.append(
                Finding(
                    case_id=e.case_id,
                    kind="unknown_case",
                    detail=f"entry {e.entry_id} references an unknown case",
                )
            )
            continue
        if e.kind == "chargeback_debit":
            debit_by_case[e.case_id] += sum(
                p.amount_minor for p in e.postings if p.account.startswith("cardholder:")
            )
            # Invariant: a cardholder can never be credited more than they paid.
            held = balances[(f"cardholder:{case.txn_id}", case.txn_currency)]
            if held > 0 and case.txn_id not in over_refunded:
                over_refunded.add(case.txn_id)
                findings.append(
                    Finding(
                        case_id=e.case_id,
                        kind="over_refund",
                        detail=f"cardholder credited {held} above capture "
                        f"({case.txn_currency} minor units)",
                    )
                )
        elif e.kind == "fee":
            fee_by_case[e.case_id] += sum(
                p.amount_minor for p in e.postings if p.account == "network:fees"
            )
    for ccy in sorted({c for _, c in balances}):
        net = sum(v for (_, c), v in balances.items() if c == ccy)
        if net != 0:
            findings.append(
                Finding(case_id="*", kind="conservation", detail=f"{ccy} balances sum to {net}")
            )
    for r in run.results:
        if (
            debit_by_case[r.case_id] != r.debit_txn_minor
            or fee_by_case[r.case_id] != r.fee_usd_minor
        ):
            findings.append(
                Finding(
                    case_id=r.case_id,
                    kind="result_ledger_mismatch",
                    detail="reported debit/fee differs from journal",
                )
            )
    kinds = [f.kind for f in findings]
    return VerifierReport(
        name=NAME,
        anchored_to="journal postings (double-entry) and capture amounts",
        findings=tuple(findings),
        metrics={
            "entries": float(len(run.journal)),
            "over_refunds": float(kinds.count("over_refund")),
            "mismatches": float(kinds.count("result_ledger_mismatch")),
            "unbalanced": float(kinds.count("unbalanced_entry")),
        },
    )
