"""One source of truth for scoring (round 7, finding 1).

Before round 7 the ach, aml, recon and payables episodes scored themselves with their
v1 `*_CHECKS` while the registry's default was `@3`, so every surface that reads an
episode's own scorecard (the MCP sim server, the stores, the CLI) paid v1: do-nothing
earned 0.64-0.74 there and 0 offline.

Two layers of test:

1. **Static**: every call site in `scruple/` that builds a scorecard (a `score_*(`
   call, `run_checks(` / `run_workflow_checks(`, a `*_CHECKS` constant, `get_verifier(`,
   `.verifier(`, `Scorecard(`) must be in `ALLOWED`, with the reason it may score
   without the default (it defines a verifier, reproduces a named version, or is a
   research harness that names its version). A new scoring path anywhere else fails
   this test until it is routed through `default_verifier` or listed here with a reason.
2. **Behavioural**: every agent-facing surface returns the check ids of
   `default_verifier(env)`, and an explicit version (`verifier_version`) reproduces an
   older one end to end.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scruple.finops.verifiers import (
    DEFAULT_VERSION,
    ENV_NAMES,
    default_verifier,
    default_verifier_id,
    verifier_version,
)

ROOT = Path(__file__).resolve().parents[1] / "scruple"
PATTERN = re.compile(
    r"\bscore_(?:recon|aml|ach|payables|commerce|record|batch|profile|batch_profile)\(|"
    r"\brun_(?:workflow_)?checks\(|\b[A-Z]+_CHECKS(?:_V\d)?\b|\bget_verifier\(|"
    r"\.verifier\(|\bScorecard\("
)

# file -> why it may score without default_verifier (keep the reasons honest)
ALLOWED: dict[str, str] = {
    # the verifiers themselves and the registry that versions them
    "finops/verifiers.py": "the registry: defines every <env>@N and default_verifier",
    "verifiers/ach.py": "defines ACH_CHECKS (ach@1) and score_ach",
    "verifiers/aml.py": "defines AML_CHECKS (aml@1) and score_aml",
    "verifiers/recon.py": "defines RECON_CHECKS (recon@1) and score_recon",
    "verifiers/payables.py": "defines PAY_CHECKS (payables@1) and score_payables",
    "verifiers/commerce.py": "defines CM_CHECKS (commerce@2) and score_commerce",
    "verifiers/dispute.py": "defines DISPUTE_CHECKS_V2/V3; score_record defaults to the default",
    "verifiers/v3.py": "builds the @3 check lists",
    "verifiers/checks.py": "defines Scorecard and run_checks",
    "verifiers/profiles.py": "explicit compliance profiles over the round-6 dispute card",
    "console/sources.py": "operator console: the named compliance profile the operator picks",
    # research harnesses that name the version they measure
    "verifiers/meta.py": "meta-evaluation of the round-6 dispute scorecard (named version)",
    "verifiers/exploits/base.py": "exploit harness: score_batch with the default verifier",
    "redteam/differential.py": "differential testing pins @2 by design",
    "redteam/envs.py": "lists every check id for coverage; scores with e.verifier()",
    "redteam/inaction.py": "takes the verifier id to measure as an argument",
    "finops/exploits.py": "exploit table per named verifier id (default when omitted)",
    "finops/anchoring.py": "anchoring study takes a named verifier (default when omitted)",
    "finops/parallel.py": "bulk grading by a named verifier id (default when omitted)",
    "finops/difficulty.py": "measured difficulty with the env's default verifier",
    "finops/tasks.py": "task export/grading: the spec's verifier id (default at export)",
    "suite/plan.py": "lists the env's default verifier in the plan",
    "suite/envs.py": "suite runner: get_env(env).verifier() is the default verifier",
    # other things called Scorecard / *_CHECKS that are not verifier scorecards
    "agents/sdk.py": "the SDK's own Scorecard model (a view of the episode's card)",
    "ci/scorecard.py": "the CI gate's Scorecard model (not a verifier card)",
    "gate/shadow_imported.py": "ACTION_CHECKS is a set of gate action names",
    # benches that score with the default (score_record without checks)
    "bench/runner.py": "score_record(rec): the default verifier",
    "zoo/rollout.py": "score_record(...): the default verifier",
}


def _hits() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith("verifiers/claimtext/"):
            continue
        lines = [
            f"{i}: {line.strip()}"
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if PATTERN.search(line) and not line.lstrip().startswith("#")
        ]
        if lines:
            out[rel] = lines
    return out


def test_every_scoring_call_site_is_accounted_for() -> None:
    unknown = {f: ls[:3] for f, ls in _hits().items() if f not in ALLOWED}
    assert not unknown, (
        "new scoring path(s) that do not go through scruple.finops.verifiers."
        f"default_verifier (route them through it, or add a reasoned entry): {unknown}"
    )


def test_the_allow_list_has_no_stale_entries() -> None:
    hits = _hits()
    stale = [f for f in ALLOWED if f not in hits]
    assert not stale, f"ALLOWED lists files that no longer score: {stale}"


AGENT_FACING = (
    "agents/ach_episode.py",
    "agents/aml_episode.py",
    "agents/recon_episode.py",
    "agents/payables_episode.py",
    "agents/commerce_episode.py",
    "agents/dispute_episode.py",
    "agents/store.py",
    "agents/store_db.py",
    "agents/cli.py",
    "mcp/sim_server.py",
    "finops/server.py",
    "finops/envs.py",
)


def test_agent_facing_modules_never_name_a_check_list() -> None:
    """The episodes, stores, CLI, MCP server and task server reach a verifier only
    through `score_default` / `default_verifier` (or the env registry, which calls it)."""
    bad = {f: ls for f, ls in _hits().items() if f in AGENT_FACING}
    assert not bad, bad
    for f in AGENT_FACING[:6]:
        assert "score_default(" in (ROOT / f).read_text(), f


def test_default_is_v4_and_explicit_versions_reproduce() -> None:
    assert DEFAULT_VERSION == 4
    for env in ENV_NAMES:
        assert default_verifier(env).id == f"{env}@4"
        assert default_verifier(env, 3).id == f"{env}@3"
        with verifier_version(3):
            assert default_verifier_id(env) == f"{env}@3"
        assert default_verifier_id(env) == f"{env}@4"


# --- behavioural: each agent-facing surface returns the default verifier's checks -----
def _ids(env: str, version: int | None = None) -> list[str]:
    return [c.id for c in default_verifier(env, version).checks]


def _floor_report(kind: str) -> dict[str, object]:
    return {"explanation": "Submitted without acting."}


@pytest.mark.parametrize("kind", ["ach", "recon", "aml", "payables", "commerce"])
def test_episode_card_mcp_and_stores_use_the_default(kind: str, tmp_path: Path) -> None:
    from scruple.agents.store import EpisodeStore, new_episode
    from scruple.mcp.sim_server import EpisodeTable

    ep = new_episode(kind, 3, None, 0, "k")
    ep.call("submit_decision", _floor_report(kind))
    assert [c.id for c in ep.score.scorecard.checks] == _ids(kind)

    t = EpisodeTable()
    m = t.start_kind(kind, 3, None)
    out = t.call(m.episode_id, "submit_decision", _floor_report(kind), kinds={kind})
    assert [c["id"] for c in out["checks"]] == _ids(kind)
    assert out["reward"] == 0.0  # do-nothing earns nothing on MCP, as offline

    st = EpisodeStore(tmp_path)
    e2 = st.start(3, None, kind)
    st.call(e2.episode_id, "submit_decision", _floor_report(kind))
    loaded = st.load(e2.episode_id)
    assert [c.id for c in loaded.score.scorecard.checks] == _ids(kind)

    with verifier_version(3):
        e3 = new_episode(kind, 3, None, 0, "k")
        e3.call("submit_decision", _floor_report(kind))
        assert [c.id for c in e3.score.scorecard.checks] == _ids(kind, 3)


def test_dispute_episode_card_uses_the_default() -> None:
    from scruple.agents.dispute_episode import DisputeEpisode

    ep = DisputeEpisode(5, None, salt="k")
    ep.call("submit_decision", {"explanation": "x", "claims": {"outcome": "pending"}})
    assert [c.id for c in ep.score.scorecard.checks] == _ids("dispute")


def test_db_store_and_durable_mcp_table_use_the_default() -> None:
    from scruple.agents.store_db import DbEpisodeStore, durable_table
    from scruple.storage.sqlite import SQLiteStorage

    store = DbEpisodeStore.on(SQLiteStorage(":memory:"))
    t = durable_table(store)
    ep = t.start_kind("aml", 4, None)
    out = t.call(ep.episode_id, "submit_decision", {"explanation": "x"}, kinds={"aml"})
    assert [c["id"] for c in out["checks"]] == _ids("aml")


def test_task_server_suite_runner_and_registry_use_the_default() -> None:
    from fastapi.testclient import TestClient

    from scruple.finops.envs import ENVS, get_env
    from scruple.finops.server import build_app
    from scruple.finops.tasks import build_task

    for name, e in ENVS.items():
        assert e.default_verifier == default_verifier_id(name)
        assert e.verifier().id == default_verifier_id(name)
    spec = build_task("payables", 2, None, "s", measure=False)
    assert spec.verifier.id == "payables@4"
    cl = TestClient(build_app({spec.task_id: spec}, salt="s"))
    eid = cl.post("/v1/episodes", json={"task_id": spec.task_id}).json()["episode_id"]
    g = cl.get(f"/v1/episodes/{eid}/grade").json()
    assert [c["id"] for c in g["checks"]] == _ids("payables")
    from scruple.suite.envs import _finops_outcome

    ep = get_env("ach").new_episode(1, None, salt="k")
    ep.call("submit_decision", {"explanation": "x"})
    out = _finops_outcome("ach")(ep)
    assert set(out.hard_failures) <= set(_ids("ach"))
