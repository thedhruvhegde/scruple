"""Shared fixtures for the CI and gate-extension tests (synthetic data only)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from scruple.ci.config import CandidateFile, CIConfig, load_config
from scruple.ci.runner import CIRun, run_candidate
from scruple.domain import RuleSpec
from scruple.registry import Provenance
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID

PROV = Provenance(agent_id="dispute-agent-1", episode_ids=("ep-1", "ep-2", "ep-3"))
APPROVER = "model-risk@demo.bank"

TOML = """\
[candidate]
file = "candidate.json"

[gate]
n_cases = 2000
bootstrap_resamples = 500

[ci]
fail_on_hold = true
scorecard = "out/scorecard.json"
"""


def candidate(rule_id: str, spec: RuleSpec, owner: str = "ops-team") -> CandidateFile:
    return CandidateFile(rule_id=rule_id, spec=spec, owner=owner, provenance=PROV)


GOOD = candidate(GOOD_RULE_ID, GOOD_RULE)
BAD = candidate(BAD_RULE_ID, BAD_RULE)


def write_repo(root: Path, cand: CandidateFile, toml: str = TOML) -> Path:
    """A minimal 'repository' with scruple.toml and a candidate rule file."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "scruple.toml").write_text(toml)
    (root / "candidate.json").write_text(json.dumps(cand.model_dump(mode="json"), indent=2))
    return root / "scruple.toml"


def config(tmp: Path) -> CIConfig:
    return load_config(write_repo(tmp, GOOD))


@cache
def cached_run(which: str) -> CIRun:
    """One gated run per candidate per session; runs are deterministic, so sharing is safe
    for tests that only read the result."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="scruple-ci-"))
    cand = GOOD if which == "good" else BAD
    cfg = load_config(write_repo(tmp, cand))
    return run_candidate(cfg, cand)
