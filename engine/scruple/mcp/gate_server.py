"""MCP server exposing gate actions through `AgentGate` (stdio by default).

An agent (or a developer in Claude Code) can submit a candidate rule version, run
the evaluation (shadow replay, then canary), read its status and decisions, read
evidence reports and verify the audit trail.

Deliberately NOT exposed: approve and rollback. Activation needs a named human
approver who is neither the rule's owner nor its agent (segregation of duties), so
it stays on the CLI and HTTP API where the approver is identified.

Run it with `scruple mcp gate --db agent-gate.db`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from scruple.domain import RuleSpec
from scruple.gate.models import DecisionRecord
from scruple.registry import InvalidTransitionError, Provenance
from scruple.service import AgentGate

RuleId = Annotated[str, Field(description="Rule id, lowercase with dashes, e.g. inr-contest.")]
Version = Annotated[int, Field(description="Rule version number.", ge=1)]


def _decision(rec: DecisionRecord) -> dict[str, Any]:
    d = rec.decision
    return {
        "decision_id": d.decision_id,
        "stage": d.stage.value,
        "outcome": d.outcome.value,
        "summary": d.summary,
        "reasons": list(d.reasons),
    }


def build_gate_server(gate: AgentGate) -> MCPServer:
    lock = threading.Lock()  # the MCP SDK runs sync tools in worker threads
    server = MCPServer(
        "scruple-gate",
        instructions="Change control for learned dispute rules. Submit a candidate, "
        "evaluate it, then read the status and evidence reports. Approval is done by a "
        "human outside this server. All data is synthetic.",
        version="0.1.0",
    )

    def guarded[T](fn: Callable[[], T]) -> T:
        with lock:
            try:
                return fn()
            except (ValidationError, ValueError, KeyError, InvalidTransitionError) as exc:
                raise ToolError(str(exc)) from exc
            except PermissionError as exc:
                raise ToolError(f"not allowed: {exc}") from exc

    @server.tool(description="List every rule version with its status.")
    def list_rules() -> dict[str, Any]:
        def go() -> dict[str, Any]:
            return {
                "rules": [
                    {
                        "rule_id": r.rule_id,
                        "version": r.version,
                        "status": r.status.value,
                        "logic": r.spec.describe(),
                        "content_hash": r.content_hash,
                    }
                    for r in gate.registry.all_versions()
                ]
            }

        return guarded(go)

    @server.tool(
        description="Submit a candidate rule version. `spec` is a RuleSpec: description, "
        "conditions [{field, op, value}], action (accept|contest|escalate), priority. "
        "Claimed metrics are recorded and ignored."
    )
    def submit_rule(
        rule_id: RuleId,
        spec: Annotated[dict[str, Any], Field(description="The RuleSpec as JSON.")],
        owner: Annotated[str, Field(description="Team that owns the rule.")],
        agent_id: Annotated[str, Field(description="Agent that learned the rule.")],
        episode_ids: Annotated[
            list[str] | None, Field(description="Episodes the rule was learned from.")
        ] = None,
        notes: Annotated[str, Field(description="Free-text provenance notes.")] = "",
        claimed_metrics: Annotated[
            dict[str, float] | None, Field(description="Self-reported; never trusted.")
        ] = None,
    ) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            rv = gate.propose(
                rule_id,
                RuleSpec.model_validate(spec),
                owner,
                Provenance(
                    agent_id=agent_id,
                    episode_ids=tuple(episode_ids or ()),
                    notes=notes,
                    claimed_metrics=claimed_metrics or {},
                ),
            )
            return {
                "rule_id": rv.rule_id,
                "version": rv.version,
                "status": rv.status.value,
                "content_hash": rv.content_hash,
            }

        return guarded(go)

    @server.tool(
        description="Evaluate a candidate: shadow replay on held-out synthetic cases, then "
        "canary if shadow promotes. Returns the decisions."
    )
    def evaluate_rule(rule_id: RuleId, version: Version) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            records = gate.evaluate(rule_id, version)
            rv = gate.registry.get(rule_id, version)
            return {"status": rv.status.value, "decisions": [_decision(r) for r in records]}

        return guarded(go)

    @server.tool(description="Status of a rule version and every decision recorded for it.")
    def get_evaluation_status(rule_id: RuleId, version: Version) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            rv = gate.registry.get(rule_id, version)
            decisions = [
                _decision(d) for d in gate.decisions(rule_id) if d.decision.version == version
            ]
            return {
                "rule_id": rule_id,
                "version": version,
                "status": rv.status.value,
                "decisions": decisions,
                "awaiting_human_approval": rv.status.value == "canary"
                and bool(decisions)
                and decisions[-1]["outcome"] == "canary_passed",
            }

        return guarded(go)

    @server.tool(description="The markdown evidence report for a decision id.")
    def get_report(
        decision_id: Annotated[str, Field(description="e.g. dec-00001")],
    ) -> dict[str, Any]:
        def go() -> dict[str, Any]:
            md = gate.report(decision_id)
            if md is None:
                raise KeyError(f"unknown decision {decision_id}")
            return {"decision_id": decision_id, "report": md}

        return guarded(go)

    @server.tool(description="Verify the audit trail: hash chain plus projection checks.")
    def verify_audit() -> dict[str, Any]:
        def go() -> dict[str, Any]:
            rep = gate.verify_integrity()
            return {
                "ok": rep.ok,
                "entries": rep.chain.entries,
                "head": rep.chain.head_hash,
                "problems": rep.problems,
            }

        return guarded(go)

    return server


def run_stdio(db: str) -> None:  # pragma: no cover - blocks on stdin
    gate = AgentGate.open(db)
    gate.bootstrap_baseline()
    build_gate_server(gate).run("stdio")
