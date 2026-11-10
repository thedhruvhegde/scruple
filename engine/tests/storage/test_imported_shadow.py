"""Imported-shadow state is stored append-only and survives a restart (every backend)."""

from __future__ import annotations

import contextlib
import itertools
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.clock import SteppingClock
from scruple.domain import Action
from scruple.gate.shadow_imported import (
    ImportedOutcome,
    ImportedShadowRecorder,
    ImportedShadowSummary,
)
from scruple.registry import DuplicateContentError
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import ImmutableHistoryError, SQLiteStorage, Storage
from scruple.storage.imported_shadow import (
    DurableImportedShadow,
    ImportedShadowRepository,
    MemoryImportedShadow,
    ShadowEvent,
    repository_for,
)
from tests.ci_support import PROV
from tests.storage.conftest import RawStorage
from tests.test_gate_shadow import CFG
from tests.test_gate_shadow_imported import Always, decisions, synthetic

ROWS = synthetic(40)
BATCH = decisions(ROWS, as_opened=True)
OUTCOMES = [ImportedOutcome(case=c, movements=mv) for c, mv in ROWS]

# The API tests use the development-only X-Actor trust; tests/test_auth_*.py cover
# real authentication.
DEV_AUTH = Authenticator(dev_actor_header=True)
READER = {"X-Actor": "auditor"}  # default caller for reads; writes set their own


def _durable(repo: ImportedShadowRepository, tenant: str = "default") -> DurableImportedShadow:
    return DurableImportedShadow(
        repo,
        "r-imp",
        1,
        lambda: ImportedShadowRecorder(Always(Action.ACCEPT), synthetic=True),
        tenant_id=tenant,
        clock=SteppingClock(),
    )


def _live() -> ImportedShadowRecorder:
    rec = ImportedShadowRecorder(Always(Action.ACCEPT), synthetic=True)
    rec.record(BATCH[:25])
    rec.record(BATCH[25:])
    rec.observe(OUTCOMES[:30])
    return rec


def _feed(d: DurableImportedShadow) -> None:
    d.record(BATCH[:25])
    d.record(BATCH[25:])
    d.observe(OUTCOMES[:30])


# --- the repository on every backend ---------------------------------------------------


def test_replay_on_a_fresh_recorder_reproduces_the_summary(store: RawStorage) -> None:
    repo = repository_for(store)
    a = _durable(repo)
    _feed(a)
    b = _durable(repo)  # a new process: nothing in memory
    assert b.summary() == a.summary() == _live().summary()
    assert b.seq == a.seq == 3
    assert [s.seq for s in b.history()] == [1, 2, 3]
    last = ImportedShadowSummary.model_validate_json(b.history()[-1].body_json)
    assert last == a.summary()


def test_refused_batches_are_not_stored(store: RawStorage) -> None:
    repo = repository_for(store)
    d = _durable(repo)
    d.record(BATCH[:10])
    with pytest.raises(ValueError, match="already recorded"):
        d.record(BATCH[5:15])
    bad = OUTCOMES[0].case.model_copy(update={"amount_minor": OUTCOMES[0].case.amount_minor + 1})
    with pytest.raises(ValueError, match="does not match"):
        d.observe([OUTCOMES[1], ImportedOutcome(case=bad)])
    assert d.observe([ImportedOutcome(case=c) for c, _ in synthetic(3, seed=99)]).updated == 0
    assert repo.last_seq("default", "r-imp", 1) == 1
    fresh = _durable(repo)
    assert fresh.summary() == d.summary() and fresh.summary().resolved == 0


def test_events_and_summaries_are_append_only(store: RawStorage) -> None:
    _durable(repository_for(store)).record(BATCH[:3])
    for sql in (
        "DELETE FROM imported_shadow_events",
        "UPDATE imported_shadow_events SET actor = 'x'",
        "DELETE FROM imported_shadow_summaries",
        "UPDATE imported_shadow_summaries SET body_json = '{}'",
    ):
        with pytest.raises(ImmutableHistoryError):
            store.unsafe_execute(sql)


def test_tenants_and_versions_are_separate(store: RawStorage) -> None:
    repo = repository_for(store)
    _durable(repo, "t1").record(BATCH[:10])
    _durable(repo, "t2").record(BATCH[:4])
    assert _durable(repo, "t1").summary().decisions == 10
    assert _durable(repo, "t2").summary().decisions == 4
    assert _durable(repo, "t3").summary().decisions == 0


def test_two_processes_on_one_database_see_each_others_batches(tmp_path: Path) -> None:
    path = str(tmp_path / "gate.db")
    s1, s2 = SQLiteStorage(path), SQLiteStorage(path)
    a, b = _durable(repository_for(s1)), _durable(repository_for(s2))
    a.record(BATCH[:10])
    b.record(BATCH[10:20])  # b catches up on a's batch before scoring its own
    with pytest.raises(ValueError, match="already recorded"):
        a.record(BATCH[15:25])  # a catches up on b's
    a.observe(OUTCOMES[:20])
    assert a.summary() == b.summary() and b.summary().resolved > 0
    assert [e.seq for e in s1.imported_shadow.events_after("default", "r-imp", 1, 0)] == [1, 2, 3]
    s1.close()
    s2.close()


class _FailingRepo(MemoryImportedShadow):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    def append_event(self, event: ShadowEvent) -> None:
        if self.fail:
            raise OSError("disk full")
        super().append_event(event)


def test_a_failed_write_rolls_memory_back_to_what_was_stored() -> None:
    repo = _FailingRepo()
    d = _durable(repo)
    d.record(BATCH[:10])
    repo.fail = True
    with pytest.raises(OSError):
        d.record(BATCH[10:20])
    repo.fail = False
    assert d.summary().decisions == 10  # rebuilt from storage, not the lost batch
    assert d.record(BATCH[10:20]).total_recorded == 20


def test_a_tampered_event_body_is_detected_on_replay() -> None:
    repo = MemoryImportedShadow()
    _durable(repo).record(BATCH[:3])
    ev = repo._events[0]
    body = json.loads(ev.body_json)
    body["decisions"][0]["action"] = "contest"
    repo._events[0] = ev.model_copy(update={"body_json": json.dumps(body)})
    with pytest.raises(ValueError, match="does not match its hash"):
        _durable(repo).summary()


@settings(max_examples=15, deadline=None)
@given(
    cuts=st.lists(st.integers(1, 39), max_size=4, unique=True),
    observed=st.integers(0, 40),
)
def test_any_batching_replays_to_the_same_state(cuts: list[int], observed: int) -> None:
    repo = MemoryImportedShadow()
    d = _durable(repo)
    bounds = [0, *sorted(cuts), 40]
    for lo, hi in itertools.pairwise(bounds):
        d.record(BATCH[lo:hi])
    if observed:
        d.observe(OUTCOMES[:observed])
    live = ImportedShadowRecorder(Always(Action.ACCEPT), synthetic=True)
    live.record(BATCH)
    if observed:
        live.observe(OUTCOMES[:observed])
    assert _durable(repo).summary() == d.summary() == live.summary()


# --- the API survives a restart -------------------------------------------------------


def _gate(storage: Storage) -> AgentGate:
    g = AgentGate(storage, SteppingClock(), CFG)
    g.bootstrap_baseline()
    with contextlib.suppress(DuplicateContentError):  # proposed before the restart
        g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    return g


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def reopen(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    """A function that opens a fresh Storage on the same database each call."""
    opened: list[Storage] = []
    schema = f"t_{uuid.uuid4().hex[:16]}"
    url = request.getfixturevalue("postgres_url") if request.param == "postgres" else ""

    def open_again() -> Storage:
        s: Storage
        if request.param == "sqlite":
            s = SQLiteStorage(str(tmp_path / "gate.db"))
        else:
            from scruple.storage.postgres import PostgresStorage

            s = PostgresStorage(url, schema=schema)
        opened.append(s)
        return s

    yield open_again
    for s in opened:
        s.close()
    if url:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_api_imported_shadow_survives_a_restart(reopen: Any) -> None:
    g1 = _gate(reopen())
    version = g1.registry.versions(GOOD_RULE_ID)[-1].version
    url = f"/shadow/rules/{GOOD_RULE_ID}/versions/{version}/imported"
    hdr = {"X-Actor": "production"}
    body = {"decisions": [json.loads(d.model_dump_json()) for d in BATCH]}
    out = {"outcomes": [{"case": json.loads(o.case.model_dump_json())} for o in OUTCOMES[:25]]}
    c1 = TestClient(create_app(g1, auth=DEV_AUTH), headers=READER)
    assert c1.post(f"{url}/decisions", json=body, headers=hdr).status_code == 200
    assert c1.post(f"{url}/outcomes", json=out, headers=hdr).json()["updated"] == 25
    before = c1.get(url).json()
    g1.storage.close()

    c2 = TestClient(
        create_app(_gate(reopen()), auth=DEV_AUTH), headers=READER
    )  # a fresh app on the same database
    assert c2.get(url).json() == before
    assert ImportedShadowSummary.model_validate(before).resolved > 0
    assert c2.post(f"{url}/decisions", json=body, headers=hdr).status_code == 409
    hist = c2.get(f"{url}/history").json()
    assert len(hist) == 2 and hist[-1] == before and hist[0]["resolved"] == 0


def test_api_imported_shadow_unknown_rule_is_404(tmp_path: Path) -> None:
    c = TestClient(
        create_app(_gate(SQLiteStorage(str(tmp_path / "g.db"))), auth=DEV_AUTH), headers=READER
    )
    assert c.get("/shadow/rules/nope/versions/1/imported").status_code == 404
