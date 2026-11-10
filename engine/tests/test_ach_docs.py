"""docs/WORKFLOWS.md: the ACH rules table is rendered from the code and cannot drift."""

from __future__ import annotations

import re
from pathlib import Path

from scruple.sim.ach.rules import CODES, rules_table

DOC = Path(__file__).resolve().parents[1] / "docs" / "WORKFLOWS.md"


def _block(name: str) -> str:
    m = re.search(rf"<!-- {name}:start -->\n(.*?)\n<!-- {name}:end -->", DOC.read_text(), re.S)
    assert m is not None, f"missing {name} block"
    return m.group(1)


def test_ach_rules_table_matches_the_code() -> None:
    assert _block("ach-rules") == rules_table()


def test_every_source_id_cited_in_the_rules_is_listed() -> None:
    text = DOC.read_text()
    ids: set[str] = set()
    for c in CODES.values():
        for part in c.source.split(", "):
            if "-" in part and part.startswith("W"):
                lo, hi = part[1:].split("-W")
                ids |= {f"W{i}" for i in range(int(lo), int(hi) + 1)}
            else:
                ids.add(part)
    for i in sorted(ids):
        assert f"| {i} |" in text or f"- {i}:" in text, i
