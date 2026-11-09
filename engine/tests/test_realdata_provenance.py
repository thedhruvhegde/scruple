"""Provenance pins, checksum verification, derivations and the CSV record loader."""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.realdata import derive, provenance
from scruple.realdata.provenance import (
    FILES,
    REPOS,
    ChecksumError,
    sha256_file,
    sha256sums_text,
    verify_file,
    verify_source_dir,
)
from scruple.realdata.records import normalise_text, read_csv, text_key

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fetch_real_data.sh"


def _real_src() -> Path | None:
    env = os.environ.get("SCRUPLE_REAL_DATA_SRC")
    cands = [Path(env)] if env else []
    cands.append(Path.home() / ".cache" / "scruple" / "real-data")
    for c in cands:
        if (c / FILES[0].local_path).is_file():
            return c
    return None


# --- pins ---------------------------------------------------------------------


def test_pins_are_well_formed() -> None:
    names = {r.name for r in REPOS}
    assert len(names) == len(REPOS)
    for r in REPOS:
        assert re.fullmatch(r"[0-9a-f]{40}", r.commit), r
        assert r.url.startswith("https://github.com/")
        assert r.licence
    paths = [f.local_path for f in FILES]
    assert len(set(paths)) == len(paths)
    for f in FILES:
        assert re.fullmatch(r"[0-9a-f]{64}", f.sha256), f
        assert f.repo in names
        assert (f.repo_path is None) == (f.derived_from is not None)
        if f.derived_from is not None:
            assert provenance.source_file(f.derived_from).derived_from is None
            assert f.derivation


def test_lookup_errors() -> None:
    with pytest.raises(KeyError):
        provenance.repo("nope")
    with pytest.raises(KeyError):
        provenance.source_file("nope.csv")
    assert provenance.repo("ecfr-title-12").commit.startswith("4dc0246c")


def test_fetch_script_matches_pins() -> None:
    text = SCRIPT.read_text()
    assert os.access(SCRIPT, os.X_OK)
    assert "GIT_LFS_SKIP_SMUDGE=1" in text
    assert "--depth 1" in text
    for r in REPOS:
        assert f'"{r.name} {r.url} {r.commit}"' in text, r.name
    raw = [f for f in FILES if f.derived_from is None]
    for f in raw:
        assert f'"{f.local_path} {f.repo} {f.repo_path}"' in text, f.local_path
    # derived files are rebuilt, never copied
    for f in FILES:
        if f.derived_from is not None:
            assert f'"{f.local_path} ' not in text


def test_sha256sums_text_format() -> None:
    lines = sha256sums_text().splitlines()
    assert len(lines) == len(FILES)
    for line, f in zip(lines, FILES, strict=True):
        assert line == f"{f.sha256}  {f.local_path}"


# --- checksums ----------------------------------------------------------------


def test_sha256_file_known_value(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"abc")
    assert sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    verify_file(p, sha256_file(p))


def test_verify_file_rejects_tampering(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"abc")
    good = sha256_file(p)
    p.write_bytes(b"abd")
    with pytest.raises(ChecksumError, match="pinned"):
        verify_file(p, good)
    with pytest.raises(ChecksumError, match="missing"):
        verify_file(tmp_path / "gone.txt", good)


def test_verify_source_dir_checks_only_requested(tmp_path: Path) -> None:
    f = FILES[0]
    with pytest.raises(ChecksumError):
        verify_source_dir(tmp_path, only=(f.local_path,))
    assert verify_source_dir(tmp_path, only=()) == []


def test_committed_regs_match_pins() -> None:
    for f in FILES:
        if f.local_path.startswith("regs/"):
            verify_file(ROOT / "data" / "real" / f.local_path, f.sha256)


@pytest.mark.skipif(_real_src() is None, reason="real source data not fetched")
def test_real_source_dir_matches_pins() -> None:
    src = _real_src()
    assert src is not None
    assert len(verify_source_dir(src)) == len(FILES)


# --- derivations ---------------------------------------------------------------

HEADER_2015 = [
    "date_received",
    "product",
    "sub_product",
    "issue",
    "sub_issue",
    "consumer_complaint_narrative",
    "company_public_response",
    "company",
    "state",
    "zipcode",
    "tags",
    "consumer_consent_provided",
    "submitted_via",
    "date_sent_to_company",
    "company_response_to_consumer",
    "timely_response",
    "consumer_disputed?",
    "complaint_id",
]


def _row(product: str, narrative: str, cid: str, issue: str = "Billing disputes") -> list[str]:
    r = dict.fromkeys(HEADER_2015, "")
    r.update(
        date_received="03/19/2015",
        product=product,
        issue=issue,
        consumer_complaint_narrative=narrative,
        company="Example Bank",
        state="OR",
        zipcode="97000",
        submitted_via="Web",
        company_response_to_consumer="Closed with explanation",
        timely_response="Yes",
        complaint_id=cid,
    )
    r["consumer_disputed?"] = "No"
    return [r[h] for h in HEADER_2015]


def _csv(rows: list[list[str]]) -> str:
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\n")
    w.writerow(HEADER_2015)
    for r in rows:
        w.writerow(r)
    return out.getvalue()


def test_payments_subset_filters_products(tmp_path: Path) -> None:
    rows = [
        _row("Credit card", 'said "hi",\nthen left', "1"),
        _row("Mortgage", "x", "2"),
        _row("Prepaid card", "", "3"),
        _row("Money transfers", "m", "4"),
        _row("Bank account or service", "b", "5"),
        _row("Debt collection", "d", "6"),
    ]
    z = tmp_path / "c.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("consumer_complaints.csv", _csv(rows))
    out = tmp_path / "p.csv"
    assert derive.payments_subset(z, out) == 4
    got = list(csv.reader(io.StringIO(out.read_text(), newline="")))
    assert got[0] == HEADER_2015
    assert [r[-1] for r in got[1:]] == ["1", "3", "4", "5"]
    assert got[1][5] == 'said "hi",\nthen left'
    assert b"\r\n" not in out.read_bytes()


def test_checking_sample_drops_extra_columns(tmp_path: Path) -> None:
    src = tmp_path / "m.csv"
    hdr = [f"c{i}" for i in range(18)] + ["clean_text", "word_count"]
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\n")
    w.writerow(hdr)
    w.writerow([str(i) for i in range(18)] + ["clean", "3"])
    src.write_text(out.getvalue())
    dst = tmp_path / "o.csv"
    assert derive.checking_sample(src, dst) == 1
    assert dst.read_text().splitlines()[0] == ",".join(hdr[:18])
    assert dst.read_text().splitlines()[1].endswith(",17")


def test_derive_main_usage() -> None:
    assert derive.main([]) == 2


# --- records ---------------------------------------------------------------------


def test_read_csv_2015_skips_empty_narratives(tmp_path: Path) -> None:
    p = tmp_path / "p.csv"
    p.write_text(_csv([_row("Credit card", "  text  ", "10"), _row("Credit card", " ", "11")]))
    got = list(read_csv("cfpb2015", p))
    assert len(got) == 1
    c = got[0]
    assert c.id == "cfpb2015-10"
    assert c.narrative == "text"
    assert c.date_received == date(2015, 3, 19)
    assert c.consumer_disputed == "No"
    assert c.company_response == "Closed with explanation"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("I was charged XXXX on XX/XX/XXXX.", "i was charged xx on xx xx xx"),
        ("Hello,   World!", "hello world"),
        ("Refund\n\nplease", "REFUND please."),
    ],
)
def test_text_key_ignores_case_punct_masks(a: str, b: str) -> None:
    assert text_key(a) == text_key(b)


def test_text_key_distinguishes_content() -> None:
    assert text_key("charged twice") != text_key("charged once")


@given(st.text(max_size=200))
def test_normalise_is_idempotent(s: str) -> None:
    n = normalise_text(s)
    assert normalise_text(n) == n
    assert n == n.strip()
    assert "  " not in n
