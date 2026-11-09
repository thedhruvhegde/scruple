"""docs/RULES.md (039) is rendered from the code and every row names a known basis."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "RULES.md"


def _rows() -> list[list[str]]:
    out = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if (
            line.startswith("| ")
            and not line.startswith("| rule |")
            and not line.startswith("| code |")
        ):
            out.append([c.strip() for c in line.strip("|").split(" | ")])
    return out


def test_rules_doc_matches_the_code() -> None:
    rendered = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "render_rules.py")],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    ).stdout
    assert rendered == DOC.read_text(encoding="utf-8"), (
        "docs/RULES.md is stale: uv run python scripts/render_rules.py > docs/RULES.md"
    )


def test_every_row_cites_a_known_source_or_assumption() -> None:
    sources = set(re.findall(r"(?m)^\| (S\d+) \|", (ROOT / "docs/SOURCES.md").read_text()))
    assumptions = set(re.findall(r"\*\*(A-\d+)\*\*", (ROOT / "docs/ASSUMPTIONS.md").read_text()))
    rows = _rows()
    assert len(rows) > 100
    for row in rows:
        cited = re.findall(
            r"\b(S\d+|A-\d+)\b",
            " ".join(row[3:]) if len(row) == 4 else " ".join((row[3], row[5], row[7])),
        )
        assert cited, row
        for ref in cited:
            assert ref in (sources if ref.startswith("S") else assumptions), (ref, row)


def test_every_reason_code_is_documented() -> None:
    from scruple.sim.reason_codes import load_catalogue

    codes = {r[0] for r in _rows()}
    for c in load_catalogue().codes:
        assert f"{c.network.value} {c.code}" in codes
