"""CFPB complaint records: one schema for both CSV vintages.

Set `cfpb2015` uses the pre-2017 column names and taxonomy, set `cfpb2025` the
current ones. Labels are never pooled across the two (DATA_CARD.md). Only the
columns the tasks need are kept; ZIP code and tags are dropped.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

Dataset = Literal["cfpb2015", "cfpb2025"]
DATASETS: tuple[Dataset, ...] = ("cfpb2015", "cfpb2025")

SOURCE_CSV: dict[Dataset, str] = {
    "cfpb2015": "cfpb_2012_2015/payments_subset.csv",
    "cfpb2025": "cfpb_checking_2025_2026/checking_complaints_sample.csv",
}

# record field -> CSV column, per vintage
_COLUMNS: dict[Dataset, dict[str, str]] = {
    "cfpb2015": {
        "complaint_id": "complaint_id",
        "date_received": "date_received",
        "product": "product",
        "sub_product": "sub_product",
        "issue": "issue",
        "sub_issue": "sub_issue",
        "narrative": "consumer_complaint_narrative",
        "company": "company",
        "state": "state",
        "submitted_via": "submitted_via",
        "company_response": "company_response_to_consumer",
        "timely_response": "timely_response",
        "consumer_disputed": "consumer_disputed?",
    },
    "cfpb2025": {
        "complaint_id": "Complaint ID",
        "date_received": "Date received",
        "product": "Product",
        "sub_product": "Sub-product",
        "issue": "Issue",
        "sub_issue": "Sub-issue",
        "narrative": "Consumer complaint narrative",
        "company": "Company",
        "state": "State",
        "submitted_via": "Submitted via",
        "company_response": "Company response to consumer",
        "timely_response": "Timely response?",
        "consumer_disputed": "Consumer disputed?",
    },
}


class Complaint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str  # "<dataset>-<complaint_id>"
    dataset: Dataset
    complaint_id: str
    date_received: date
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
    consumer_disputed: str


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%m/%d/%Y").date()


def read_csv(dataset: Dataset, path: Path) -> Iterator[Complaint]:
    """Rows with a non-empty narrative, in file order."""
    cols = _COLUMNS[dataset]
    csv.field_size_limit(1 << 24)
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            narrative = row[cols["narrative"]].strip()
            if not narrative:
                continue
            values = {k: row[c].strip() for k, c in cols.items() if k != "narrative"}
            yield Complaint(
                id=f"{dataset}-{values['complaint_id']}",
                dataset=dataset,
                narrative=narrative,
                date_received=_parse_date(values.pop("date_received")),
                **values,
            )


_MASK = re.compile(r"\bx{2,}\b")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_text(text: str) -> str:
    """Lowercase, collapse CFPB masks (XXXX, XX/XX/XXXX) and punctuation/whitespace.

    Two narratives that differ only in case, spacing, punctuation or mask length
    normalise to the same string; used for duplicate detection across splits.
    """
    t = _NON_ALNUM.sub(" ", text.lower())
    t = _MASK.sub("x", t)
    return " ".join(t.split())


def text_key(text: str) -> str:
    return hashlib.sha256(normalise_text(text).encode("utf-8")).hexdigest()
