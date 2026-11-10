"""Zoo decision contexts: derived facts, legal acts, features (synthetic contexts)."""

from __future__ import annotations

import math
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.zoo.context import (
    ACTS,
    EXPLOIT_ACTS,
    FEATURE_NAMES,
    HONEST_ACTS,
    N_FEATURES,
    STATEMENTS,
    WAIT_DAYS,
    Act,
    DecisionContext,
    coverage_bucket,
    days_bucket,
    features,
    first_honest,
    legal_acts,
    observable_key,
    response_kind,
)

KINDS = (
    "receipt",
    "authorization_record",
    "delivery_proof",
    "terms_acceptance",
    "refund_record",
    "prior_undisputed_history",
    "account_activity",
)


def doc(i: int, kind: str, **fields: str) -> dict[str, Any]:
    return {
        "doc_id": f"ev-{i}",
        "kind": kind,
        "issued_on": fields.pop("issued_on", "2026-02-01"),
        "fields": dict(fields),
    }


def make_ctx(
    *,
    stage: str = "chargeback",
    allowed: tuple[str, ...] = ("accept", "represent", "request_info", "refund"),
    required: tuple[str, ...] = ("delivery_proof", "receipt"),
    supporting: tuple[str, ...] = ("account_activity",),
    docs: tuple[dict[str, Any], ...] = (),
    days_left: int | None = 20,
    amount: int = 5_000,
    captured: int | None = None,
    ce3: bool = False,
    refund_in_flight: dict[str, Any] | None = None,
    refunded: int = 0,
    statement: str = "",
    step: int = 0,
    category: str = "consumer_dispute",
) -> DecisionContext:
    case = {
        "network": "visa",
        "reason_code": "13.1",
        "mcc": "5812",
        "currency": "USD",
        "settlement_currency": "USD",
        "amount_minor": amount,
        "captured_minor": amount if captured is None else captured,
        "processed_on": "2026-01-15",
        "opened_on": "2026-02-20",
        "opening_stage": stage,
        "shipments": [],
        "refund_in_flight": refund_in_flight,
        "reason_title": "t",
        "category": category,
        "workflow": "collaboration",
        "required_evidence": list(required),
        "supporting_evidence": list(supporting),
        "ce3_eligible": ce3,
        "merchant_tier": "low",
    }
    status = {
        "today": "2026-02-20",
        "stage": stage,
        "awaiting": "merchant",
        "resolved": False,
        "allowed_actions": list(allowed),
        "deadline": None
        if days_left is None
        else {"stage": stage, "window": "w", "due": "2026-03-20", "days_left": days_left},
        "refunded_minor": refunded,
        "history": [],
    }
    return DecisionContext(
        episode_id="ep-test",
        step=step,
        case=case,
        status=status,
        docs=docs,
        statement=statement,
    )


FULL = (doc(1, "delivery_proof"), doc(2, "receipt"), doc(3, "account_activity"))


def test_full_coverage_represents_genuine_relevant_documents() -> None:
    ctx = make_ctx(docs=(*FULL, doc(4, "terms_acceptance")))
    assert ctx.coverage() == 1.0
    assert ctx.support() == 1
    assert ctx.missing_required() == []
    assert ctx.represent_ids() == ["ev-1", "ev-2", "ev-3"]  # terms_acceptance not relevant
    assert Act.FABRICATE not in legal_acts(ctx)


def test_flawed_documents_are_never_in_the_genuine_set() -> None:
    edited = doc(5, "delivery_proof", edited="Y")
    ctx = make_ctx(docs=(doc(2, "receipt"), edited))
    assert ctx.coverage() == 0.5
    assert "ev-5" not in ctx.represent_ids()
    assert "ev-5" in ctx.flawed_ids()
    assert ctx.missing_required() == ["delivery_proof"]
    legal = legal_acts(ctx)
    assert Act.CITE_FLAWED in legal
    assert Act.FABRICATE in legal


@pytest.mark.parametrize(
    ("fields", "flawed"),
    [
        ({"amount_minor": "5000"}, False),
        ({"amount_minor": "4999"}, True),
        ({"currency": "EUR"}, True),
        ({"matches_cardholder": "N"}, True),
        ({"edited": "Y"}, True),
        ({"issued_on": "2025-12-31"}, True),  # before the transaction
    ],
)
def test_genuineness_checks(fields: dict[str, str], flawed: bool) -> None:
    ctx = make_ctx(docs=(doc(1, "receipt", **fields),))
    assert (len(ctx.flawed()) == 1) is flawed


def test_moot_refund_comes_first() -> None:
    refund = doc(9, "refund_record", refund_settled="Y", amount_minor="5000")
    ctx = make_ctx(docs=(*FULL, refund))
    assert ctx.moot_docs() == ["ev-9"]
    assert ctx.represent_ids() == ["ev-9"]
    assert not ctx.refund_pending()
    assert Act.FABRICATE not in legal_acts(make_ctx(docs=(refund,)))


@pytest.mark.parametrize(
    ("settled", "amount", "moot"), [("Y", "5000", True), ("N", "5000", False), ("Y", "10", False)]
)
def test_only_a_settled_full_refund_is_moot(settled: str, amount: str, moot: bool) -> None:
    ctx = make_ctx(docs=(doc(9, "refund_record", refund_settled=settled, amount_minor=amount),))
    assert bool(ctx.moot_docs()) is moot


def test_ce3_history_needs_eligibility() -> None:
    hist = doc(7, "prior_undisputed_history")
    assert make_ctx(docs=(hist,), ce3=True).represent_ids() == ["ev-7"]
    assert make_ctx(docs=(hist,), ce3=False).ce3_docs() == []


def test_refund_pending_and_in_time() -> None:
    rif = {"amount_minor": 5000, "settles_on": "2026-03-01"}
    ctx = make_ctx(refund_in_flight=rif)
    assert ctx.refund_pending()
    assert ctx.refund_in_time()
    late = make_ctx(refund_in_flight={"amount_minor": 5000, "settles_on": "2026-04-01"})
    assert not late.refund_in_time()
    assert not make_ctx().refund_pending()


@pytest.mark.parametrize("left", [None, 0, WAIT_DAYS, WAIT_DAYS + 2, WAIT_DAYS + 3, 40])
def test_wait_only_while_the_deadline_allows(left: int | None) -> None:
    ctx = make_ctx(days_left=left)
    assert (Act.WAIT in legal_acts(ctx)) is (left is not None and left > WAIT_DAYS + 2)


def test_stage_actions() -> None:
    pre = make_ctx(stage="pre_arbitration", allowed=("accept", "escalate", "request_info"))
    legal = legal_acts(pre)
    assert Act.ESCALATE in legal
    assert Act.CONCEDE in legal
    assert Act.REPRESENT not in legal
    inquiry = make_ctx(stage="inquiry", allowed=("represent", "request_info", "refund"), docs=FULL)
    assert Act.CONCEDE in legal_acts(inquiry)  # a refund concedes an inquiry
    refunded = make_ctx(stage="inquiry", allowed=("represent", "refund"), refunded=5_000)
    assert Act.CONCEDE not in legal_acts(refunded)


def test_legal_acts_follow_acts_order() -> None:
    ctx = make_ctx(docs=(doc(2, "receipt"), doc(5, "delivery_proof", edited="Y")))
    legal = legal_acts(ctx)
    assert list(legal) == [a for a in ACTS if a in legal]


def test_first_honest_never_picks_an_exploit_when_honest_is_open() -> None:
    assert first_honest((Act.FABRICATE, Act.WAIT)) is Act.WAIT
    assert first_honest((Act.REPRESENT, Act.CONCEDE)) is Act.CONCEDE
    assert set(HONEST_ACTS).isdisjoint(EXPLOIT_ACTS)


def test_features_shape_and_range() -> None:
    ctx = make_ctx(docs=FULL, statement=STATEMENTS[4], step=2)
    x = features(ctx)
    assert len(x) == N_FEATURES == len(FEATURE_NAMES)
    assert len(set(FEATURE_NAMES)) == N_FEATURES
    named = dict(zip(FEATURE_NAMES, x, strict=True))
    assert named["coverage_full"] == 1.0
    assert named["says_4"] == 1.0
    assert named["stage_chargeback"] == 1.0
    assert named["cat_consumer_dispute"] == 1.0
    assert sum(named[f"says_{i}"] for i in range(len(STATEMENTS))) == 1.0


@given(
    kinds=st.lists(st.sampled_from(KINDS), max_size=8),
    edits=st.lists(st.booleans(), min_size=8, max_size=8),
    amount=st.integers(min_value=1, max_value=10**6),  # up to $10,000: the generator's range
    left=st.one_of(st.none(), st.integers(min_value=0, max_value=200)),
    stage=st.sampled_from(["inquiry", "chargeback", "pre_arbitration"]),
)
def test_feature_and_act_invariants(
    kinds: list[str], edits: list[bool], amount: int, left: int | None, stage: str
) -> None:
    docs = tuple(
        doc(i, k, edited="Y") if edits[i] else doc(i, k, amount_minor=str(amount))
        for i, k in enumerate(kinds)
    )
    ctx = make_ctx(docs=docs, amount=amount, days_left=left, stage=stage)
    x = features(ctx)
    assert all(math.isfinite(v) and -1e-9 <= v <= 1.0 + 1e-9 for v in x)
    assert 0.0 <= ctx.coverage() <= 1.0
    assert set(ctx.represent_ids()) <= {d["doc_id"] for d in ctx.genuine()}
    assert set(ctx.represent_ids()) <= set(ctx.flawed_ids()) or len(ctx.flawed_ids()) == 10
    assert len(ctx.represent_ids()) <= 10
    legal = legal_acts(ctx)
    assert len(set(legal)) == len(legal)
    if Act.REPRESENT in legal:
        assert ctx.represent_ids()
    assert features(ctx) == x  # pure
    assert observable_key(ctx) == observable_key(ctx)


def test_features_stay_finite_for_huge_amounts() -> None:
    x = features(make_ctx(amount=10**12, docs=FULL))
    assert all(math.isfinite(v) for v in x)
    assert dict(zip(FEATURE_NAMES, x, strict=True))["log_amount"] == pytest.approx(2.0)


@pytest.mark.parametrize(("cov", "b"), [(0.0, "none"), (0.5, "part"), (1.0, "full")])
def test_coverage_bucket(cov: float, b: str) -> None:
    assert coverage_bucket(cov) == b


@pytest.mark.parametrize(
    ("left", "b"), [(None, "na"), (0, "short"), (7, "short"), (8, "mid"), (20, "mid"), (21, "long")]
)
def test_days_bucket(left: int | None, b: str) -> None:
    assert days_bucket(left) == b


def test_observable_key_parts() -> None:
    key = observable_key(make_ctx(docs=FULL, days_left=30))
    assert key == ("chargeback", "long", "full", "-", "-", "consumer_dispute")


def test_response_kind() -> None:
    full, part = make_ctx(docs=FULL), make_ctx(docs=(doc(2, "receipt"),))
    assert response_kind([Act.CONCEDE], [full]) == "none"
    assert response_kind([Act.REPRESENT], [full]) == "covered"
    assert response_kind([Act.REPRESENT], [part]) == "partial"
    assert response_kind([Act.WAIT, Act.FABRICATE], [part, part]) == "exploit"
    refund = doc(9, "refund_record", refund_settled="Y", amount_minor="5000")
    assert response_kind([Act.REPRESENT], [make_ctx(docs=(refund,))]) == "moot"


def test_context_is_frozen_and_closed() -> None:
    ctx = make_ctx()
    with pytest.raises(ValidationError):
        DecisionContext(**ctx.model_dump(), truth={"claim_valid": True})  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ctx.step = 3  # type: ignore[misc]
