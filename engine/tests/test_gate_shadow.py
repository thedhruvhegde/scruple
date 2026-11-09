"""Shadow mode: live decisions are scored with zero side effects."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scruple.api.app import create_app
from scruple.audit.log import AuditLog
from scruple.clock import SteppingClock
from scruple.domain import Action
from scruple.gate import shadow as shadow_mod
from scruple.gate.config import GateConfig
from scruple.gate.shadow import LiveDecision, ShadowRecorder
from scruple.registry import Registry
from scruple.service import AgentGate
from scruple.sim.generator import generate_cases
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID, baseline_policy
from scruple.sim.models import DisputeCase
from scruple.storage import SQLiteStorage
from tests.ci_support import PROV

CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300)
WRITE_METHODS = (
    "insert_rule_version",
    "update_rule_status",
    "set_rollback_to",
    "append_audit",
    "insert_decision",
)


class CountingStorage(SQLiteStorage):
    """SQLite storage that counts every write call made through the Storage API."""

    def __init__(self) -> None:
        super().__init__(":memory:")
        self.writes: list[str] = []

    def __getattribute__(self, name: str) -> Any:
        attr = super().__getattribute__(name)
        if name in WRITE_METHODS:
            super().__getattribute__("writes").append(name)
        return attr


def _gate(rule_id: str = GOOD_RULE_ID) -> tuple[AgentGate, CountingStorage]:
    st = CountingStorage()
    g = AgentGate(st, SteppingClock(), CFG)
    g.bootstrap_baseline()
    g.propose(rule_id, GOOD_RULE if rule_id == GOOD_RULE_ID else BAD_RULE, "ops-team", PROV)
    return g, st


def _production(cases: list[DisputeCase]) -> list[LiveDecision]:
    """What the production agent did: the synthetic baseline policy."""
    pol = baseline_policy()
    return [LiveDecision(case=c, action=pol.decide(c.view())[0]) for c in cases]


def test_module_imports_nothing_that_can_write() -> None:
    tree = ast.parse(Path(shadow_mod.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    forbidden = (
        "scruple.storage",
        "scruple.service",
        "scruple.audit",
        "scruple.registry.registry",
        "scruple.api",
        "sqlite3",
    )
    assert not {m for m in mods if m and m.startswith(forbidden)}


def _reachable(obj: object, seen: set[int] | None = None, depth: int = 0) -> list[object]:
    seen = set() if seen is None else seen
    if id(obj) in seen or depth > 6:
        return []
    seen.add(id(obj))
    out = [obj]
    children: list[object] = []
    if isinstance(obj, dict):
        children = [*obj.keys(), *obj.values()]
    elif isinstance(obj, list | tuple | set | frozenset):
        children = list(obj)
    elif hasattr(obj, "__dict__") and not isinstance(obj, type):
        children = list(vars(obj).values())
    for c in children:
        out += _reachable(c, seen, depth + 1)
    return out


def test_recorder_holds_no_handle_to_storage_registry_or_audit() -> None:
    g, _ = _gate()
    rec = g.shadow_recorder(GOOD_RULE_ID, 1)
    rec.record(_production(generate_cases(5, 300, prefix="s")))
    bad = (SQLiteStorage, AuditLog, Registry, AgentGate)
    assert not [o for o in _reachable(rec) if isinstance(o, bad)]
    public = sorted(m for m in dir(rec) if not m.startswith("_"))
    assert public == ["active", "candidate", "record", "summary"]


def test_recording_writes_nothing_and_changes_no_state() -> None:
    g, st = _gate()
    before = (g.audit.head(), [r.model_dump() for r in g.registry.all_versions()], g.decisions())
    st.writes.clear()
    rec = g.shadow_recorder(GOOD_RULE_ID, 1)
    for seed in (1, 2, 3):
        rec.record(_production(generate_cases(seed, 400, prefix="s")))
    rec.summary()
    assert st.writes == []
    after = (g.audit.head(), [r.model_dump() for r in g.registry.all_versions()], g.decisions())
    assert before == after
    assert g.verify_integrity().ok


def test_counting_storage_sees_writes() -> None:
    """Guard for the guard: a normal state change is counted."""
    g, st = _gate()
    st.writes.clear()
    g.evaluate(GOOD_RULE_ID, 1)
    assert "append_audit" in st.writes and "update_rule_status" in st.writes


def test_inputs_are_not_mutated_and_no_actions_returned() -> None:
    cases = generate_cases(9, 300, prefix="s")
    decisions = _production(cases)
    snapshot = [d.model_dump() for d in decisions]
    g, _ = _gate()
    result = g.shadow_recorder(GOOD_RULE_ID, 1).record(decisions)
    assert [d.model_dump() for d in decisions] == snapshot
    assert set(type(result).model_fields) == {
        "recorded",
        "agree",
        "candidate_hard_findings",
        "total_recorded",
    }


def test_good_candidate_improves_not_received_only() -> None:
    g, _ = _gate()
    rec = g.shadow_recorder(GOOD_RULE_ID, 1)
    rec.record(_production(generate_cases(11, 2_000, prefix="s")))
    s = rec.summary()
    assert s.data == "synthetic" and s.n == 2_000
    rows = {r.case_type: r for r in s.by_case_type}
    assert set(rows) == {"fraud", "not_as_described", "not_received"}
    inr = rows["not_received"]
    assert inr.candidate_decided > 0
    assert inr.candidate_mean_regret_usd_minor < inr.production_mean_regret_usd_minor
    for t in ("fraud", "not_as_described"):
        assert rows[t].agree == rows[t].n and rows[t].candidate_decided == 0
    assert s.candidate_hard_findings == s.production_hard_findings
    assert s.regret_improvement.low > 0


def test_bad_candidate_shows_new_hard_findings() -> None:
    g, _ = _gate(BAD_RULE_ID)
    rec = g.shadow_recorder(BAD_RULE_ID, 1)
    res = rec.record(_production(generate_cases(11, 2_000, prefix="s")))
    s = rec.summary()
    assert s.candidate_hard_findings > s.production_hard_findings
    assert res.candidate_hard_findings == s.candidate_hard_findings


def test_production_violations_are_attributed_to_production() -> None:
    cases = generate_cases(4, 500, prefix="s")
    reckless = [LiveDecision(case=c, action=Action.CONTEST) for c in cases]
    g, _ = _gate()
    rec = g.shadow_recorder(GOOD_RULE_ID, 1)
    rec.record(reckless)
    s = rec.summary()
    assert s.production_hard_findings > 0 and s.candidate_hard_findings == 0


def test_statistics_accumulate_across_batches_and_match_one_batch() -> None:
    cases = generate_cases(21, 900, prefix="s")
    # Duplicate disputes share a transaction; keep each pair in one batch.
    pairs: dict[str, list[DisputeCase]] = {}
    for c in cases:
        pairs.setdefault(c.txn_id, []).append(c)
    groups = list(pairs.values())
    first = [c for grp in groups[: len(groups) // 2] for c in grp]
    second = [c for grp in groups[len(groups) // 2 :] for c in grp]
    g, _ = _gate()
    split = g.shadow_recorder(GOOD_RULE_ID, 1)
    split.record(_production(first))
    split.record(_production(second))
    whole = g.shadow_recorder(GOOD_RULE_ID, 1)
    whole.record(_production(first + second))
    a, b = split.summary(), whole.summary()
    assert a.by_case_type == b.by_case_type
    assert a.n == b.n == len(cases)


def test_duplicates_rejected_whole_batch() -> None:
    cases = generate_cases(3, 200, prefix="s")
    g, _ = _gate()
    rec = g.shadow_recorder(GOOD_RULE_ID, 1)
    rec.record(_production(cases[:100]))
    with pytest.raises(ValueError, match="already recorded"):
        rec.record(_production(cases[90:150]))
    with pytest.raises(ValueError, match="already recorded"):
        rec.record(_production([cases[150], cases[150]]))
    with pytest.raises(ValueError, match="empty"):
        rec.record([])
    assert rec.summary().n == 100


def test_empty_summary() -> None:
    g, _ = _gate()
    s = g.shadow_recorder(GOOD_RULE_ID, 1).summary()
    assert s.n == 0 and s.by_case_type == () and s.regret_improvement.mean == 0.0


def test_recorder_is_deterministic() -> None:
    def run() -> str:
        g, _ = _gate()
        rec = g.shadow_recorder(GOOD_RULE_ID, 1)
        rec.record(_production(generate_cases(8, 600, prefix="s")))
        return rec.summary().model_dump_json()

    assert run() == run()


def test_recorder_direct_construction_is_pure() -> None:
    g, _ = _gate()
    rv = g.registry.get(GOOD_RULE_ID, 1)
    rec = ShadowRecorder(rv, g.registry.active_rules())
    assert rec.record(_production(generate_cases(2, 100, prefix="s"))).recorded == 100


def test_api_shadow_routes_have_no_side_effects() -> None:
    g, st = _gate()
    client = TestClient(create_app(g))
    head = g.audit.head()
    st.writes.clear()
    body = {
        "decisions": [
            d.model_dump(mode="json") for d in _production(generate_cases(6, 120, prefix="s"))
        ]
    }
    url = f"/shadow/rules/{GOOD_RULE_ID}/versions/1"
    r = client.post(f"{url}/decisions", json=body, headers={"X-Actor": "production"})
    assert r.status_code == 200 and r.json()["recorded"] == 120
    r = client.post(f"{url}/decisions", json=body, headers={"X-Actor": "production"})
    assert r.status_code == 409
    r = client.post(f"{url}/decisions", json=body, headers={"X-Actor": "someone-else"})
    assert r.status_code == 403
    summary = client.get(url).json()
    assert summary["n"] == 120 and summary["data"] == "synthetic"
    bad = {"decisions": [{**body["decisions"][0], "execute": True}]}
    assert (
        client.post(f"{url}/decisions", json=bad, headers={"X-Actor": "production"}).status_code
        == 422
    )
    assert client.get("/shadow/rules/nope-rule/versions/1").status_code == 404
    assert st.writes == [] and g.audit.head() == head
