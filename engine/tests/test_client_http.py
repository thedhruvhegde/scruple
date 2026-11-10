"""scruple.Client against the real app, in process."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

import scruple
from scruple.api.app import create_app
from scruple.auth.authn import Authenticator
from scruple.auth.keys import ApiKeyService, ScryptParams
from scruple.auth.rbac import Role, key_permissions
from scruple.client import (
    NO_RETRY,
    Client,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from scruple.client.models import DecisionOut, Outcome, Provenance, RuleStatus, RuleVersion
from scruple.clock import SteppingClock
from scruple.gate import GateConfig
from scruple.service import AgentGate
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID
from scruple.storage import SQLiteStorage
from scruple.version import package_version

AGENT = "dispute-agent-01"
RISK = "model-risk@demo.bank"
# Most tests run the server in the development-only X-Actor mode, so one client can
# act as several people; the TestClient reads as "auditor" by default.
# test_api_keys_end_to_end uses real API keys.
DEV_AUTH = Authenticator(dev_actor_header=True)
READER = {"X-Actor": "auditor"}


def _dev(gate: AgentGate) -> TestClient:
    return TestClient(create_app(gate, auth=DEV_AUTH), headers=READER)


def _borrow(test_client: TestClient) -> httpx.Client:
    # Starlette's TestClient has httpx.Client's request API (it may be built on the
    # httpx2 fork, which mypy sees as a different class).
    return cast(httpx.Client, test_client)


@pytest.fixture
def gate() -> AgentGate:
    g = AgentGate(
        SQLiteStorage(":memory:"),
        SteppingClock(),
        GateConfig(n_cases=1_500, bootstrap_resamples=300),
    )
    g.bootstrap_baseline()
    return g


@pytest.fixture
def client(gate: AgentGate) -> Client:
    return Client(http_client=_borrow(_dev(gate)), retry=NO_RETRY)


def _prov() -> Provenance:
    return Provenance(agent_id=AGENT, episode_ids=("ep-1",))


def test_exported_from_the_package_root() -> None:
    assert scruple.Client is Client


def test_meta(client: Client) -> None:
    assert client.health() is True
    assert client.version().version == package_version()
    assert client.openapi()["info"]["version"] == package_version()


def test_full_flow(client: Client, gate: AgentGate) -> None:
    rv = client.propose(GOOD_RULE_ID, GOOD_RULE, _prov(), owner="payments-ops")
    assert isinstance(rv, RuleVersion)
    assert (rv.rule_id, rv.version, rv.owner) == (GOOD_RULE_ID, 1, "payments-ops")

    decided = client.evaluate(GOOD_RULE_ID, 1, actor=AGENT)
    assert [d.outcome for d in decided] == [Outcome.PROMOTE, Outcome.CANARY_PASSED]
    assert all(isinstance(d, DecisionOut) for d in decided)

    with pytest.raises(ForbiddenError) as exc:
        client.approve(GOOD_RULE_ID, 1, actor=AGENT)  # the proposing agent cannot approve
    assert exc.value.status == 403 and exc.value.request_id

    activated = client.approve(GOOD_RULE_ID, 1, actor=RISK)
    assert activated.outcome is Outcome.ACTIVATED
    assert client.report(activated.decision_id).startswith("# Evidence report")
    full = client.decision(activated.decision_id)
    assert full.decision_id == activated.decision_id

    [current] = client.rule_versions(GOOD_RULE_ID)
    assert current.status is RuleStatus.ACTIVE

    rolled = client.rollback(GOOD_RULE_ID, "drill", actor=RISK)
    assert rolled.outcome is Outcome.ROLLED_BACK
    assert client.verify_integrity().ok is True
    assert len(client.decisions(GOOD_RULE_ID)) == 4
    # The client saw exactly what the service recorded.
    assert [d.decision_id for d in client.decisions()] == [
        r.decision.decision_id for r in gate.decisions()
    ]


def test_bad_rule_is_blocked(client: Client) -> None:
    client.propose(BAD_RULE_ID, BAD_RULE, _prov())
    [blocked] = client.evaluate(BAD_RULE_ID, 1, actor=AGENT)
    assert blocked.outcome is Outcome.BLOCK
    assert client.decision(blocked.decision_id).reasons


def test_errors_are_typed(client: Client) -> None:
    with pytest.raises(NotFoundError) as nf:
        client.rule_versions("no-such-rule")
    assert "no-such-rule" in str(nf.value.detail)
    with pytest.raises(NotFoundError):
        client.decision("nope")
    with pytest.raises(NotFoundError):
        client.report("nope")

    client.propose(GOOD_RULE_ID, GOOD_RULE, _prov())
    with pytest.raises(ConflictError):
        client.propose(GOOD_RULE_ID, GOOD_RULE, _prov())  # same content twice

    with pytest.raises(ValidationError) as ve:
        client.request("GET", "/decisions", params={"rule": "typo"})
    assert ve.value.status == 422 and isinstance(ve.value.detail, list)


def test_state_changing_calls_need_an_actor(client: Client) -> None:
    with pytest.raises(ValueError, match="actor"):
        client.approve(GOOD_RULE_ID, 1)
    with pytest.raises(ValueError, match="actor"):
        client.rollback(GOOD_RULE_ID, "because")


def test_default_actor(gate: AgentGate) -> None:
    c = Client(http_client=_borrow(_dev(gate)), actor=AGENT, retry=NO_RETRY)
    c.propose(GOOD_RULE_ID, GOOD_RULE, _prov())
    assert len(c.evaluate(GOOD_RULE_ID, 1)) == 2


@pytest.mark.parametrize("bad", ["", "a/b", "..", "."])
def test_path_segments_cannot_escape(client: Client, bad: str) -> None:
    with pytest.raises(ValueError, match="path segment"):
        client.rule_versions(bad)


def test_segments_are_percent_encoded(client: Client) -> None:
    with pytest.raises(NotFoundError) as exc:
        client.decision("a?b#c d")
    assert exc.value.path == "/decisions/a%3Fb%23c%20d"


def test_context_manager_does_not_close_a_borrowed_client(gate: AgentGate) -> None:
    http = _dev(gate)
    with Client(http_client=_borrow(http)) as c:
        assert c.health()
    assert http.get("/health").status_code == 200


def test_api_keys_end_to_end(gate: AgentGate) -> None:
    """Against a server with real authentication: the key is the identity."""
    keys = ApiKeyService(gate.storage, params=ScryptParams(n=1024), permissions_for=key_permissions)

    def key(subject: str, role: Role) -> str:
        return keys.create(subject, (), "local-admin", roles=[role])[1].get_secret_value()

    http = _borrow(TestClient(create_app(gate, auth=Authenticator(keys))))
    agent = Client(http_client=http, api_key=key(AGENT, Role.PROPOSER), retry=NO_RETRY)
    risk = Client(http_client=http, api_key=key(RISK, Role.APPROVER), retry=NO_RETRY)
    auditor = Client(http_client=http, api_key=key("auditor", Role.AUDITOR), retry=NO_RETRY)
    assert agent.whoami()["subject"] == AGENT and agent.whoami()["method"] == "api_key"
    agent.propose(GOOD_RULE_ID, GOOD_RULE, _prov())
    assert [d.outcome for d in agent.evaluate(GOOD_RULE_ID, 1)] == [
        Outcome.PROMOTE,
        Outcome.CANARY_PASSED,
    ]
    with pytest.raises(ForbiddenError):
        auditor.approve(GOOD_RULE_ID, 1)  # no `approve` permission
    with pytest.raises(ForbiddenError):
        agent.approve(GOOD_RULE_ID, 1)
    assert risk.approve(GOOD_RULE_ID, 1).outcome is Outcome.ACTIVATED
    assert gate.decisions(GOOD_RULE_ID)[-1].decision.actor == RISK
    assert auditor.verify_integrity().ok
    with pytest.raises(UnauthorizedError):
        Client(http_client=http, api_key="scr_0123456789abcdef_" + "a" * 43).rules()
    with pytest.raises(UnauthorizedError):
        Client(http_client=http, retry=NO_RETRY).rules()  # no credential at all


def test_dev_mode_actor_is_sent_on_reads_too(gate: AgentGate) -> None:
    """A development-only server authenticates reads by X-Actor as well."""
    http = _borrow(TestClient(create_app(gate, auth=DEV_AUTH)))  # no default header
    assert Client(http_client=http, actor="auditor", retry=NO_RETRY).rules()
    with pytest.raises(UnauthorizedError):
        Client(http_client=http, retry=NO_RETRY).rules()
