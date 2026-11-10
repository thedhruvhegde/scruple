"""Optional paging on list routes, and the client iterators over it."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scruple.api.app import MAX_PAGE, TOTAL_COUNT, create_app
from scruple.auth.authn import Authenticator
from scruple.client import NO_RETRY, Client, ValidationError
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.registry import Provenance
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage

READER = {"X-Actor": "auditor"}


def _borrow(test_client: TestClient) -> httpx.Client:
    # Starlette's TestClient has httpx.Client's request API (it may be built on the
    # httpx2 fork, which mypy sees as a different class).
    return cast(httpx.Client, test_client)


@pytest.fixture(scope="module")
def http() -> TestClient:
    g = AgentGate(
        SQLiteStorage(":memory:"),
        SteppingClock(),
        GateConfig(n_cases=1_500, bootstrap_resamples=300),
    )
    g.bootstrap_baseline()
    prov = Provenance(agent_id="agent-1")
    for rule_id, spec in ((GOOD_RULE_ID, GOOD_RULE), (BAD_RULE_ID, BAD_RULE)):
        g.propose(rule_id, spec, "ops", prov)
        g.evaluate(rule_id, 1, "agent-1")
    # Development-only X-Actor trust; tests/test_client_http.py covers real keys.
    return TestClient(create_app(g, auth=Authenticator(dev_actor_header=True)), headers=READER)


@pytest.fixture(scope="module")
def client(http: TestClient) -> Client:
    return Client(http_client=_borrow(http), retry=NO_RETRY)


def test_without_limit_everything_comes_back(http: TestClient) -> None:
    r = http.get("/rules")
    assert r.status_code == 200
    assert int(r.headers[TOTAL_COUNT]) == len(r.json()) > 1


def test_limit_and_offset(http: TestClient) -> None:
    every = http.get("/rules").json()
    assert http.get("/rules", params={"limit": 1}).json() == every[:1]
    assert http.get("/rules", params={"limit": 2, "offset": 1}).json() == every[1:3]
    assert http.get("/rules", params={"offset": len(every)}).json() == []
    r = http.get("/decisions", params={"limit": 1})
    assert r.status_code == 200 and r.headers[TOTAL_COUNT] == str(
        len(http.get("/decisions").json())
    )


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": MAX_PAGE + 1}, {"offset": -1}, {"limit": "x"}]
)
def test_bad_paging_is_422(http: TestClient, params: dict[str, int | str]) -> None:
    assert http.get("/rules", params=params).status_code == 422


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(page_size=st.integers(1, 7))
def test_iter_rules_matches_one_full_list(client: Client, page_size: int) -> None:
    assert [r.ref for r in client.iter_rules(page_size=page_size)] == [
        r.ref for r in client.rules()
    ]


def test_list_with_limit(client: Client) -> None:
    every = client.rules()
    assert client.rules(limit=1, offset=1) == every[1:2]


def test_iter_decisions_filters_by_rule(client: Client) -> None:
    every = client.decisions()
    assert len({d.rule_id for d in every}) == 2
    assert list(client.iter_decisions(page_size=1)) == every
    good = client.decisions(GOOD_RULE_ID)
    assert 0 < len(good) < len(every)
    assert list(client.iter_decisions(GOOD_RULE_ID, page_size=1)) == good


@pytest.mark.parametrize("size", [0, MAX_PAGE + 1])
def test_page_size_is_checked(client: Client, size: int) -> None:
    with pytest.raises(ValueError, match="page_size"):
        next(client.iter_rules(page_size=size))


def test_paging_without_total_header_stops_on_a_short_page() -> None:
    pages = [[1, 2], [3]]
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[] if len(seen) > len(pages) else pages[len(seen) - 1])

    c = Client(
        http_client=httpx.Client(base_url="http://t", transport=httpx.MockTransport(handler))
    )
    from pydantic import TypeAdapter

    got = list(c._pages("/x", TypeAdapter(list[int]), 2))
    assert got == [1, 2, 3]
    assert seen == [{"limit": "2", "offset": "0"}, {"limit": "2", "offset": "2"}]


def test_server_side_validation_error_surfaces(client: Client) -> None:
    with pytest.raises(ValidationError):
        client.rules(limit=MAX_PAGE + 1)
