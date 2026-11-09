"""Backtest end to end through `scruple backtest`, on two histories:

1. edited copies of the vendored real Stripe fixtures (tests/fixtures/stripe: stripe/openapi
   at 6360907, MIT). They are Stripe's published example objects, not customer data;
2. a SYNTHETIC 5,000-case Stripe-shaped history from `scruple backtest synth`
   (scruple.calibration.synthetic). Every report on it must say SYNTHETIC.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from scruple.calibration import CalibrationProfile, ReasonProfile
from scruple.cli.main import main
from scruple.ingest.stripe.wire import Dispute

FIX = Path(__file__).parent / "fixtures" / "stripe"
REAL: dict[str, Any] = json.loads((FIX / "fixtures3.subset.json").read_text())["resources"]

CONTEST_FRAUD = {
    "description": "Contest every fraud dispute.",
    "conditions": [{"field": "reason_code", "op": "eq", "value": "fraud"}],
    "action": "contest",
    "priority": 10,
}
ACCEPT_ALL_AGENT = "def decide(view):\n    return 'accept'\n"


def pdf_text(path: Path) -> str:
    return "".join(p.extract_text() for p in PdfReader(io.BytesIO(path.read_bytes())).pages)


def run_cli(*args: str) -> None:
    assert main(list(args)) == 0


# --- 1. real Stripe fixture objects ------------------------------------------------------


def fixture_history() -> list[dict[str, Any]]:
    """The real dispute fixture verbatim, plus edited copies (statuses, ids, amounts) that
    carry edited copies of the real balance transaction fixture."""
    out = [copy.deepcopy(REAL["dispute"])]
    statuses = ("won", "lost", "lost", "needs_response")
    reasons = ("fraudulent", "product_not_received", "general")
    for i in range(36):
        d = copy.deepcopy(REAL["dispute"])
        status = statuses[i % 4]
        amount = 1_000 + 250 * i
        d.update(
            id=f"dp_fixturecopy{i:03d}",
            charge=f"ch_fixturecopy{i:03d}",
            status=status,
            reason=reasons[i % 3],
            amount=amount,
            created=d["created"] + i * 86_400,
        )
        d["evidence_details"]["submission_count"] = 1 if status == "won" or i % 8 == 1 else 0
        bts = []
        if status != "warning_needs_response":
            bt = copy.deepcopy(REAL["balance_transaction"])
            bt.update(
                id=f"txn_fixturecopy{i:03d}d",
                amount=-amount,
                fee=1_500,
                net=-amount - 1_500,
                currency="usd",
                created=d["created"],
                reporting_category="dispute",
            )
            bts.append(bt)
            if status == "won":
                cr = copy.deepcopy(bt)
                cr.update(
                    id=f"txn_fixturecopy{i:03d}c",
                    amount=amount,
                    fee=0,
                    net=amount,
                    created=d["created"] + 40 * 86_400,
                    reporting_category="dispute_reversal",
                )
                bts.append(cr)
        d["balance_transactions"] = bts
        out.append(d)
    return out


def test_real_fixture_history_end_to_end(tmp_path: Path) -> None:
    hist = tmp_path / "stripe_fixtures.json"
    hist.write_text(json.dumps(fixture_history()))
    rule = tmp_path / "contest_fraud.json"
    rule.write_text(json.dumps(CONTEST_FRAUD))
    out = tmp_path / "report"
    run_cli(
        "backtest", "run", "--history", str(hist), "--format", "stripe",
        "--candidate", str(rule), "--out", str(out),
        "--label", "Stripe OpenAPI fixtures (stripe/openapi 6360907, MIT), edited copies",
    )  # fmt: skip
    doc = json.loads((out / "report.json").read_text())
    assert doc["synthetic"] is False
    assert doc["dataset"].startswith("Stripe OpenAPI fixtures")
    assert doc["n_input"] == 37
    assert doc["excluded"]["inquiry or prevented (no chargeback decision)"] == 1
    assert doc["excluded"]["open (no outcome yet)"] == 9
    assert doc["n_included"] == 27 and doc["ledger_reconciled"] is True
    assert doc["n_observed"] + doc["n_estimated"] == 27 and doc["n_estimated"] > 0
    # 27 cases cannot show anything: the report must say so
    assert doc["verdict"] == "insufficient evidence"
    assert any(not r["plan"]["sufficient"] for r in doc["by_reason"])
    assert doc["calibration"]["profile"]["fallbacks"]  # too few cases for every cell
    md = (out / "report.md").read_text()
    assert "SYNTHETIC DATA" not in md and "insufficient evidence" in md
    assert "estimated" in pdf_text(out / "report.pdf")


def test_calibrate_command_on_fixtures(tmp_path: Path) -> None:
    hist = tmp_path / "stripe_fixtures.json"
    hist.write_text(json.dumps(fixture_history()))
    out = tmp_path / "calibration.json"
    prof = tmp_path / "profile.json"
    run_cli("backtest", "calibrate", "--history", str(hist), "--format", "stripe",
            "--out", str(out), "--profile-out", str(prof))  # fmt: skip
    doc = json.loads(out.read_text())
    text = out.read_text()
    assert "dp_fixturecopy" not in text and "ch_fixturecopy" not in text  # aggregates only
    assert doc["aggregates"]["k"] == 10
    CalibrationProfile.model_validate_json(prof.read_text())


# --- 2. SYNTHETIC 5,000-case Stripe-shaped history --------------------------------------


SYNTH_PROFILE = CalibrationProfile(
    kind="default",
    network_weights={"mastercard": 0.4, "visa": 0.6},
    reasons={
        "fraudulent": ReasonProfile(
            weight=0.4,
            p_contest=0.3,
            key_evidence="customer_purchase_ip",
            p_key_evidence=0.5,
            p_win_with_key=0.7,
            p_win_without_key=0.4,
        ),
        "general": ReasonProfile(
            weight=0.3, p_contest=0.5, p_win_with_key=0.2, p_win_without_key=0.2
        ),
        "product_not_received": ReasonProfile(
            weight=0.3, p_contest=0.6, p_win_with_key=0.5, p_win_without_key=0.5
        ),
    },
    p_inquiry=0.15,
    p_open=0.05,
    p_late=0.05,
    amount_log_mean=8.5,
    amount_log_sd=0.5,
    dispute_fee_minor=1_500,
)


@pytest.fixture(scope="module")
def synthetic_history(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("synthetic")
    prof = d / "profile.json"
    prof.write_text(SYNTH_PROFILE.model_dump_json())
    path = d / "SYNTHETIC_stripe_5000.json"
    run_cli("backtest", "synth", "--n", "5000", "--seed", "11", "--profile", str(prof),
            "--out", str(path))  # fmt: skip
    return path


def test_synthetic_history_is_stripe_shaped(synthetic_history: Path) -> None:
    objs = json.loads(synthetic_history.read_text())
    assert len(objs) == 5_000
    real_keys = set(REAL["dispute"])
    for o in objs[:200]:
        assert set(o) <= real_keys  # every field we write exists on the real object
        assert o["metadata"] == {"scruple_synthetic": "true"}
        Dispute.model_validate(o)


def test_synthetic_contest_fraud_is_better(synthetic_history: Path, tmp_path: Path) -> None:
    rule = tmp_path / "contest_fraud.json"
    rule.write_text(json.dumps(CONTEST_FRAUD))
    runs = []
    for name in ("a", "b"):
        out = tmp_path / name
        run_cli("backtest", "run", "--history", str(synthetic_history), "--format", "stripe",
                "--candidate", str(rule), "--out", str(out))  # fmt: skip
        runs.append({f.name: f.read_bytes() for f in sorted(out.iterdir())})
    assert runs[0] == runs[1]  # JSON, Markdown and PDF are byte-identical across runs
    out = tmp_path / "a"
    doc = json.loads((out / "report.json").read_text())
    assert doc["synthetic"] is True and doc["dataset"].startswith("SYNTHETIC")
    assert doc["verdict"] == "better" and doc["robust"] is True
    fraud = next(r for r in doc["by_reason"] if r["reason"] == "fraudulent")
    assert fraud["verdict"] == "better" and fraud["better_fdr"] and fraud["n_estimated"] > 0
    others = [r for r in doc["by_reason"] if r["reason"] != "fraudulent"]
    assert all(r["verdict"] == "no change" and r["n_estimated"] == 0 for r in others)
    cal = doc["calibration"]
    assert cal["after"]["overall"] < cal["before"]["overall"]
    assert doc["ledger_reconciled"] is True and doc["cost_source"] == {"ledger": doc["n_included"]}
    md = (out / "report.md").read_text()
    assert md.count("SYNTHETIC") >= 2 and "| fraudulent |" in md
    assert "SYNTHETIC" in pdf_text(out / "report.pdf")


def test_synthetic_accept_everything_is_worse(synthetic_history: Path, tmp_path: Path) -> None:
    agent = tmp_path / "accept_all.py"
    agent.write_text(ACCEPT_ALL_AGENT)
    out = tmp_path / "r"
    run_cli("backtest", "run", "--history", str(synthetic_history), "--format", "stripe",
            "--candidate", str(agent), "--out", str(out), "--no-pdf",
            "--include-cases")  # fmt: skip
    doc = json.loads((out / "report.json").read_text())
    assert doc["verdict"] == "worse" and doc["stats"]["regressed_fwer"]
    assert not (out / "report.pdf").exists()
    lines = doc["cases"]
    assert {x["basis"] for x in lines} == {"observed", "estimated"}
    assert all((x["basis"] == "observed") == (x["historical_action"] == "accept") for x in lines)


def test_cli_errors_are_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[1]")
    rule = tmp_path / "r.json"
    rule.write_text(json.dumps(CONTEST_FRAUD))
    args = ["backtest", "run", "--history", str(bad), "--format", "stripe",
            "--candidate", str(rule), "--out", str(tmp_path / "o")]  # fmt: skip
    assert main(args) == 2
    assert "error" in capsys.readouterr().err
    csv_args = ["backtest", "run", "--history", str(bad), "--format", "csv",
                "--candidate", str(rule), "--out", str(tmp_path / "o")]  # fmt: skip
    assert main(csv_args) == 2
