"""Audit tamper tests at every chain position (roadmap 171).

A real chain (a promotion, a rejection, an activation and a rollback, nineteen
entries) is tampered with at every index, in every way: edit each field, edit and
re-hash, delete, insert a forged entry, insert a copy, swap neighbours, delete and
renumber, and rewrite the whole tail. Verification must fail and `first_bad_seq`
must name the first position that cannot be trusted. Attacks at the very end (and a
full tail rewrite) are internally consistent by construction, so they must fail
against the externally anchored head instead. The same attacks are then applied to
the database file itself and caught by `AgentGate.verify_integrity`, and to an
evidence-pack segment. Synthetic data only.
"""

from __future__ import annotations

from datetime import timedelta
from functools import cache
from pathlib import Path

import pytest

from scruple.audit.log import compute_hash, verify_entries
from scruple.audit.models import GENESIS_HASH, AuditEntry
from scruple.ci.evidence import verify_segment
from scruple.clock import SteppingClock
from scruple.gate.config import GateConfig
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300)
APPROVER = "model-risk@demo.bank"


def _populate(g: AgentGate) -> None:
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    g.evaluate(GOOD_RULE_ID, 1)
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)
    g.evaluate(BAD_RULE_ID, 1)
    g.approve(GOOD_RULE_ID, 1, APPROVER)
    g.rollback(GOOD_RULE_ID, APPROVER, "tamper drill")


@cache
def chain() -> tuple[AuditEntry, ...]:
    g = AgentGate(SQLiteStorage(":memory:"), SteppingClock(), CFG)
    _populate(g)
    return tuple(g.audit.entries())


N = 19  # checked below; positions are parametrised up front


def test_the_chain_is_long_and_valid() -> None:
    es = chain()
    assert len(es) == N
    res = verify_entries(es, expected_head=es[-1].entry_hash)
    assert res.ok and res.first_bad_seq is None


def _rehash(e: AuditEntry, **update: object) -> AuditEntry:
    e = e.model_copy(update=update)
    return e.model_copy(update={"entry_hash": compute_hash(e)})


EDITS: dict[str, dict[str, object]] = {
    "payload": {"payload": {"tampered": True}},
    "actor": {"actor": "mallory@demo.bank"},
    "event_type": {"event_type": "rule.tampered"},
    "time": {},  # filled per entry: one second later
    "prev_hash": {"prev_hash": "e" * 64},
    "entry_hash": {"entry_hash": "f" * 64},
}
POSITIONS = list(range(1, N + 1))


def _edited(e: AuditEntry, field: str) -> AuditEntry:
    update = dict(EDITS[field])
    if field == "time":
        update = {"at": e.at + timedelta(seconds=1)}
    return e.model_copy(update=update)


@pytest.mark.parametrize("field", sorted(EDITS))
@pytest.mark.parametrize("i", POSITIONS)
def test_editing_any_field_at_any_position_is_caught_there(i: int, field: str) -> None:
    es = list(chain())
    es[i - 1] = _edited(es[i - 1], field)
    res = verify_entries(es)
    assert not res.ok
    assert res.first_bad_seq == i, res.problem


@pytest.mark.parametrize("i", POSITIONS)
def test_edit_and_rehash_is_caught_at_the_next_link_or_the_anchor(i: int) -> None:
    es = list(chain())
    head = es[-1].entry_hash
    es[i - 1] = _rehash(es[i - 1], payload={"tampered": True})
    res = verify_entries(es, expected_head=head)
    assert not res.ok
    if i < N:
        assert res.first_bad_seq == i + 1 and "prev_hash" in (res.problem or "")
    else:  # the last entry: only the anchored head can tell
        assert verify_entries(es).ok
        assert res.first_bad_seq is None and "anchored head" in (res.problem or "")


@pytest.mark.parametrize("i", POSITIONS)
def test_deleting_any_entry_is_caught_at_its_position(i: int) -> None:
    es = list(chain())
    head = es[-1].entry_hash
    del es[i - 1]
    res = verify_entries(es, expected_head=head)
    assert not res.ok
    if i < N:
        assert res.first_bad_seq == i, res.problem
    else:  # truncation
        assert verify_entries(es).ok and res.first_bad_seq is None


@pytest.mark.parametrize("i", POSITIONS)
def test_deleting_and_renumbering_is_caught_at_its_position(i: int) -> None:
    es = list(chain())
    head = es[-1].entry_hash
    del es[i - 1]
    es = [*es[: i - 1], *(e.model_copy(update={"seq": e.seq - 1}) for e in es[i - 1 :])]
    res = verify_entries(es, expected_head=head)
    assert not res.ok
    assert res.first_bad_seq == (i if i < N else None), res.problem


@pytest.mark.parametrize("i", [*POSITIONS, N + 1])
def test_inserting_a_forged_entry_is_caught_right_after_it(i: int) -> None:
    """A forger can hash a well-formed entry at position i (there is no key), so the
    first position that fails is the original entry, now at i + 1, or the anchor."""
    es = list(chain())
    head = es[-1].entry_hash
    prev = GENESIS_HASH if i == 1 else es[i - 2].entry_hash
    base = es[min(i, N) - 1]
    forged = _rehash(
        base,
        seq=i,
        prev_hash=prev,
        event_type="rule.activated",
        actor="mallory@demo.bank",
        payload={"rule_id": BAD_RULE_ID, "version": 1},
    )
    es.insert(i - 1, forged)
    res = verify_entries(es, expected_head=head)
    assert not res.ok
    assert res.first_bad_seq == (i + 1 if i <= N else None), res.problem


@pytest.mark.parametrize("i", POSITIONS)
def test_inserting_a_copy_is_caught(i: int) -> None:
    es = list(chain())
    es.insert(i, es[i - 1])  # replay entry i right after itself
    res = verify_entries(es)
    assert not res.ok and res.first_bad_seq == i + 1


@pytest.mark.parametrize("i", POSITIONS[:-1])
def test_swapping_neighbours_is_caught_at_the_first(i: int) -> None:
    es = list(chain())
    es[i - 1], es[i] = es[i], es[i - 1]
    res = verify_entries(es)
    assert not res.ok and res.first_bad_seq == i, res.problem


@pytest.mark.parametrize("i", POSITIONS)
def test_rewriting_the_whole_tail_needs_the_anchor(i: int) -> None:
    """Recompute every hash from position i on: consistent, so only the anchored head
    (kept outside the system) exposes it."""
    es = list(chain())
    head = es[-1].entry_hash
    prev = GENESIS_HASH if i == 1 else es[i - 2].entry_hash
    for j in range(i - 1, N):
        upd: dict[str, object] = {"prev_hash": prev}
        if j == i - 1:
            upd["actor"] = "mallory@demo.bank"
        es[j] = _rehash(es[j], **upd)
        prev = es[j].entry_hash
    assert verify_entries(es).ok
    res = verify_entries(es, expected_head=head)
    assert not res.ok and res.first_bad_seq is None


# --- the same attacks on the stored database ------------------------------------
@pytest.fixture(scope="module")
def db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("tamper") / "base.db"
    g = AgentGate(SQLiteStorage(str(path)), SteppingClock(), CFG)
    _populate(g)
    g.storage.close()
    return path


def _open_copy(template: Path, tmp_path: Path) -> tuple[AgentGate, SQLiteStorage]:
    dst = tmp_path / "t.db"
    dst.write_bytes(template.read_bytes())
    s = SQLiteStorage(str(dst))
    s.unsafe_execute("DROP TRIGGER audit_no_update")
    s.unsafe_execute("DROP TRIGGER audit_no_delete")
    return AgentGate(s, SteppingClock(), CFG), s


@pytest.mark.parametrize("attack", ["update_actor", "update_payload", "delete", "renumber"])
@pytest.mark.parametrize("i", sorted({*POSITIONS[::3], N}))
def test_raw_sql_tampering_is_reported_with_its_position(
    db_template: Path, tmp_path: Path, attack: str, i: int
) -> None:
    g, s = _open_copy(db_template, tmp_path)
    head = g.audit.head()
    if attack == "update_actor":
        s.unsafe_execute(f"UPDATE audit_events SET actor='mallory' WHERE seq={i}")
    elif attack == "update_payload":
        s.unsafe_execute(f"UPDATE audit_events SET payload_json='{{}}' WHERE seq={i}")
    elif attack == "delete":
        s.unsafe_execute(f"DELETE FROM audit_events WHERE seq={i}")
    else:
        s.unsafe_execute(f"DELETE FROM audit_events WHERE seq={i}")
        s.unsafe_execute(f"UPDATE audit_events SET seq=seq-1 WHERE seq>{i}")
    report = g.verify_integrity(expected_head=head)
    assert not report.ok
    if attack in ("delete", "renumber") and i == N:
        assert report.chain.first_bad_seq is None  # truncation: caught by the anchor
    else:
        assert report.chain.first_bad_seq == i
        assert f"(seq {i})" in report.problems[0]
    s.close()


@pytest.mark.parametrize("i", POSITIONS[::4])
def test_a_tampered_evidence_segment_is_refused(i: int) -> None:
    es = list(chain())
    head = es[-1].entry_hash
    seg = es[i - 1 :]
    assert verify_segment(seg, head, N, True).ok
    bad = [_edited(seg[0], "payload"), *seg[1:]]
    assert not verify_segment(bad, head, N, True).ok
    if len(seg) > 1:
        assert not verify_segment(seg[1:2] + seg[:1] + seg[2:], head, N, True).ok
        assert not verify_segment(seg[:-1], head, N, True).ok  # truncated segment
