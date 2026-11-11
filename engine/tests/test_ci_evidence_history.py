"""Evidence packs carry the partner-history source of a CI scorecard (JSON and PDF)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from scruple.ci.evidence import load_pack
from scruple.ci.scorecard import load_scorecard
from scruple.cli.main import main
from tests.ci_support import write_repo
from tests.test_ci_history import FRAUD, synth_objs, write_store

STEM = f"evidence-{FRAUD.rule_id}-v1"


def _pdf_text(data: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One persisted CI run with history and an evidence pack; tests only read it."""
    tmp = tmp_path_factory.mktemp("evh")
    store = write_store(tmp / "h.db", synth_objs(600, 3))
    cfg = write_repo(tmp / "repo", FRAUD)
    args = ["ci", "run", "--config", str(cfg), "--out", str(tmp / "card.json")]
    args += ["--db", str(tmp / "g.db"), "--history", str(store), "--evidence", str(tmp / "ev")]
    main(args)
    return tmp


def test_pack_from_a_history_run_holds_both_sources(run: Path) -> None:
    card = load_scorecard((run / "card.json").read_text())
    pack, claimed = load_pack((run / "ev" / f"{STEM}.json").read_text())
    assert pack.pack_sha256 == claimed and pack.audit.proof.ok
    assert card.history is not None
    assert pack.scorecard.history == card.history
    assert pack.scorecard.synthetic_verdict == card.synthetic_verdict
    assert pack.scorecard.verdict == card.verdict
    assert pack.scorecard.exit_code == card.exit_code
    assert pack.data == card.data and "SYNTHETIC" in pack.data


def test_pdf_shows_the_history_section(run: Path) -> None:
    card = load_scorecard((run / "card.json").read_text())
    assert card.history is not None
    text = _pdf_text((run / "ev" / f"{STEM}.pdf").read_bytes())
    for needle in (
        f"Partner history: {card.history.verdict.upper()} (SYNTHETIC DATA)",
        card.history.history_sha256,
        f"Backtest verdict: {card.history.backtest_verdict}",
        "observed",
        "estimated",
        f"Overall verdict {card.verdict.value.upper()}",
        "synthetic data and SYNTHETIC history",
    ):
        assert needle in text, needle
    for r in card.history.by_reason:
        assert r.reason in text


def test_evidence_command_takes_history_from_the_scorecard(run: Path) -> None:
    cfg = run / "repo" / "scruple.toml"
    out = run / "again"
    args = ["ci", "evidence", "--db", str(run / "g.db"), FRAUD.rule_id, "1", "--out", str(out)]
    assert main([*args, "--config", str(cfg), "--scorecard", str(run / "card.json")]) == 0
    for suffix in (".json", ".pdf"):  # the same bytes as the pack the run wrote
        assert (out / f"{STEM}{suffix}").read_bytes() == (
            run / "ev" / f"{STEM}{suffix}"
        ).read_bytes()


def test_without_a_scorecard_the_pack_has_no_history(run: Path) -> None:
    out = run / "plain"
    args = ["ci", "evidence", "--db", str(run / "g.db"), FRAUD.rule_id, "1", "--out", str(out)]
    assert main([*args, "--config", str(run / "repo" / "scruple.toml")]) == 0
    body = json.loads((out / f"{STEM}.json").read_text())
    assert "history" not in body["scorecard"] and body["data"] == "synthetic"
    assert "Partner history" not in _pdf_text((out / f"{STEM}.pdf").read_bytes())


@pytest.mark.parametrize(
    ("field", "value"),
    [("rule_id", "someone-else"), ("version", 2), ("decisions", [])],
)
def test_a_scorecard_from_another_run_is_refused(
    run: Path, field: str, value: object, capsys: pytest.CaptureFixture[str]
) -> None:
    body = json.loads((run / "card.json").read_text())
    body[field] = value
    other = run / f"other-{field}.json"
    other.write_text(json.dumps(body))
    out = run / f"refused-{field}"
    args = ["ci", "evidence", "--db", str(run / "g.db"), FRAUD.rule_id, "1", "--out", str(out)]
    assert (
        main([*args, "--config", str(run / "repo" / "scruple.toml"), "--scorecard", str(other)])
        == 2
    )
    assert "error:" in capsys.readouterr().err
    assert not (out / f"{STEM}.json").exists()


def test_pack_hash_covers_the_history(run: Path) -> None:
    body = json.loads((run / "ev" / f"{STEM}.json").read_text())
    body["scorecard"]["history"]["n_observed"] += 1
    pack, claimed = load_pack(json.dumps(body))
    assert pack.pack_sha256 != claimed


def test_history_packs_are_deterministic(tmp_path: Path) -> None:
    store = write_store(tmp_path / "h.db", synth_objs(400, 5))
    cfg = write_repo(tmp_path / "repo", FRAUD)
    outs = []
    for i in range(2):  # in-memory database and fixed clock: reproducible
        ev = tmp_path / f"ev{i}"
        args = ["ci", "run", "--config", str(cfg), "--out", str(tmp_path / f"c{i}.json")]
        main([*args, "--history", str(store), "--evidence", str(ev)])
        outs.append(((ev / f"{STEM}.json").read_bytes(), (ev / f"{STEM}.pdf").read_bytes()))
    assert outs[0] == outs[1]
    assert b'"history"' in outs[0][0]
