"""Chaos tests (roadmap 170): fail every storage call of every promotion step.

For each state-changing operation of `AgentGate` (propose, evaluate to canary or to
rejection, approve, N-of-M grant that activates, rollback, ramp start, ramp stage,
kill switch, policy change), the operation is first run cleanly to count its storage
calls and record the resulting state. Then, for every call k, it is re-run from the
same starting state with call k failing: an ordinary error on even k, a crash
(`KeyboardInterrupt`, a `BaseException`) on odd k. Each time the stored state must be
exactly the state before or exactly the state after: never a status change without
its audit entry, a decision without its transition, or half a ramp step. The
integrity check must pass, and a retry must then succeed. The same runs on
PostgreSQL, and on a SQLite file whose process is killed (`os._exit`) mid-step.

Gate verdicts are pure functions of (rule, active rules, config), so they are
memoised here to keep the hundreds of re-runs fast. Synthetic data only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest

import scruple.service as service_mod
from scruple.clock import SteppingClock
from scruple.gate.approval import ApprovalPolicy, NamedApprover
from scruple.gate.canary import RampSchedule
from scruple.gate.config import GateConfig
from scruple.gate.engine import Verdict, evaluate_canary, evaluate_shadow
from scruple.registry.models import RuleVersion
from scruple.service import AgentGate
from scruple.sim.generator import generate_cases
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.sim.models import DisputeCase
from scruple.storage import SQLiteStorage, Storage
from tests.ci_support import PROV
from tests.faults import FaultyStorage, InjectedFaultError

ROOT = Path(__file__).resolve().parents[1]
CFG = GateConfig(n_cases=2_000, bootstrap_resamples=300)
APPROVER = "model-risk@demo.bank"
SCHEDULE = RampSchedule(stages=(10.0, 25.0))
POLICY = ApprovalPolicy(
    approvers=(
        NamedApprover(name="alice@demo.bank", roles=("model-risk",)),
        NamedApprover(name="bob@demo.bank"),
    ),
    required=2,
    required_role="model-risk",
)


# --- memoised pure verdicts ---------------------------------------------------------
def _key(rv: RuleVersion, active: Any) -> tuple[str, str, tuple[str, ...]]:
    return (rv.ref, rv.content_hash, tuple(a.ref for a in active))


_MEMO: dict[tuple[str, tuple[str, str, tuple[str, ...]]], Verdict] = {}


def _memo(kind: str, fn: Callable[..., Verdict]) -> Callable[..., Verdict]:
    def wrapped(rv: RuleVersion, active: Any, cfg: GateConfig) -> Verdict:
        k = (kind, _key(rv, active))
        if k not in _MEMO:
            _MEMO[k] = fn(rv, active, cfg)
        return _MEMO[k]

    return wrapped


@pytest.fixture(autouse=True)
def _fast_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "evaluate_shadow", _memo("shadow", evaluate_shadow))
    monkeypatch.setattr(service_mod, "evaluate_canary", _memo("canary", evaluate_canary))


@cache
def _live(stage: int) -> tuple[DisputeCase, ...]:
    return tuple(generate_cases(900 + stage, 2_500, prefix=f"chaos{stage}-"))


# --- scenarios: (setup to reach the starting state, the operation under test) --------
Step = Callable[[AgentGate], Any]


def _propose_good(g: AgentGate) -> None:
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)


def _canary_passed(g: AgentGate) -> None:
    _propose_good(g)
    g.evaluate(GOOD_RULE_ID, 1)


def _active(g: AgentGate) -> None:
    _canary_passed(g)
    g.approve(GOOD_RULE_ID, 1, APPROVER)


def _ramping(g: AgentGate) -> None:
    _canary_passed(g)
    g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)


def _one_grant(g: AgentGate) -> None:
    _canary_passed(g)
    g.set_approval_policy(POLICY, APPROVER)
    g.request_approval(GOOD_RULE_ID, 1, "ops-lead@demo.bank")
    g.grant_approval(GOOD_RULE_ID, 1, "bob@demo.bank")


def _bad_proposed(g: AgentGate) -> None:
    g.propose(BAD_RULE_ID, BAD_RULE, "ops-team", PROV)


SCENARIOS: dict[str, tuple[Step, Step]] = {
    "propose": (lambda g: None, _propose_good),
    "evaluate_to_canary": (_propose_good, lambda g: g.evaluate(GOOD_RULE_ID, 1)),
    "evaluate_to_rejected": (_bad_proposed, lambda g: g.evaluate(BAD_RULE_ID, 1)),
    "approve": (_canary_passed, lambda g: g.approve(GOOD_RULE_ID, 1, APPROVER)),
    "rollback": (_active, lambda g: g.rollback(GOOD_RULE_ID, APPROVER, "chaos drill")),
    "start_ramp": (_canary_passed, lambda g: g.start_canary_ramp(GOOD_RULE_ID, 1, SCHEDULE)),
    "advance_ramp": (_ramping, lambda g: g.advance_canary(GOOD_RULE_ID, 1, list(_live(0)))),
    "breach_ramp": (_ramping, lambda g: g.breach_canary(GOOD_RULE_ID, 1, APPROVER, "alert")),
    "set_policy": (_canary_passed, lambda g: g.set_approval_policy(POLICY, APPROVER)),
    "grant_activates": (
        _one_grant,
        lambda g: g.grant_approval(GOOD_RULE_ID, 1, "alice@demo.bank"),
    ),
}


def snapshot(s: Storage) -> str:
    """Everything stored, as one canonical string."""
    return json.dumps(
        {
            "audit": [e.model_dump(mode="json") for e in s.list_audit()],
            "decisions": s.list_decisions(),
            "rules": [r.model_dump(mode="json") for r in s.list_rule_versions()],
        },
        sort_keys=True,
    )


def _prepared(storage: Storage, name: str) -> tuple[AgentGate, FaultyStorage]:
    faulty = FaultyStorage(storage)
    g = AgentGate(faulty.as_storage(), SteppingClock(), CFG)
    g.bootstrap_baseline()
    SCENARIOS[name][0](g)
    return g, faulty


def _reference(make: Callable[[], Storage], name: str) -> tuple[str, str, int]:
    storage = make()
    g, faulty = _prepared(storage, name)
    before = snapshot(storage)
    start = len(faulty.calls)
    SCENARIOS[name][1](g)
    n = len(faulty.calls) - start
    after = snapshot(storage)
    storage.close()
    assert before != after, f"{name} changed nothing"
    return before, after, n


def _chaos(make: Callable[[], Storage], name: str, ks: range | list[int] | None = None) -> int:
    before, after, n = _reference(make, name)
    tried = 0
    for k in ks if ks is not None else range(n):
        storage = make()
        g, faulty = _prepared(storage, name)
        assert snapshot(storage) == before
        exc: BaseException = InjectedFaultError(f"call {k}") if k % 2 == 0 else KeyboardInterrupt()
        faulty.arm(k, exc)
        with pytest.raises(type(exc)):
            SCENARIOS[name][1](g)
        now = snapshot(storage)
        assert now in (before, after), (
            f"{name}: partial state after a fault at storage call {k} "
            f"({faulty.calls[-1]}); calls: {faulty.calls[-12:]}"
        )
        report = g.verify_integrity()
        assert report.ok, (name, k, report.problems)
        if now == before:  # nothing happened, so the operation can simply be retried
            SCENARIOS[name][1](g)
            assert g.verify_integrity().ok
        storage.close()
        tried += 1
    return tried


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_storage_fault_leaves_all_or_nothing_sqlite(name: str) -> None:
    assert _chaos(lambda: SQLiteStorage(":memory:"), name) > 0


@pytest.fixture
def pg_factory(postgres_url: str) -> Iterator[Callable[[], Storage]]:
    import psycopg

    from scruple.storage.postgres import PostgresStorage

    schemas: list[str] = []

    def make() -> Storage:
        schema = f"chaos_{uuid.uuid4().hex[:12]}"
        schemas.append(schema)
        return PostgresStorage(postgres_url, schema=schema)

    yield make
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        for schema in schemas:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.mark.postgres
@pytest.mark.parametrize("name", ["evaluate_to_canary", "approve", "rollback", "grant_activates"])
def test_every_storage_fault_leaves_all_or_nothing_postgres(
    name: str, pg_factory: Callable[[], Storage]
) -> None:
    _, _, n = _reference(pg_factory, name)
    # every third call on postgres (each run builds a schema); all calls on sqlite above
    assert _chaos(pg_factory, name, list(range(0, n, 3))) > 0


# --- a real crash: the process dies in the middle of a step -------------------------
CHILD = """
import sys
from tests.test_chaos import kill_child
kill_child(sys.argv[1], sys.argv[2], int(sys.argv[3]))
"""


def kill_child(db: str, name: str, k: int) -> None:
    """Child process: open `db`, run scenario `name` and die at storage call k."""
    setattr(service_mod, "evaluate_shadow", _memo("shadow", evaluate_shadow))  # noqa: B010
    setattr(service_mod, "evaluate_canary", _memo("canary", evaluate_canary))  # noqa: B010
    faulty = FaultyStorage(SQLiteStorage(db))
    g = AgentGate(faulty.as_storage(), SteppingClock(), CFG)
    # replay the setup with the same clock so timestamps match the reference run
    g.bootstrap_baseline()
    SCENARIOS[name][0](g)
    faulty.arm(k, kill=True)
    SCENARIOS[name][1](g)
    raise SystemExit(0)  # the fault was past the end: the step completed


@pytest.mark.parametrize("name", ["evaluate_to_canary", "approve", "rollback"])
def test_killing_the_process_mid_step_leaves_all_or_nothing(name: str, tmp_path: Path) -> None:
    before, after, n = _reference(lambda: SQLiteStorage(":memory:"), name)
    for k in sorted({0, 1, n // 3, n // 2, (2 * n) // 3, n - 2, n - 1}):
        db = tmp_path / f"{name}-{k}.db"
        proc = subprocess.run(
            [sys.executable, "-c", CHILD, str(db), name, str(k)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 17, proc.stderr[-2000:]
        s = SQLiteStorage(str(db))
        assert snapshot(s) in (before, after), f"{name}: partial state after kill at call {k}"
        assert AgentGate(s, SteppingClock(), CFG).verify_integrity().ok
        s.close()
