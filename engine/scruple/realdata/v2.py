"""Real-data v2: more public CFPB data, as small derived tables (`data/real/v2/`).

v1 (`splits.py`) holds narratives for the four 2012-2015 payments products and a
2025-2026 checking/savings sample. v2 adds, without touching v1:

- `cfpb_cc2024/meta.csv.gz`: every row of a public pull of CFPB credit card
  *dispute* complaints received 2024-08-04 to 2026-07-15 (21,508 rows, current
  taxonomy), CFPB columns only, no narrative.
- `cfpb_cc2024/narratives.jsonl.gz`: the narratives of a deterministic sample of
  those rows (the first `NARRATIVE_SAMPLE` by sha256(f"{SEED}:{complaint_id}")).
- `timeliness/cfpb2015_payments.csv.gz`, `timeliness/cfpb2025_checking.csv.gz`:
  the date and response fields of every row of the two v1 source CSVs (135,313 and
  8,000 rows), for the complaint-handling timeliness study (`timeliness.py`).
- `enforcement/cfpb_enforcement_orders.csv`: CFPB enforcement orders with ordered
  amounts (civil money penalties, redress, disgorgement), from CFPB records released
  under FOIA to the Data Liberation Project. 359 rows, 2012-2023.

Licences: CFPB complaint data is a US federal government work, public domain
(17 USC 105). The card-dispute mirror has no licence file; we keep only the CFPB
columns and drop the mirror author's keyword tags (P1..P6). The enforcement table is
CFPB records obtained under FOIA (public domain as government records); the mirror's
README licenses the files its author generated under CC BY-SA 4.0, so we redistribute
the derived table under CC BY-SA 4.0 with attribution, to be safe.

Every raw input is pinned by repository, commit and sha256 (`RAW`); `build_v2`
re-derives the committed files byte for byte (gzip mtime 0, `\\n` line ends) and
`fetch_v2` re-downloads the raw inputs with anonymous git. See docs/data/CFPB.md.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from scruple.realdata.provenance import (
    ChecksumError,
    SourceFile,
    SourceRepo,
    source_file,
    verify_file,
)

VERSION = "v2"
SEED = 2026
NARRATIVE_SAMPLE = 3000

REPOS: tuple[SourceRepo, ...] = (
    SourceRepo(
        name="cfpb-cc-disputes-2024-2026",
        url="https://github.com/THEpranavsomalraju/card-dispute-prd",
        commit="aaa209545cd6cfa695233512cc0feb77fcbf51d3",
        licence="CFPB data: public domain (17 USC 105); repo has no licence file, only "
        "the CFPB columns are kept (the author's P1-P6 tags are dropped)",
    ),
    SourceRepo(
        name="cfpb-enforcement-relief",
        url="https://github.com/LukasNichols/CFPB_Enforcement_Relief_And_Penalties",
        commit="13014e6ec9fe02b44855c5866de40f9694089582",
        licence="CFPB records released under FOIA to the Data Liberation Project (public "
        "domain government records); the repo's generated files are CC BY-SA 4.0, so the "
        "derived table is redistributed under CC BY-SA 4.0 with attribution",
    ),
)

RAW: tuple[SourceFile, ...] = (
    SourceFile(
        local_path="cfpb_cc_2024_2026/ccdb_disputes_raw.zip",
        repo="cfpb-cc-disputes-2024-2026",
        repo_path="ccdb_disputes_raw.zip",
        sha256="0b121cdacd059afba27c15871f8ea78a90060f9951613df5688184d9960a060f",
    ),
    SourceFile(
        local_path="cfpb_enforcement/Disp.csv",
        repo="cfpb-enforcement-relief",
        repo_path="Disp.csv",
        sha256="5cd6578cced51eec5ba21bd6f86317ee82242009a55e7f184894451eb694829b",
    ),
)
# v1 raw inputs reused here (pinned in provenance.py)
V1_INPUTS: tuple[str, ...] = (
    "cfpb_2012_2015/payments_subset.csv",
    "cfpb_checking_2025_2026/checking_complaints_sample.csv",
)

META_COLUMNS: tuple[str, ...] = (
    "complaint_id",
    "date_received",
    "date_sent_to_company",
    "product",
    "sub_product",
    "issue",
    "sub_issue",
    "company",
    "state",
    "submitted_via",
    "company_response",
    "timely_response",
)

# meta column -> raw CSV column, per source
_RAW_COLUMNS: dict[str, dict[str, str]] = {
    "cfpb2015_payments": {
        "complaint_id": "complaint_id",
        "date_received": "date_received",
        "date_sent_to_company": "date_sent_to_company",
        "product": "product",
        "sub_product": "sub_product",
        "issue": "issue",
        "sub_issue": "sub_issue",
        "company": "company",
        "state": "state",
        "submitted_via": "submitted_via",
        "company_response": "company_response_to_consumer",
        "timely_response": "timely_response",
    },
    "cfpb2025_checking": {
        "complaint_id": "Complaint ID",
        "date_received": "Date received",
        "date_sent_to_company": "Date sent to company",
        "product": "Product",
        "sub_product": "Sub-product",
        "issue": "Issue",
        "sub_issue": "Sub-issue",
        "company": "Company",
        "state": "State",
        "submitted_via": "Submitted via",
        "company_response": "Company response to consumer",
        "timely_response": "Timely response?",
    },
    "cfpb_cc2024": {
        "complaint_id": "complaint_id",
        "date_received": "date_received",
        "date_sent_to_company": "date_sent_to_company",
        "product": "product",
        "sub_product": "sub_product",
        "issue": "issue",
        "sub_issue": "sub_issue",
        "company": "company",
        "state": "state",
        "submitted_via": "submitted_via",
        "company_response": "company_response",
        "timely_response": "timely",
    },
}

MetaSet = Literal["cfpb2015_payments", "cfpb2025_checking", "cfpb_cc2024"]
META_SETS: tuple[MetaSet, ...] = ("cfpb2015_payments", "cfpb2025_checking", "cfpb_cc2024")
META_PATHS: dict[MetaSet, str] = {
    "cfpb2015_payments": "timeliness/cfpb2015_payments.csv.gz",
    "cfpb2025_checking": "timeliness/cfpb2025_checking.csv.gz",
    "cfpb_cc2024": "cfpb_cc2024/meta.csv.gz",
}
NARRATIVES_PATH = "cfpb_cc2024/narratives.jsonl.gz"
ENFORCEMENT_PATH = "enforcement/cfpb_enforcement_orders.csv"


class MetaRow(BaseModel):
    """Date and response fields of one complaint (no narrative)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    complaint_id: str
    date_received: date
    date_sent_to_company: date
    product: str
    sub_product: str
    issue: str
    sub_issue: str
    company: str
    state: str
    submitted_via: str
    company_response: str
    timely_response: str

    @property
    def days_to_company(self) -> int:
        return (self.date_sent_to_company - self.date_received).days


class CardComplaint(BaseModel):
    """One credit card dispute complaint with its narrative (cfpb_cc2024 sample)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str  # "cfpbcc2024-<complaint_id>"
    dataset: Literal["cfpbcc2024"]
    complaint_id: str
    date_received: date
    date_sent_to_company: date
    product: str
    sub_product: str
    issue: str
    sub_issue: str
    narrative: str
    company: str
    state: str
    submitted_via: str
    company_response: str
    timely_response: str


def parse_cfpb_date(raw: str) -> date:
    """'08/30/2013' (2015 vintage), '2025-10-30', or '2024-08-05T14:22:48.000Z'."""
    s = raw.strip()
    if "/" in s:
        return datetime.strptime(s, "%m/%d/%Y").date()
    if len(s) == 10:
        return date.fromisoformat(s)
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


# --- deterministic encodings ------------------------------------------------------------
def gzip_bytes(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _csv_bytes(header: Sequence[str], rows: Iterable[Sequence[str]]) -> bytes:
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return out.getvalue().encode("utf-8")


def order_key(seed: int, complaint_id: str) -> str:
    return hashlib.sha256(f"{seed}:{complaint_id}".encode()).hexdigest()


# --- derivations --------------------------------------------------------------------------
def _read_dicts(text: str) -> list[dict[str, str]]:
    csv.field_size_limit(1 << 24)
    return list(csv.DictReader(io.StringIO(text, newline="")))


def meta_rows(source: MetaSet, rows: Iterable[dict[str, str]]) -> list[MetaRow]:
    cols = _RAW_COLUMNS[source]
    out: list[MetaRow] = []
    for r in rows:
        v = {k: r[c].strip() for k, c in cols.items()}
        out.append(
            MetaRow(
                **{k: x for k, x in v.items() if not k.startswith("date_")},
                date_received=parse_cfpb_date(v["date_received"]),
                date_sent_to_company=parse_cfpb_date(v["date_sent_to_company"]),
            )
        )
    return sorted(out, key=lambda m: int(m.complaint_id))


def _meta_csv(rows: Sequence[MetaRow]) -> bytes:
    return _csv_bytes(
        META_COLUMNS,
        ([str(getattr(m, c)) for c in META_COLUMNS] for m in rows),
    )


def card_narratives(
    rows: Iterable[dict[str, str]], n: int = NARRATIVE_SAMPLE
) -> list[CardComplaint]:
    """The deterministic narrative sample of the card-dispute pull, sorted by id."""
    cols = _RAW_COLUMNS["cfpb_cc2024"]
    pool = [r for r in rows if r["complaint_what_happened"].strip()]
    pool.sort(key=lambda r: order_key(SEED, r["complaint_id"].strip()))
    out = []
    for r in pool[:n]:
        v = {k: r[c].strip() for k, c in cols.items()}
        out.append(
            CardComplaint(
                id=f"cfpbcc2024-{v['complaint_id']}",
                dataset="cfpbcc2024",
                narrative=r["complaint_what_happened"].strip(),
                **{k: x for k, x in v.items() if not k.startswith("date_")},
                date_received=parse_cfpb_date(v["date_received"]),
                date_sent_to_company=parse_cfpb_date(v["date_sent_to_company"]),
            )
        )
    return sorted(out, key=lambda c: int(c.complaint_id))


def _jsonl(models: Iterable[BaseModel]) -> bytes:
    lines = [
        json.dumps(m.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) for m in models
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def enforcement_table(raw: bytes) -> bytes:
    """The FOIA orders table, BOM stripped, fields trimmed, '\\n' line ends."""
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""))
    header = [h.strip() for h in next(reader)]
    rows = [[c.strip() for c in r] for r in reader if any(c.strip() for c in r)]
    return _csv_bytes(header, rows)


def _check(src: Path, f: SourceFile) -> Path:
    path = src / f.local_path
    verify_file(path, f.sha256)
    return path


def build_v2(src: Path, out: Path) -> dict[str, Any]:
    """Verify every pinned raw input under `src` and write `out/v2/` (deterministic)."""
    root = out / VERSION
    outputs: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    # card disputes 2024-2026
    zpath = _check(src, RAW[0])
    with zipfile.ZipFile(zpath) as z, z.open("ccdb_disputes_raw.csv") as fh:
        cc = _read_dicts(fh.read().decode("utf-8"))
    cc_meta = meta_rows("cfpb_cc2024", cc)
    outputs[META_PATHS["cfpb_cc2024"]] = _meta_csv(cc_meta)
    counts[META_PATHS["cfpb_cc2024"]] = len(cc_meta)
    narr = card_narratives(cc)
    outputs[NARRATIVES_PATH] = _jsonl(narr)
    counts[NARRATIVES_PATH] = len(narr)
    # v1 sources, date and response fields only
    v1: tuple[tuple[MetaSet, str], ...] = (
        ("cfpb2015_payments", V1_INPUTS[0]),
        ("cfpb2025_checking", V1_INPUTS[1]),
    )
    for name, local in v1:
        rows = _read_dicts(_check(src, source_file(local)).read_text(encoding="utf-8"))
        meta = meta_rows(name, rows)
        outputs[META_PATHS[name]] = _meta_csv(meta)
        counts[META_PATHS[name]] = len(meta)
    # enforcement orders
    enf = enforcement_table(_check(src, RAW[1]).read_bytes())
    outputs[ENFORCEMENT_PATH] = enf
    counts[ENFORCEMENT_PATH] = enf.count(b"\n") - 1

    manifest: dict[str, Any] = {
        "version": VERSION,
        "seed": SEED,
        "narrative_sample": NARRATIVE_SAMPLE,
        "procedure": "scruple.realdata.v2.build_v2 (see module docstring)",
        "repos": [r.model_dump() for r in REPOS],
        "raw": [f.model_dump() for f in RAW] + [source_file(p).model_dump() for p in V1_INPUTS],
        "files": {},
    }
    derived_from = {
        META_PATHS["cfpb_cc2024"]: RAW[0].local_path,
        NARRATIVES_PATH: RAW[0].local_path,
        META_PATHS["cfpb2015_payments"]: V1_INPUTS[0],
        META_PATHS["cfpb2025_checking"]: V1_INPUTS[1],
        ENFORCEMENT_PATH: RAW[1].local_path,
    }
    for rel, content in sorted(outputs.items()):
        blob = gzip_bytes(content) if rel.endswith(".gz") else content
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(blob)
        manifest["files"][rel] = {
            "rows": counts[rel],
            "sha256": sha256_bytes(blob),
            "sha256_content": sha256_bytes(content),
            "derived_from": derived_from[rel],
        }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def fetch_v2(dest: Path) -> list[str]:  # pragma: no cover - network
    """Re-download the v2 raw inputs at their pinned commits (anonymous git) and
    verify them. v1 inputs come from scripts/fetch_real_data.sh."""
    got: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for r in REPOS:
            d = Path(tmp) / r.name
            d.mkdir()

            def run(*a: str, d: Path = d) -> None:
                env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"}
                subprocess.run(["git", "-C", str(d), *a], check=True, env=env)  # noqa: S603,S607

            run("init", "-q")
            run("remote", "add", "origin", r.url)
            run("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", r.commit)
            for f in RAW:
                if f.repo != r.name:
                    continue
                assert f.repo_path is not None
                run("checkout", "-q", "FETCH_HEAD", "--", f.repo_path)
                target = dest / f.local_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((d / f.repo_path).read_bytes())
                verify_file(target, f.sha256)
                got.append(f.local_path)
    return got


# --- loading --------------------------------------------------------------------------------
def v2_dir(base: Path | None = None) -> Path:
    from scruple.realdata.splits import data_dir

    return (base or data_dir()) / VERSION


def load_manifest(base: Path | None = None) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((v2_dir(base) / "manifest.json").read_text())
    return data


def read_file(rel: str, base: Path | None = None, *, verify: bool = True) -> bytes:
    """The content of a committed v2 file (decompressed), checked against the manifest."""
    meta = load_manifest(base)["files"][rel]
    blob = (v2_dir(base) / rel).read_bytes()
    content = gzip.decompress(blob) if rel.endswith(".gz") else blob
    if verify and sha256_bytes(content) != meta["sha256_content"]:
        raise ChecksumError(f"{rel}: content sha256 does not match the v2 manifest")
    return content


@cache
def load_meta(name: MetaSet, base: Path | None = None) -> tuple[MetaRow, ...]:
    text = read_file(META_PATHS[name], base).decode("utf-8")
    return tuple(
        MetaRow(
            **{k: v for k, v in r.items() if not k.startswith("date_")},
            date_received=date.fromisoformat(r["date_received"]),
            date_sent_to_company=date.fromisoformat(r["date_sent_to_company"]),
        )
        for r in csv.DictReader(io.StringIO(text, newline=""))
    )


@cache
def load_card_narratives(base: Path | None = None) -> tuple[CardComplaint, ...]:
    raw = read_file(NARRATIVES_PATH, base).decode("utf-8")
    return tuple(CardComplaint.model_validate_json(line) for line in raw.splitlines())


def load_enforcement(base: Path | None = None) -> list[dict[str, str]]:
    text = read_file(ENFORCEMENT_PATH, base).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text, newline="")))


__all__ = [
    "META_SETS",
    "RAW",
    "REPOS",
    "CardComplaint",
    "MetaRow",
    "build_v2",
    "load_card_narratives",
    "load_enforcement",
    "load_manifest",
    "load_meta",
]
