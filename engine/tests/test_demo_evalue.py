"""The demo on both statistics paths: the e-value default (30,000 held-out cases,
sized by the gate's plan) and the original bootstrap story. Synthetic data."""

from __future__ import annotations

from pathlib import Path

import pytest

from scruple.cli.demo import DEMO_HELD_OUT, demo_config, run_demo
from scruple.gate.config import GateConfig
from scruple.gate.redteam import run_redteam

OLD_STORY = (
    "PROMOTE",
    "CANARY_PASSED",
    "BLOCK",
    "ACTIVATED",
    "ROLLED_BACK",
    "TAMPERING DETECTED",
    "INTEGRITY OK",
    "7/7 attacks stopped",
    "# Evidence report dec-00003: BLOCKED",
)


@pytest.fixture(scope="module")
def evalue_out(tmp_path_factory: pytest.TempPathFactory) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert run_demo(tmp_path_factory.mktemp("ev")) == 0
    return buf.getvalue()


def test_demo_config() -> None:
    assert demo_config("evalue").stats == "evalue"
    assert demo_config("evalue").n_cases == DEMO_HELD_OUT == 30_000
    assert demo_config("bootstrap") == GateConfig()


def test_evalue_demo_tells_the_story(evalue_out: str) -> None:
    for needle in OLD_STORY:
        assert needle in evalue_out
    assert "anytime-valid e-values on 30,000 held-out cases" in evalue_out


def test_evalue_demo_certifies_good_and_flags_bad_by_case_type(evalue_out: str) -> None:
    lines = evalue_out.splitlines()
    regressed = [ln for ln in lines if ln.rstrip().endswith("REGRESSED")]
    assert len(regressed) == 1 and "late_evidence/<$80" in regressed[0]
    improved = [ln for ln in lines if ln.rstrip().endswith("improved")]
    assert any("not_received/<$10,000" in ln for ln in improved)
    plans = [ln for ln in lines if ln.startswith("  e-value gate:")]
    assert len(plans) == 2
    for ln in plans:  # both rules: the plan fits inside the held-out set
        need = int(ln.split("plan needs ")[1].split()[0])
        assert need <= DEMO_HELD_OUT
    assert "sequential two-sample test" in evalue_out


def test_bootstrap_demo_keeps_the_old_story(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_demo(tmp_path, stats="bootstrap") == 0
    out = capsys.readouterr().out
    for needle in OLD_STORY:
        assert needle in out
    assert "seeded bootstrap on 3,000 held-out cases" in out
    assert "e-value gate" not in out


@pytest.mark.slow  # the default suite checks the e-value demo (tests/test_cli.py)
def test_bootstrap_demo_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_demo(tmp_path / "a", print_reports=False, stats="bootstrap")
    a = capsys.readouterr().out.replace(str(tmp_path / "a"), "X")
    run_demo(tmp_path / "b", print_reports=False, stats="bootstrap")
    b = capsys.readouterr().out.replace(str(tmp_path / "b"), "X")
    assert a == b


def test_redteam_is_stopped_under_evalue_statistics() -> None:
    results = run_redteam(GateConfig(stats="evalue", n_cases=3_000))
    assert all(r.defended for r in results)


@pytest.mark.slow
def test_redteam_is_stopped_under_evalue_at_demo_size() -> None:
    results = run_redteam(demo_config("evalue"))
    assert all(r.defended for r in results)
