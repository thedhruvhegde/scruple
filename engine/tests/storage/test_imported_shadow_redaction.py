"""The imported-shadow store keeps no card number and no free-text personal data
(every backend). Batches are redacted as `scruple ingest` redacts a case, before they
are scored or stored, so a replay answers exactly what the live call answered."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.gate.shadow_imported import ImportedDecision, ImportedOutcome
from scruple.sim.library import GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from scruple.storage.imported_shadow import repository_for
from tests.storage.conftest import RawStorage
from tests.storage.test_imported_shadow import BATCH, DEV_AUTH, OUTCOMES, READER, _durable, _gate

PAN = "4242424242424242"  # a Luhn-valid test card number
EMAIL = "jane.doe@example.com"


def _with_case(d: ImportedDecision, **update: object) -> ImportedDecision:
    return d.model_copy(update={"case": d.case.model_copy(update=update)})


def _stored(store: RawStorage) -> str:
    events = repository_for(store).events_after("default", "r-imp", 1, 0)
    return "".join(e.body_json for e in events)


def test_free_text_is_redacted_before_it_is_stored(store: RawStorage) -> None:
    repo = repository_for(store)
    dirty = [
        _with_case(BATCH[0], source_reason=f"card {PAN} mail {EMAIL}"),
        _with_case(BATCH[1], merchant_ref=f"order for {EMAIL}"),
        *BATCH[2:10],
    ]
    d = _durable(repo)
    d.record(dirty)
    case = OUTCOMES[0].case.model_copy(update={"network_reason_code": f"see {PAN}"})
    assert d.observe([ImportedOutcome(case=case)]).updated == 1
    body = _stored(store)
    assert PAN not in body and EMAIL not in body
    assert "[PAN]" in body and "[EMAIL]" in body
    assert _durable(repo).summary() == d.summary()  # replay matches the live answer


@pytest.mark.parametrize(("field", "value"), [("external_id", f"dp_{PAN}"), ("payment_ref", EMAIL)])
def test_an_id_holding_personal_data_refuses_the_batch(
    store: RawStorage, field: str, value: str
) -> None:
    repo = repository_for(store)
    d = _durable(repo)
    with pytest.raises(ValueError, match="ids cannot be redacted"):
        d.record([*BATCH[:3], _with_case(BATCH[3], **{field: value})])
    assert repo.last_seq("default", "r-imp", 1) == 0
    assert d.summary().decisions == 0


def test_a_card_number_anywhere_else_refuses_the_batch(store: RawStorage) -> None:
    repo = repository_for(store)
    d = _durable(repo)
    with_mv = next(x for x in BATCH if x.movements)
    mv = with_mv.movements[0].model_copy(update={"category": f"refund {PAN}"})
    with pytest.raises(ValueError, match="contains a card number"):
        d.record([with_mv.model_copy(update={"movements": (mv,)})])
    with pytest.raises(ValueError, match="contains a card number"):
        d.record([_with_case(BATCH[0], evidence_fields=(PAN,))])
    assert repo.last_seq("default", "r-imp", 1) == 0


def test_api_refuses_a_card_number_with_409_and_stores_nothing() -> None:
    storage = SQLiteStorage(":memory:")
    g = _gate(storage)
    version = g.registry.versions(GOOD_RULE_ID)[-1].version
    url = f"/shadow/rules/{GOOD_RULE_ID}/versions/{version}/imported"
    c = TestClient(create_app(g, auth=DEV_AUTH), headers=READER)
    bad = _with_case(BATCH[0], external_id=f"dp_{PAN}")
    body = {"decisions": [json.loads(bad.model_dump_json())]}
    r = c.post(f"{url}/decisions", json=body, headers={"X-Actor": "production"})
    assert r.status_code == 409 and PAN not in r.text
    assert c.get(url).json()["decisions"] == 0
    assert repository_for(storage).last_seq("default", GOOD_RULE_ID, version) == 0
