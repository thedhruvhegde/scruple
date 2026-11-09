"""Sealed seed splits (033): disjoint namespaces, a salt-only held-out side, manifests."""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from scruple.settings import SettingsError, load_settings
from scruple.sim.difficulty import difficulty_mix
from scruple.sim.disputes import DEFAULT_MIX, FULL_MIX, generate_cases
from scruple.sim.splits import (
    MIN_SALT_CHARS,
    PREFIX,
    SealedSplitError,
    SeedSpace,
    Split,
    SplitManifest,
    cases_hash,
    overlap,
    split_of,
    verify_manifest,
)

SALT = "test-salt-0123456789abcdef"
SPACE = SeedSpace(SALT)
indices = st.integers(min_value=0, max_value=10**9)
splits = st.sampled_from(list(Split))
salts = st.text(min_size=MIN_SALT_CHARS, max_size=40)


@given(splits, indices, splits, indices)
def test_namespaces_are_disjoint(a: Split, i: int, b: Split, j: int) -> None:
    sa, sb = SPACE.seed(a, i), SPACE.seed(b, j)
    if a is not b:
        assert sa != sb
    assert (sa == sb) == ((a, i) == (b, j))


@given(splits, indices)
def test_a_seed_names_its_namespace(s: Split, i: int) -> None:
    seed = SPACE.seed(s, i)
    assert 0 <= seed < 2**64
    assert split_of(seed) is s
    assert SPACE.is_member(seed, s, i)
    assert not SPACE.is_member(seed, s, i + 1)


@given(st.integers(min_value=0, max_value=2**62 - 1))
def test_ordinary_seeds_belong_to_no_split(seed: int) -> None:
    assert split_of(seed) is None


@pytest.mark.parametrize("seed", [-1, 2**64, 2**70])
def test_out_of_range_seeds_belong_to_no_split(seed: int) -> None:
    assert split_of(seed) is None


@given(salts, salts, indices)
def test_salt_only_moves_the_heldout_side(s1: str, s2: str, i: int) -> None:
    a, b = SeedSpace(s1), SeedSpace(s2)
    assert a.seed(Split.TRAIN, i) == b.seed(Split.TRAIN, i) == SeedSpace().seed(Split.TRAIN, i)
    assert a.seed(Split.DEV, i) == b.seed(Split.DEV, i)
    if s1 != s2:
        assert a.seed(Split.HELDOUT, i) != b.seed(Split.HELDOUT, i)
        assert a.salt_commitment() != b.salt_commitment()


def test_heldout_needs_the_salt() -> None:
    sealed = SeedSpace()
    assert sealed.sealed and not SPACE.sealed
    with pytest.raises(SealedSplitError):
        sealed.seed(Split.HELDOUT, 0)
    with pytest.raises(SealedSplitError):
        sealed.id_prefix(Split.HELDOUT, 0)
    with pytest.raises(SealedSplitError):
        sealed.generate(Split.HELDOUT, [0], 5)
    assert sealed.salt_commitment() is None
    sealed.generate(Split.TRAIN, [0], 5)  # the public side still works


@pytest.mark.parametrize("salt", ["", "short", "x" * (MIN_SALT_CHARS - 1), b"tiny"])
def test_short_salts_are_refused(salt: str | bytes) -> None:
    with pytest.raises(ValueError, match="at least"):
        SeedSpace(salt)


def test_salt_forms_agree_and_never_print() -> None:
    seeds = {SeedSpace(s).seed(Split.HELDOUT, 3) for s in (SALT, SALT.encode(), SecretStr(SALT))}
    assert len(seeds) == 1
    assert SALT not in repr(SPACE)


def test_negative_index_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        SPACE.seed(Split.TRAIN, -1)


def test_salt_comes_from_settings_env_only(tmp_path: object) -> None:
    s = load_settings(env={"SCRUPLE_HELDOUT_SALT": SALT})
    assert SeedSpace.from_settings(s).seed(Split.HELDOUT, 1) == SPACE.seed(Split.HELDOUT, 1)
    assert SeedSpace.from_settings(load_settings(env={})).sealed
    from pathlib import Path

    cfg = Path(str(tmp_path)) / "c.toml"
    cfg.write_text(f'heldout_salt = "{SALT}"\n')
    with pytest.raises(SettingsError, match="environment"):
        load_settings(config_file=cfg, env={})


def test_generated_splits_share_nothing() -> None:
    train, _ = SPACE.generate(Split.TRAIN, range(3), 40, FULL_MIX)
    dev, _ = SPACE.generate(Split.DEV, range(3), 40, FULL_MIX)
    held, _ = SPACE.generate(Split.HELDOUT, range(3), 40, FULL_MIX)
    for a, b in ((train, dev), (train, held), (dev, held)):
        assert overlap(a, b) == {"case_ids": 0, "txn_ids": 0}
    assert len({c.case_id for c in train + dev + held}) == 360


def test_heldout_case_ids_do_not_reveal_the_seed() -> None:
    held, _ = SPACE.generate(Split.HELDOUT, [0, 1], 5)
    for i, c in zip([0] * 5 + [1] * 5, held, strict=True):
        seed = SPACE.seed(Split.HELDOUT, i)
        assert str(seed) not in c.case_id and str(seed) not in c.txn_id
        assert c.case_id.startswith(f"cd-{PREFIX[Split.HELDOUT]}-")
        assert c.case_id == f"cd-{SPACE.id_prefix(Split.HELDOUT, i)}-{c.case_id[-6:]}"


def test_default_ids_are_unchanged() -> None:
    assert generate_cases(7, 2)[1].case_id == "cd-7-000001"
    assert generate_cases(7, 2, id_prefix="x")[1].case_id == "cd-x-000001"
    a = generate_cases(7, 3)
    b = generate_cases(7, 3, id_prefix="7")
    assert [c.model_dump() for c in a] == [c.model_dump() for c in b]


def test_manifest_proves_the_split() -> None:
    cases, m = SPACE.generate(Split.HELDOUT, [4, 9], 30, DEFAULT_MIX)
    assert m.split is Split.HELDOUT and m.n_cases == 60 and m.indices == (4, 9)
    assert m.salt_commitment == SPACE.salt_commitment()
    assert SALT not in m.model_dump_json()
    assert all(str(SPACE.seed(Split.HELDOUT, i)) not in m.model_dump_json() for i in (4, 9))
    assert verify_manifest(m, cases) == ()
    assert verify_manifest(m, cases, SPACE, DEFAULT_MIX) == ()
    again, m2 = SPACE.generate(Split.HELDOUT, [4, 9], 30, DEFAULT_MIX)
    assert m2.manifest_hash == m.manifest_hash and cases_hash(again) == m.cases_hash
    rt = SplitManifest.model_validate_json(m.model_dump_json())
    assert rt.manifest_hash == m.manifest_hash


def test_manifest_detects_tampering() -> None:
    cases, m = SPACE.generate(Split.TRAIN, [1], 20)
    bad = list(cases)
    bad[3] = bad[3].model_copy(update={"amount_minor": bad[3].amount_minor + 1})
    assert "cases hash does not match the manifest" in verify_manifest(m, bad)
    assert verify_manifest(m, cases[:-1])  # count and hash
    assert "mix does not match the manifest" in verify_manifest(m, cases, mix=FULL_MIX)
    other = SeedSpace("another-salt-0123456789")
    h, hm = SPACE.generate(Split.HELDOUT, [1], 5)
    assert verify_manifest(hm, h, other) == (
        "salt commitment does not match (wrong or missing salt)",
    )
    assert verify_manifest(hm, h, SeedSpace())
    forged = m.model_copy(update={"indices": (2,)})
    assert "seed commitments do not match" in verify_manifest(forged, cases, SPACE)


@pytest.mark.parametrize(
    "change",
    [
        {"split": Split.DEV},
        {"indices": (0, 1)},
        {"n_per_seed": 11},
        {"start": date(2026, 2, 1)},
    ],
)
def test_manifest_hash_names_every_input(change: dict[str, object]) -> None:
    _, m = SPACE.generate(Split.TRAIN, [0], 10)
    assert m.model_copy(update=change).manifest_hash != m.manifest_hash


def test_manifest_hash_tracks_the_mix() -> None:
    _, a = SPACE.generate(Split.TRAIN, [0], 10, difficulty_mix(0.2))
    _, b = SPACE.generate(Split.TRAIN, [0], 10, difficulty_mix(0.8))
    assert a.mix_hash != b.mix_hash and a.manifest_hash != b.manifest_hash


@pytest.mark.parametrize(
    ("indices_", "n", "msg"),
    [([], 5, "at least one"), ([1, 1], 5, "unique"), ([1], 0, "positive")],
)
def test_generate_validates(indices_: list[int], n: int, msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        SPACE.generate(Split.TRAIN, indices_, n)
