"""Frozen, versioned train/dev/test splits of the real CFPB narratives.

Procedure (`build_splits`), deterministic for a given source and seed:

1. Read every row with a narrative (`records.read_csv`).
2. Drop near-duplicates: narratives with the same normalised hash
   (`records.text_key`) form a group; the row with the smallest complaint id is
   kept, the others are dropped and counted in the manifest. So no narrative (up
   to case, punctuation, whitespace and mask length) can appear in two splits.
3. Stratify (cfpb2015 by product and issue, cfpb2025 by issue and sub-issue).
   Within a stratum, order rows by sha256(f"{seed}:{id}") and cut at cumulative
   fractions: test, then dev, then train. What is left is an uncommitted pool
   (kept out only to fit the repository's size budget). Cuts are
   `round(n * cumulative_fraction)`, so a one-row stratum lands in train.
4. Write each split as gzip JSONL (sorted by complaint id, canonical JSON, gzip
   mtime 0) and a manifest with counts and sha256 of the gzip and of the
   uncompressed JSONL.

The test split is sealed: `load_split(..., "test")` refuses unless `unseal=True`.
Use train for fitting, dev for choices, test once for the reported numbers.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal

from scruple.realdata.provenance import FILES, source_file, verify_file
from scruple.realdata.records import (
    DATASETS,
    SOURCE_CSV,
    Complaint,
    Dataset,
    read_csv,
    text_key,
)

Split = Literal["train", "dev", "test"]
SPLITS: tuple[Split, ...] = ("train", "dev", "test")
VERSION = "v1"
SPLIT_SEED = 2026
# cumulative order is test, dev, train; the remainder is the uncommitted pool
FRACTIONS: dict[Dataset, dict[Split, float]] = {
    "cfpb2015": {"test": 0.20, "dev": 0.10, "train": 0.45},
    "cfpb2025": {"test": 0.20, "dev": 0.10, "train": 0.40},
}
_CUT_ORDER: tuple[Split, ...] = ("test", "dev", "train")


class SealedSplitError(PermissionError):
    """The sealed test split was requested without `unseal=True`."""


def data_dir() -> Path:
    """Committed real-data directory (`data/real` in a checkout).

    Override with SCRUPLE_REAL_DATA_DIR. The data is not shipped in the wheel.
    """
    env = os.environ.get("SCRUPLE_REAL_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "real"


def stratum(c: Complaint) -> tuple[str, str]:
    if c.dataset == "cfpb2015":
        return (c.product, c.issue)
    return (c.issue, c.sub_issue)


def dedupe(rows: Iterable[Complaint]) -> tuple[list[Complaint], list[str]]:
    """Keep the smallest complaint id per normalised narrative; return (kept, dropped ids)."""
    groups: dict[str, list[Complaint]] = defaultdict(list)
    for c in rows:
        groups[text_key(c.narrative)].append(c)
    kept: list[Complaint] = []
    dropped: list[str] = []
    for members in groups.values():
        members.sort(key=lambda c: int(c.complaint_id))
        kept.append(members[0])
        dropped.extend(c.id for c in members[1:])
    kept.sort(key=lambda c: int(c.complaint_id))
    return kept, sorted(dropped)


def order_key(seed: int, item_id: str) -> str:
    return hashlib.sha256(f"{seed}:{item_id}".encode()).hexdigest()


def assign(
    items: Sequence[tuple[str, tuple[str, ...]]],
    fractions: dict[Split, float],
    seed: int,
) -> dict[str, Split | None]:
    """Map item id -> split (None = pool) by stratified cumulative cuts.

    `items` are (id, stratum) pairs. Pure function of its inputs.
    """
    total = sum(fractions.get(s, 0.0) for s in _CUT_ORDER)
    if not 0.0 < total <= 1.0 or any(v < 0 for v in fractions.values()):
        raise ValueError(f"bad fractions {fractions}")
    by_stratum: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for item_id, st in items:
        by_stratum[st].append(item_id)
    out: dict[str, Split | None] = {}
    for st in sorted(by_stratum):
        ids = sorted(by_stratum[st], key=lambda i: order_key(seed, i))
        n = len(ids)
        bounds: list[tuple[int, Split]] = []
        acc = 0.0
        for s in _CUT_ORDER:
            acc += fractions.get(s, 0.0)
            bounds.append((round(n * acc), s))
        start = 0
        for end, s in bounds:
            for i in ids[start:end]:
                out[i] = s
            start = max(start, end)
        for i in ids[start:]:
            out[i] = None
    return out


def _jsonl(rows: Iterable[Complaint]) -> bytes:
    lines = [
        json.dumps(c.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) for c in rows
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def _gzip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_dataset(
    dataset: Dataset, rows: Iterable[Complaint], seed: int = SPLIT_SEED
) -> tuple[dict[Split, list[Complaint]], dict[str, Any]]:
    """Dedupe and split one dataset. Returns (splits, stats for the manifest)."""
    all_rows = list(rows)
    kept, dropped = dedupe(all_rows)
    plan = assign([(c.id, stratum(c)) for c in kept], FRACTIONS[dataset], seed)
    splits: dict[Split, list[Complaint]] = {s: [] for s in SPLITS}
    pool = 0
    for c in kept:
        s = plan[c.id]
        if s is None:
            pool += 1
        else:
            splits[s].append(c)
    stats = {
        "rows_with_narrative": len(all_rows),
        "near_duplicates_dropped": len(dropped),
        "pool_not_committed": pool,
        "fractions": FRACTIONS[dataset],
    }
    return splits, stats


def build_splits(src: Path, out: Path, seed: int = SPLIT_SEED) -> dict[str, Any]:
    """Verify the pinned source CSVs, split both datasets and write `out/<version>/`."""
    root = out / VERSION
    manifest: dict[str, Any] = {
        "version": VERSION,
        "seed": seed,
        "procedure": "scruple.realdata.splits (see module docstring)",
        "datasets": {},
    }
    for ds in DATASETS:
        sf = source_file(SOURCE_CSV[ds])
        verify_file(src / sf.local_path, sf.sha256)
        splits, stats = split_dataset(ds, read_csv(ds, src / sf.local_path), seed)
        entry: dict[str, Any] = {"source": {"path": sf.local_path, "sha256": sf.sha256}}
        entry.update(stats)
        entry["splits"] = {}
        (root / ds).mkdir(parents=True, exist_ok=True)
        for s in SPLITS:
            raw = _jsonl(splits[s])
            gz = _gzip(raw)
            rel = f"{ds}/{s}.jsonl.gz"
            (root / rel).write_bytes(gz)
            entry["splits"][s] = {
                "path": rel,
                "n": len(splits[s]),
                "sha256_gz": _sha(gz),
                "sha256_jsonl": _sha(raw),
                "sealed": s == "test",
            }
        manifest["datasets"][ds] = entry
    manifest["regs"] = {}
    (out / "regs").mkdir(parents=True, exist_ok=True)
    for f in FILES:
        if f.local_path.startswith("regs/"):
            verify_file(src / f.local_path, f.sha256)
            (out / f.local_path).write_bytes((src / f.local_path).read_bytes())
            manifest["regs"][f.local_path] = f.sha256
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load_manifest(base: Path | None = None) -> dict[str, Any]:
    root = (base or data_dir()) / VERSION
    data: dict[str, Any] = json.loads((root / "manifest.json").read_text())
    return data


def read_split_file(path: Path, sha256_jsonl: str | None = None) -> list[Complaint]:
    raw = gzip.decompress(path.read_bytes())
    if sha256_jsonl is not None and _sha(raw) != sha256_jsonl:
        from scruple.realdata.provenance import ChecksumError

        raise ChecksumError(f"{path}: content sha256 {_sha(raw)} != manifest {sha256_jsonl}")
    return [Complaint.model_validate_json(line) for line in raw.decode("utf-8").splitlines()]


def load_split(
    dataset: Dataset,
    split: Split,
    *,
    unseal: bool = False,
    base: Path | None = None,
    verify: bool = True,
) -> list[Complaint]:
    """Load a committed split, checking its hash against the manifest."""
    if split == "test" and not unseal:
        raise SealedSplitError(
            "the test split is sealed: pass unseal=True only for a final, reported evaluation"
        )
    if dataset not in DATASETS or split not in SPLITS:
        raise KeyError(f"unknown split {dataset}/{split}")
    root = (base or data_dir()) / VERSION
    meta = load_manifest(base)["datasets"][dataset]["splits"][split]
    return read_split_file(root / meta["path"], meta["sha256_jsonl"] if verify else None)


def label_counts(rows: Iterable[Complaint], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(getattr(c, field)) for c in rows).items()))
