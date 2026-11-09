"""The frozen held-out benchmark: build, regenerate, verify, score (roadmap 174)."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from functools import cache
from pathlib import Path

import pytest

from scruple.agents.dispute_baselines import evidence_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.bench.core import BenchFile, build, cases_for, check_blocks, score_agent, verify
from scruple.bench.metrics import estimate, render
from scruple.bench.runner import (
    BenchAgentError,
    BlockResult,
    blocks,
    load_agent,
    run_agent,
)
from scruple.bench.spec import (
    INDEX_STRIDE,
    TIERS,
    bench_cases,
    case_of,
    heldout_index,
    tier_of,
)
from scruple.cli.main import main
from scruple.sim.splits import SeedSpace, Split, split_of

SALT = "bench-test-salt-0123456789"
OTHER = "another-salt-abcdefghijkl"
N, BLOCKSZ = 12, 4


@cache
def built() -> tuple[BenchFile, dict[str, list[BlockResult]]]:
    return build(SeedSpace(SALT), 1, N, block=BLOCKSZ)


def test_cases_come_from_the_sealed_namespace() -> None:
    space = SeedSpace(SALT)
    cases = bench_cases(space, 1, range(5))
    assert [c.index for c in cases] == list(range(5))
    assert all(split_of(c.seed) is Split.HELDOUT for c in cases)
    assert cases[0].seed == space.seed(Split.HELDOUT, INDEX_STRIDE)
    assert bench_cases(space, 2, [0])[0].seed != cases[0].seed  # versions are disjoint
    assert bench_cases(SeedSpace(OTHER), 1, [0])[0].seed != cases[0].seed
    assert bench_cases(space, 1, range(5)) == cases  # deterministic
    with pytest.raises(PermissionError):
        bench_cases(SeedSpace(), 1, [0])


@pytest.mark.parametrize(("version", "i"), [(0, 0), (1, -1), (1, INDEX_STRIDE)])
def test_heldout_index_bounds(version: int, i: int) -> None:
    with pytest.raises(ValueError):
        heldout_index(version, i)


def test_agents_never_see_the_seed() -> None:
    """Case ids carry the id prefix: nothing a tool returns names the held-out seed."""
    for bc in bench_cases(SeedSpace(SALT), 1, range(4)):
        ep = DisputeEpisode(bc.seed, None, id_prefix=bc.id_prefix)
        seen = [ep.observe().model_dump(mode="json")]
        seen.append(ep.call("get_case"))
        listed = ep.call("list_evidence")
        seen += [listed, ep.call("get_ledger_view")]
        seen += [
            ep.call("get_evidence", {"evidence_id": d["evidence_id"]}) for d in listed["evidence"]
        ]
        text = json.dumps(seen)
        assert str(bc.seed) not in text
        assert bc.id_prefix in case_of(bc).case_id


def test_tiers_partition_the_cases() -> None:
    tiers = {tier_of(case_of(c)) for c in bench_cases(SeedSpace(SALT), 1, range(40))}
    assert tiers <= set(TIERS) and len(tiers) >= 2


def test_build_manifest_and_reference_scores() -> None:
    f, runs = built()
    m = f.manifest
    assert m.n_cases == N and m.block == BLOCKSZ and len(m.block_digests) == N // BLOCKSZ
    assert f.manifest_hash == m.manifest_hash
    assert sum(m.mix.families.values()) == sum(m.mix.tiers.values()) == N
    assert set(f.reference) == {"evidence", "always_accept", "oracle"}
    ev = f.reference["evidence"]
    assert ev.n_cases == N and ev.regret.mean == 0.0 and ev.synthetic
    assert ev.reward.low <= ev.reward.mean <= ev.reward.high
    assert f.reference["always_accept"].win.mean == 0.0
    assert sum(g.n for g in ev.by_family.values()) == N
    assert all(not check_blocks(m, r) for r in runs.values())
    again, _ = build(SeedSpace(SALT), 1, N, block=BLOCKSZ, agents=("evidence",))
    assert again.manifest == m  # deterministic
    other, _ = build(SeedSpace(OTHER), 1, N, block=BLOCKSZ, agents=())
    assert other.manifest.cases_digest != m.cases_digest
    assert SALT not in f.model_dump_json() and "seed" not in json.loads(f.model_dump_json())


def test_file_roundtrip_and_tamper(tmp_path: Path) -> None:
    f, _ = built()
    path = tmp_path / "m.json"
    f.dump(path)
    assert BenchFile.load(path) == f
    raw = json.loads(path.read_text())
    raw["manifest"]["n_cases"] = N + 1
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="manifest hash"):
        BenchFile.load(path)


def test_verify_needs_the_right_salt() -> None:
    f, _ = built()
    assert verify(f, SeedSpace(SALT)) == []
    with pytest.raises(PermissionError, match="wrong salt"):
        verify(f, SeedSpace(OTHER))
    with pytest.raises(PermissionError, match="sealed"):
        verify(f, SeedSpace())
    forged = f.manifest.model_copy(update={"cases_digest": "0" * 64})
    bad = f.model_copy(update={"manifest": forged})
    assert verify(bad, SeedSpace(SALT)) == ["cases digest differs"]


def test_score_reproduces_reference_and_checks_blocks() -> None:
    f, _ = built()
    run, res = score_agent(f, "evidence", SeedSpace(SALT))
    assert not run.prefix and run.score.reward == f.reference["evidence"].reward
    assert run.manifest_hash == f.manifest_hash
    prefix, _ = score_agent(f, "evidence", SeedSpace(SALT), limit=3)
    assert prefix.prefix and prefix.score.n_cases == BLOCKSZ  # rounded up to a block
    forged = f.manifest.model_copy(update={"block_digests": ("0" * 64,) * (N // BLOCKSZ)})
    with pytest.raises(ValueError, match="do not match the manifest"):
        score_agent(f.model_copy(update={"manifest": forged}), "evidence", SeedSpace(SALT))
    with pytest.raises(ValueError, match="positive"):
        cases_for(f.manifest, SeedSpace(SALT), 0)
    assert len(res) == N // BLOCKSZ


def lazy_policy(ep: DisputeEpisode) -> None:
    """Does nothing: the case lapses through its deadlines."""


def raising_policy(ep: DisputeEpisode) -> None:
    ep.call("no_such_tool")


def test_module_agents_and_errors() -> None:
    f, _ = built()
    run, _ = score_agent(f, "tests.test_bench_core:lazy_policy", SeedSpace(SALT))
    assert run.score.agent == "tests.test_bench_core:lazy_policy"
    assert run.score.reward.mean < f.reference["evidence"].reward.mean
    broken, _ = score_agent(f, "tests.test_bench_core:raising_policy", SeedSpace(SALT))
    assert broken.score.lapsed == N and broken.score.hard_failures.get("SCOPE-01") == N
    for bad in (
        "nope",
        "no_module_xyz:fn",
        "tests.test_bench_core:missing",
        "cli:",
        "json:dumps.x",
    ):
        with pytest.raises(BenchAgentError):
            load_agent(bad)
    with pytest.raises(BenchAgentError, match="not callable"):
        load_agent("tests.test_bench_core:SALT")


CLI_AGENT = r"""
import json, sys
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
def recv():
    return json.loads(sys.stdin.readline())
while True:
    msg = recv()
    if msg["type"] == "done":
        break
    assert msg["type"] == "episode" and "submit_decision" in msg["tools"]
    send({"tool": "get_case"})
    got = recv()["result"]
    allowed = got["status"]["allowed_actions"]
    send({"tool": "bogus_tool"})
    assert recv()["ok"] is False
    if "accept" in allowed:
        send({"tool": "accept", "args": {"reason": "concede"}})
        recv()
    send({"tool": "submit_decision", "args": {"explanation": "accepted the dispute"}})
    assert recv()["type"] == "closed"
"""


def test_cli_agent_protocol(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(CLI_AGENT)
    f, _ = built()
    run, res = score_agent(f, f"cli:{sys.executable} {script}", SeedSpace(SALT), limit=BLOCKSZ)
    assert run.score.n_cases == BLOCKSZ and run.score.lapsed == 0
    assert all("SCOPE-01" in r.hard_failures for b in res for r in b.rows)  # the bogus call
    garbage = tmp_path / "garbage.py"
    garbage.write_text("print('not json')\n")
    with pytest.raises(BenchAgentError, match="invalid JSON"):
        score_agent(f, f"cli:{sys.executable} {garbage}", SeedSpace(SALT), limit=1)
    silent = tmp_path / "silent.py"
    silent.write_text("")
    with pytest.raises(BenchAgentError, match="exited"):
        score_agent(f, f"cli:{sys.executable} {silent}", SeedSpace(SALT), limit=1)
    wrong = tmp_path / "wrong.py"
    wrong.write_text("print('[1, 2]')\n")
    with pytest.raises(BenchAgentError, match="must be"):
        score_agent(f, f"cli:{sys.executable} {wrong}", SeedSpace(SALT), limit=1)


def test_cli_agent_call_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scruple.bench.runner as runner

    monkeypatch.setattr(runner, "MAX_CALLS", 3)
    script = tmp_path / "loop.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m['type'] in ('episode', 'result'):\n"
        "        print(json.dumps({'tool': 'get_case'}), flush=True)\n"
    )
    f, _ = built()
    run, _ = score_agent(f, f"cli:{sys.executable} {script}", SeedSpace(SALT), limit=1)
    assert run.score.lapsed == BLOCKSZ


def test_parallel_equals_serial() -> None:
    cases = bench_cases(SeedSpace(SALT), 1, range(8))
    agent = load_agent("always_accept")
    serial = run_agent(agent, cases, block=4)
    parallel = run_agent(agent, cases, jobs=2, block=4)
    assert serial == parallel
    assert [len(b) for b in blocks(cases, 3)] == [3, 3, 2]


@pytest.mark.parametrize(
    ("xs", "lo", "hi"), [([], 0.0, 0.0), ([0.5], 0.5, 0.5), ([1.0] * 5 + [0.0], 0.507, 1.0)]
)
def test_estimate(xs: list[float], lo: float, hi: float) -> None:
    e = estimate(xs, 0.0, 1.0)
    assert e.n == len(xs) and e.low == pytest.approx(lo, abs=0.01) and e.high == hi


def test_cli_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCRUPLE_HELDOUT_SALT", SALT)
    out = tmp_path / "m.json"
    argv = ["bench", "build", "--cases", "8", "--block", "4", "--out", str(out)]
    assert main(argv) == 0
    assert "8 synthetic held-out dispute cases" in capsys.readouterr().out
    assert main(["bench", "show", "--bench", str(out)]) == 0
    assert "reference scores" in capsys.readouterr().out
    assert main(["bench", "show", "--bench", str(out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["manifest"]["n_cases"] == 8
    assert main(["bench", "verify", "--bench", str(out)]) == 0
    assert "VERIFIED" in capsys.readouterr().out
    res = tmp_path / "run.json"
    assert (
        main(["bench", "score", "--bench", str(out), "--agent", "evidence", "--out", str(res)]) == 0
    )
    assert "vs reference" in capsys.readouterr().out and json.loads(res.read_text())["score"]
    assert main(["bench", "score", "--bench", str(out), "--agent", "oracle", "--limit", "4"]) == 0
    assert "(prefix)" in capsys.readouterr().out
    assert main(["bench", "score", "--bench", str(out), "--agent", "evidence", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["prefix"] is False
    monkeypatch.setenv("SCRUPLE_HELDOUT_SALT", OTHER)
    assert main(["bench", "score", "--bench", str(out), "--agent", "evidence"]) == 2
    assert "wrong salt" in capsys.readouterr().err
    monkeypatch.delenv("SCRUPLE_HELDOUT_SALT")
    assert main(["bench", "verify", "--bench", str(out)]) == 2
    raw = json.loads(out.read_text())
    raw["manifest"]["cases_digest"] = "0" * 64
    raw["manifest_hash"] = BenchFile.model_validate(raw).manifest.manifest_hash
    out.write_text(json.dumps(raw))
    monkeypatch.setenv("SCRUPLE_HELDOUT_SALT", SALT)
    assert main(["bench", "verify", "--bench", str(out)]) == 1


def test_render_has_a_row_per_agent() -> None:
    f, _ = built()
    text = render(list(f.reference.values()))
    assert all(re.search(rf"^\| {a} \|", text, re.M) for a in f.reference)


def test_evidence_module_agent_matches_builtin() -> None:
    f, _ = built()
    fn: Callable[[DisputeEpisode], None] = evidence_policy
    assert load_agent("scruple.agents.dispute_baselines:evidence_policy").policy is fn
    run, _ = score_agent(f, "scruple.agents.dispute_baselines:evidence_policy", SeedSpace(SALT))
    assert run.score.cost == f.reference["evidence"].cost
