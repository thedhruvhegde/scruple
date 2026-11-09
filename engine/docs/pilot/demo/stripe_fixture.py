"""Write the vendored REAL Stripe fixture dispute, with its charge expanded, as an export.

Run from the engine directory:  uv run python docs/pilot/demo/stripe_fixture.py

Source: tests/fixtures/stripe/fixtures3.subset.json, copied verbatim from stripe/openapi
(MIT, commit 6360907, see tests/fixtures/stripe/README.md). These are Stripe's own API
documentation sample objects, not a customer's data. The dispute's `charge` is replaced
by the fixture charge object, the shape `expand[]=charge` returns. Two copies are
written: the dispute as published (`needs_response`, an inquiry in the sample) and the
same dispute re-exported later as `lost` (so the backtest has one resolved case).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

FIX = Path("tests/fixtures/stripe/fixtures3.subset.json")
OUT = Path("demo-output/pilot")


def main() -> None:
    res = json.loads(FIX.read_text(encoding="utf-8"))["resources"]
    dispute = copy.deepcopy(res["dispute"])
    dispute["charge"] = copy.deepcopy(res["charge"])
    later = copy.deepcopy(dispute)
    later["status"] = "lost"
    later["payment_method_details"]["card"]["case_type"] = "chargeback"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stripe-fixture.json").write_text(json.dumps([dispute]) + "\n", encoding="utf-8")
    (OUT / "stripe-fixture-later.json").write_text(json.dumps([later]) + "\n", encoding="utf-8")
    print(f"wrote {OUT / 'stripe-fixture.json'} and {OUT / 'stripe-fixture-later.json'}")
    print("REAL Stripe API fixture objects (stripe/openapi, MIT), not customer data")


if __name__ == "__main__":
    main()
