"""Rebuild the vendored Stripe subset from a full download of stripe/openapi.

Usage: python tests/fixtures/stripe/extract.py spec3.json fixtures3.json tests/fixtures/stripe
See README.md in this directory for the source URLs and the commit the subset came from.
"""

from __future__ import annotations

import json
import sys
from typing import Any

SCHEMAS = (
    "dispute",
    "dispute_evidence",
    "dispute_evidence_details",
    "dispute_payment_method_details",
    "dispute_payment_method_details_card",
    "balance_transaction",
    "fee",
    "charge",
    "event",
    "notification_event_data",
)
RESOURCES = ("dispute", "charge", "balance_transaction", "event")


def _load(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(obj: object, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")


def main(spec_path: str, fix_path: str, out_dir: str) -> None:
    spec = _load(spec_path)
    fix = _load(fix_path)
    hook = spec["paths"]["/v1/webhook_endpoints"]["post"]
    form = hook["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
    enabled = form["properties"]["enabled_events"]["items"]["enum"]
    subset = {
        "x-scruple-dispute-event-types": sorted(
            e for e in enabled if e.startswith("charge.dispute.")
        ),
        "openapi": spec["openapi"],
        "info": {"title": spec["info"]["title"], "version": spec["info"]["version"]},
        "paths": {
            "/v1/disputes": {"get": spec["paths"]["/v1/disputes"]["get"]},
            "/v1/disputes/{dispute}": {"get": spec["paths"]["/v1/disputes/{dispute}"]["get"]},
        },
        "components": {"schemas": {n: spec["components"]["schemas"][n] for n in SCHEMAS}},
    }
    _dump(subset, f"{out_dir}/spec3.subset.json")
    _dump(
        {"resources": {n: fix["resources"][n] for n in RESOURCES}},
        f"{out_dir}/fixtures3.subset.json",
    )


if __name__ == "__main__":
    main(*sys.argv[1:4])
