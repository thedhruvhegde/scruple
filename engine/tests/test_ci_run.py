from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scruple.ci.scorecard import (
    Scorecard,
    Verdict,
    build_scorecard,
    exit_code_for,
    load_scorecard,
    verdict_of,
)
from scruple.cli.main import main
from scruple.gate.config import GateConfig
from scruple.gate.models import Decision, DecisionRecord, Outcome, Stage
from scruple.gate.provider import BootstrapProvider, StatsProvider
from scruple.gate.stats import paired_bootstrap_ci
from scruple.sim.library import BAD_RULE_ID, GOOD_RULE_ID
from tests.ci_support import BAD, GOOD, TOML, cached_run, write_repo


def _rec(outcome: Outcome, stage: Stage = Stage.SHADOW) -> DecisionRecord:
    d = Decision(
        decision_id="dec-00001",
        stage=stage,
        outcome=outcome,
        rule_id="r-1",
        version=1,
        content_hash="0" * 64,
        rule_description="d",
        rule_logic="l",
        owner="o",
        agent_id="a",
        episodes=0,
        actor="x",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        summary="s",
        reasons=(),
    )
    return DecisionRecord(decision=d, decision_sha256="1" * 64, audit_seq=1, audit_entry_hash="2")


@pytest.mark.parametrize(
    ("outcome", "verdict"),
    [
        (Outcome.PROMOTE, Verdict.PASS),
        (Outcome.CANARY_PASSED, Verdict.PASS),
        (Outcome.ACTIVATED, Verdict.PASS),
        (Outcome.HEALTHY, Verdict.PASS),
        (Outcome.HOLD, Verdict.HOLD),
        (Outcome.BLOCK, Verdict.BLOCK),
        (Outcome.ROLLBACK, Verdict.BLOCK),
        (Outcome.ROLLED_BACK, Verdict.BLOCK),
    ],
)
def test_verdict_follows_last_decision(outcome: Outcome, verdict: Verdict) -> None:
    assert verdict_of([_rec(Outcome.PROMOTE), _rec(outcome, Stage.CANARY)]) is verdict


@pytest.mark.parametrize(
    ("verdict", "fail_on_hold", "code"),
    [
        (Verdict.PASS, True, 0),
        (Verdict.PASS, False, 0),
        (Verdict.HOLD, True, 1),
        (Verdict.HOLD, False, 0),
        (Verdict.BLOCK, True, 1),
        (Verdict.BLOCK, False, 1),
    ],
)
def test_exit_code_table(verdict: Verdict, fail_on_hold: bool, code: int) -> None:
    assert exit_code_for(verdict, fail_on_hold) == code


def test_scorecard_needs_a_decision() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_scorecard([], GateConfig(), "0" * 64)


def test_good_candidate_passes() -> None:
    card = cached_run("good").scorecard
    assert card.verdict is Verdict.PASS and card.exit_code == 0
    assert [d.outcome for d in card.decisions] == [Outcome.PROMOTE, Outcome.CANARY_PASSED]
    assert card.regressions == ()
    assert card.statistics is not None and card.statistics.ci_low > 0
    assert card.canary is not None and card.data == "synthetic"
    assert card.rule_id == GOOD_RULE_ID


def test_bad_candidate_blocks_with_regressions() -> None:
    card = cached_run("bad").scorecard
    assert card.verdict is Verdict.BLOCK and card.exit_code == 1
    kinds = {(r.kind, r.name) for r in card.regressions}
    assert ("check", "no_new_policy_violations") in kinds
    assert ("slice", "late_evidence") in kinds
    assert card.canary is None and card.rule_id == BAD_RULE_ID


def test_scorecard_json_round_trips_and_is_sorted() -> None:
    card = cached_run("good").scorecard
    text = card.to_json()
    assert load_scorecard(text) == card
    assert list(json.loads(text)) == sorted(json.loads(text))


def test_scorecard_is_hashed_against_config_and_chain() -> None:
    run = cached_run("good")
    assert run.scorecard.audit_head == run.gate.audit.head()
    assert run.gate.verify_integrity().ok


def test_cli_run_good_writes_scorecard(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_repo(tmp_path, GOOD)
    assert main(["ci", "run", "--config", str(cfg)]) == 0
    card = Scorecard.model_validate_json((tmp_path / "out" / "scorecard.json").read_text())
    assert card.verdict is Verdict.PASS
    assert "scruple gate: PASS" in capsys.readouterr().out


def test_cli_run_bad_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = write_repo(tmp_path, BAD)
    out = tmp_path / "card.json"
    assert main(["ci", "run", "--config", str(cfg), "--out", str(out)]) == 1
    assert load_scorecard(out.read_text()).verdict is Verdict.BLOCK
    assert "REGRESSION" in capsys.readouterr().out


def test_cli_candidate_override(tmp_path: Path) -> None:
    cfg = write_repo(tmp_path, GOOD)
    bad = tmp_path / "other.json"
    bad.write_text(json.dumps(BAD.model_dump(mode="json")))
    out = tmp_path / "c.json"
    assert main(["ci", "run", "--config", str(cfg), "--candidate", str(bad), "--out", str(out)])
    assert load_scorecard(out.read_text()).rule_id == BAD_RULE_ID


def test_cli_run_is_byte_reproducible(tmp_path: Path) -> None:
    outs = []
    for i in range(2):
        cfg = write_repo(tmp_path / str(i), GOOD)
        out = tmp_path / f"c{i}.json"
        main(["ci", "run", "--config", str(cfg), "--out", str(out)])
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]


def test_cli_run_with_db_file(tmp_path: Path) -> None:
    cfg = write_repo(tmp_path, GOOD)
    db = tmp_path / "gate.db"
    out = tmp_path / "c.json"
    assert main(["ci", "run", "--config", str(cfg), "--db", str(db), "--out", str(out)]) == 0
    assert db.exists()


@pytest.mark.parametrize(
    "toml",
    [
        TOML + "\n[unknown]\nx = 1\n",
        TOML.replace("n_cases = 2000", "n_cases = 5"),
        "not toml at all [[[",
        "[ci]\nfail_on_hold = true\n",  # candidate section missing
    ],
)
def test_bad_config_is_a_usage_error(tmp_path: Path, toml: str) -> None:
    cfg = write_repo(tmp_path, GOOD, toml)
    assert main(["ci", "run", "--config", str(cfg)]) == 2


def test_missing_config_is_a_usage_error(tmp_path: Path) -> None:
    assert main(["ci", "run", "--config", str(tmp_path / "nope.toml")]) == 2


def test_bootstrap_provider_matches_existing_stats() -> None:
    diffs = [0] * 900 + [5, -3, 12, 40, -7] * 20
    p = BootstrapProvider(resamples=300, seed=3)
    assert isinstance(p, StatsProvider)
    iv = p.paired_interval(diffs, 0.9)
    assert (iv.mean, iv.low, iv.high) == paired_bootstrap_ci(diffs, 300, 0.9, 3)
    assert iv.n == len(diffs) and iv.method.endswith("paired")


def test_two_sample_interval_contains_mean_and_is_seeded() -> None:
    p = BootstrapProvider(resamples=400, seed=1)
    t = [float(x % 17) for x in range(200)]
    c = [float(x % 11) for x in range(150)]
    a, b = p.two_sample_interval(t, c, 0.95), p.two_sample_interval(t, c, 0.95)
    assert a == b and a.low <= a.mean <= a.high
    empty = p.two_sample_interval([], c, 0.95)
    assert (empty.mean, empty.low, empty.high) == (0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="positive"):
        BootstrapProvider(resamples=0)
