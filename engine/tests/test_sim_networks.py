from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scruple.sim.networks import (
    DATA_DIR,
    Category,
    DayKind,
    Network,
    WindowId,
    Workflow,
    load_networks,
    parse_networks,
    rules_for,
)

SOURCES = Path(__file__).parent.parent / "docs" / "SOURCES.md"


def source_ids() -> set[str]:
    return set(re.findall(r"^\| (S\d+) \|", SOURCES.read_text(encoding="utf-8"), re.M))


def raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA_DIR / "networks.json").read_text(encoding="utf-8"))
    return data


def test_both_networks_load() -> None:
    assert set(load_networks()) == set(Network)


@pytest.mark.parametrize("network", list(Network))
@pytest.mark.parametrize("wid", list(WindowId))
def test_every_window_is_sourced_or_an_assumption(network: Network, wid: WindowId) -> None:
    w = rules_for(network).window(wid)
    assert w.window is wid
    if w.assumption:
        assert w.source is None
        assert "A-1" in w.note  # points at docs/ASSUMPTIONS.md
    else:
        assert w.source is not None
        assert set(w.source.split(",")) <= source_ids()


@pytest.mark.parametrize("network", list(Network))
def test_workflow_sources_exist(network: Network) -> None:
    src = rules_for(network).workflow_source
    if src is not None:
        assert set(src.split(",")) <= source_ids()


def test_visa_workflows_follow_s8_s9() -> None:
    visa = rules_for(Network.VISA)
    assert visa.workflow(Category.FRAUD) is Workflow.ALLOCATION
    assert visa.workflow(Category.AUTHORIZATION) is Workflow.ALLOCATION
    assert visa.workflow(Category.PROCESSING_ERROR) is Workflow.COLLABORATION
    assert visa.workflow(Category.CONSUMER_DISPUTE) is Workflow.COLLABORATION


def test_sourced_headline_windows() -> None:
    visa, mc = rules_for(Network.VISA), rules_for(Network.MASTERCARD)
    assert visa.window(WindowId.CHARGEBACK_FILING).days == 120
    assert visa.window(WindowId.CHARGEBACK_RESPONSE).days == 30
    assert mc.window(WindowId.CHARGEBACK_RESPONSE).days == 45
    assert visa.window(WindowId.INQUIRY_RESPONSE).day_kind is DayKind.BUSINESS


def test_window_needs_source_or_assumption() -> None:
    data = raw()
    data["networks"][0]["windows"][0].update(source=None, assumption=False)
    with pytest.raises(ValidationError, match="source id or assumption"):
        parse_networks(json.dumps(data))


def test_sourced_window_cannot_also_be_assumed() -> None:
    data = raw()
    data["networks"][0]["windows"][1]["assumption"] = True
    with pytest.raises(ValidationError, match="not assumptions"):
        parse_networks(json.dumps(data))


def test_missing_window_rejected() -> None:
    data = raw()
    data["networks"][0]["windows"].pop()
    with pytest.raises(ValidationError, match="one window per"):
        parse_networks(json.dumps(data))


def test_missing_workflow_rejected() -> None:
    data = raw()
    del data["networks"][1]["workflows"]["fraud"]
    with pytest.raises(ValidationError, match="every category"):
        parse_networks(json.dumps(data))


def test_missing_network_rejected() -> None:
    data = raw()
    data["networks"].pop()
    with pytest.raises(ValidationError, match="one rule set per network"):
        parse_networks(json.dumps(data))


def test_unknown_fields_rejected() -> None:
    data = raw()
    data["networks"][0]["windows"][0]["surprise"] = 1
    with pytest.raises(ValidationError):
        parse_networks(json.dumps(data))


def test_rules_are_frozen() -> None:
    with pytest.raises(ValidationError):
        rules_for(Network.VISA).window(WindowId.CHARGEBACK_FILING).days = 1  # type: ignore[misc]
