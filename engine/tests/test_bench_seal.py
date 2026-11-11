"""`scruple bench seal`: per-partner salts, disjoint held-out sets, manifest handover.
Synthetic cases only; small benchmarks (40 cases) to keep this fast."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.bench.core import BenchFile, verify
from scruple.bench.seal import SealResult, check_disjoint, heldout_keys, read_salt, seal
from scruple.cli.main import main
from scruple.sim.splits import SeedSpace

N, BLOCK = 40, 20
HOUSE = "house-salt-for-tests-only-" + "x" * 16  # dummy, not the v1 salt


def _seal(name: str, root: Path) -> SealResult:
    return seal(name, root / "handover" / name, salt_dir=root / "salts", n=N, block=BLOCK, env={})


@pytest.fixture(scope="module")
def two(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("seal")
    _seal("acme-pay", root)
    seal(
        "globex",
        root / "handover" / "globex",
        salt_dir=root / "salts",
        n=N,
        block=BLOCK,
        env={"SCRUPLE_HELDOUT_SALT": HOUSE},
    )
    return root


def test_salt_file_is_private_and_outside_the_outputs(two: Path) -> None:
    salt_path = two / "salts" / "acme-pay.salt"
    mode = stat.S_IMODE(salt_path.stat().st_mode)
    assert mode == 0o600
    salt = read_salt(salt_path)
    assert len(salt) >= 32
    for f in (two / "handover" / "acme-pay").iterdir():
        assert salt not in f.read_text()


def test_manifest_loads_verifies_and_names_the_partner(two: Path) -> None:
    f = BenchFile.load(two / "handover" / "acme-pay" / "manifest.json")
    assert f.manifest.name == "scruple-dispute-bench/acme-pay"
    assert f.manifest.n_cases == N and f.reference == {}
    space = SeedSpace(read_salt(two / "salts" / "acme-pay.salt"))
    assert f.manifest.salt_commitment == space.salt_commitment()
    assert verify(f, space) == []
    with pytest.raises(PermissionError, match="wrong salt"):
        verify(f, SeedSpace(read_salt(two / "salts" / "globex.salt")))


def test_handover_note_has_the_hashes_and_the_rules(two: Path) -> None:
    f = BenchFile.load(two / "handover" / "globex" / "manifest.json")
    note = (two / "handover" / "globex" / "HANDOVER.md").read_text()
    assert f.manifest_hash in note and f.manifest.salt_commitment in note
    assert "separate channel" in note and "SYNTHETIC" in note
    assert "acme-pay, house benchmark salt" in note  # what its seeds were checked against


def test_two_partners_have_disjoint_held_out_sets(two: Path) -> None:
    a = SeedSpace(read_salt(two / "salts" / "acme-pay.salt"))
    b = SeedSpace(read_salt(two / "salts" / "globex.salt"))
    sa, pa = heldout_keys(a, 1, N)
    sb, pb = heldout_keys(b, 1, N)
    assert len(sa) == len(sb) == N
    assert not sa & sb and not pa & pb
    fa = BenchFile.load(two / "handover" / "acme-pay" / "manifest.json").manifest
    fb = BenchFile.load(two / "handover" / "globex" / "manifest.json").manifest
    assert fa.cases_digest != fb.cases_digest
    assert not set(fa.block_digests) & set(fb.block_digests)
    sh, _ = heldout_keys(SeedSpace(HOUSE), 1, N)
    assert not sb & sh


@given(st.text(min_size=16, max_size=40), st.text(min_size=16, max_size=40))
@settings(max_examples=25, deadline=None)
def test_distinct_salts_give_disjoint_seeds(x: str, y: str) -> None:
    if x == y:
        with pytest.raises(ValueError, match="equals"):
            check_disjoint(SeedSpace(x), [("other", SeedSpace(y))], 1, 50)
        return
    assert check_disjoint(SeedSpace(x), [("other", SeedSpace(y))], 1, 50) == ("other",)


def test_refusals(two: Path, tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        _seal("acme-pay", two)
    for bad in ("Acme", "a", "has space", "../escape", "x" * 70):
        with pytest.raises(ValueError, match="partner name"):
            _seal(bad, tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="git work tree"):
        seal("initech", tmp_path / "o", salt_dir=repo / "salts", n=N, block=BLOCK, env={})
    assert not (repo / "salts" / "initech.salt").exists()


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["bench", "seal", "--partner", "hooli", "--out", str(tmp_path / "h")]
    rc = main([*argv, "--salt-dir", str(tmp_path / "s"), "--cases", str(N), "--block", str(BLOCK)])
    assert rc == 0
    out = capsys.readouterr().out
    salt = read_salt(tmp_path / "s" / "hooli.salt")
    assert salt not in out and "manifest hash" in out
    rc = main([*argv, "--salt-dir", str(tmp_path / "s"), "--cases", str(N), "--block", str(BLOCK)])
    assert rc == 2
