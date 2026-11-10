"""Per-tenant audit genesis: each tenant's hash chain is bound to its tenant.

Before, every tenant's chain started from the same all-zero genesis, so a tenant's
rows copied into another tenant (or another database) still verified. Now a chain
starts from `genesis_hash(tenant)`; the default tenant keeps the old genesis so
chains written before tenancy still verify (docs/ARCHITECTURE.md, tenancy)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.audit.log import verify_entries
from scruple.audit.models import GENESIS_HASH, genesis_hash
from scruple.auth.models import TENANT_PATTERN
from scruple.clock import SteppingClock
from scruple.service import AgentGate
from scruple.storage import SQLiteStorage
from scruple.storage.sql import SQLStorage

TENANTS = st.from_regex(TENANT_PATTERN, fullmatch=True)


def _gate(storage: SQLStorage, tenant: str) -> AgentGate:
    g = AgentGate(storage.for_tenant(tenant), SteppingClock())
    g.bootstrap_baseline()
    return g


def test_default_tenant_keeps_the_legacy_genesis() -> None:
    assert genesis_hash("default") == GENESIS_HASH == "0" * 64
    assert genesis_hash() == GENESIS_HASH


def test_other_tenants_start_from_their_own_genesis() -> None:
    want = hashlib.sha256(b"scruple-audit-genesis\x00bank-a").hexdigest()
    assert genesis_hash("bank-a") == want
    assert len({genesis_hash(t) for t in ("default", "bank-a", "bank-b", "bank-a-")}) == 4


@given(TENANTS, TENANTS)
def test_genesis_is_deterministic_and_distinct(a: str, b: str) -> None:
    assert genesis_hash(a) == genesis_hash(a)
    assert (genesis_hash(a) == genesis_hash(b)) == (a == b)


@pytest.mark.parametrize("tenant", ["default", "bank-a"])
def test_a_tenants_chain_starts_from_its_genesis_and_verifies(tenant: str) -> None:
    storage = SQLiteStorage(":memory:")
    g = _gate(storage, tenant)
    entries = g.audit.entries()
    assert entries and entries[0].prev_hash == genesis_hash(tenant)
    assert g.audit.verify().ok
    assert g.verify_integrity().ok


@pytest.mark.parametrize(
    ("source", "target"),
    [("bank-a", "bank-b"), ("default", "bank-b"), ("bank-a", "default")],
)
def test_a_chain_copied_to_another_tenant_fails_verification(source: str, target: str) -> None:
    """The attack: copy one tenant's audit rows, unchanged, into another tenant."""
    storage = SQLiteStorage(":memory:")
    src = _gate(storage, source)
    entries = src.audit.entries()
    assert src.audit.verify().ok
    victim = storage.for_tenant(target)
    for e in entries:  # the rows exactly as stored, now under the target tenant
        victim.append_audit(e)
    copied = AgentGate(victim, SteppingClock()).audit
    assert copied.entries() == entries
    result = copied.verify()
    assert not result.ok and result.first_bad_seq == 1
    assert "genesis" in (result.problem or "")
    assert src.audit.verify().ok  # the source is untouched


def test_a_chain_copied_to_another_database_fails_under_another_tenant(tmp_path: Path) -> None:
    a = SQLiteStorage(str(tmp_path / "a.db"))
    entries = _gate(a, "bank-a").audit.entries()
    a.close()
    b = SQLiteStorage(str(tmp_path / "b.db")).for_tenant("bank-b")
    for e in entries:
        b.append_audit(e)
    assert not AgentGate(b, SteppingClock()).audit.verify().ok
    assert verify_entries(entries, tenant="bank-a").ok
    assert not verify_entries(entries, tenant="bank-b").ok
    assert not verify_entries(entries).ok  # nor as the default tenant


def test_a_pre_tenancy_chain_still_verifies(tmp_path: Path) -> None:
    """A chain written with the all-zero genesis (every chain before tenancy) is the
    default tenant's and verifies unchanged; the same rows under another tenant fail."""
    entries = _gate(SQLiteStorage(":memory:"), "default").audit.entries()
    assert entries[0].prev_hash == "0" * 64
    assert verify_entries(entries).ok
    assert not verify_entries(entries, tenant="bank-a").ok
