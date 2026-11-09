from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from scruple.domain import ReasonCode
from scruple.sim.evidence import EvidenceKind
from scruple.sim.networks import DATA_DIR, Category, Network
from scruple.sim.reason_codes import (
    ReasonCodeSpec,
    load_catalogue,
    parse_catalogue,
    reason_code,
)
from tests.test_sim_networks import source_ids

CODES = load_catalogue().codes
IDS = [c.key for c in CODES]


def raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA_DIR / "reason_codes.json").read_text(encoding="utf-8"))
    return data


def test_catalogue_counts_match_the_dataset_readme() -> None:
    # S1 README: Visa 20 and Mastercard 11 active codes.
    cat = load_catalogue()
    assert len(cat.for_network(Network.VISA)) == 20
    assert len(cat.for_network(Network.MASTERCARD)) == 11


def test_dataset_attribution_is_present() -> None:
    ds = load_catalogue().dataset
    assert ds.licence == "CC BY 4.0"
    assert ds.source_id in source_ids()
    assert ds.url.startswith("https://github.com/")


@pytest.mark.parametrize("spec", CODES, ids=IDS)
def test_every_code_is_well_formed(spec: ReasonCodeSpec) -> None:
    assert spec.title and spec.key_evidence_text
    assert spec.response_days_source in source_ids()
    if spec.category_source is not None:
        assert spec.category_source in source_ids()
    assert set(spec.all_evidence) <= set(EvidenceKind)
    assert reason_code(spec.network, spec.code) == spec


@pytest.mark.parametrize("spec", CODES, ids=IDS)
def test_response_days_follow_the_network(spec: ReasonCodeSpec) -> None:
    assert spec.response_days == (30 if spec.network is Network.VISA else 45)


@pytest.mark.parametrize("spec", CODES, ids=IDS)
def test_legacy_family_is_consistent_with_category(spec: ReasonCodeSpec) -> None:
    if spec.legacy_family is ReasonCode.FRAUD:
        assert spec.category is Category.FRAUD
    elif spec.legacy_family is not None:
        assert spec.category is Category.CONSUMER_DISPUTE


@pytest.mark.parametrize(
    "spec", [c for c in CODES if c.category is Category.FRAUD], ids=lambda c: c.key
)
def test_fraud_codes_need_authentication_style_evidence(spec: ReasonCodeSpec) -> None:
    strong = {
        EvidenceKind.EMV_CHIP_READ,
        EvidenceKind.DEVICE_IP_MATCH,
        EvidenceKind.AVS_CVV_MATCH,
        EvidenceKind.RECEIPT,
    }
    assert set(spec.required_evidence) & strong


def test_visa_categories_follow_code_prefix() -> None:
    for spec in load_catalogue().for_network(Network.VISA):
        assert spec.category_source == "S1"


def test_unknown_code_raises() -> None:
    with pytest.raises(KeyError):
        reason_code(Network.VISA, "99.9")


def _first(data: dict[str, Any], network: str) -> dict[str, Any]:
    row: dict[str, Any] = next(c for c in data["codes"] if c["network"] == network)
    return row


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r.update(category="consumer_dispute"), "prefix implies"),
        (lambda r: r.update(required_evidence=[]), "at least 1"),
        (lambda r: r.update(supporting_evidence=r["required_evidence"][:1]), "both required"),
        (lambda r: r.update(required_evidence=["receipt", "receipt"]), "duplicate required"),
        (lambda r: r.update(required_evidence=["selfie"]), "Input should be"),
        (lambda r: r.update(response_days=0), "greater than 0"),
        (lambda r: r.update(extra=1), "Extra inputs"),
    ],
)
def test_bad_visa_rows_rejected(mutate: Any, match: str) -> None:
    data = raw()
    mutate(_first(data, "visa"))
    with pytest.raises(ValidationError, match=match):
        parse_catalogue(json.dumps(data))


def test_bad_mastercard_code_rejected() -> None:
    data = raw()
    _first(data, "mastercard")["code"] = "48A7"
    with pytest.raises(ValidationError, match="four digits"):
        parse_catalogue(json.dumps(data))


def test_duplicate_codes_rejected() -> None:
    data = raw()
    data["codes"].append(data["codes"][0])
    with pytest.raises(ValidationError, match="duplicate reason code"):
        parse_catalogue(json.dumps(data))
