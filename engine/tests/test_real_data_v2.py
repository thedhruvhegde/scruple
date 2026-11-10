"""Real-data v2 (data/real/v2): pins, manifest hashes, loaders, rebuild."""

from __future__ import annotations

import csv
import gzip
import io
import os
import re
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.realdata import v2
from scruple.realdata.provenance import ChecksumError, sha256_file
from scruple.realdata.splits import data_dir

V2 = data_dir() / "v2"


def _src() -> Path | None:
    for c in (os.environ.get("SCRUPLE_REAL_DATA_SRC"), "/home/user/data-src/real"):
        if c and (Path(c) / v2.RAW[0].local_path).is_file():
            return Path(c)
    return None


def test_pins_are_well_formed() -> None:
    names = {r.name for r in v2.REPOS}
    assert len(names) == len(v2.REPOS)
    for r in v2.REPOS:
        assert re.fullmatch(r"[0-9a-f]{40}", r.commit)
        assert r.url.startswith("https://github.com/")
        assert "public domain" in r.licence
    for f in v2.RAW:
        assert f.repo in names
        assert re.fullmatch(r"[0-9a-f]{64}", f.sha256)
        assert f.repo_path is not None


def test_manifest_matches_committed_files() -> None:
    m = v2.load_manifest()
    assert m["version"] == "v2"
    assert set(m["files"]) == {*v2.META_PATHS.values(), v2.NARRATIVES_PATH, v2.ENFORCEMENT_PATH}
    for rel, meta in m["files"].items():
        assert sha256_file(V2 / rel) == meta["sha256"], rel
        blob = (V2 / rel).read_bytes()
        content = gzip.decompress(blob) if rel.endswith(".gz") else blob
        assert v2.sha256_bytes(content) == meta["sha256_content"]


def test_committed_size_is_modest() -> None:
    total = sum(p.stat().st_size for p in V2.rglob("*") if p.is_file())
    assert total < 15 * 1024 * 1024


@pytest.mark.parametrize(
    ("name", "n", "products"),
    [
        (
            "cfpb2015_payments",
            135_313,
            {"Credit card", "Bank account or service", "Money transfers", "Prepaid card"},
        ),
        ("cfpb2025_checking", 8_000, {"Checking or savings account"}),
        ("cfpb_cc2024", 21_508, {"Credit card"}),
    ],
)
def test_meta_sets_load(name: v2.MetaSet, n: int, products: set[str]) -> None:
    rows = v2.load_meta(name)
    assert len(rows) == n
    assert {r.product for r in rows} == products
    ids = [int(r.complaint_id) for r in rows]
    assert ids == sorted(ids) and len(set(ids)) == n
    assert {r.timely_response for r in rows} <= {"Yes", "No"}
    assert v2.load_manifest()["files"][v2.META_PATHS[name]]["rows"] == n


def test_card_narratives_are_a_subset_of_the_meta_rows() -> None:
    narr = v2.load_card_narratives()
    assert len(narr) == v2.NARRATIVE_SAMPLE
    meta = {r.complaint_id: r for r in v2.load_meta("cfpb_cc2024")}
    for c in narr:
        m = meta[c.complaint_id]
        assert c.id == f"cfpbcc2024-{c.complaint_id}"
        assert (c.issue, c.sub_issue, c.company, c.date_received) == (
            m.issue,
            m.sub_issue,
            m.company,
            m.date_received,
        )
        assert c.narrative.strip()


def test_narrative_sample_is_the_first_by_seeded_hash() -> None:
    narr = v2.load_card_narratives()
    picked = {c.complaint_id for c in narr}
    keys = sorted(v2.order_key(v2.SEED, r.complaint_id) for r in v2.load_meta("cfpb_cc2024"))
    cut = keys[v2.NARRATIVE_SAMPLE - 1]
    assert all(v2.order_key(v2.SEED, i) <= cut for i in picked)


def test_enforcement_table() -> None:
    rows = v2.load_enforcement()
    assert len(rows) == 359
    assert "Total Ordered: Civil Money Penalties" in rows[0]
    assert sum(1 for r in rows if r["Case Caption"]) > 300
    for r in rows:
        float(r["Total Ordered Amount"] or 0)


def test_tampered_content_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "real"
    (base / "v2" / "enforcement").mkdir(parents=True)
    (base / "v2" / "manifest.json").write_bytes((V2 / "manifest.json").read_bytes())
    raw = gzip.decompress((V2 / v2.ENFORCEMENT_PATH).read_bytes())
    tampered = v2.gzip_bytes(raw.replace(b"Regions", b"Regionz", 1))
    (base / "v2" / v2.ENFORCEMENT_PATH).write_bytes(tampered)
    with pytest.raises(ChecksumError):
        v2.read_file(v2.ENFORCEMENT_PATH, base)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("08/30/2013", date(2013, 8, 30)),
        ("2025-10-30", date(2025, 10, 30)),
        ("2024-08-05T14:22:48.000Z", date(2024, 8, 5)),
    ],
)
def test_parse_cfpb_date(raw: str, want: date) -> None:
    assert v2.parse_cfpb_date(raw) == want


@given(st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)))
def test_date_formats_agree(d: date) -> None:
    assert v2.parse_cfpb_date(d.strftime("%m/%d/%Y")) == d
    assert v2.parse_cfpb_date(d.isoformat()) == d
    assert v2.parse_cfpb_date(d.isoformat() + "T23:59:59.000Z") == d


@given(st.binary(max_size=200))
def test_gzip_is_deterministic(data: bytes) -> None:
    assert v2.gzip_bytes(data) == v2.gzip_bytes(data)
    assert gzip.decompress(v2.gzip_bytes(data)) == data


def test_enforcement_table_strips_bom_and_blank_rows() -> None:
    raw = "﻿A ,B\r\n x ,1\r\n,\r\n".encode()
    out = v2.enforcement_table(raw).decode()
    assert out == "A,B\nx,1\n"
    assert list(csv.reader(io.StringIO(out))) == [["A", "B"], ["x", "1"]]


@pytest.mark.slow
def test_rebuild_is_byte_identical(tmp_path: Path) -> None:
    src = _src()
    if src is None:
        pytest.skip("raw real-data sources not on disk")
    m = v2.build_v2(src, tmp_path)
    for rel, meta in v2.load_manifest()["files"].items():
        assert m["files"][rel] == meta
        assert (tmp_path / "v2" / rel).read_bytes() == (V2 / rel).read_bytes()
