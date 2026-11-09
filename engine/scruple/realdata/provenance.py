"""Pinned provenance for every real (non-synthetic) file scruple reads.

Each source is a public GitHub mirror at a pinned commit. Files are either copied
as-is from the repository (`derived_from is None`) or derived from a raw file by a
deterministic transform in `scruple.realdata.derive`. Every file, raw or derived,
has a pinned sha256; `scripts/fetch_real_data.sh` re-downloads the raw files with
anonymous git, re-derives the rest and checks every hash.

Licences (see docs/data/CFPB.md):
- CFPB complaint data: US federal government work, public domain (17 USC 105).
- eCFR text (12 CFR parts 1005 and 1026): public domain, published by GPO.
- chargeback reason codes: CC BY 4.0, attribution to the mirror's author. Lookup
  table only; its evidence and winnability fields are not official guidance.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ChecksumError(ValueError):
    """A real-data file does not match its pinned sha256."""


class SourceRepo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    url: str
    commit: str
    licence: str


class SourceFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    local_path: str  # relative to the real-data source directory
    repo: str  # SourceRepo.name; for derived files, the repo of the raw input
    repo_path: str | None  # path inside the repo; None for derived files
    sha256: str
    derived_from: str | None = None  # local_path of the raw input
    derivation: str | None = None


REPOS: tuple[SourceRepo, ...] = (
    SourceRepo(
        name="cfpb-2012-2015",
        url="https://github.com/jiegzhan/multi-class-text-classification-cnn",
        commit="e75a4b1fafb011873055c2d39ce7a48f835f0c86",
        licence="CFPB data: public domain (17 USC 105); repo code Apache-2.0 (not used)",
    ),
    SourceRepo(
        name="cfpb-checking-2025-2026",
        url="https://github.com/mhoehler22/cfpb-agglomerative-clustering",
        commit="5ddb49aeb18218b10d11df15cfe60a5dbe8ece09",
        licence="CFPB data: public domain (17 USC 105); repo has no licence, only CFPB "
        "columns are kept",
    ),
    SourceRepo(
        name="ecfr-title-12",
        url="https://github.com/xerj-org/corpus-ecfr-title-12",
        commit="4dc0246c43bef051d64cb023596993d787281e3d",
        licence="public domain (US federal government work, GPO eCFR bulk data)",
    ),
    SourceRepo(
        name="chargeback-reason-codes",
        url="https://github.com/small-business-software-maker/chargeback-reason-codes",
        commit="8ded36825231948b3d94057d77cdc0875b7a7510",
        licence="CC BY 4.0",
    ),
)

PAYMENTS_PRODUCTS: tuple[str, ...] = (
    "Bank account or service",
    "Credit card",
    "Money transfers",
    "Prepaid card",
)

FILES: tuple[SourceFile, ...] = (
    SourceFile(
        local_path="cfpb_2012_2015/consumer_complaints.csv.zip",
        repo="cfpb-2012-2015",
        repo_path="data/consumer_complaints.csv.zip",
        sha256="38b11cfd7ebd5ec92b56bc86a453251b684c06dcdc7ef2979450458227d9c41d",
    ),
    SourceFile(
        local_path="cfpb_2012_2015/payments_subset.csv",
        repo="cfpb-2012-2015",
        repo_path=None,
        sha256="0ae2fefa23550ef941f9fcbf198d2e1e82ef51dd7bedca4478d4d4c7f40c3393",
        derived_from="cfpb_2012_2015/consumer_complaints.csv.zip",
        derivation="rows whose product is one of PAYMENTS_PRODUCTS, csv.writer, '\\n' line ends",
    ),
    SourceFile(
        local_path="cfpb_checking_2025_2026/sample_metadata.csv",
        repo="cfpb-checking-2025-2026",
        repo_path="data/outputs/sample_metadata.csv",
        sha256="d654ee76a036c0b1c9ce9de75f10065e9350cdbe08b2dac206e2bb5cfc7dd295",
    ),
    SourceFile(
        local_path="cfpb_checking_2025_2026/checking_complaints_sample.csv",
        repo="cfpb-checking-2025-2026",
        repo_path=None,
        sha256="483854821b404dc6ef9454c06b8aab1be464a1b0cdb0c941c2610f942a81c318",
        derived_from="cfpb_checking_2025_2026/sample_metadata.csv",
        derivation="first 18 (CFPB) columns; drops clean_text and word_count",
    ),
    SourceFile(
        local_path="regs/sec-1005.11.txt",
        repo="ecfr-title-12",
        repo_path="parts/part-1005/sec-1005.11.txt",
        sha256="c15391f755d0b79b30548798db305563b07e8a8b8a361ea2d4b9460c30935752",
    ),
    SourceFile(
        local_path="regs/sec-1005.6.txt",
        repo="ecfr-title-12",
        repo_path="parts/part-1005/sec-1005.6.txt",
        sha256="498413014e3bebf7ffb3b4ca03d01865e0e0c2761048abc3be1a02f683d59c68",
    ),
    SourceFile(
        local_path="regs/sec-1026.12.txt",
        repo="ecfr-title-12",
        repo_path="parts/part-1026/sec-1026.12.txt",
        sha256="19274b9eaae7826f439c41029f1f45127f4df2e2cf8ddb77048f973434808152",
    ),
    SourceFile(
        local_path="regs/sec-1026.13.txt",
        repo="ecfr-title-12",
        repo_path="parts/part-1026/sec-1026.13.txt",
        sha256="93b01d12266e36048f105a2a297bc1c63130da157f9575b5e46a5f048875bed8",
    ),
    SourceFile(
        local_path="chargeback_codes/chargeback-reason-codes.csv",
        repo="chargeback-reason-codes",
        repo_path="chargeback-reason-codes.csv",
        sha256="63c8ff04d7295efc7f4d98fe19f2a66855cccc28b3e00838468997b36bfc573f",
    ),
)


def repo(name: str) -> SourceRepo:
    for r in REPOS:
        if r.name == name:
            return r
    raise KeyError(f"unknown source repo {name!r}")


def source_file(local_path: str) -> SourceFile:
    for f in FILES:
        if f.local_path == local_path:
            return f
    raise KeyError(f"unknown real-data file {local_path!r}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file(path: Path, expected: str) -> None:
    """Raise ChecksumError unless `path` hashes to `expected`."""
    if not path.is_file():
        raise ChecksumError(f"{path}: missing")
    got = sha256_file(path)
    if got != expected:
        raise ChecksumError(f"{path}: sha256 {got} != pinned {expected}")


def verify_source_dir(src: Path, *, only: tuple[str, ...] | None = None) -> list[str]:
    """Check every pinned file under `src` (or just `only`). Returns the checked paths."""
    checked: list[str] = []
    for f in FILES:
        if only is not None and f.local_path not in only:
            continue
        verify_file(src / f.local_path, f.sha256)
        checked.append(f.local_path)
    return checked


def sha256sums_text() -> str:
    """The pinned hashes in `sha256sum -c` format (used by the fetch script)."""
    return "".join(f"{f.sha256}  {f.local_path}\n" for f in FILES)
