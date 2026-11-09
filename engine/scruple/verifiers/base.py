from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    kind: str
    detail: str


class VerifierReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    anchored_to: str
    findings: tuple[Finding, ...]
    metrics: dict[str, float]

    @property
    def keys(self) -> set[tuple[str, str]]:
        return {(f.case_id, f.kind) for f in self.findings}


def new_findings(baseline: VerifierReport, candidate: VerifierReport) -> list[Finding]:
    """Findings present under the candidate policy but not under the baseline."""
    old = baseline.keys
    return [f for f in candidate.findings if (f.case_id, f.kind) not in old]
