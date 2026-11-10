"""Batch runner: records, summary, resume, concurrency, leak checks and the CLI.

Fake transports only: no network, no keys."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.providers import FakeModel, Price, RunSettings
from scruple.agents.store import EpisodeStore
from scruple.agents.strategies import STRATEGIES, parse_seeds
from scruple.cli.main import main
from scruple.eval import EvalConfig, import_store, run_eval, write_summary
from scruple.eval.batch import GROUND_TRUTH_MARKERS, GroundTruthLeakError, check_no_ground_truth
from scruple.eval.records import load_manifest, load_records, record_path

SEEDS = (5001, 5002, 5003, 5004)


def cfg(**kw: Any) -> EvalConfig:
    base: dict[str, Any] = {
        "provider": "anthropic",
        "model": "fake-1",
        "strategy": "zero-shot",
        "seeds": SEEDS,
        "transport": "fake",
        "concurrency": 2,
    }
    base.update(kw)
    return EvalConfig(**base)


def test_run_writes_records_store_and_summary(tmp_path: Path) -> None:
    summary = run_eval(cfg(), tmp_path)
    recs = load_records(tmp_path)
    assert [r.seed for r in recs] == list(SEEDS)
    assert all(r.stop == "submitted" and not r.real_model for r in recs)
    assert summary["n"] == 4
    assert summary["real_model"] is False
    assert summary["synthetic_cases"] is True
    names = [s["system"] for s in summary["systems"]]
    assert names == [
        "anthropic/fake-1/zero-shot",
        "ref:evidence",
        "ref:always_accept",
        "ref:oracle",
    ]
    md = (tmp_path / "SUMMARY.md").read_text()
    assert "FAKE model" in md and "Synthetic cases" in md
    # the store replays to the same scores
    st = EpisodeStore(tmp_path / "store")
    for r in recs:
        ep = st.load(r.episode_id)
        assert isinstance(ep, DisputeEpisode) and ep.score is not None
        assert ep.score.trace_head == r.score["trace_head"]
    m = load_manifest(tmp_path)
    assert m is not None and m.seeds == "5001-5004" and m.path == "api"


def test_concurrency_does_not_change_results(tmp_path: Path) -> None:
    a = run_eval(cfg(concurrency=1), tmp_path / "a")
    b = run_eval(cfg(concurrency=4), tmp_path / "b")
    assert a["rows"] == b["rows"]
    ra = [r.model_dump() for r in load_records(tmp_path / "a")]
    rb = [r.model_dump() for r in load_records(tmp_path / "b")]
    assert ra == rb


def test_concurrency_cap_is_respected(tmp_path: Path) -> None:
    fm = FakeModel("anthropic-messages")
    live = 0
    peak = 0
    lock = threading.Lock()
    gate = threading.Barrier(2, timeout=5)
    first = threading.local()

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        if not getattr(first, "done", False):
            first.done = True
            gate.wait()  # make two workers overlap at least once
        try:
            return fm(req)
        finally:
            with lock:
                live -= 1

    run_eval(
        cfg(concurrency=2, seeds=(1, 2, 3, 4, 5, 6)),
        tmp_path,
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert peak == 2


def test_resume_skips_finished_and_reruns_transient(tmp_path: Path) -> None:
    calls = {"n": 0}

    def down(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="down")

    settings = RunSettings(max_retries=0)
    run_eval(
        cfg(seeds=(1, 2), settings=settings),
        tmp_path,
        transport_factory=lambda: httpx.MockTransport(down),
    )
    assert {r.stop for r in load_records(tmp_path)} == {"http_error"}
    fm = FakeModel("anthropic-messages")
    run_eval(cfg(seeds=(1, 2, 3), settings=settings), tmp_path, transport_factory=fm.transport)
    recs = load_records(tmp_path)
    assert [(r.seed, r.stop) for r in recs] == [
        (1, "submitted"),
        (2, "submitted"),
        (3, "submitted"),
    ]
    before = record_path(tmp_path, 1).stat().st_mtime_ns
    n = len(fm.requests)
    run_eval(cfg(seeds=(1, 2, 3), settings=settings), tmp_path, transport_factory=fm.transport)
    assert len(fm.requests) == n  # nothing replayed
    assert record_path(tmp_path, 1).stat().st_mtime_ns == before
    m = load_manifest(tmp_path)
    assert m is not None and m.seeds == "1-3"


def test_resume_refuses_a_different_run(tmp_path: Path) -> None:
    run_eval(cfg(seeds=(1,)), tmp_path)
    with pytest.raises(ValueError, match="different run"):
        run_eval(cfg(seeds=(1,), strategy="react"), tmp_path)


def test_price_and_tokens_in_summary(tmp_path: Path) -> None:
    s = run_eval(cfg(seeds=(1,), price=Price(input_per_mtok=1, output_per_mtok=1)), tmp_path)
    assert s["input_tokens"] > 0
    assert s["api_cost_usd"] == pytest.approx((s["input_tokens"] + s["output_tokens"]) / 1e6)
    assert "API cost $" in (tmp_path / "SUMMARY.md").read_text()


def test_bad_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        run_eval(cfg(concurrency=0), tmp_path)
    with pytest.raises(ValueError, match="no seeds"):
        run_eval(cfg(seeds=()), tmp_path)
    with pytest.raises(ValueError, match="transport"):
        run_eval(cfg(transport="pigeon"), tmp_path)
    with pytest.raises(ValueError, match="no evaluation records"):
        write_summary(tmp_path / "empty")


# --- ground truth never leaks into prompts ----------------------------------------------


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini", "mistral"])
def test_no_request_body_carries_ground_truth(provider: str, tmp_path: Path) -> None:
    wire = {"anthropic": "anthropic-messages", "gemini": "gemini-generate-content"}.get(
        provider, "openai-chat"
    )
    fm = FakeModel(wire)
    seeds = tuple(range(5001, 5013))
    for strategy in STRATEGIES:
        run_eval(
            cfg(provider=provider, strategy=strategy, seeds=seeds, concurrency=4),
            tmp_path / strategy,
            transport_factory=fm.transport,
        )
    families = {r.score["family"] for r in load_records(tmp_path / "zero-shot")}
    assert len(families) >= 4  # many hidden families were in play
    assert fm.requests
    for body in fm.requests:
        blob = json.dumps(body)
        for marker in GROUND_TRUTH_MARKERS:
            assert marker not in blob


def test_leak_check_trips_on_markers() -> None:
    for marker in GROUND_TRUTH_MARKERS:
        with pytest.raises(GroundTruthLeakError, match=marker):
            check_no_ground_truth(f"the case is {marker}", "test")
    check_no_ground_truth(DisputeEpisode(5001).observe().model_dump_json(), "obs")


def test_a_leaking_strategy_aborts_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scruple.agents.strategies import library

    bad = STRATEGIES["zero-shot"].model_copy(update={"objective": "hint: true_fraud"})
    monkeypatch.setitem(library.STRATEGIES, "zero-shot", bad)
    fm = FakeModel("anthropic-messages")
    with pytest.raises(GroundTruthLeakError):
        run_eval(cfg(seeds=(1,)), tmp_path, transport_factory=fm.transport)
    assert fm.requests == []


# --- CLI-brief path ---------------------------------------------------------------------


def _cli_play(store: Path, seed: int, submit: bool = True) -> str:
    st = EpisodeStore(store)
    eid = st.start(seed, None, "dispute").episode_id
    st.call(eid, "get_case", None)
    st.call(eid, "list_evidence", None)
    st.call(eid, "accept", {"reason": "concede"})
    if submit:
        st.call(eid, "submit_decision", {"explanation": "accepted"})
    return eid


def test_import_store(tmp_path: Path) -> None:
    store = tmp_path / "store"
    first = _cli_play(store, 5001)
    _cli_play(store, 5001)  # a retry of the same seed does not count
    _cli_play(store, 5002, submit=False)
    EpisodeStore(store).start(9, None, "simple")  # other kinds are ignored
    summary = import_store(store, tmp_path / "out", model="sonnet", strategy="checklist-first")
    recs = load_records(tmp_path / "out")
    assert [(r.seed, r.stop) for r in recs] == [(5001, "submitted"), (5002, "not_submitted")]
    assert recs[0].episode_id == first
    assert recs[0].path == "cli-brief" and recs[0].real_model
    assert "lapsed:not_submitted" in recs[1].score["flags"]
    assert summary["system"] == "cli-brief/sonnet/checklist-first"
    assert "real model behaviour" in (tmp_path / "out" / "SUMMARY.md").read_text()


def test_import_store_needs_dispute_episodes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no dispute episodes"):
        import_store(tmp_path, tmp_path / "out", model="m", strategy="zero-shot")


# --- CLI --------------------------------------------------------------------------------


def test_cli_run_fake(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "run"
    argv = [
        "eval", "run", "--provider", "gemini", "--model", "fake-gemini",
        "--strategy", "plan-then-act", "--seeds", "5001-5003", "--out", str(out),
        "--transport", "fake", "--concurrency", "2", "--price-in", "1", "--price-out", "4",
    ]  # fmt: skip
    assert main(argv) == 0
    cap = capsys.readouterr()
    assert "| gemini/fake-gemini/plan-then-act | 3 |" in cap.out
    assert "seed 5001: submitted" in cap.err
    assert main(["eval", "summary", str(out)]) == 0
    assert "| ref:evidence | 3 |" in capsys.readouterr().out


def test_cli_run_needs_keys_for_http(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    argv = ["eval", "run", "--provider", "openai", "--model", "m", "--strategy", "react",
            "--seeds", "1", "--out", str(tmp_path / "x")]  # fmt: skip
    assert main(argv) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def test_cli_price_needs_both(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["eval", "run", "--provider", "anthropic", "--model", "m", "--strategy", "react",
            "--seeds", "1", "--out", str(tmp_path / "x"), "--transport", "fake",
            "--price-in", "3"]  # fmt: skip
    assert main(argv) == 2
    assert "--price-out" in capsys.readouterr().err


def test_cli_providers(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", "providers"]) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["gemini"]["key_env"] == ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
    assert rows["openai-compatible"]["base_url"] is None


def test_cli_import_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = tmp_path / "store"
    _cli_play(store, 5001)
    argv = ["eval", "import-store", "--store", str(store), "--model", "opus",
            "--strategy", "zero-shot", "--out", str(tmp_path / "o")]  # fmt: skip
    assert main(argv) == 0
    assert "| cli-brief/opus/zero-shot | 1 |" in capsys.readouterr().out


def test_parse_seeds_feeds_the_runner(tmp_path: Path) -> None:
    run_eval(cfg(seeds=parse_seeds("7,9")), tmp_path)
    assert [r.seed for r in load_records(tmp_path)] == [7, 9]
