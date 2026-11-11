"""Tenant policy files (scruple.policy): parsing, hashing, compiling, storing, applying,
the API and the CLI. Synthetic data only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import Role, key_permissions
from scruple.ci.evidence import build_evidence_pack
from scruple.ci.scorecard import build_scorecard
from scruple.clock import SteppingClock
from scruple.gate.approval import ApprovalError
from scruple.gate.config import GateConfig
from scruple.hashing import content_hash
from scruple.policy import Policy, PolicyFileError, diff, dump_policy, load_policy, parse_policy
from scruple.policy.apply import PolicyGates, apply_policy, configured, current_policy
from scruple.policy.cli import add_policy_parser
from scruple.policy.files import MAX_BYTES
from scruple.policy.models import band_labels
from scruple.policy.store import PolicyStore
from scruple.service import AgentGate
from scruple.sim.library import GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from scruple.verifiers.profiles import VerifierProfile
from tests.ci_support import APPROVER, PROV

YAML = """\
policy_version: 1
name: acme-disputes
description: synthetic example
stats:
  provider: evalue
  alpha: 0.05
  beta: 0.2
case_types:
  late_evidence: {tolerance_usd_minor: 200}
  "fraud/<$80": {tolerance_usd_minor: 0, min_detectable_regression_usd_minor: 400}
fairness: {profile: balanced}
approval:
  approvers:
    - {name: alice@bank.example, roles: [model-risk]}
    - {name: bob@bank.example}
  required: 2
  required_role: model-risk
canary:
  stages: [5, 10, 25, 50]
"""

TOML = """\
policy_version = 1
name = "acme-disputes"
description = "synthetic example"

[stats]
beta = 0.2
alpha = 0.05
provider = "evalue"

[case_types.late_evidence]
tolerance_usd_minor = 200

[case_types."fraud/<$80"]
min_detectable_regression_usd_minor = 400
tolerance_usd_minor = 0

[fairness]
profile = "balanced"

[approval]
required = 2
required_role = "model-risk"
approvers = [
  {name = "alice@bank.example", roles = ["model-risk"]},
  {name = "bob@bank.example"},
]

[canary]
stages = [5, 10, 25, 50]
"""


def _yaml(text: str = YAML) -> Policy:
    return parse_policy(text.encode(), "yaml")


def _doc() -> dict[str, Any]:
    out: dict[str, Any] = yaml.safe_load(YAML)
    return out


# --- parsing and hashing -----------------------------------------------------------


def test_yaml_toml_and_json_give_the_same_policy_and_hash() -> None:
    a = _yaml()
    b = parse_policy(TOML.encode(), "toml")
    c = parse_policy(json.dumps(_doc()).encode(), "json")
    assert a == b == c
    assert a.policy_sha256 == b.policy_sha256 == c.policy_sha256
    assert len(a.policy_sha256) == 64


def test_the_hash_changes_with_any_threshold() -> None:
    doc = _doc()
    doc["case_types"]["late_evidence"]["tolerance_usd_minor"] = 201
    assert Policy.model_validate(doc).policy_sha256 != _yaml().policy_sha256


@settings(max_examples=30, deadline=None)
@given(st.randoms(use_true_random=False))
def test_key_order_never_changes_the_hash(rnd: Any) -> None:
    def shuffle(obj: Any) -> Any:
        if isinstance(obj, dict):
            items = list(obj.items())
            rnd.shuffle(items)
            return {k: shuffle(v) for k, v in items}
        if isinstance(obj, list):
            return [shuffle(v) for v in obj]
        return obj

    text = yaml.safe_dump(shuffle(_doc()), sort_keys=False)
    assert _yaml(text).policy_sha256 == _yaml().policy_sha256


def test_canonical_dump_round_trips() -> None:
    p = _yaml()
    for fmt in ("yaml", "json"):
        assert parse_policy(dump_policy(p, fmt).encode(), fmt) == p
    with pytest.raises(PolicyFileError):
        dump_policy(p, "toml")


@pytest.mark.parametrize(
    ("text", "fmt", "needle"),
    [
        ("name: a\nname: b\n", "yaml", "duplicate key 'name'"),
        ("stats: {provider: evalue, provider: bootstrap}\nname: a\n", "yaml", "duplicate"),
        ('{"name": "a", "name": "b"}', "json", "duplicate key"),
        ('name = "a"\nname = "b"\n', "toml", "not valid toml"),
        ("- a\n- b\n", "yaml", "mapping"),
        ("name: [unclosed\n", "yaml", "not valid yaml"),
        ("name: a\nbogus: 1\n", "yaml", "bogus"),
        ("name: Not Valid\n", "yaml", "name"),
        ("name: a\npolicy_version: 2\n", "yaml", "policy_version"),
        ("name: a\nstats: {provider: magic}\n", "yaml", "stats.provider"),
        ("name: a\ncase_types: {late_evidence: {tolerance_usd_minor: 1}}\n", "yaml", "evalue"),
        (
            "name: a\nstats: {provider: evalue}\ncase_types: {lunar: {tolerance_usd_minor: 1}}\n",
            "yaml",
            "unknown scenario",
        ),
        (
            "name: a\nstats: {provider: evalue}\ncase_types: {'fraud/80': {}}\n",
            "yaml",
            "band must look like",
        ),
        (
            "name: a\nstats: {provider: evalue}\ncase_types: {'fraud/<$81': {}}\n",
            "yaml",
            "no band",
        ),
        ("name: a\ncase_types: {}\nstats: {band_edges_usd_minor: [5, 3]}\n", "yaml", "increasing"),
        ("name: a\ncanary: {stages: [50, 10]}\n", "yaml", "canary"),
        ("name: a\napproval: {approvers: [{name: x@y.z}], required: 2}\n", "yaml", "approval"),
        ("name: a\nfairness: {profile: lenient}\n", "yaml", "fairness"),
        ("name: !!python/object:os.system x\n", "yaml", "not valid yaml"),
    ],
)
def test_invalid_policies_are_refused_with_a_reason(text: str, fmt: str, needle: str) -> None:
    with pytest.raises(PolicyFileError) as exc:
        parse_policy(text.encode(), fmt)
    assert needle in str(exc.value)


def test_size_and_encoding_limits() -> None:
    with pytest.raises(PolicyFileError, match="larger than"):
        parse_policy(b"#" * (MAX_BYTES + 1), "yaml")
    with pytest.raises(PolicyFileError, match="UTF-8"):
        parse_policy(b"name: \xff\n", "yaml")
    with pytest.raises(PolicyFileError, match="format"):
        parse_policy(b"name: a", "ini")


def test_load_policy_by_suffix(tmp_path: Path) -> None:
    for name, text in (("p.yaml", YAML), ("p.yml", YAML), ("p.toml", TOML)):
        (tmp_path / name).write_text(text)
        assert load_policy(tmp_path / name) == _yaml()
    (tmp_path / "p.ini").write_text("x")
    with pytest.raises(PolicyFileError, match="ends in"):
        load_policy(tmp_path / "p.ini")
    with pytest.raises(PolicyFileError):
        load_policy(tmp_path / "missing.yaml")
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (MAX_BYTES + 1))
    with pytest.raises(PolicyFileError, match="larger"):
        load_policy(big)


# --- compiling onto the gate config -------------------------------------------------


def test_gate_config_carries_thresholds_provider_and_hash() -> None:
    p = _yaml()
    cfg = p.gate_config(GateConfig(n_cases=2_000))
    assert cfg.n_cases == 2_000  # untouched base settings survive
    assert cfg.stats == "evalue" and cfg.evalue_alpha == 0.05 and cfg.evalue_beta == 0.2
    assert cfg.evalue_type_tolerances_usd_minor == {"late_evidence": 200.0, "fraud/<$80": 0.0}
    assert cfg.evalue_type_min_detectable_regression_usd_minor == {"fraud/<$80": 400.0}
    assert cfg.policy_sha256 == p.policy_sha256
    assert p.fairness.profile is VerifierProfile.BALANCED
    assert p.ramp().stages == (5.0, 10.0, 25.0, 50.0)
    assert Policy(name="x").ramp().stages == (5.0, 10.0, 25.0, 50.0)


def test_config_without_policy_hashes_as_before() -> None:
    cfg = GateConfig()
    assert "policy_sha256" not in cfg.model_dump()
    stamped = cfg.model_copy(update={"policy_sha256": "a" * 64})
    assert content_hash(stamped) != content_hash(cfg)


def test_band_edges_from_the_policy_define_the_bands() -> None:
    p = Policy.model_validate(
        {
            "name": "x",
            "stats": {"provider": "evalue", "band_edges_usd_minor": [5_000]},
            "case_types": {"fraud/<$50": {"tolerance_usd_minor": 1}},
        }
    )
    assert p.gate_config().evalue_band_edges_usd_minor == (5_000,)
    assert band_labels((5_000, 1_000_050)) == ("<$50", "<$10,000.50", ">=$10,000.50")


def test_diff_lists_every_changed_leaf() -> None:
    a = _yaml()
    doc = _doc()
    doc["stats"]["provider"] = "evalue_vr"
    del doc["case_types"]["late_evidence"]
    doc["case_types"]["high_value"] = {"tolerance_usd_minor": 5}
    doc["canary"]["stages"] = [5, 50]
    b = Policy.model_validate(doc)
    lines = [c.line() for c in diff(a, b)]
    assert "~ stats.provider: 'evalue' -> 'evalue_vr'" in lines
    assert "- case_types.late_evidence.tolerance_usd_minor: 200.0" in lines
    assert "+ case_types.high_value.tolerance_usd_minor: 5.0" in lines
    assert any(line.startswith("~ canary.stages") for line in lines)
    assert diff(a, a) == ()
    assert [c.path for c in diff(a, b)] == sorted(c.path for c in diff(a, b))


# --- storage, apply, audit ----------------------------------------------------------


def _gate(tenant: str = "default") -> AgentGate:
    s = SQLiteStorage(":memory:")
    return AgentGate(s.for_tenant(tenant), SteppingClock(), GateConfig(n_cases=2_000))


def test_reading_a_fresh_database_creates_nothing() -> None:
    s = SQLiteStorage(":memory:")
    g = AgentGate(s, SteppingClock())
    assert current_policy(g) is None and PolicyStore(g.storage).history() == []
    assert configured(g) is g
    rows = s.connection.query("SELECT name FROM sqlite_master WHERE name='tenant_policies'")
    assert rows == []


def test_apply_is_versioned_audited_and_idempotent() -> None:
    g = _gate()
    p = _yaml()
    rec, changed = apply_policy(g, p, APPROVER)
    assert changed and rec.number == 1 and rec.policy_sha256 == p.policy_sha256
    again, changed = apply_policy(g, p, APPROVER)
    assert not changed and again.number == 1
    doc = _doc()
    doc["stats"]["alpha"] = 0.01
    rec2, changed = apply_policy(g, Policy.model_validate(doc), APPROVER)
    assert changed and rec2.number == 2
    assert [r.number for r in PolicyStore(g.storage).history()] == [1, 2]
    assert PolicyStore(g.storage).get(1) == rec and PolicyStore(g.storage).get(9) is None
    events = [e for e in g.audit.entries() if e.event_type == "policy.set"]
    assert [e.payload["number"] for e in events] == [1, 2]
    assert events[1].payload["previous_sha256"] == p.policy_sha256
    # the approval section was put in force once (unchanged the second time)
    assert g.approval_policy() == p.approval
    assert sum(e.event_type == "approval.policy_set" for e in g.audit.entries()) == 1
    assert g.verify_integrity().ok


def test_an_approval_section_needs_a_gate_operator() -> None:
    g = _gate()
    with pytest.raises(ApprovalError):
        apply_policy(g, _yaml(), "alice@bank.example")
    assert current_policy(g) is None  # nothing written
    assert not [e for e in g.audit.entries() if e.event_type == "policy.set"]
    # without an approval section, any admin may apply it
    plain = Policy(name="plain")
    rec, _ = apply_policy(g, plain, "local-admin")
    assert rec.number == 1


def test_policies_are_per_tenant() -> None:
    s = SQLiteStorage(":memory:")
    a = AgentGate(s.for_tenant("bank-a"), SteppingClock())
    b = AgentGate(s.for_tenant("bank-b"), SteppingClock())
    apply_policy(a, Policy(name="a-only"), "local-admin")
    assert current_policy(a) is not None and current_policy(b) is None
    assert PolicyStore(b.storage).current_sha256() is None
    assert configured(b) is b
    assert configured(a).config.policy_sha256 == Policy(name="a-only").policy_sha256


def test_policy_gates_follow_the_current_policy() -> None:
    g = _gate()
    gates = PolicyGates()
    assert gates.gate(g) is g
    apply_policy(g, Policy(name="one"), "local-admin")
    first = gates.gate(g)
    assert first.config.policy_sha256 == Policy(name="one").policy_sha256
    assert gates.gate(g) is first  # cached while the policy is unchanged
    apply_policy(g, Policy(name="two"), "local-admin")
    assert gates.gate(g).config.policy_sha256 == Policy(name="two").policy_sha256


def test_decisions_scorecards_and_packs_name_the_policy() -> None:
    g = AgentGate(
        SQLiteStorage(":memory:"),
        SteppingClock(),
        GateConfig(n_cases=2_000, bootstrap_resamples=300),
    )
    g.bootstrap_baseline()
    p = Policy(name="pinned", stats={"bootstrap_resamples": 300})  # type: ignore[arg-type]
    apply_policy(g, p, "local-admin")
    g = configured(g)
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    records = g.evaluate(GOOD_RULE_ID, 1)
    shas = {
        e.payload.get("policy_sha256")
        for e in g.audit.entries()
        if e.event_type == "decision.recorded" and e.payload["rule_id"] == GOOD_RULE_ID
    }
    assert shas == {p.policy_sha256}
    card = build_scorecard(records, g.config, g.audit.head())
    assert card.policy_sha256 == p.policy_sha256
    assert json.loads(card.to_json())["policy_sha256"] == p.policy_sha256
    pack = build_evidence_pack(g, GOOD_RULE_ID, 1)
    assert pack.policy_sha256 == p.policy_sha256
    assert json.loads(pack.to_json())["policy_sha256"] == p.policy_sha256
    assert g.verify_integrity().ok


def test_no_policy_keeps_scorecards_and_packs_unchanged() -> None:
    g = AgentGate(
        SQLiteStorage(":memory:"),
        SteppingClock(),
        GateConfig(n_cases=2_000, bootstrap_resamples=300),
    )
    g.bootstrap_baseline()
    g.propose(GOOD_RULE_ID, GOOD_RULE, "ops-team", PROV)
    records = g.evaluate(GOOD_RULE_ID, 1)
    assert "policy_sha256" not in json.loads(build_scorecard(records, g.config, "h").to_json())
    assert "policy_sha256" not in json.loads(build_evidence_pack(g, GOOD_RULE_ID, 1).to_json())
    assert all("policy_sha256" not in e.payload for e in g.audit.entries())


# --- HTTP ---------------------------------------------------------------------------


def _client() -> tuple[TestClient, dict[str, dict[str, str]], AgentGate]:
    db = SQLiteStorage(":memory:")
    root = AgentGate(db, SteppingClock(), GateConfig(n_cases=1_000))
    root.bootstrap_baseline()
    keys = ApiKeyService(db, params=ScryptParams(n=1024), permissions_for=key_permissions)
    hdr: dict[str, dict[str, str]] = {}
    for subject, role in (
        ("auditor", Role.AUDITOR),
        ("admin", Role.RISK_ADMIN),
        (APPROVER, Role.RISK_ADMIN),
    ):
        _, tok = keys.create(subject, [], "local-admin", roles=[role])
        hdr[subject] = {"X-API-Key": tok.get_secret_value()}
    return TestClient(create_app(root, auth=Authenticator(keys))), hdr, root


def test_policy_routes_and_permissions() -> None:
    c, hdr, root = _client()
    assert c.get("/policy").status_code == 401
    assert c.get("/policy", headers=hdr["auditor"]).status_code == 404
    body = _doc()
    assert c.post("/policy", json=body, headers=hdr["auditor"]).status_code == 403
    # an approval section needs a gate operator, even for an admin
    assert c.post("/policy", json=body, headers=hdr["admin"]).status_code == 403
    r = c.post("/policy", json=body, headers=hdr[APPROVER])
    assert r.status_code == 200, r.text
    assert r.json()["changed"] and r.json()["policy_sha256"] == _yaml().policy_sha256
    assert c.post("/policy", json=body, headers=hdr[APPROVER]).json()["changed"] is False
    got = c.get("/policy", headers=hdr["auditor"]).json()
    assert got["number"] == 1 and Policy.model_validate(got["policy"]) == _yaml()
    assert len(c.get("/policy/history", headers=hdr["auditor"]).json()) == 1
    bad = {**body, "unknown": 1}
    assert c.post("/policy", json=bad, headers=hdr[APPROVER]).status_code == 422
    nob = {"name": "x", "stats": {"provider": "evalue"}, "case_types": {"fraud/<$81": {}}}
    assert c.post("/policy", json=nob, headers=hdr["admin"]).status_code == 422
    # the API's gate now runs under the policy
    gate = c.app.state.gates.get("default")  # type: ignore[attr-defined]
    assert gate.config.policy_sha256 == _yaml().policy_sha256
    assert root.config.policy_sha256 is None


# --- CLI ----------------------------------------------------------------------------


def _run(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_policy_parser(sub, "x.db")
    args = parser.parse_args(argv)
    return int(args.fn(args))


def test_cli_validate_show_diff_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCRUPLE_DATABASE_URL", raising=False)
    monkeypatch.delenv("SCRUPLE_TENANT", raising=False)
    good = tmp_path / "p.yaml"
    good.write_text(YAML)
    other = tmp_path / "q.toml"
    other.write_text(TOML.replace("alpha = 0.05", "alpha = 0.01"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: a\nbogus: 1\n")
    assert _run(["policy", "validate", str(good), str(other)]) == 0
    assert _yaml().policy_sha256 in capsys.readouterr().out
    assert _run(["policy", "validate", str(good), str(bad)]) == 1
    assert "bogus" in capsys.readouterr().err
    assert _run(["policy", "show", str(good)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"# policy_sha256: {_yaml().policy_sha256}")
    assert _run(["policy", "show", str(good), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["policy_sha256"] == _yaml().policy_sha256
    assert _run(["policy", "diff", str(good), str(good)]) == 0
    assert "(no changes)" in capsys.readouterr().out
    assert _run(["policy", "diff", str(good), str(other)]) == 1
    assert "~ stats.alpha: 0.05 -> 0.01" in capsys.readouterr().out
    assert _run(["policy", "diff", str(good), str(other), "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["changes"][0]["path"] == "stats.alpha"
    assert _run(["policy", "diff", str(good), str(bad)]) == 2
    capsys.readouterr()

    db = str(tmp_path / "g.db")
    assert _run(["policy", "show", "--current", "--db", db]) == 1
    assert "no policy" in capsys.readouterr().err
    assert _run(["policy", "apply", str(good), "--db", db]) == 1  # approval needs an operator
    assert "refused" in capsys.readouterr().err
    assert _run(["policy", "apply", str(bad), "--db", db]) == 1
    capsys.readouterr()
    assert _run(["policy", "apply", str(good), "--db", db, "--actor", APPROVER]) == 0
    assert "#1 applied" in capsys.readouterr().out
    assert _run(["policy", "apply", str(good), "--db", db, "--actor", APPROVER]) == 0
    assert "already in force" in capsys.readouterr().out
    assert _run(["policy", "show", "--current", "--db", db]) == 0
    assert _yaml().policy_sha256 in capsys.readouterr().out
    assert _run(["policy", "diff", "@current", str(other), "--db", db]) == 1
    assert "stats.alpha" in capsys.readouterr().out
    assert _run(["policy", "history", "--db", db]) == 0
    assert APPROVER in capsys.readouterr().out
