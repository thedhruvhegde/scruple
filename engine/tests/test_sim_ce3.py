from __future__ import annotations

from datetime import date, timedelta
from itertools import combinations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.sim.ce3 import (
    ELEMENTS,
    Fingerprint,
    PriorTransaction,
    ce3_eligibility,
    check_prior,
)
from scruple.sim.disputes import CaseMix, generate_cases
from scruple.sim.evidence import EvidenceKind
from scruple.sim.families import FAMILY_WEIGHTS, CaseFamily
from scruple.sim.issuer import DECISION_STAGES, SeededIssuer
from scruple.sim.networks import Network
from scruple.sim.reason_codes import ReasonCodeSpec, load_catalogue

D = date(2026, 6, 1)
FP = Fingerprint(
    account_id="acct-1",
    ip="198.51.100.7",
    shipping_address="addr-1",
    device_id="device-0123456789",
)
CODES = load_catalogue().codes
HISTORY = CaseMix(p_ce3_history=1.0, family_weights=FAMILY_WEIGHTS)


def prior(age: int, fp: Fingerprint = FP, disputed: bool = False, i: int = 0) -> PriorTransaction:
    return PriorTransaction(
        txn_id=f"p{i}", processed_on=D - timedelta(days=age), disputed=disputed, fingerprint=fp
    )


GOOD = (prior(200, i=1), prior(300, i=2))


@pytest.mark.parametrize("spec", CODES, ids=[c.key for c in CODES])
def test_only_visa_10_4_can_be_eligible(spec: ReasonCodeSpec) -> None:
    res = ce3_eligibility(spec.network, spec.code, D, FP, GOOD)
    assert res.eligible is (spec.network is Network.VISA and spec.code == "10.4")


@pytest.mark.parametrize(
    ("age", "ok"), [(119, False), (120, False), (121, True), (364, True), (365, False)]
)
def test_age_window_is_exclusive(age: int, ok: bool) -> None:
    assert check_prior(D, FP, prior(age)).qualifies is ok


SUBSETS = [c for k in range(len(ELEMENTS) + 1) for c in combinations(ELEMENTS, k)]


@pytest.mark.parametrize("shared", SUBSETS, ids=lambda s: "+".join(s) or "none")
def test_two_matches_including_ip_or_device(shared: tuple[str, ...]) -> None:
    other = Fingerprint(
        **{e: (getattr(FP, e) if e in shared else f"other-{e}-000000000") for e in ELEMENTS}
    )
    ok = len(shared) >= 2 and bool({"ip", "device_id"} & set(shared))
    assert check_prior(D, FP, prior(200, fp=other)).qualifies is ok


def test_short_device_ids_do_not_count() -> None:
    short = FP.model_copy(update={"device_id": "dev-123"})
    other = Fingerprint(device_id="dev-123", shipping_address="addr-1")
    check = check_prior(D, short, prior(200, fp=other))
    assert check.matched == ("shipping_address",) and not check.qualifies


def test_missing_elements_never_match() -> None:
    empty = Fingerprint()
    assert check_prior(D, empty, prior(200, fp=empty)).matched == ()


def test_disputed_priors_and_too_few_priors() -> None:
    assert not check_prior(D, FP, prior(200, disputed=True)).qualifies
    one = ce3_eligibility(Network.VISA, "10.4", D, FP, GOOD[:1])
    assert not one.eligible and "needs 2" in one.reason
    two = ce3_eligibility(Network.VISA, "10.4", D, FP, GOOD)
    assert two.eligible and two.qualifying == ("p1", "p2")
    assert not ce3_eligibility(Network.VISA, "10.4", D, None, GOOD).eligible


@given(ages=st.lists(st.integers(0, 600), min_size=0, max_size=6))
def test_eligibility_counts_qualifying_priors(ages: list[int]) -> None:
    priors = tuple(prior(a, i=i) for i, a in enumerate(ages))
    res = ce3_eligibility(Network.VISA, "10.4", D, FP, priors)
    n = sum(1 for a in ages if 120 < a < 365)
    assert res.eligible is (n >= 2)
    assert len(res.qualifying) == n


def test_generated_histories_separate_friendly_from_true_fraud() -> None:
    cases = generate_cases(2, 3_000, HISTORY)
    friendly = [
        c
        for c in cases
        if c.truth.family is CaseFamily.FRIENDLY_FRAUD
        and c.network is Network.VISA
        and c.reason_code == "10.4"
    ]
    true = [c for c in cases if c.truth.family is CaseFamily.TRUE_FRAUD]
    assert friendly and true
    assert all(not c.ce3().eligible for c in true)
    share = sum(c.ce3().eligible for c in friendly) / len(friendly)
    assert 0.3 < share < 1.0  # most, not all, histories qualify
    for c in cases:
        if c.prior_transactions:
            assert c.fingerprint is not None
            assert any(d.kind is EvidenceKind.PRIOR_UNDISPUTED_HISTORY for d in c.documents)


def test_issuer_honours_ce3_when_the_history_is_submitted() -> None:
    issuer = SeededIssuer()
    cases = [c for c in generate_cases(3, 3_000, HISTORY) if c.ce3().eligible]
    assert cases
    for c in cases[:50]:
        hist = tuple(d for d in c.documents if d.kind is EvidenceKind.PRIOR_UNDISPUTED_HISTORY)
        for stage in DECISION_STAGES:
            assert issuer.merchant_prevails(c, stage, hist)
            assert issuer.decide(c, stage, ()).odds == issuer.odds(c, stage, ())


def test_no_history_without_the_option() -> None:
    assert all(not c.prior_transactions for c in generate_cases(2, 300))
