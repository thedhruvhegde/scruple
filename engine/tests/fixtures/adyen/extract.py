"""Rebuild the vendored Adyen subset (see README.md for sources and commits).

Usage: python tests/fixtures/adyen/extract.py <adyen-openapi checkout> <adyen-python-api-library
checkout> tests/fixtures/adyen
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
from pathlib import Path
from typing import Any

SCHEMAS = (
    "Amount",
    "ChargebackNotificationRequest",
    "DisputesNotificationRequestItemWrapper",
    "DisputesNotificationRequestItem",
    "DisputesNotificationAdditionalData",
)
MOCKS = (
    "backslash_notification.json",
    "colon_notification.json",
    "forwardslash_notification.json",
    "mixed_notification.json",
)


def _dump(obj: object, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _payload_vector(test_src: str) -> str:
    """The payload string literal inside UtilTest.test_is_valid_hmac_payload, exactly."""
    for node in ast.walk(ast.parse(test_src)):
        if isinstance(node, ast.FunctionDef) and node.name == "test_is_valid_hmac_payload":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "payload"
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    return stmt.value.value
    raise SystemExit("payload vector not found")


def main(openapi_dir: str, lib_dir: str, out: str) -> None:
    out_dir = Path(out)
    spec: dict[str, Any] = json.loads(
        (Path(openapi_dir) / "json" / "Webhooks-v1.json").read_text(encoding="utf-8")
    )
    hooks = {
        name: hook
        for name, hook in spec["webhooks"].items()
        if "Dispute" in hook["post"].get("tags", [])
    }
    examples = {}
    for hook in hooks.values():
        content = hook["post"]["requestBody"]["content"]["application/json"]
        for ex in content.get("examples", {}).values():
            key = ex["$ref"].rsplit("/", 1)[-1]
            examples[key] = spec["components"]["examples"][key]
    subset = {
        "info": spec["info"],
        "x-scruple-dispute-webhooks": {
            name.lstrip("/"): {
                "summary": h["post"]["summary"],
                "schema": h["post"]["requestBody"]["content"]["application/json"]["schema"],
            }
            for name, h in sorted(hooks.items())
        },
        "components": {
            "schemas": {n: spec["components"]["schemas"][n] for n in SCHEMAS},
            "examples": examples,
        },
    }
    _dump(subset, out_dir / "webhooks-v1.subset.json")
    lib = Path(lib_dir)
    for m in MOCKS:
        shutil.copyfile(lib / "test" / "mocks" / "util" / m, out_dir / m)
    vector = _payload_vector((lib / "test" / "UtilTest.py").read_text(encoding="utf-8"))
    (out_dir / "hmac_payload_vector.txt").write_bytes(vector.encode("utf-8"))


if __name__ == "__main__":
    main(*sys.argv[1:4])
