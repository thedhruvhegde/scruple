"""Rewrite every golden episode: `uv run python -m tests.golden.regen [name ...]`.

Only run this after an intended behaviour change, then review `git diff tests/golden`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.golden.episodes import TRACES, dumps

HERE = Path(__file__).parent


def path_for(name: str) -> Path:
    return HERE / f"{name}.json"


def main(names: list[str]) -> int:
    unknown = sorted(set(names) - set(TRACES))
    if unknown:
        print(f"unknown golden episode(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    for name in names or sorted(TRACES):
        text = dumps(TRACES[name]())
        p = path_for(name)
        old = p.read_text(encoding="utf-8") if p.exists() else None
        p.write_text(text, encoding="utf-8")
        print(f"{'unchanged' if old == text else 'wrote'} {p.name} ({len(text):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
