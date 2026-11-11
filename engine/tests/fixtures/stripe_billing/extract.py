"""Rebuild the vendored Stripe billing subset from a full download of stripe/openapi.

Usage:
    python tests/fixtures/stripe_billing/extract.py spec3.json fixtures3.json OUT_DIR
See README.md in this directory for the source URLs and the commit the subset came from.
"""

from __future__ import annotations

import json
import sys
from typing import Any

PATH = "/v1/billing/meter_events"
SCHEMAS = ("billing.meter_event", "error", "api_errors")
RESOURCES = ("billing.meter_event",)


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
    subset = {
        "openapi": spec["openapi"],
        "info": {"title": spec["info"]["title"], "version": spec["info"]["version"]},
        "paths": {PATH: {"post": spec["paths"][PATH]["post"]}},
        "components": {"schemas": {n: spec["components"]["schemas"][n] for n in SCHEMAS}},
    }
    _dump(subset, f"{out_dir}/spec3.billing.json")
    resources = {"resources": {n: fix["resources"][n] for n in RESOURCES}}
    _dump(resources, f"{out_dir}/fixtures3.billing.json")


if __name__ == "__main__":
    main(*sys.argv[1:4])
