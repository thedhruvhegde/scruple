"""Frozen real-data splits: hashes, sealing, disjointness, determinism, stratification."""

from __future__ import annotations

import gzip
import json
import os
from collections import Counter
from datetime import date
from functools import cache
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.realdata import splits as S  # noqa: N812
from scruple.realdata.provenance import ChecksumError, sha256_file
from scruple.realdata.records import DATASETS, Complaint, text_key

ROOT = Path(__file__).resolve().parents[1]


@cache
def _all() -> dict[tuple[str, str], list[Complaint]]:
    return {(d, s): S.load_split(d, s, unseal=True) for d in DATASETS for s in S.SPLITS}


def test_data_dir_points_at_checkout() -> None:
    assert S.data_dir() == ROOT / "data" / "real"


def test_manifest_hashes_match_committed_files() -> None:
    m = S.load_manifest()
    assert m["version"] == S.VERSION
    assert m["seed"] == S.SPLIT_SEED
    root = S.data_dir() / S.VERSION
    for d in DATASETS:
        for s in S.SPLITS:
            meta = m["datasets"][d]["splits"][s]
            p = root / meta["path"]
            assert sha256_file(p) == meta["sha256_gz"]
            assert S._sha(gzip.decompress(p.read_bytes())) == meta["sha256_jsonl"]
            assert meta["sealed"] is (s == "test")
            assert len(_all()[(d, s)]) == meta["n"]


def test_committed_size_budget() -> None:
    real = ROOT / "data" / "real"
    v1 = [p for p in real.rglob("*") if p.is_file() and (real / "v2") not in p.parents]
    assert sum(p.stat().st_size for p in v1) < 8_000_000  # v2 has its own budget


def test_test_split_is_sealed() -> None:
    with pytest.raises(S.SealedSplitError):
        S.load_split("cfpb2015", "test")
    assert S.load_split("cfpb2015", "dev")


def test_unknown_split_rejected() -> None:
    with pytest.raises(KeyError):
        S.load_split("cfpb2015", "holdout", unseal=True)  # type: ignore[arg-type]


def test_tampered_split_is_detected(tmp_path: Path) -> None:
    root = tmp_path / S.VERSION
    (root / "cfpb2015").mkdir(parents=True)
    src = S.data_dir() / S.VERSION
    (root / "manifest.json").write_text((src / "manifest.json").read_text())
    rows = gzip.decompress((src / "cfpb2015/dev.jsonl.gz").read_bytes()).decode().splitlines()
    first = json.loads(rows[0])
    first["issue"] = "Tampered"
    rows[0] = json.dumps(first, sort_keys=True)
    (root / "cfpb2015/dev.jsonl.gz").write_bytes(S._gzip(("\n".join(rows) + "\n").encode()))
    with pytest.raises(ChecksumError):
        S.load_split("cfpb2015", "dev", base=tmp_path)
    assert S.load_split("cfpb2015", "dev", base=tmp_path, verify=False)[0].issue == "Tampered"


def test_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCRUPLE_REAL_DATA_DIR", str(tmp_path))
    assert S.data_dir() == tmp_path


def test_ids_are_disjoint_and_unique() -> None:
    seen: dict[str, str] = {}
    for (d, s), rows in _all().items():
        for c in rows:
            assert c.dataset == d
            assert c.id not in seen, (c.id, seen.get(c.id), s)
            seen[c.id] = s


def test_no_narrative_in_two_splits_even_near_duplicate() -> None:
    owner: dict[str, tuple[str, str]] = {}
    for key, rows in _all().items():
        for c in rows:
            k = text_key(c.narrative)
            assert k not in owner, (c.id, owner.get(k), key)
            owner[k] = key


def test_split_sizes_follow_fractions() -> None:
    m = S.load_manifest()
    for d in DATASETS:
        e = m["datasets"][d]
        kept = e["rows_with_narrative"] - e["near_duplicates_dropped"]
        n = {s: e["splits"][s]["n"] for s in S.SPLITS}
        assert sum(n.values()) + e["pool_not_committed"] == kept
        for s in S.SPLITS:
            # per-stratum rounding moves each split by at most one row per stratum
            assert abs(n[s] - kept * S.FRACTIONS[d][s]) < 0.03 * kept


def test_test_split_is_stratified() -> None:
    for d in DATASETS:
        test = Counter(S.stratum(c) for c in _all()[(d, "test")])
        train = Counter(S.stratum(c) for c in _all()[(d, "train")])
        for st_, n in train.items():
            if n >= 50:
                ratio = test[st_] / n
                want = S.FRACTIONS[d]["test"] / S.FRACTIONS[d]["train"]
                assert abs(ratio - want) < 0.1, (st_, ratio)


def test_rows_are_sorted_and_have_narratives() -> None:
    for rows in _all().values():
        ids = [int(c.complaint_id) for c in rows]
        assert ids == sorted(ids)
        assert all(c.narrative.strip() for c in rows)


# --- pure split logic --------------------------------------------------------------


def _c(cid: int, narrative: str, issue: str = "A", product: str = "Credit card") -> Complaint:
    return Complaint(
        id=f"cfpb2015-{cid}",
        dataset="cfpb2015",
        complaint_id=str(cid),
        date_received=date(2015, 4, 1),
        product=product,
        sub_product="",
        issue=issue,
        sub_issue="",
        narrative=narrative,
        company="Example Bank",
        state="",
        submitted_via="Web",
        company_response="Closed with explanation",
        timely_response="Yes",
        consumer_disputed="No",
    )


def test_dedupe_keeps_smallest_id() -> None:
    rows = [_c(30, "Charged twice!"), _c(4, "charged   TWICE"), _c(9, "other")]
    kept, dropped = S.dedupe(rows)
    assert [c.complaint_id for c in kept] == ["4", "9"]
    assert dropped == ["cfpb2015-30"]


def test_split_dataset_reports_stats() -> None:
    rows = [_c(i, f"narrative {i}", issue="AB"[i % 2]) for i in range(100)]
    rows.append(_c(1000, "narrative 1"))
    splits, stats = S.split_dataset("cfpb2015", rows, seed=1)
    assert stats["near_duplicates_dropped"] == 1
    assert sum(len(v) for v in splits.values()) + stats["pool_not_committed"] == 100
    assert len(splits["test"]) == 20


def test_assign_rejects_bad_fractions() -> None:
    with pytest.raises(ValueError):
        S.assign([("a", ("x",))], {"test": 0.8, "dev": 0.5}, 1)
    with pytest.raises(ValueError):
        S.assign([("a", ("x",))], {"test": -0.1, "train": 0.5}, 1)


def test_one_row_stratum_goes_to_train() -> None:
    assert S.assign([("a", ("x",))], {"test": 0.2, "dev": 0.1, "train": 0.5}, 7) == {"a": "train"}


def test_gzip_is_byte_deterministic() -> None:
    data = b'{"a": 1}\n' * 100
    assert S._gzip(data) == S._gzip(data)
    assert gzip.decompress(S._gzip(data)) == data


items_st = st.lists(
    st.tuples(st.integers(0, 10_000), st.sampled_from(["p", "q", "r"])),
    max_size=300,
    unique_by=lambda t: t[0],
)


@settings(max_examples=60, deadline=None)
@given(items_st, st.integers(0, 2**32), st.randoms(use_true_random=False))
def test_assign_is_order_independent_and_partitions(
    raw: list[tuple[int, str]], seed: int, rnd: object
) -> None:
    items = [(f"id{i}", (s,)) for i, s in raw]
    fr: dict[S.Split, float] = {"test": 0.2, "dev": 0.1, "train": 0.5}
    a = S.assign(items, fr, seed)
    shuffled = list(items)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert S.assign(shuffled, fr, seed) == a
    assert set(a) == {i for i, _ in items}
    for stratum in ("p", "q", "r"):
        members = [i for i, s in items if s == (stratum,)]
        n = len(members)
        got = Counter(a[i] for i in members)
        assert got["test"] == round(n * 0.2)
        assert got["test"] + got["dev"] == round(n * (0.2 + 0.1))
        assert got["test"] + got["dev"] + got["train"] == round(n * (0.2 + 0.1 + 0.5))


def test_seed_changes_assignment() -> None:
    items = [(f"id{i}", ("s",)) for i in range(200)]
    fr: dict[S.Split, float] = {"test": 0.2, "dev": 0.1, "train": 0.5}
    assert S.assign(items, fr, 1) != S.assign(items, fr, 2)
    assert S.assign(items, fr, 1) == S.assign(items, fr, 1)


def _real_src() -> Path | None:
    env = os.environ.get("SCRUPLE_REAL_DATA_SRC")
    cands = [Path(env)] if env else []
    cands.append(Path.home() / ".cache" / "scruple" / "real-data")
    for c in cands:
        if (c / "cfpb_2012_2015/payments_subset.csv").is_file():
            return c
    return None


@pytest.mark.slow
@pytest.mark.skipif(_real_src() is None, reason="real source data not fetched")
def test_rebuild_reproduces_committed_manifest(tmp_path: Path) -> None:
    src = _real_src()
    assert src is not None
    m = S.build_splits(src, tmp_path)
    assert m == S.load_manifest()
    for d in DATASETS:
        for s in S.SPLITS:
            rel = f"{S.VERSION}/{d}/{s}.jsonl.gz"
            assert (tmp_path / rel).read_bytes() == (S.data_dir() / rel).read_bytes()
