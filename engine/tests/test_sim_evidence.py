from __future__ import annotations

import random
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    ALWAYS_HELD,
    ActionError,
    CardDispute,
    DisputeEngine,
    Represent,
    _documents,
    generate_cases,
)
from scruple.sim.evidence import (
    EvidenceError,
    EvidenceKind,
    EvidenceVault,
    make_document,
)
from scruple.sim.models import Stage
from scruple.sim.reason_codes import ReasonCodeSpec, load_catalogue

CODES = load_catalogue().codes
D = date(2026, 2, 1)


def test_sealed_document_verifies() -> None:
    d = make_document("cd-a", EvidenceKind.RECEIPT, D, {"ref": "r1"}, 0)
    assert d.hash_ok
    assert len(d.content_hash) == 64


@pytest.mark.parametrize(
    "update",
    [
        {"fields": {"ref": "r2"}},
        {"issued_on": date(2026, 2, 2)},
        {"kind": EvidenceKind.DELIVERY_PROOF},
        {"case_id": "cd-b"},
    ],
)
def test_any_edit_after_sealing_breaks_the_hash(update: dict[str, object]) -> None:
    d = make_document("cd-a", EvidenceKind.RECEIPT, D, {"ref": "r1"}, 0)
    assert not d.model_copy(update=update).hash_ok


def test_ids_are_unique_per_case_index_and_kind() -> None:
    ids = {
        make_document(c, k, D, {}, i).doc_id
        for c in ("cd-a", "cd-b")
        for k in EvidenceKind
        for i in range(3)
    }
    assert len(ids) == 2 * len(EvidenceKind) * 3


def test_vault_resolution_rules() -> None:
    a0 = make_document("cd-a", EvidenceKind.RECEIPT, D, {}, 0)
    a1 = make_document("cd-a", EvidenceKind.DELIVERY_PROOF, D, {}, 1)
    b0 = make_document("cd-b", EvidenceKind.RECEIPT, D, {}, 0)
    v = EvidenceVault([a0, a1, b0])
    assert len(v) == 3
    assert v.resolve("cd-a", [a1.doc_id, a0.doc_id]) == (a1, a0)
    assert v.for_case("cd-b") == (b0,)
    with pytest.raises(EvidenceError, match="another case"):
        v.resolve("cd-a", [b0.doc_id])
    with pytest.raises(EvidenceError, match="unknown"):
        v.resolve("cd-a", ["ev-nope"])
    with pytest.raises(EvidenceError, match="twice"):
        v.resolve("cd-a", [a0.doc_id, a0.doc_id])
    with pytest.raises(EvidenceError, match="duplicate"):
        v.add(a0)


@settings(max_examples=50, deadline=None)
@given(seed=st.integers(0, 10_000), data=st.data())
def test_no_evidence_id_resolves_to_another_cases_document(seed: int, data: st.DataObject) -> None:
    cases = generate_cases(seed, 12)
    vault = EvidenceVault(d for c in cases for d in c.documents)
    asker = data.draw(st.sampled_from(cases))
    owner = data.draw(st.sampled_from(cases))
    d = data.draw(st.sampled_from(owner.documents))
    if owner.case_id == asker.case_id:
        assert vault.resolve(asker.case_id, [d.doc_id]) == (d,)
    else:
        with pytest.raises(EvidenceError):
            vault.resolve(asker.case_id, [d.doc_id])


@pytest.mark.parametrize("spec", CODES, ids=[c.key for c in CODES])
def test_merchant_in_the_right_holds_every_required_document(spec: ReasonCodeSpec) -> None:
    docs = _documents(random.Random(1), "cd-x", spec, False, D, 1_000, "USD")
    kinds = {d.kind for d in docs}
    assert set(spec.required_evidence) <= kinds
    assert set(ALWAYS_HELD) <= kinds
    assert kinds <= set(spec.all_evidence) | set(ALWAYS_HELD)
    assert all(d.hash_ok and d.case_id == "cd-x" for d in docs)


@pytest.mark.parametrize("spec", CODES, ids=[c.key for c in CODES])
def test_merchant_facing_a_valid_claim_often_lacks_evidence(spec: ReasonCodeSpec) -> None:
    missing = 0
    for s in range(200):
        docs = _documents(random.Random(s), "cd-x", spec, True, D, 1_000, "USD")
        if not set(spec.required_evidence) <= {d.kind for d in docs}:
            missing += 1
    if set(spec.required_evidence) <= set(ALWAYS_HELD):
        assert missing == 0  # e.g. authorization codes: always held
    else:
        assert missing > 100


def test_generated_documents_belong_to_their_case() -> None:
    for c in generate_cases(3, 300):
        assert c.documents
        assert all(d.case_id == c.case_id and d.hash_ok for d in c.documents)
        assert c.evidence_ids == tuple(d.doc_id for d in c.documents)
        assert "documents" in c.view()


def test_case_rejects_foreign_or_duplicate_documents() -> None:
    a, b = generate_cases(3, 2)
    with pytest.raises(ValidationError, match="another case"):
        CardDispute.model_validate({**a.model_dump(), "documents": b.documents})
    with pytest.raises(ValidationError, match="duplicate document"):
        CardDispute.model_validate({**a.model_dump(), "documents": a.documents * 2})


def test_engine_refuses_foreign_evidence_without_side_effects() -> None:
    cases = [c for c in generate_cases(4, 40) if c.opening_stage is Stage.CHARGEBACK][:2]
    a, b = cases
    clock = ManualClock()
    clock.set_date(a.opened_on)
    e = DisputeEngine([a, b], clock)
    before = len(e.ledger.entries)
    r = e.act(a.case_id, Represent(evidence_ids=b.evidence_ids[:1]))
    assert r.error is ActionError.BAD_EVIDENCE and "another case" in r.detail
    assert len(e.ledger.entries) == before
    assert e.act(a.case_id, Represent(evidence_ids=a.evidence_ids)).ok
