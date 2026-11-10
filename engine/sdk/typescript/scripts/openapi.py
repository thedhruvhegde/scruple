"""Write sdk/typescript/openapi.json: the HTTP API's OpenAPI schema, as the server builds it.

    uv run python sdk/typescript/scripts/openapi.py          # rewrite the snapshot
    cd sdk/typescript && npm run generate                    # then regenerate src/schema.ts

tests/test_client_typescript_schema.py fails when the snapshot and the live schema
differ, so a route change cannot silently leave the TypeScript types behind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "openapi.json"


def live_schema() -> dict[str, Any]:
    from scruple.api.app import create_app
    from scruple.service import AgentGate
    from scruple.storage import SQLiteStorage

    schema: dict[str, Any] = create_app(AgentGate(SQLiteStorage(":memory:"))).openapi()
    return schema


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    OUT.write_text(render(live_schema()))
    print(f"wrote {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
