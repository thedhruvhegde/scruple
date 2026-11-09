"""`scruple ci run --history`: gate a candidate on partner history next to the synthetic
gate. Histories here are SYNTHETIC Stripe-shaped (`scruple.calibration.stripe_disputes`)
or the vendored real Stripe fixture; the two sources stay separate on the scorecard."""

from __future__ import annotations

import copy
import json
import random
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.backtest.history import load_store, load_stripe
from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.ci.cli import add_ci_parser  # noqa: F401 - the parser under test
from scruple.ci.comment import render_comment
from scruple.ci.config import CandidateFile, load_config
from scruple.ci.history import (
    HistorySection,
    combine,
    gate_on_history,
    history_sha256,
    load_history,
    verdict_for,
)
from scruple.ci.runner import run_candidate
from scruple.ci.scorecard import Scorecard, Verdict, load_scorecard, with_history
from scruple.ci.status import description
from scruple.cli.main import main
from scruple.domain import RuleSpec
from scruple.ingest.pipeline import IngestOptions, ingest
from scruple.ingest.store import CaseStore
from tests.ci_support import GOOD, PROV, TOML, cached_run, write_repo

FRAUD = CandidateFile(
    rule_id="fraud-contest-all",
    spec=RuleSpec.model_validate(
        {
            "description": "Contest every fraud dispute.",
            "conditions": [{"field": "reason_code", "op": "eq", "value": "fraud"}],
            "action": "contest",
            "priority": 10,
        }
    ),
    owner="ops-team",
    provenance=PROV,
)
REAL: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "stripe" / "fixtures3.subset.json").read_text()
)["resources"]


@cache
def synth_objs(n: int = 1500, seed: int = 11) -> tuple[dict[str, Any], ...]:
    return tuple(stripe_disputes(DEFAULT_PROFILE, n, seed))


def write_store(path: Path, objs: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> Path:
    st_ = CaseStore(path)
    ingest(json.dumps(list(objs)), IngestOptions(source="stripe"), st_)
    st_.close()
    return path


def cli_run(tmp: Path, cand: CandidateFile, *extra: str) -> tuple[int, Scorecard]:
    cfg = write_repo(tmp / "repo", cand)
    out = tmp / "card.json"
    rc = main(["ci", "run", "--config", str(cfg), "--out", str(out), *extra])
    return rc, load_scorecard(out.read_text())


# --- verdicts --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "worst"),
    [
        ("pass", "pass", "pass"),
        ("pass", "hold", "hold"),
        ("hold", "pass", "hold"),
        ("pass", "block", "block"),
        ("block", "hold", "block"),
        ("hold", "hold", "hold"),
    ],
)
def test_combine_is_the_worst(a: str, b: str, worst: str) -> None:
    assert combine(a, b) == worst


@pytest.fixture(scope="module")
def fraud_report() -> Any:
    hist = load_stripe(json.dumps(list(synth_objs())), label="t")
    _, rep = gate_on_history(hist, FRAUD.rule_id, FRAUD.spec, HistorySection(file="-"))
    return rep


@pytest.mark.parametrize(
    ("verdict", "robust", "expected"),
    [
        ("worse", True, "block"),
        ("worse", False, "block"),
        ("better", True, "pass"),
        ("better", False, "hold"),
        ("insufficient evidence", True, "hold"),
        ("no change", False, "pass"),
    ],
)
def test_history_verdict_mapping(
    fraud_report: Any, verdict: str, robust: bool, expected: str
) -> None:
    rep = fraud_report.model_copy(update={"verdict": verdict, "robust": robust})
    v, why = verdict_for(rep)
    assert v == expected and why


@pytest.mark.parametrize(
    ("synthetic", "history", "overall", "code"),
    [
        (Verdict.PASS, "pass", Verdict.PASS, 0),
        (Verdict.PASS, "hold", Verdict.HOLD, 1),
        (Verdict.PASS, "block", Verdict.BLOCK, 1),
        (Verdict.BLOCK, "pass", Verdict.BLOCK, 1),
    ],
)
def test_with_history_keeps_both_verdicts(
    synthetic: Verdict, history: str, overall: Verdict, code: int
) -> None:
    card = cached_run("good").scorecard.model_copy(update={"verdict": synthetic})
    hist = load_stripe(json.dumps(list(synth_objs(200, 1))), label="t")
    hg, _ = gate_on_history(hist, GOOD.rule_id, GOOD.spec, HistorySection(file="-"))
    out = with_history(card, hg.model_copy(update={"verdict": history}))
    assert out.synthetic_verdict is synthetic and out.verdict is overall
    assert out.history is not None and out.history.verdict == history
    assert out.exit_code == code


# --- end to end ------------------------------------------------------------------------


def test_cli_history_from_a_case_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = write_store(tmp_path / "h.db", synth_objs())
    rc, card = cli_run(tmp_path, FRAUD, "--history", str(db))
    out = capsys.readouterr().out
    assert "source synthetic held-out cases" in out and "source partner history" in out
    assert card.history is not None and card.synthetic_verdict is not None
    h = card.history
    assert h.synthetic and h.format == "store" and h.n_estimated > 0 and h.n_observed > 0
    assert h.n_included == h.n_observed + h.n_estimated
    assert {r.reason for r in h.by_reason} >= {"fraudulent"}
    assert "anytime-valid" in h.method
    assert card.verdict.value == combine(card.synthetic_verdict.value, h.verdict)
    assert rc == card.exit_code
    assert "SYNTHETIC" in card.data


def test_history_path_is_deterministic(tmp_path: Path) -> None:
    db = write_store(tmp_path / "h.db", synth_objs())
    cfg = write_repo(tmp_path / "repo", FRAUD)
    outs = []
    for i in range(2):
        out = tmp_path / f"card{i}.json"
        main(["ci", "run", "--config", str(cfg), "--out", str(out), "--history", str(db)])
        outs.append(out.read_bytes())
    assert outs[0] == outs[1]
    assert str(tmp_path) not in outs[0].decode()  # no machine-specific path in the card


def test_store_and_export_give_the_same_history_result(tmp_path: Path) -> None:
    objs = synth_objs(600, 3)
    db = write_store(tmp_path / "h.db", objs)
    export = tmp_path / "h.json"
    export.write_text(json.dumps(list(objs)))
    a = gate_on_history(load_store(str(db)), FRAUD.rule_id, FRAUD.spec, HistorySection(file="-"))[0]
    b = gate_on_history(
        load_history(HistorySection(file=str(export))),
        FRAUD.rule_id,
        FRAUD.spec,
        HistorySection(file="-"),
    )[0]
    assert a.history_sha256 == b.history_sha256
    assert a.model_dump(exclude={"dataset", "format"}) == b.model_dump(
        exclude={"dataset", "format"}
    )


@settings(max_examples=5, deadline=None)
@given(seed=st.integers(0, 2**16))
def test_property_history_order_does_not_matter(seed: int) -> None:
    objs = list(synth_objs(300, 5))
    shuffled = copy.deepcopy(objs)
    random.Random(seed).shuffle(shuffled)
    a = load_stripe(json.dumps(objs), label="x")
    b = load_stripe(json.dumps(shuffled), label="x")
    assert history_sha256(a) == history_sha256(b)
    ga = gate_on_history(a, FRAUD.rule_id, FRAUD.spec, HistorySection(file="-"))[0]
    gb = gate_on_history(b, FRAUD.rule_id, FRAUD.spec, HistorySection(file="-"))[0]
    assert ga.model_dump_json() == gb.model_dump_json()


def test_without_history_the_scorecard_is_unchanged(tmp_path: Path) -> None:
    _, card = cli_run(tmp_path, GOOD)
    body = json.loads(card.to_json())
    assert "history" not in body and "synthetic_verdict" not in body
    assert card.data == "synthetic"
    assert card.to_json() == cached_run("good").scorecard.to_json()


def test_real_fixture_history_is_insufficient_evidence(tmp_path: Path) -> None:
    d = copy.deepcopy(REAL["dispute"])
    d.update(status="lost", reason="fraudulent")
    export = tmp_path / "real.json"
    export.write_text(json.dumps([d]))
    rc, card = cli_run(tmp_path, FRAUD, "--history", str(export), "--history-format", "stripe")
    h = card.history
    assert h is not None and not h.synthetic and h.n_included == 1
    assert h.verdict == "hold" and h.backtest_verdict == "insufficient evidence"
    assert "below k" in h.profile and "fewer than k" in h.why
    assert card.data == "synthetic held-out cases and partner history"
    assert rc == 1


def test_config_history_section_is_resolved_against_the_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_store(repo / "cases.db", synth_objs(300, 2))
    toml = TOML + '\n[history]\nfile = "cases.db"\nlabel = "pilot history"\n'
    cfg = load_config(write_repo(repo, GOOD, toml))
    assert cfg.history is not None
    run = run_candidate(cfg, GOOD)
    assert run.backtest is not None and run.scorecard.history is not None
    assert "pilot history" in run.scorecard.history.dataset


def test_csv_history(tmp_path: Path) -> None:
    from scruple.ingest.models import CaseState
    from scruple.ingest.tabular import ColumnMapping, ImportConfig, StatusTarget
    from scruple.sim.models import Stage

    cfg = ImportConfig(
        columns=ColumnMapping(
            external_id="id",
            payment_ref="pay",
            amount="amt",
            currency="ccy",
            status="st",
            reason="why",
            opened_at="at",
        ),
        status_map={"lost": StatusTarget(stage=Stage.RESOLVED, state=CaseState.LOST)},
        reason_map={"fraud": "fraud"},  # type: ignore[dict-item]
    )
    conf = tmp_path / "import.json"
    conf.write_text(cfg.model_dump_json())
    rows = "".join(f"c{i},p{i},10.00,USD,lost,fraud,2026-01-01\n" for i in range(30))
    csv = tmp_path / "h.csv"
    csv.write_text("id,pay,amt,ccy,st,why,at\n" + rows)
    _, card = cli_run(tmp_path, FRAUD, "--history", str(csv), "--history-config", str(conf))
    assert card.history is not None and card.history.format == "csv"
    assert card.history.n_estimated == 30  # every lost fraud case would now be contested


def test_unknown_history_format_is_a_usage_error(tmp_path: Path) -> None:
    junk = tmp_path / "h.bin"
    junk.write_bytes(b"\x00\x01")
    cfg = write_repo(tmp_path / "repo", GOOD)
    assert main(["ci", "run", "--config", str(cfg), "--history", str(junk)]) == 2


# --- rendering ---------------------------------------------------------------------------


def test_comment_and_status_show_both_sources(tmp_path: Path) -> None:
    db = write_store(tmp_path / "h.db", synth_objs())
    _, card = cli_run(tmp_path, FRAUD, "--history", str(db))
    md = render_comment(card)
    assert "### Sources" in md and "| synthetic held-out cases |" in md
    assert "### Partner history" in md and "SYNTHETIC data" in md
    assert "observed" in md and "estimated" in md and "anytime-valid" in md
    assert render_comment(card) == md
    text = description(card)
    assert "history" in text and len(text) <= 140
    assert "Synthetic simulator data only" not in md
