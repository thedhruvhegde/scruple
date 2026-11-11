"""`scruple pilot run` end to end: a labelled SYNTHETIC history (`backtest synth`), edited
copies of the vendored real Stripe fixtures, a case store, and a fake Stripe API."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scruple.calibration import DEFAULT_PROFILE, stripe_disputes
from scruple.cli.main import main
from scruple.ingest.errors import IngestError
from scruple.pilot.run import MANIFEST, PilotManifest, PilotOptions, run_pilot, sha256_file
from tests.test_ingest_stripe_pull import FIXTURES, KEY, T0, FakeStripe, history

ROOT = Path(__file__).resolve().parents[1]
RULE = ROOT / "docs" / "pilot" / "demo" / "fraud.json"
STEPS = ["ingest", "calibration", "backtest", "shadow", "gate", "evidence"]


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("hist") / "synthetic.json"
    path.write_text(json.dumps(stripe_disputes(DEFAULT_PROFILE, 500, 11), sort_keys=True))
    return path


@pytest.fixture(scope="module")
def first(synth: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, PilotManifest]:
    out = tmp_path_factory.mktemp("run") / "pilot"
    m, rebuilt = run_pilot(
        PilotOptions(history=str(synth), candidate=str(RULE), out=str(out), n_cases=200)
    )
    assert rebuilt
    return out, m


def test_every_step_runs_and_is_recorded(first: tuple[Path, PilotManifest]) -> None:
    out, m = first
    assert [s.name for s in m.steps] == STEPS
    assert all(s.status == "ok" for s in m.steps)
    assert m.data == "synthetic" and m.history["cases"] == 500
    assert PilotManifest.model_validate_json((out / MANIFEST).read_text()) == m
    for s in m.steps:
        for rel, sha in s.outputs.items():
            assert sha256_file(out / rel) == sha
    assert (out / "01-ingest" / "cases.db").is_file()
    assert m.step("ingest").summary["raw_pans_found"] == 0
    bt = json.loads((out / "03-backtest" / "report.json").read_text())
    assert bt["synthetic"] and bt["verdict"] == m.step("backtest").summary["verdict"]
    assert bt["calibration"] is None or isinstance(bt["calibration"], dict)
    card = json.loads((out / "05-gate" / "scorecard.json").read_text())
    assert card["history"]["synthetic"] is True
    assert m.step("gate").summary["verdict"] == card["verdict"]


def test_rerun_is_a_no_op_and_rebuild_is_byte_identical(
    synth: Path, first: tuple[Path, PilotManifest], tmp_path: Path
) -> None:
    out, m = first
    before = (out / MANIFEST).read_bytes()
    again, rebuilt = run_pilot(
        PilotOptions(history=str(synth), candidate=str(RULE), out=str(out), n_cases=200)
    )
    assert not rebuilt and again == m and (out / MANIFEST).read_bytes() == before
    other = tmp_path / "elsewhere"
    m2, rebuilt2 = run_pilot(
        PilotOptions(history=str(synth), candidate=str(RULE), out=str(other), n_cases=200)
    )
    assert rebuilt2 and (other / MANIFEST).read_bytes() == before
    assert m2 == m


def test_a_tampered_output_or_new_option_rebuilds(
    synth: Path, first: tuple[Path, PilotManifest], tmp_path: Path
) -> None:
    out = tmp_path / "p"
    opts = PilotOptions(history=str(synth), candidate=str(RULE), out=str(out), n_cases=200)
    m, _ = run_pilot(opts)
    (out / "04-shadow" / "shadow.json").write_text("{}")
    m2, rebuilt = run_pilot(opts)
    assert rebuilt and m2 == m
    m3, rebuilt = run_pilot(opts.model_copy(update={"k": 12}))
    assert rebuilt and m3.run_key != m.run_key


def _fixture_export(n: int) -> list[dict[str, Any]]:
    """Edited copies of the vendored real Stripe dispute (ids, times, status, reason)."""
    out = []
    statuses = ["won", "lost", "needs_response", "lost", "won"]
    reasons = ["fraudulent", "product_not_received", "fraudulent", "general"]
    for i in range(n):
        d = copy.deepcopy(FIXTURES["dispute"])
        d.update(
            id=f"dp_fx{i:04d}",
            charge=f"ch_fx{i:04d}",
            created=T0 + i * 86_400,
            status=statuses[i % len(statuses)],
            reason=reasons[i % len(reasons)],
            amount=1_000 + 37 * i,
        )
        out.append(d)
    return out


def test_vendored_fixture_export_with_an_agent_skips_the_gate(tmp_path: Path) -> None:
    export = tmp_path / "disputes.json"
    export.write_text(json.dumps(_fixture_export(40)))
    agent = tmp_path / "agent.py"
    agent.write_text("def decide(view):\n    return 'contest'\n")
    m, _ = run_pilot(
        PilotOptions(history=str(export), candidate=str(agent), out=str(tmp_path / "o"))
    )
    assert m.data == "real (imported)"  # the fixtures carry no synthetic marker
    assert m.step("ingest").summary["cases"] == 40
    assert [s.status for s in m.steps] == ["ok", "ok", "ok", "ok", "skipped", "skipped"]
    assert "agent" in m.step("gate").why
    assert not (tmp_path / "o" / "05-gate").exists()


def test_a_case_store_is_used_as_given(first: tuple[Path, PilotManifest], tmp_path: Path) -> None:
    store = first[0] / "01-ingest" / "cases.db"
    m, _ = run_pilot(
        PilotOptions(history=str(store), candidate=str(RULE), out=str(tmp_path / "s"), n_cases=200)
    )
    ing = m.step("ingest")
    assert ing.status == "skipped" and "case store" in ing.why
    assert m.step("backtest").summary == first[1].step("backtest").summary


def test_multi_rule_candidate_is_backtested_not_gated(synth: Path, tmp_path: Path) -> None:
    spec = json.loads(RULE.read_text())["spec"]
    multi = tmp_path / "multi.json"
    multi.write_text(json.dumps({"name": "two", "rules": [spec, spec]}))
    m, _ = run_pilot(
        PilotOptions(history=str(synth), candidate=str(multi), out=str(tmp_path / "m"))
    )
    assert m.step("backtest").status == "ok"
    assert m.step("gate").status == "skipped" and "multi-rule" in m.step("gate").why


def test_a_bare_rule_spec_is_wrapped_for_the_gate(synth: Path, tmp_path: Path) -> None:
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(json.loads(RULE.read_text())["spec"]))
    m, _ = run_pilot(
        PilotOptions(history=str(synth), candidate=str(bare), out=str(tmp_path / "b"), n_cases=200)
    )
    assert m.step("gate").summary["rule_id"] == "pilot-candidate"


def test_stripe_pull_history(tmp_path: Path) -> None:
    fake = FakeStripe(history(30, step=86_400))
    opts = PilotOptions(
        history="stripe-pull",
        since=str(T0),
        until="2100-01-01",
        candidate=str(RULE),
        out=str(tmp_path / "pull"),
        n_cases=200,
    )
    m, _ = run_pilot(opts, env={"STRIPE_API_KEY": KEY}, transport=httpx.MockTransport(fake))
    assert m.history["source"] == "stripe-pull" and m.history["cases"] == 30
    assert (tmp_path / "pull" / "00-pull" / "stripe-pull.jsonl").is_file()
    with pytest.raises(IngestError, match="since"):
        run_pilot(opts.model_copy(update={"since": None}), env={"STRIPE_API_KEY": KEY})


def test_csv_needs_an_import_config(tmp_path: Path) -> None:
    f = tmp_path / "h.csv"
    f.write_text("a,b\n1,2\n")
    with pytest.raises(IngestError, match="import-config"):
        run_pilot(PilotOptions(history=str(f), candidate=str(RULE), out=str(tmp_path / "c")))


def test_cli(synth: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["pilot", "run", "--history", str(synth), "--candidate", str(RULE)]
    assert main([*argv, "--out", str(tmp_path / "cli"), "--n-cases", "200"]) == 0
    text = capsys.readouterr().out
    assert "[SYNTHETIC DATA]" in text and "manifest written" in text
    assert main([*argv, "--out", str(tmp_path / "x"), "--format", "csv"]) == 2
