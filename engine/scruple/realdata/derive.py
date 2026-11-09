"""Deterministic transforms from the raw mirrored files to the derived ones.

Both transforms were checked to reproduce the pinned sha256 of the derived file
byte for byte (Python `csv` reader/writer, minimal quoting, '\\n' line ends).

Usage (from scripts/fetch_real_data.sh): `python -m scruple.realdata.derive SRC_DIR`.
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from pathlib import Path

from scruple.realdata.provenance import PAYMENTS_PRODUCTS

CFPB_COLUMNS = 18


def payments_subset(zip_path: Path, out_path: Path) -> int:
    """Filter the 2012-2015 complaints to the four payments products."""
    keep = set(PAYMENTS_PRODUCTS)
    with zipfile.ZipFile(zip_path) as z, z.open("consumer_complaints.csv") as fh:
        text = fh.read().decode("utf-8")
    reader = csv.reader(io.StringIO(text, newline=""))
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    header = next(reader)
    writer.writerow(header)
    product = header.index("product")
    n = 0
    for row in reader:
        if row[product] in keep:
            writer.writerow(row)
            n += 1
    out_path.write_bytes(out.getvalue().encode("utf-8"))
    return n


def checking_sample(metadata_path: Path, out_path: Path) -> int:
    """Keep only the CFPB columns of the 2025-2026 checking/savings sample."""
    reader = csv.reader(io.StringIO(metadata_path.read_text(encoding="utf-8"), newline=""))
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    n = -1
    for row in reader:
        writer.writerow(row[:CFPB_COLUMNS])
        n += 1
    out_path.write_bytes(out.getvalue().encode("utf-8"))
    return n


def derive_all(src: Path) -> dict[str, int]:
    a = payments_subset(
        src / "cfpb_2012_2015/consumer_complaints.csv.zip",
        src / "cfpb_2012_2015/payments_subset.csv",
    )
    b = checking_sample(
        src / "cfpb_checking_2025_2026/sample_metadata.csv",
        src / "cfpb_checking_2025_2026/checking_complaints_sample.csv",
    )
    return {"cfpb_2012_2015/payments_subset.csv": a, "cfpb_checking_2025_2026": b}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m scruple.realdata.derive SRC_DIR", file=sys.stderr)
        return 2
    for name, n in derive_all(Path(args[0])).items():
        print(f"derived {name}: {n} rows")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
