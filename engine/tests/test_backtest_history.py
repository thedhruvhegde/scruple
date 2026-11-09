"""Backtest (154): loading Stripe, Adyen and CSV exports into a history."""

from __future__ import annotations

import contextlib
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.backtest import from_records, load_adyen, load_stripe, load_tabular
from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.ingest.adyen import item_signature
from scruple.ingest.errors import IngestError, SchemaMismatchError, SignatureError
from scruple.ingest.ledger import LedgerConflictError, LedgerLine, LedgerTransaction
from scruple.ingest.models import CaseState
from scruple.ingest.tabular import ColumnMapping, ImportConfig, StatusTarget
from scruple.sim.models import Stage
from tests.test_backtest_view import case

STRIPE = json.loads(
    (Path(__file__).parent / "fixtures" / "stripe" / "fixtures3.subset.json").read_text()
)["resources"]
KEY = "00" * 32


def real_dispute(i: int, **update: Any) -> dict[str, Any]:
    """An edited copy of the vendored real Stripe dispute fixture."""
    d: dict[str, Any] = copy.deepcopy(STRIPE["dispute"])
    d.update(id=f"dp_fixture{i}", charge=f"ch_fixture{i}", **update)
    return d


def test_stripe_shapes_array_list_and_jsonl() -> None:
    docs = [real_dispute(i) for i in range(3)]
    a = load_stripe(json.dumps(docs), label="x")
    b = load_stripe(
        json.dumps({"object": "list", "data": docs, "has_more": False, "url": "/"}), label="x"
    )
    c = load_stripe("\n".join(json.dumps(d) for d in docs), label="x")
    d = load_stripe(json.dumps(docs[0]), label="x")
    assert a.cases == b.cases == c.cases and len(a.cases) == 3 and len(d.cases) == 1
    assert not a.synthetic and a.label == "x" and a.format == "stripe"


def test_stripe_list_with_more_pages_is_refused() -> None:
    with pytest.raises(SchemaMismatchError, match="more pages"):
        load_stripe(json.dumps({"object": "list", "data": [], "has_more": True}), label="x")


def test_stripe_balance_transactions_become_ledger_postings() -> None:
    bt = copy.deepcopy(STRIPE["balance_transaction"])
    bt.update(amount=-1000, fee=1500, net=-2500, currency="usd")
    d = real_dispute(1, status="lost", balance_transactions=[bt])
    h = load_stripe(json.dumps([d]), label="x")
    hc = h.cases[0]
    assert len(hc.ledger) == 2  # debit and fee
    assert h.ledger().balance("merchant", "USD") == -2500


def test_synthetic_records_are_labelled_and_never_mixed() -> None:
    syn = stripe_disputes(DEFAULT_PROFILE, 20, 1)
    h = load_stripe(json.dumps(syn), label="synthetic.json")
    assert h.synthetic and h.label.startswith("SYNTHETIC")
    with pytest.raises(SchemaMismatchError, match="mixes"):
        load_stripe(json.dumps([*syn, real_dispute(1)]), label="x")


def test_stripe_live_mode_refused_and_bad_records_raise() -> None:
    with pytest.raises(IngestError):
        load_stripe(json.dumps([real_dispute(1, livemode=True)]), label="x")
    with pytest.raises(IngestError):
        load_stripe(json.dumps([1, 2]), label="x")
    with pytest.raises(IngestError):
        load_stripe(b"\xff\xfe", label="x")


@settings(max_examples=30)
@given(st.binary(max_size=300))
def test_stripe_loader_raises_only_ingest_errors(data: bytes) -> None:
    with contextlib.suppress(IngestError):
        load_stripe(data, label="fuzz")


def test_duplicate_case_ids_and_orphan_postings_are_errors() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        from_records([case(1), case(1)], label="x")
    orphan = LedgerTransaction(
        external_id="t1",
        occurred_at=case(1).opened_at,
        currency="USD",
        lines=(
            LedgerLine(account="merchant", amount_minor=-5),
            LedgerLine(account="b", amount_minor=5),
        ),
        metadata={"case": "dp_404"},
    )
    with pytest.raises(ValueError, match="name no case"):
        from_records([case(1)], ledger=[orphan], label="x")
    ok = orphan.model_copy(update={"metadata": {"case": "dp_1"}})
    h = from_records([case(1)], ledger=[ok], label="x")
    assert h.cases[0].ledger == (ok,)
    clash = ok.model_copy(
        update={
            "lines": (
                LedgerLine(account="merchant", amount_minor=-6),
                LedgerLine(account="b", amount_minor=6),
            )
        }
    )
    with pytest.raises(LedgerConflictError):
        from_records(
            [case(1), case(2)],
            ledger=[ok, clash.model_copy(update={"metadata": {"case": "dp_2"}})],
            label="x",
        )


def test_cases_are_in_time_order() -> None:
    h = from_records([case(3), case(1), case(2)], label="x")
    assert [hc.case.external_id for hc in h.cases] == ["dp_1", "dp_2", "dp_3"]


# --- Adyen --------------------------------------------------------------------------


def item(
    code: str, date: str, psp: str = "PSP1", orig: str = "PAY1", **extra: Any
) -> dict[str, Any]:
    it: dict[str, Any] = {
        "amount": {"currency": "EUR", "value": 2500},
        "eventCode": code,
        "eventDate": date,
        "merchantAccountCode": "TestMerchant",
        "merchantReference": "order-1",
        "originalReference": orig,
        "pspReference": psp,
        "success": "true",
        "additionalData": dict(extra),
    }
    it["additionalData"]["hmacSignature"] = item_signature(it, KEY)
    return {"NotificationRequestItem": it}


def envelope(*items: dict[str, Any]) -> dict[str, Any]:
    return {"live": "false", "notificationItems": list(items)}


def test_adyen_events_fold_into_one_case_per_payment() -> None:
    payloads = [
        envelope(
            item(
                "CHARGEBACK",
                "2026-01-05T10:00:00+00:00",
                defensePeriodEndsAt="2026-01-25T00:00:00+00:00",
            )
        ),
        envelope(
            item("INFORMATION_SUPPLIED", "2026-01-10T10:00:00+00:00"),
            item("CHARGEBACK", "2026-01-06T10:00:00+00:00", psp="PSP2", orig="PAY2"),
        ),
        envelope(item("CHARGEBACK_REVERSED", "2026-02-10T10:00:00+00:00")),
        envelope(item("AUTHORISATION", "2026-01-01T10:00:00+00:00", psp="X", orig="X")),
    ]
    h = load_adyen("\n".join(json.dumps(p) for p in payloads), label="adyen", hmac_keys=[KEY])
    by = {hc.case.payment_ref: hc.case for hc in h.cases}
    won = by["PAY1"]
    assert won.state is CaseState.WON and won.evidence_submitted
    assert won.opened_at.day == 5 and won.evidence_due_at is not None
    assert by["PAY2"].state is CaseState.OPEN and not by["PAY2"].evidence_submitted
    assert h.skipped == {"adyen: not a dispute event": 1}


def test_adyen_needs_keys_and_checks_signatures() -> None:
    p = envelope(item("CHARGEBACK", "2026-01-05T10:00:00+00:00"))
    with pytest.raises(ValueError):
        load_adyen(json.dumps(p), label="x")
    with pytest.raises(SignatureError):
        load_adyen(json.dumps(p), label="x", hmac_keys=["11" * 32])
    assert len(load_adyen(json.dumps([p]), label="x", verify=False).cases) == 1


# --- CSV ------------------------------------------------------------------------------


CFG = ImportConfig(
    columns=ColumnMapping(
        external_id="id",
        payment_ref="charge",
        amount="amount",
        currency="ccy",
        status="status",
        reason="reason",
        opened_at="opened",
    ),
    status_map={
        "won": StatusTarget(stage=Stage.RESOLVED, state=CaseState.WON),
        "lost_contested": StatusTarget(stage=Stage.REPRESENTMENT, state=CaseState.LOST),
        "accepted": StatusTarget(stage=Stage.RESOLVED, state=CaseState.ACCEPTED),
    },
    reason_map={"fraud": None, "missing": None},
)


def test_csv_history_and_row_errors() -> None:
    text = (
        "id,charge,amount,ccy,status,reason,opened\n"
        "a,p1,10.00,USD,won,fraud,2026-01-01\n"
        "b,p2,12.00,USD,lost_contested,missing,2026-01-02\n"
        "c,p3,1.00,USD,bogus,fraud,2026-01-03\n"
    )
    h = load_tabular(text, CFG, label="csv")
    assert len(h.cases) == 2 and h.skipped == {"row errors": 1}
    jl = "\n".join(
        json.dumps(
            {
                "id": i,
                "charge": i,
                "amount": "5.00",
                "ccy": "USD",
                "status": "accepted",
                "reason": "fraud",
                "opened": "2026-01-04",
            }
        )
        for i in ("x", "y")
    )
    assert load_tabular(jl, CFG, label="jl", jsonl=True).format == "jsonl"
