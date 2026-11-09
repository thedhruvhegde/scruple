from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import CardDispute, CaseMix, DisputeEngine, RequestInfo, generate_cases
from scruple.sim.evidence import EvidenceKind
from scruple.sim.families import (
    CLAIM_VALID,
    FAMILY_CATEGORIES,
    FAMILY_WEIGHTS,
    STATEMENTS,
    CaseFamily,
    codes_for,
    family_of,
    statement,
)
from scruple.sim.issuer import SeededIssuer
from scruple.sim.models import Stage
from scruple.sim.networks import Category, Network

FAMILY_MIX = CaseMix(family_weights=FAMILY_WEIGHTS)


@pytest.mark.parametrize("category", list(Category))
@pytest.mark.parametrize("valid", [True, False])
def test_every_category_and_validity_has_one_family(category: Category, valid: bool) -> None:
    fam = family_of(category, valid)
    assert category in FAMILY_CATEGORIES[fam]
    assert CLAIM_VALID[fam] is valid


@pytest.mark.parametrize("network", list(Network))
@pytest.mark.parametrize("family", list(CaseFamily))
def test_every_family_has_card_absent_codes(network: Network, family: CaseFamily) -> None:
    codes = codes_for(network, family)
    assert codes
    assert all(not c.card_present and c.category in FAMILY_CATEGORIES[family] for c in codes)


def test_weights_and_statements_cover_every_family() -> None:
    assert set(FAMILY_WEIGHTS) == set(CaseFamily) == set(STATEMENTS)
    assert abs(sum(FAMILY_WEIGHTS.values()) - 1) < 1e-9
    for opts in STATEMENTS.values():
        assert abs(sum(w for _, w in opts) - 1) < 1e-9


def test_legacy_mode_derives_family_from_validity() -> None:
    for c in generate_cases(1, 300):
        assert c.truth.family is family_of(c.category, c.truth.claim_valid)


def test_family_mode_follows_the_weights() -> None:
    cases = generate_cases(2, 4_000, FAMILY_MIX)
    seen = Counter(c.truth.family for c in cases)
    for fam, w in FAMILY_WEIGHTS.items():
        assert abs(seen[fam] / 4_000 - w) < 0.03
    for c in cases:
        assert c.truth.claim_valid is CLAIM_VALID[c.truth.family]


def test_family_mode_is_deterministic() -> None:
    assert generate_cases(3, 100, FAMILY_MIX) == generate_cases(3, 100, FAMILY_MIX)


def _device_flags(cases: list[CardDispute], fam: CaseFamily) -> set[str]:
    return {
        d.fields["matches_cardholder"]
        for c in cases
        if c.truth.family is fam
        for d in c.documents
        if d.kind is EvidenceKind.DEVICE_IP_MATCH
    }


def test_friendly_fraud_links_the_device_and_true_fraud_does_not() -> None:
    cases = generate_cases(4, 2_000, FAMILY_MIX)
    assert _device_flags(cases, CaseFamily.FRIENDLY_FRAUD) == {"Y"}
    assert _device_flags(cases, CaseFamily.TRUE_FRAUD) == {"N"}


def test_friendly_fraud_is_far_more_winnable_than_true_fraud() -> None:
    issuer = SeededIssuer()
    cases = generate_cases(5, 3_000, FAMILY_MIX)

    def rate(fam: CaseFamily) -> float:
        xs = [
            issuer.merchant_prevails(c, Stage.REPRESENTMENT, c.documents)
            for c in cases
            if c.truth.family is fam
        ]
        return sum(xs) / len(xs)

    assert rate(CaseFamily.FRIENDLY_FRAUD) > rate(CaseFamily.TRUE_FRAUD) + 0.4


def test_mismatched_device_evidence_does_not_count_for_the_merchant() -> None:
    issuer = SeededIssuer()
    for c in generate_cases(6, 1_000, FAMILY_MIX):
        if c.truth.family is not CaseFamily.TRUE_FRAUD:
            continue
        device = tuple(d for d in c.documents if d.kind is EvidenceKind.DEVICE_IP_MATCH)
        if device:
            assert issuer.odds(c, Stage.REPRESENTMENT, device) == issuer.odds(
                c, Stage.REPRESENTMENT, ()
            )


@pytest.mark.parametrize("family", list(CaseFamily))
def test_statements_are_deterministic_and_from_the_family(family: CaseFamily) -> None:
    for i in range(30):
        s = statement(f"cd-{i}", family)
        assert s == statement(f"cd-{i}", family)
        assert s in {x for x, _ in STATEMENTS[family]}


def test_statement_is_hidden_until_requested() -> None:
    c = next(x for x in generate_cases(7, 50, FAMILY_MIX) if x.opening_stage is Stage.CHARGEBACK)
    clock = ManualClock()
    clock.set_date(c.opened_on)
    e = DisputeEngine([c], clock)
    obs = e.observe(c.case_id)
    assert "request_info" in obs.allowed_actions
    assert c.truth.statement not in str(obs.model_dump())
    before = len(e.ledger.entries)
    r = e.act(c.case_id, RequestInfo())
    assert r.ok and r.info == {"statement": c.truth.statement}
    assert r.stage_after is r.stage_before
    assert len(e.ledger.entries) == before


def test_family_must_match_code_and_validity() -> None:
    c = next(x for x in generate_cases(8, 50) if x.category is Category.FRAUD)
    data = c.model_dump()
    bad_cat = c.truth.model_copy(update={"family": CaseFamily.SERVICE_FAILURE, "claim_valid": True})
    with pytest.raises(ValidationError, match="cannot file under"):
        CardDispute.model_validate({**data, "truth": bad_cat})
    flipped = CaseFamily.FRIENDLY_FRAUD if c.truth.claim_valid else CaseFamily.TRUE_FRAUD
    with pytest.raises(ValidationError, match="implies claim_valid"):
        CardDispute.model_validate(
            {**data, "truth": c.truth.model_copy(update={"family": flipped})}
        )
