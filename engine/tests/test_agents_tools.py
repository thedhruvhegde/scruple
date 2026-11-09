from __future__ import annotations

import contextlib
from typing import Any

import jsonschema
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.agents import Decision, Episode, EpisodeClosedError, EpisodeError, Toolbox, run_agent
from scruple.agents.env import SimCase, evidence_for
from scruple.agents.errors import ToolArgumentError
from scruple.agents.models import Observation
from scruple.agents.tools import (
    TOOL_BY_NAME,
    TOOL_NAMES,
    TOOLS,
    Scope,
    anthropic_tools,
    openai_tools,
)
from scruple.sim.models import Scenario

READS = [t.name for t in TOOLS if t.scope is Scope.READ]
ACTIONS = [t.name for t in TOOLS if t.scope is Scope.ACTION]

VALID_ARGS: dict[str, dict[str, Any]] = {
    "get_case": {},
    "list_evidence": {},
    "get_evidence": {"evidence_id": "ev-01"},
    "get_ledger_view": {},
    "request_info": {"question": "was the parcel signed for?"},
    "accept_liability": {"reason": "valid claim"},
    "refund": {},
    "represent": {"evidence_ids": ["ev-01"], "narrative": "authenticated"},
    "escalate": {"reason": "unsure"},
    "submit_decision": {"explanation": "done", "action": "refund"},
}

INVALID_ARGS: list[tuple[str, Any]] = [
    ("get_case", {"x": 1}),
    ("get_evidence", {}),
    ("get_evidence", {"evidence_id": ""}),
    ("get_evidence", {"evidence_id": 7}),
    ("represent", {"evidence_ids": []}),
    ("represent", {"evidence_ids": "ev-01"}),
    ("represent", {"evidence_ids": ["ev-01"] * 11}),
    ("request_info", {"question": ""}),
    ("accept_liability", {"reason": "x" * 1001}),
    ("submit_decision", {}),
    ("submit_decision", {"explanation": "x", "action": "steal"}),
    ("submit_decision", {"explanation": "x", "regret": 0}),
    ("submit_decision", {"explanation": "x", "claimed_cost_usd_minor": -1}),
]


def ledger_digest(ep: Episode) -> str:
    return ep._case.run().digest()


# --- schemas ---------------------------------------------------------------------


def test_tool_names_are_unique_and_complete() -> None:
    assert len(set(TOOL_NAMES)) == len(TOOLS) == 10
    assert set(VALID_ARGS) == set(TOOL_NAMES)
    assert Episode(1, "fraud").observe().tools == TOOL_NAMES


@pytest.mark.parametrize("spec", TOOLS, ids=lambda t: t.name)
def test_schema_is_valid_json_schema(spec: Any) -> None:
    schema = spec.json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "$defs" not in str(schema)
    assert "$ref" not in str(schema)


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_schema_accepts_valid_args(name: str) -> None:
    jsonschema.validate(VALID_ARGS[name], TOOL_BY_NAME[name].json_schema())
    TOOL_BY_NAME[name].parse(VALID_ARGS[name])


@pytest.mark.parametrize(("name", "args"), INVALID_ARGS)
def test_schema_and_pydantic_agree_on_invalid_args(name: str, args: Any) -> None:
    spec = TOOL_BY_NAME[name]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(args, spec.json_schema())
    with pytest.raises(ToolArgumentError, match=f"invalid arguments for {name}"):
        spec.parse(args)


def test_provider_tool_shapes() -> None:
    a = anthropic_tools()
    o = openai_tools()
    assert [t["name"] for t in a] == list(TOOL_NAMES)
    assert all(set(t) == {"name", "description", "input_schema"} for t in a)
    assert [t["function"]["name"] for t in o] == list(TOOL_NAMES)
    assert all(t["type"] == "function" and t["function"]["parameters"] for t in o)


def test_submit_schema_lists_actions() -> None:
    props = TOOL_BY_NAME["submit_decision"].json_schema()["properties"]
    assert "explanation" in props
    flat = str(props["action"])
    for kind in ("accept_liability", "refund", "represent", "escalate"):
        assert kind in flat


# --- permission scopes -------------------------------------------------------------


@pytest.mark.parametrize("name", READS)
def test_reads_never_move_money(name: str) -> None:
    ep = Episode(2, "duplicate_dispute")
    before = ledger_digest(ep)
    for _ in range(3):
        ep.call(name, VALID_ARGS[name])
    assert ledger_digest(ep) == before
    assert ep.action is None


@pytest.mark.parametrize("name", READS)
def test_reads_allowed_after_action(name: str) -> None:
    ep = Episode(2, "fraud")
    ep.call("escalate", {})
    ep.call(name, VALID_ARGS[name])


@pytest.mark.parametrize("name", ACTIONS)
def test_actions_move_money_once(name: str) -> None:
    ep = Episode(2, "not_received")
    before = ledger_digest(ep)
    out = ep.call(name, VALID_ARGS[name])
    assert out["ok"] is True
    assert ledger_digest(ep) != before
    for other in ACTIONS:
        with pytest.raises(EpisodeError, match="already taken"):
            ep.call(other, VALID_ARGS[other])


def test_request_info_only_before_action() -> None:
    ep = Episode(2, "fraud")
    assert ep.call("request_info", VALID_ARGS["request_info"])["answer"]
    ep.call("refund", {})
    with pytest.raises(EpisodeError, match="before the action"):
        ep.call("request_info", VALID_ARGS["request_info"])
    assert ep.info_requests == 1


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_rejected_after_submit(name: str) -> None:
    ep = Episode(2, "fraud")
    ep.call("submit_decision", {"explanation": "x", "action": "escalate"})
    errors = ep.tool_errors
    digest = ledger_digest(ep)
    with pytest.raises(EpisodeClosedError):
        ep.call(name, VALID_ARGS[name])
    assert ep.tool_errors == errors
    assert ledger_digest(ep) == digest


def test_ledger_view_is_a_copy() -> None:
    ep = Episode(2, "partial_refund")
    view = ep.call("get_ledger_view")
    view["entries"].clear()
    view["balances"]["merchant USD"] = 10**9
    again = ep.call("get_ledger_view")
    assert again["entries"]
    assert again["balances"]["merchant USD"] != 10**9
    model = ep._case.ledger_view()
    with pytest.raises(ValueError, match="frozen"):
        model.read_only = False  # type: ignore[misc]


def test_ledger_view_shows_money_moving() -> None:
    ep = Episode(2, "partial_refund")
    kinds = [e["kind"] for e in ep.call("get_ledger_view")["entries"]]
    assert kinds == ["capture", "refund"]
    ep.call("accept_liability")
    after = ep.call("get_ledger_view")
    assert [e["kind"] for e in after["entries"]][-1] == "chargeback_debit"
    assert sum(after["balances"].values()) == 0


def test_ledger_view_is_anonymised() -> None:
    ep = Episode(2, "duplicate_dispute")
    view = ep.call("get_ledger_view")
    accounts = {p["account"] for e in view["entries"] for p in e["postings"]}
    assert accounts <= {"merchant", "cardholder", "network_fees"}
    case = ep._case.cases[-1]
    for secret in (case.txn_id, case.case_id, case.merchant_id):
        assert secret not in str(view)
    assert any(e["earlier_dispute"] for e in view["entries"]) or len(view["entries"]) == 1


# --- evidence ----------------------------------------------------------------------


def test_evidence_matches_case() -> None:
    ep = Episode(2, "partial_refund")
    kinds = [e["kind"] for e in ep.call("list_evidence")["evidence"]]
    assert "refund_record" in kinds
    assert kinds[-1] == "merchant_statement"
    doc = ep.call("get_evidence", {"evidence_id": "ev-01"})
    assert doc["kind"] == "authorization"


@pytest.mark.parametrize("case_type", [s.value for s in Scenario])
def test_evidence_ignores_hidden_fields(case_type: str) -> None:
    case = Episode(4, case_type)._case.cases[-1]
    twin = case.model_copy(
        update={"claim_valid": not case.claim_valid, "case_id": "z", "txn_id": "y"}
    )
    assert SimCase([], case).evidence() == SimCase([], twin).evidence()
    assert evidence_for(SimCase([], case).observable()) == SimCase([], case).evidence()


def test_unknown_evidence_is_refused_and_flagged() -> None:
    ep = Episode(2, "fraud")
    with pytest.raises(EpisodeError, match="unknown evidence"):
        ep.call("get_evidence", {"evidence_id": "ev-99"})
    with pytest.raises(EpisodeError, match="unknown evidence"):
        ep.call("represent", {"evidence_ids": ["ev-01", "ev-77"]})
    assert ep.action is None
    with pytest.raises(EpisodeError, match="unique"):
        ep.call("represent", {"evidence_ids": ["ev-01", "ev-01"]})
    ep.call("represent", {"evidence_ids": ["ev-01"]})
    score = ep.call("submit_decision", {"explanation": "x"})
    assert "unknown_evidence_probe" in score["flags"]
    assert score["tool_errors"] == 3


# --- malformed calls -----------------------------------------------------------------


@pytest.mark.parametrize("args", [[1, 2], "ev-01", 3, {"nope": True}])
def test_malformed_args_are_tool_errors(args: Any) -> None:
    ep = Episode(2, "fraud")
    with pytest.raises(ToolArgumentError):
        ep.call("get_evidence", args)
    assert ep.tool_errors == 1
    assert not ep.log[-1].ok


def test_unknown_tool() -> None:
    ep = Episode(2, "fraud")
    with pytest.raises(EpisodeError, match="unknown tool"):
        ep.call("transfer_funds", {"amount": 10**6})
    assert ep.tool_errors == 1


def test_submit_without_action_is_refused_then_allowed() -> None:
    ep = Episode(2, "fraud")
    with pytest.raises(EpisodeError, match="take an action"):
        ep.call("submit_decision", {"explanation": "x"})
    assert not ep.closed
    out = ep.call("submit_decision", {"explanation": "x", "action": "represent"})
    assert out["action"] == "represent"


# --- determinism and replay ------------------------------------------------------------


SCRIPT: list[tuple[str, Any]] = [
    ("get_case", {}),
    ("list_evidence", {}),
    ("get_evidence", {"evidence_id": "ev-99"}),
    ("get_ledger_view", {}),
    ("request_info", {"question": "anything else?"}),
    ("represent", {"evidence_ids": ["ev-01"]}),
    ("get_ledger_view", {}),
    ("submit_decision", {"explanation": "contested", "claimed_cost_usd_minor": 0}),
]


def play(seed: int, case_type: str) -> list[Any]:
    ep = Episode(seed, case_type)
    out: list[Any] = []
    for tool, args in SCRIPT:
        try:
            out.append(ep.call(tool, args))
        except EpisodeError as exc:
            out.append(str(exc))
    return out


@pytest.mark.parametrize("case_type", [s.value for s in Scenario])
def test_episode_is_deterministic(case_type: str) -> None:
    assert play(17, case_type) == play(17, case_type)


def test_replay_rebuilds_the_episode() -> None:
    ep = Episode(17, "fraud")
    for tool, args in SCRIPT:
        with contextlib.suppress(EpisodeError):
            ep.call(tool, args)
    again = Episode.replay(17, "fraud", [(r.tool, r.args) for r in ep.log])
    assert again.score == ep.score
    assert again.log == ep.log


def test_toolbox_call() -> None:
    def agent(obs: Observation, tools: Toolbox) -> Decision:
        ids = [e["evidence_id"] for e in tools.call("list_evidence")["evidence"]]
        tools.call("represent", evidence_ids=ids)
        return Decision(explanation="contest with everything")

    score = run_agent(agent, Episode(3, "fraud"))
    assert score.action is not None
    assert score.action.value == "represent"
    assert score.tool_calls == 2


_tool_calls = st.lists(
    st.tuples(
        st.sampled_from([*TOOL_NAMES, "bogus"]),
        st.one_of(
            st.sampled_from(list(VALID_ARGS.values())),
            st.dictionaries(st.text(max_size=5), st.integers(), max_size=2),
            st.none(),
        ),
    ),
    max_size=12,
)


@settings(max_examples=60, deadline=None)
@given(seed=st.integers(0, 500), calls=_tool_calls)
def test_random_call_sequences_are_safe(seed: int, calls: list[tuple[str, Any]]) -> None:
    ep = Episode(seed, None)
    actions = 0
    for tool, args in calls:
        before = ledger_digest(ep)
        try:
            ep.call(tool, args)
        except EpisodeError:
            assert ledger_digest(ep) == before or ep.closed
            continue
        if tool in ACTIONS:
            actions += 1
    assert actions <= 1
    assert ep.tool_errors == sum(1 for r in ep.log if not r.ok and "closed" not in str(r.error))
