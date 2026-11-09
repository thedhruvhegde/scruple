"""`scruple.toml`: what a CI job gates and how strictly.

Example:

    [candidate]
    file = "rules/candidate.json"   # same JSON shape as `scruple propose`

    [gate]                          # any GateConfig field (thresholds, sizes, seeds)
    n_cases = 2000

    [ci]
    fail_on_hold = true
    scorecard = "scruple-scorecard.json"

Paths are resolved relative to the directory that holds the config file.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.domain import RuleSpec
from scruple.gate.config import GateConfig
from scruple.registry import Provenance
from scruple.registry.models import ACTOR_PATTERN, RULE_ID_PATTERN


class CandidateFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(pattern=RULE_ID_PATTERN)
    spec: RuleSpec
    owner: str = Field(pattern=ACTOR_PATTERN)
    provenance: Provenance


class CandidateSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str


class CISection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Hold means "not enough evidence". Conservative default: it fails the check
    # (false blocks over false promotes, docs/DECISIONS.md D-010).
    fail_on_hold: bool = True
    scorecard: str = "scruple-scorecard.json"
    db: str = ":memory:"
    status_context: str = Field(default="scruple/gate", min_length=1, max_length=100)


class CIConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: CandidateSection
    gate: GateConfig = GateConfig()
    ci: CISection = CISection()
    root: str = "."  # directory the config was loaded from; set by load_config

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else Path(self.root) / p


def load_config(path: str | Path) -> CIConfig:
    p = Path(path)
    raw: dict[str, Any] = tomllib.loads(p.read_text())
    raw["root"] = str(p.parent)
    return CIConfig.model_validate(raw)


def load_candidate(path: str | Path) -> CandidateFile:
    return CandidateFile.model_validate(json.loads(Path(path).read_text()))
