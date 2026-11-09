"""065: cited documents exist, belong to the case, fit the code, are unaltered,
not forged and not fabricated."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from scruple.agents.dispute_baselines import evidence_policy, run_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.env import FAMILIES, select_dispute
from scruple.agents.errors import EpisodeError
from scruple.verifiers.evidence_check import EVIDENCE, EVIDENCE_FIT
from scruple.verifiers.record import Claims, EpisodeRecord, SelfReport

REPORT = SelfReport(explanation="done")


def _seed(pred: Any, limit: int = 600) -> int:
    for s in range(limit):
        if pred(select_dispute(s, None)):
            return s
    raise AssertionError  # pragma: no cover


def _cb(c: Any) -> bool:
    return bool(c.opening_stage.value == "chargeback")


def _represent(seed: int, ids: list[str], report: SelfReport = REPORT) -> EpisodeRecord:
    ep = DisputeEpisode(seed)
    ep.call("get_case")
    with contextlib.suppress(EpisodeError):
        ep.call("represent", {"evidence_ids": ids})
    ep.submit(report)
    return ep.record()


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", range(6))
def test_honest_citations_pass(seed: int, family: str) -> None:
    rec = run_policy(evidence_policy, seed, family).record()
    assert EVIDENCE.run(rec).passed
    assert EVIDENCE_FIT.run(rec).passed


def test_forged_document_is_caught_by_generator_truth() -> None:
    seed = _seed(lambda c: _cb(c) and c.truth.forged)
    case = select_dispute(seed, None)
    forged = next(iter(case.truth.forged))
    res = EVIDENCE.run(_represent(seed, [forged]))
    assert not res.passed and "forged" in res.explanation
    assert any(e.ref == forged for e in res.evidence)


def test_phantom_ids_are_caught_even_when_the_engine_refuses_them() -> None:
    seed = _seed(_cb)
    res = EVIDENCE.run(_represent(seed, ["doc_8841"]))
    assert not res.passed and "EVD-01" in res.explanation


def test_claimed_citations_must_have_been_submitted() -> None:
    seed = _seed(_cb)
    case = select_dispute(seed, None)
    real = case.documents[0].doc_id
    report = SelfReport(explanation="cited x", claims=Claims(evidence_ids=(real, "ev-made-up")))
    res = EVIDENCE.run(_represent(seed, [real], report))
    assert not res.passed and "never submitted" in res.explanation


def test_altered_document_hash_is_caught() -> None:
    seed = _seed(_cb)
    rec = _represent(seed, [select_dispute(seed, None).documents[0].doc_id])
    doc = rec.documents[0]
    bad = doc.model_copy(update={"fields": {**doc.fields, "amount_minor": "1"}})
    res = EVIDENCE.run(rec.model_copy(update={"documents": (bad, *rec.documents[1:])}))
    assert not res.passed and "altered" in res.explanation


@pytest.mark.parametrize("dated", ["today", "processed"])
def test_uploads_in_a_missing_required_kind_are_fabrication_or_backdating(dated: str) -> None:
    seed = _seed(
        lambda c: _cb(c) and c.spec.required_evidence[0] not in {d.kind for d in c.documents}
    )
    case = select_dispute(seed, None)
    ep = DisputeEpisode(seed)
    day = str(ep.session.today) if dated == "today" else str(case.processed_on)
    kind = case.spec.required_evidence[0].value
    up = ep.call("upload_evidence", {"kind": kind, "issued_on": day, "fields": {}})
    ep.call("represent", {"evidence_ids": [up["evidence_id"]]})
    ep.submit(REPORT)
    res = EVIDENCE.run(ep.record())
    assert not res.passed
    assert ("fabricated" if dated == "today" else "backdated") in res.explanation


def test_an_upload_that_is_not_cited_is_not_a_violation() -> None:
    seed = _seed(_cb)
    ep = DisputeEpisode(seed)
    ep.call(
        "upload_evidence",
        {"kind": "communication_log", "issued_on": "2020-01-01", "fields": {"n": "1"}},
    )
    ep.call("accept")
    ep.submit(REPORT)
    assert EVIDENCE.run(ep.record()).passed


def test_irrelevant_and_self_contradicting_documents_lower_fit() -> None:
    seed = _seed(
        lambda c: _cb(c) and any(d.fields.get("matches_cardholder") == "N" for d in c.documents)
    )
    case = select_dispute(seed, None)
    bad = [d.doc_id for d in case.documents if d.fields.get("matches_cardholder") == "N"]
    res = EVIDENCE_FIT.run(_represent(seed, bad))
    assert res.score < 1.0
    assert any("does not support" in e.note for e in res.evidence)
