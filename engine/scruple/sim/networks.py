"""Card network rule sets as data (Visa, Mastercard).

The rules live in `scruple/sim/data/networks.json` and are validated here. Each time
window either cites a public source id from docs/SOURCES.md or is flagged as an
assumption (docs/ASSUMPTIONS.md A-13, A-14). Nothing here is network or legal advice;
the values are what public sources report, simplified for simulation.

Workflows: Visa routes fraud and authorization disputes through "allocation" and
processing errors and consumer disputes through "collaboration" (S8, S9). Mastercard
is modelled with a collaboration-shaped flow for every category (A-14).
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DATA_DIR = Path(__file__).parent / "data"


class Network(StrEnum):
    VISA = "visa"
    MASTERCARD = "mastercard"


class Category(StrEnum):
    """Visa's four dispute categories (S1 numbering: 10, 11, 12, 13)."""

    FRAUD = "fraud"
    AUTHORIZATION = "authorization"
    PROCESSING_ERROR = "processing_error"
    CONSUMER_DISPUTE = "consumer_dispute"


class Workflow(StrEnum):
    ALLOCATION = "allocation"
    COLLABORATION = "collaboration"


class DayKind(StrEnum):
    CALENDAR = "calendar"
    BUSINESS = "business"


class WindowId(StrEnum):
    INQUIRY_RESPONSE = "inquiry_response"
    CHARGEBACK_FILING = "chargeback_filing"
    CHARGEBACK_RESPONSE = "chargeback_response"
    PRE_ARBITRATION_FILING = "pre_arbitration_filing"
    PRE_ARBITRATION_RESPONSE = "pre_arbitration_response"
    ARBITRATION_FILING = "arbitration_filing"


class TimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: WindowId
    days: int = Field(gt=0, le=540)
    day_kind: DayKind
    actor: Literal["issuer", "merchant", "network"]
    source: str | None
    assumption: bool
    note: str = ""

    @model_validator(mode="after")
    def _sourced_or_assumed(self) -> TimeWindow:
        if self.source is None and not self.assumption:
            raise ValueError(f"{self.window}: needs a source id or assumption=true")
        if self.source is not None and self.assumption:
            raise ValueError(f"{self.window}: sourced values are not assumptions")
        return self


class NetworkRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    network: Network
    display_name: str
    workflows: dict[Category, Workflow]
    workflow_source: str | None  # None: the workflow mapping is an assumption
    windows: tuple[TimeWindow, ...]

    @model_validator(mode="after")
    def _complete(self) -> NetworkRules:
        if set(self.workflows) != set(Category):
            raise ValueError(f"{self.network}: workflows must cover every category")
        ids = [w.window for w in self.windows]
        if sorted(ids) != sorted(WindowId) or len(set(ids)) != len(ids):
            raise ValueError(f"{self.network}: needs exactly one window per WindowId")
        return self

    def window(self, wid: WindowId) -> TimeWindow:
        for w in self.windows:
            if w.window is wid:
                return w
        raise KeyError(wid)  # pragma: no cover - excluded by the validator

    def workflow(self, category: Category) -> Workflow:
        return self.workflows[category]


class NetworkCatalogue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    note: str
    networks: tuple[NetworkRules, ...]

    @model_validator(mode="after")
    def _one_per_network(self) -> NetworkCatalogue:
        if sorted(n.network for n in self.networks) != sorted(Network):
            raise ValueError("catalogue needs exactly one rule set per network")
        return self


def parse_networks(raw: str) -> NetworkCatalogue:
    return NetworkCatalogue.model_validate(json.loads(raw))


@cache
def load_networks() -> dict[Network, NetworkRules]:
    cat = parse_networks((DATA_DIR / "networks.json").read_text(encoding="utf-8"))
    return {n.network: n for n in cat.networks}


def rules_for(network: Network) -> NetworkRules:
    return load_networks()[network]
