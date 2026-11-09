"""Reason code catalogue with the evidence each code needs.

Loaded from `scruple/sim/data/reason_codes.json`, which reuses the public CC BY 4.0
dataset in docs/SOURCES.md S1 (codes, titles, response windows, key-evidence text).
The required/supporting `EvidenceKind` sets, the Mastercard category mapping and the
legacy family are scruple's own mapping (docs/ASSUMPTIONS.md A-15, A-16).

`required_evidence`: without every one of these a representment is treated as weak.
`supporting_evidence`: strengthens a representment but is not needed.
"""

from __future__ import annotations

import json
from functools import cache

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scruple.domain import ReasonCode
from scruple.sim.evidence import EvidenceKind
from scruple.sim.networks import DATA_DIR, Category, Network

_VISA_PREFIX = {
    "10": Category.FRAUD,
    "11": Category.AUTHORIZATION,
    "12": Category.PROCESSING_ERROR,
    "13": Category.CONSUMER_DISPUTE,
}


class ReasonCodeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    network: Network
    code: str
    title: str
    category: Category
    category_source: str | None  # None: our mapping (A-16)
    legacy_family: ReasonCode | None  # bridge to the original 3-family sim
    card_present: bool
    response_days: int = Field(gt=0)
    response_days_source: str
    required_evidence: tuple[EvidenceKind, ...] = Field(min_length=1)
    supporting_evidence: tuple[EvidenceKind, ...]
    key_evidence_text: str  # verbatim from S1, CC BY 4.0

    @model_validator(mode="after")
    def _consistent(self) -> ReasonCodeSpec:
        if set(self.required_evidence) & set(self.supporting_evidence):
            raise ValueError(f"{self.key}: evidence both required and supporting")
        if len(set(self.required_evidence)) != len(self.required_evidence):
            raise ValueError(f"{self.key}: duplicate required evidence")
        if self.network is Network.VISA:
            expected = _VISA_PREFIX.get(self.code.split(".")[0])
            if expected is not self.category:
                raise ValueError(f"{self.key}: Visa code prefix implies {expected}")
        elif not (self.code.isdigit() and len(self.code) == 4):
            raise ValueError(f"{self.key}: Mastercard codes are four digits")
        return self

    @property
    def key(self) -> str:
        return f"{self.network.value}:{self.code}"

    @property
    def all_evidence(self) -> tuple[EvidenceKind, ...]:
        return (*self.required_evidence, *self.supporting_evidence)


class DatasetInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    url: str
    commit: str
    licence: str
    attribution: str
    source_id: str


class ReasonCodeCatalogue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: DatasetInfo
    codes: tuple[ReasonCodeSpec, ...]

    @model_validator(mode="after")
    def _unique(self) -> ReasonCodeCatalogue:
        keys = [c.key for c in self.codes]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate reason code")
        return self

    def get(self, network: Network, code: str) -> ReasonCodeSpec:
        for c in self.codes:
            if c.network is network and c.code == code:
                return c
        raise KeyError(f"{network.value}:{code}")

    def for_network(self, network: Network) -> tuple[ReasonCodeSpec, ...]:
        return tuple(c for c in self.codes if c.network is network)


def parse_catalogue(raw: str) -> ReasonCodeCatalogue:
    return ReasonCodeCatalogue.model_validate(json.loads(raw))


@cache
def load_catalogue() -> ReasonCodeCatalogue:
    return parse_catalogue((DATA_DIR / "reason_codes.json").read_text(encoding="utf-8"))


@cache
def reason_code(network: Network, code: str) -> ReasonCodeSpec:
    """Cached lookup (038): every case validates its code, several times per case."""
    return load_catalogue().get(network, code)
