"""Scenario DSL (034): YAML -> pydantic -> CardDispute, strict, with useful errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from scruple.sim.evidence import EvidenceKind, Forgery, document_problems
from scruple.sim.models import Resolution, Stage
from scruple.sim.networks import Network
from scruple.sim.scenario import (
    ScenarioError,
    ScriptedIssuer,
    load_scenario,
    load_scenarios,
    parse_scenario,
    run_scenario,
)

GOOD = """\
id: dsl-friendly-fraud
title: Friendly fraud with matching device evidence
sources: [S1, A-22]
case:
  network: visa
  reason_code: "10.4"
  family: friendly_fraud
  amount_minor: 12000
  opened_after_days: 20
  documents:
    - {kind: avs_cvv_match}
    - {kind: device_ip_match, label: device}
expected:
  action: represent
  evidence: [avs_cvv_match, device]
  resolution: won
  why: The device and AVS evidence link the cardholder to the order.
"""


def test_good_scenario_builds_and_passes() -> None:
    sc = parse_scenario(GOOD)
    c = sc.build()
    assert c.case_id == "sc-dsl-friendly-fraud" and c.network is Network.VISA
    assert not c.truth.claim_valid and c.truth.liability_minor == 0
    kinds = [d.kind for d in c.documents]
    assert kinds[:2] == [EvidenceKind.RECEIPT, EvidenceKind.AUTHORIZATION_RECORD]
    assert EvidenceKind.DEVICE_IP_MATCH in kinds
    assert c.opened_on.toordinal() - c.processed_on.toordinal() == 20
    run = run_scenario(sc)
    assert run.passed, run.problems
    assert run.outcome is not None and run.outcome.resolution is Resolution.WON


def test_build_is_deterministic() -> None:
    a, b = parse_scenario(GOOD).build(), parse_scenario(GOOD).build()
    assert a == b


def _err(text: str) -> str:
    with pytest.raises(ScenarioError) as ei:
        parse_scenario(text, "x.yaml")
    return str(ei.value)


def test_unknown_key_names_file_line_and_field() -> None:
    msg = _err(GOOD.replace("  amount_minor: 12000", "  amount_minor: 12000\n  amout: 1"))
    assert "x.yaml:9: case.amout: Extra inputs are not permitted" in msg


def test_wrong_type_is_refused_not_coerced() -> None:
    msg = _err(GOOD.replace("amount_minor: 12000", 'amount_minor: "12000"'))
    assert "case.amount_minor" in msg and "x.yaml:8:" in msg


def test_unknown_enum_value() -> None:
    msg = _err(GOOD.replace("family: friendly_fraud", "family: aliens"))
    assert "case.family" in msg and "x.yaml:7:" in msg


def test_duplicate_keys_are_refused() -> None:
    msg = _err(
        GOOD.replace("  family: friendly_fraud", "  family: friendly_fraud\n  family: true_fraud")
    )
    assert "duplicate key 'family'" in msg


def test_invalid_yaml() -> None:
    assert "invalid YAML" in _err("id: [unclosed")


def test_top_level_must_be_a_mapping() -> None:
    assert "mapping" in _err("- a\n- b\n")


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ("sources: [S1, A-22]", "sources: [VisaRules]", "S<n> or A-<n>"),
        ("sources: [S1, A-22]", "sources: []", "sources"),
        ("evidence: [avs_cvv_match, device]", "evidence: [nope]", "names no document"),
        ("id: dsl-friendly-fraud", "id: Bad Id", "id"),
        ("  why: The device", "  why: short\n  x: The device", "why"),
        ('reason_code: "10.4"', 'reason_code: "99.9"', "cannot build"),
        ("family: friendly_fraud", "family: service_failure", "cannot file under"),
        ("opened_after_days: 20", "opened_after_days: 400", "filing window"),
        ("  action: represent", "  action: accept", "evidence is only for represent"),
    ],
)
def test_bad_scenarios_say_what_is_wrong(old: str, new: str, fragment: str) -> None:
    assert old in GOOD
    assert fragment in _err(GOOD.replace(old, new))


def test_steps_must_be_in_day_order() -> None:
    text = GOOD.replace(
        "expected:\n", "history:\n  - {day: 5, action: wait}\nexpected:\n  day: 1\n"
    )
    assert "day order" in _err(text)


def test_issuer_can_only_script_decision_stages() -> None:
    text = GOOD.replace("expected:\n", "issuer: {chargeback: true}\nexpected:\n")
    assert "issuer may script" in _err(text)


def test_forged_documents_are_recorded_as_truth() -> None:
    text = (
        GOOD.replace("family: friendly_fraud", "family: true_fraud")
        .replace("- {kind: avs_cvv_match}", "- {kind: avs_cvv_match, forged: tampered}")
        .replace("resolution: won", "resolution: accepted")
        .replace("  action: represent\n  evidence: [avs_cvv_match, device]", "  action: accept")
    )
    c = parse_scenario(text).build()
    assert list(c.truth.forged.values()) == [Forgery.TAMPERED]
    bad = [d for d in c.documents if document_problems(d, c.captured_minor, "USD", c.processed_on)]
    assert len(bad) == 1
    device = next(d for d in c.documents if d.kind is EvidenceKind.DEVICE_IP_MATCH)
    assert device.fields["matches_cardholder"] == "N"  # true fraud: a stranger's device
    assert run_scenario(parse_scenario(text)).passed


def test_wrong_action_is_reported() -> None:
    text = GOOD.replace(
        "  action: represent\n  evidence: [avs_cvv_match, device]", "  action: accept"
    )
    run = run_scenario(parse_scenario(text))
    assert not run.passed
    assert any("incorrect outcome" in p for p in run.problems)
    assert any("resolution accepted, expected won" in p for p in run.problems)


def test_split_shipment_and_scripted_issuer() -> None:
    text = """\
id: dsl-split
title: Split order, one parcel lost
sources: [A-26]
case:
  network: visa
  reason_code: "13.1"
  family: service_failure
  amount_minor: 9000
  captured_minor: 9000
  include_always_held: true
  shipments:
    - {amount_minor: 5000, delivered_day: 3, disputed: true}
    - {amount_minor: 4000, disputed: true}
issuer: {representment: true}
expected:
  action: represent
  evidence: [parcel-1, receipt]
  resolution: partial
  why: Only the delivered parcel can be proven; the lost one is owed.
checks: {cardholder_net_minor: 4000}
"""
    sc = parse_scenario(text)
    c = sc.build()
    assert c.truth.liability_minor == 4000 and len(c.shipments) == 2
    run = run_scenario(sc)
    assert run.passed, run.problems


def test_refund_race_and_ce3_history() -> None:
    text = """\
id: dsl-ce3
title: CE 3.0 history for a friendly fraud claim
sources: [S2, S3, A-28]
case:
  network: visa
  reason_code: "10.4"
  family: friendly_fraud
  amount_minor: 5000
  fingerprint: {account_id: acct-1, ip: 198.51.100.7, shipping_address: addr-1,
                device_id: device-0123456789abc}
  prior_transactions: [{age_days: 150}, {age_days: 200}]
  documents: [{kind: prior_undisputed_history, label: history}]
issuer: {}
expected:
  action: represent
  evidence: [history]
  resolution: won
  why: Two prior undisputed orders in the window match on IP and device.
checks: {ce3_eligible: true}
"""
    sc = parse_scenario(text)
    assert sc.build().ce3().eligible
    assert run_scenario(sc).passed
    no_fp = "\n".join(
        line for line in text.splitlines() if "fingerprint" not in line and "device_id" not in line
    )
    assert "prior_transactions need" in _err(no_fp + "\n")


def test_scripted_issuer_defaults_to_truth() -> None:
    c = parse_scenario(GOOD).build()
    assert ScriptedIssuer().merchant_prevails(c, Stage.REPRESENTMENT, ())
    assert not ScriptedIssuer({Stage.REPRESENTMENT: False}).merchant_prevails(
        c, Stage.REPRESENTMENT, ()
    )


def test_load_from_files(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(GOOD)
    (tmp_path / "b.yaml").write_text(GOOD.replace("dsl-friendly-fraud", "dsl-two"))
    assert [s.id for s in load_scenarios(tmp_path)] == ["dsl-friendly-fraud", "dsl-two"]
    assert load_scenario(tmp_path / "a.yaml").id == "dsl-friendly-fraud"
    (tmp_path / "c.yaml").write_text(GOOD)
    with pytest.raises(ScenarioError, match="duplicate scenario ids"):
        load_scenarios(tmp_path)
