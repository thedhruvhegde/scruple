"""The run-level ledger verifier and realized costs (the original gate verifiers).

Mutation testing found most of `ledger.py` and `costs.py` unchecked by the verifier
unit tests (docs/TESTING_MUTATION.md): every finding kind, its case attribution and
the metrics are pinned here on a real simulator run and on tampered copies of it.
"""

from __future__ import annotations

from collections import Counter
from functools import cache

import pytest

from scruple.domain import Action
from scruple.sim.engine import RunResult, Simulator
from scruple.sim.generator import generate_cases
from scruple.sim.ledger import JournalEntry, Posting
from scruple.sim.library import baseline_policy
from scruple.sim.models import ESCALATION_COST_USD, DisputeCase, to_usd
from scruple.verifiers.base import Finding, VerifierReport, new_findings
from scruple.verifiers.costs import realized_costs
from scruple.verifiers.ledger import NAME, verify_ledger


@cache
def world() -> tuple[tuple[DisputeCase, ...], RunResult]:
    cases = tuple(generate_cases(1, 300))
    sim = Simulator(cases)
    return cases, sim.run(baseline_policy())


def _journal(run: RunResult, entries: list[JournalEntry]) -> RunResult:
    return run.model_copy(update={"journal": tuple(entries)})


def _first(run: RunResult, kind: str, *, case: bool = True) -> int:
    return next(
        i for i, e in enumerate(run.journal) if e.kind == kind and (e.case_id is not None) is case
    )


def _skew(e: JournalEntry, by: int = 1) -> JournalEntry:
    p0 = e.postings[0]
    posts = (p0.model_copy(update={"amount_minor": p0.amount_minor + by}), *e.postings[1:])
    return e.model_copy(update={"postings": posts})


def test_honest_run_is_clean_and_metrics_count_entries() -> None:
    cases, run = world()
    rep = verify_ledger(cases, run)
    assert rep.name == NAME == "ledger_correctness"
    assert rep.findings == ()
    assert rep.metrics == {
        "entries": float(len(run.journal)),
        "over_refunds": 0.0,
        "mismatches": 0.0,
        "unbalanced": 0.0,
    }
    # the journal mixes entries without a case (captures, early refunds) and with one
    assert any(e.case_id is None for e in run.journal[run.opening_entries :]) or (
        run.journal[0].case_id is None
    )


@pytest.mark.parametrize("by", [1, -1, 250])
def test_unbalanced_case_entry(by: int) -> None:
    cases, run = world()
    i = _first(run, "chargeback_debit")
    e = run.journal[i]
    entries = list(run.journal)
    entries[i] = _skew(e, by)
    rep = verify_ledger(cases, _journal(run, entries))
    unbalanced = [f for f in rep.findings if f.kind == "unbalanced_entry"]
    assert unbalanced == [
        Finding(
            case_id=str(e.case_id),
            kind="unbalanced_entry",
            detail=f"entry {e.entry_id} sums to {by}",
        )
    ]
    assert Finding(
        case_id="*", kind="conservation", detail=f"{e.currency} balances sum to {by}"
    ) in (rep.findings)
    assert rep.metrics["unbalanced"] == 1.0
    others = {f.kind for f in rep.findings} - {"unbalanced_entry", "conservation"}
    assert others <= {"result_ledger_mismatch", "over_refund"}


def test_unbalanced_entry_without_a_case_names_the_transaction() -> None:
    cases, run = world()
    i = _first(run, "capture", case=False)
    e = run.journal[i]
    entries = list(run.journal)
    entries[i] = _skew(e)
    rep = verify_ledger(cases, _journal(run, entries))
    assert rep.findings[0] == Finding(
        case_id=e.txn_id, kind="unbalanced_entry", detail=f"entry {e.entry_id} sums to 1"
    )


def test_conservation_is_per_currency() -> None:
    cases, run = world()
    i = next(i for i, e in enumerate(run.journal) if e.currency != "USD")
    entries = list(run.journal)
    entries[i] = _skew(run.journal[i], 7)
    rep = verify_ledger(cases, _journal(run, entries))
    cons = [f for f in rep.findings if f.kind == "conservation"]
    assert [f.detail for f in cons] == [f"{run.journal[i].currency} balances sum to 7"]


def test_entry_for_an_unknown_case_is_reported_and_not_counted() -> None:
    cases, run = world()
    i = _first(run, "chargeback_debit")
    e = run.journal[i]
    entries = list(run.journal)
    entries[i] = e.model_copy(update={"case_id": "case-nope"})
    rep = verify_ledger(cases, _journal(run, entries))
    kinds = Counter((f.case_id, f.kind) for f in rep.findings)
    assert kinds[("case-nope", "unknown_case")] == 1
    assert kinds[(str(e.case_id), "result_ledger_mismatch")] == 1  # its debit went missing
    unknown = next(f for f in rep.findings if f.kind == "unknown_case")
    assert unknown.detail == f"entry {e.entry_id} references an unknown case"
    assert rep.metrics["mismatches"] == 1.0


def test_unknown_case_entries_later_in_the_journal_are_still_checked() -> None:
    cases, run = world()
    debits = [i for i, e in enumerate(run.journal) if e.kind == "chargeback_debit"]
    entries = list(run.journal)
    entries[debits[0]] = entries[debits[0]].model_copy(update={"case_id": "case-nope"})
    last = run.journal[debits[-1]]
    entries[debits[-1]] = _skew(last)
    rep = verify_ledger(cases, _journal(run, entries))
    assert any(f.kind == "unbalanced_entry" and f.case_id == last.case_id for f in rep.findings)


def _extra_debit(run: RunResult, i: int, times: int = 1) -> list[JournalEntry]:
    e = run.journal[i]
    top = max(x.entry_id for x in run.journal)
    extra = [e.model_copy(update={"entry_id": top + k + 1}) for k in range(times)]
    return [*run.journal, *extra]


@pytest.mark.parametrize("times", [1, 2])
def test_double_chargeback_is_one_over_refund_per_transaction(times: int) -> None:
    cases, run = world()
    i = _first(run, "chargeback_debit")
    e = run.journal[i]
    case = next(c for c in cases if c.case_id == e.case_id)
    rep = verify_ledger(cases, _journal(run, _extra_debit(run, i, times)))
    over = [f for f in rep.findings if f.kind == "over_refund"]
    assert len(over) == 1 and over[0].case_id == e.case_id
    acct = f"cardholder:{case.txn_id}"
    held = sum(p.amount_minor for x in run.journal for p in x.postings if p.account == acct)
    held += sum(p.amount_minor for p in e.postings if p.account == acct)
    assert held > 0
    assert (
        over[0].detail
        == f"cardholder credited {held} above capture ({case.txn_currency} minor units)"
    )
    assert rep.metrics["over_refunds"] == 1.0
    assert rep.metrics["mismatches"] == 1.0  # reported debit no longer matches


def test_a_full_debit_with_no_refund_is_not_an_over_refund() -> None:
    cases, run = world()
    assert not [f for f in verify_ledger(cases, run).findings if f.kind == "over_refund"]


@pytest.mark.parametrize("field", ["debit_txn_minor", "fee_usd_minor"])
@pytest.mark.parametrize("delta", [1, -1])
def test_misreported_result_is_a_mismatch_for_that_case_only(field: str, delta: int) -> None:
    cases, run = world()
    j = next(j for j, r in enumerate(run.results) if r.debit_txn_minor > 0)
    res = list(run.results)
    res[j] = res[j].model_copy(update={field: getattr(res[j], field) + delta})
    rep = verify_ledger(cases, run.model_copy(update={"results": tuple(res)}))
    assert rep.findings == (
        Finding(
            case_id=res[j].case_id,
            kind="result_ledger_mismatch",
            detail="reported debit/fee differs from journal",
        ),
    )


def test_fees_count_only_network_fee_postings_of_fee_entries() -> None:
    cases, run = world()
    i = _first(run, "fee")
    e = run.journal[i]
    renamed = tuple(
        p.model_copy(update={"account": "network:other"}) if p.account == "network:fees" else p
        for p in e.postings
    )
    for edit in ({"postings": renamed}, {"kind": "fee_waived"}):
        entries = list(run.journal)
        entries[i] = e.model_copy(update=edit)
        rep = verify_ledger(cases, _journal(run, entries))
        assert [(f.case_id, f.kind) for f in rep.findings] == [
            (e.case_id, "result_ledger_mismatch")
        ]


def test_debits_count_only_cardholder_postings() -> None:
    cases, run = world()
    i = _first(run, "chargeback_debit")
    e = run.journal[i]
    posts = (
        *e.postings,
        Posting(account="suspense:x", amount_minor=5),
        Posting(account="suspense:y", amount_minor=-5),
    )
    entries = list(run.journal)
    entries[i] = e.model_copy(update={"postings": posts})
    assert verify_ledger(cases, _journal(run, entries)).findings == ()


def test_new_findings_are_the_candidates_extra_keys() -> None:
    a = Finding(case_id="c1", kind="over_refund", detail="x")
    b = Finding(case_id="c2", kind="over_refund", detail="y")
    base = VerifierReport(name="n", anchored_to="t", findings=(a,), metrics={})
    cand = VerifierReport(
        name="n", anchored_to="t", findings=(a.model_copy(update={"detail": "z"}), b), metrics={}
    )
    assert new_findings(base, cand) == [b]
    assert cand.keys == {("c1", "over_refund"), ("c2", "over_refund")}


# --- realized costs ---------------------------------------------------------------------


def _expected_costs(run: RunResult) -> dict[str, int]:
    paid: Counter[str] = Counter()
    for e in run.journal:
        for p in e.postings:
            if e.case_id is not None and p.account.startswith("merchant:") and p.amount_minor < 0:
                paid[e.case_id] += to_usd(-p.amount_minor, e.currency)
    return {
        r.case_id: paid[r.case_id] + (ESCALATION_COST_USD if r.action is Action.ESCALATE else 0)
        for r in run.results
    }


def test_realized_costs_from_merchant_outflows_plus_escalation() -> None:
    _, run = world()
    got = realized_costs(run)
    assert got == _expected_costs(run)
    esc = [r for r in run.results if r.action is Action.ESCALATE]
    assert esc and all(got[r.case_id] >= ESCALATION_COST_USD for r in esc)
    flat = [r for r in run.results if r.action is not Action.ESCALATE and got[r.case_id] == 0]
    assert flat  # a won contest costs nothing: no escalation charge leaks onto it
    assert set(got) == {r.case_id for r in run.results}


def test_realized_costs_match_reported_costs() -> None:
    _, run = world()
    got = realized_costs(run)
    assert all(got[r.case_id] == r.cost_usd_minor for r in run.results)


def test_realized_costs_ignore_inflows_other_accounts_and_caseless_entries() -> None:
    _, run = world()
    i = _first(run, "chargeback_debit")
    e = run.journal[i]
    cid = str(e.case_id)
    before = realized_costs(run)[cid]
    noise = [
        e.model_copy(
            update={
                "entry_id": 10_001,
                "postings": (
                    Posting(account="merchant:m", amount_minor=500),
                    Posting(account="cardholder:t", amount_minor=-500),
                ),
            }
        ),
        e.model_copy(
            update={
                "entry_id": 10_002,
                "postings": (
                    Posting(account="network:fees", amount_minor=-500),
                    Posting(account="cardholder:t", amount_minor=500),
                ),
            }
        ),
        e.model_copy(update={"entry_id": 10_003, "case_id": None}),
    ]
    assert realized_costs(_journal(run, [*run.journal, *noise]))[cid] == before
    zero = e.model_copy(
        update={
            "entry_id": 10_004,
            "postings": (
                Posting(account="merchant:m", amount_minor=0),
                Posting(account="cardholder:t", amount_minor=0),
            ),
        }
    )
    assert realized_costs(_journal(run, [*run.journal, zero]))[cid] == before
    one = e.model_copy(
        update={
            "entry_id": 10_005,
            "postings": (
                Posting(account="merchant:m", amount_minor=-1),
                Posting(account="cardholder:t", amount_minor=1),
            ),
        }
    )
    after = realized_costs(_journal(run, [*run.journal, one]))[cid]
    assert after == before + to_usd(1, e.currency)


def test_realized_costs_accumulate_over_entries() -> None:
    _, run = world()
    i = _first(run, "chargeback_debit")
    e = run.journal[i]
    cid = str(e.case_id)
    before = realized_costs(run)[cid]
    again = e.model_copy(update={"entry_id": 10_010})
    out = sum(
        to_usd(-p.amount_minor, e.currency)
        for p in e.postings
        if p.account.startswith("merchant:") and p.amount_minor < 0
    )
    assert realized_costs(_journal(run, [*run.journal, again]))[cid] == before + out > before
