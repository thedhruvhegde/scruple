from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.hashing import canonical_json
from scruple.sim.disputes import ALWAYS_HELD, CaseMix, generate_cases
from scruple.sim.evidence import (
    EvidenceKind,
    Forgery,
    document_problems,
    forge,
    make_document,
)

D = date(2026, 2, 1)
EXPECTED = {
    Forgery.TAMPERED: "hash_mismatch",
    Forgery.WRONG_ORDER: "amount_mismatch",
    Forgery.BACKDATED: "dated_before_transaction",
}


def dump(mix: CaseMix | None = None) -> str:
    cases = generate_cases(5, 300) if mix is None else generate_cases(5, 300, mix)
    return canonical_json([c.model_dump(mode="json") for c in cases])


def test_zero_options_leave_generation_unchanged() -> None:
    assert dump() == dump(CaseMix(p_missing_evidence=0.0, p_forged_evidence=0.0))


def test_defaults_have_no_forgeries_or_losses() -> None:
    for c in generate_cases(5, 300):
        assert c.truth.forged == {}
        assert c.truth.missing_evidence == ()


@pytest.mark.parametrize("how", list(Forgery))
def test_each_forgery_is_detectable(how: Forgery) -> None:
    d = make_document(
        "cd-a",
        EvidenceKind.DELIVERY_PROOF,
        D,
        {"amount_minor": "500", "currency": "USD"},
        0,
    )
    assert document_problems(d, 500, "USD", D) == ()
    f = forge(d, how, D)
    assert f.doc_id == d.doc_id and f.case_id == d.case_id
    assert EXPECTED[how] in document_problems(f, 500, "USD", D)


def test_currency_mismatch_is_reported() -> None:
    d = make_document("cd-a", EvidenceKind.RECEIPT, D, {"currency": "EUR"}, 0)
    assert document_problems(d, 1, "USD", D) == ("currency_mismatch",)


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 5_000))
def test_forged_documents_fill_gaps_in_valid_claims_only(seed: int) -> None:
    cases = generate_cases(seed, 80, CaseMix(p_forged_evidence=1.0))
    for c in cases:
        if not c.truth.claim_valid:
            assert c.truth.forged == {}
        held = {d.kind for d in c.documents}
        if c.truth.claim_valid:
            assert set(c.spec.required_evidence) <= held  # every gap was forged
        for d in c.documents:
            problems = document_problems(d, c.captured_minor, c.currency, c.processed_on)
            if d.doc_id in c.truth.forged:
                assert EXPECTED[c.truth.forged[d.doc_id]] in problems
                assert d.kind in c.spec.required_evidence
            else:
                assert problems == ()  # genuine documents always pass the checks


def test_forgeries_are_hidden_from_the_view() -> None:
    cases = generate_cases(2, 200, CaseMix(p_forged_evidence=1.0))
    assert any(c.truth.forged for c in cases)
    for c in cases:
        assert "forged" not in canonical_json(c.view())


def test_missing_evidence_strips_required_documents() -> None:
    cases = generate_cases(8, 300, CaseMix(p_missing_evidence=1.0))
    lost = 0
    for c in cases:
        if c.truth.claim_valid:
            assert c.truth.missing_evidence == ()
            continue
        held = {d.kind for d in c.documents}
        need = set(c.spec.required_evidence) - set(ALWAYS_HELD)
        assert not (need & held)
        assert set(c.truth.missing_evidence) == need
        lost += len(need)
    assert lost > 0


def test_options_are_validated() -> None:
    with pytest.raises(ValueError):
        CaseMix(p_forged_evidence=1.5)
    with pytest.raises(ValueError):
        CaseMix(p_missing_evidence=-0.1)
