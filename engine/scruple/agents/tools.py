"""The tool suite exposed to agents, with JSON-schema definitions from pydantic.

Scopes decide when a tool may be called:

- read: any time before the episode closes; never changes state
- info: before the action is taken (asking questions after acting is pointless)
- action: exactly once per episode; moves money on the ledger
- submit: closes the episode and returns the verifier scorecard

The same specs feed every runner (Anthropic, OpenAI, local), the MCP server, the
Claude Agent SDK and LangGraph adapters, and the `scruple episode` CLI.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.agents.env import ActionKind
from scruple.agents.errors import ToolArgumentError
from scruple.agents.models import Decision


class Scope(StrEnum):
    READ = "read"
    INFO = "info"
    ACTION = "action"
    SUBMIT = "submit"


class _Args(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NoArgs(_Args):
    pass


class GetEvidenceArgs(_Args):
    evidence_id: str = Field(
        min_length=1, max_length=64, description="An id from list_evidence, e.g. ev-01."
    )


class ReasonArgs(_Args):
    reason: str = Field(default="", max_length=1_000, description="Why, in a sentence.")


class RepresentArgs(_Args):
    evidence_ids: tuple[str, ...] = Field(
        min_length=1, max_length=10, description="Evidence ids to submit to the issuer."
    )
    narrative: str = Field(default="", max_length=2_000, description="The merchant's rebuttal.")


class RequestInfoArgs(_Args):
    question: str = Field(min_length=1, max_length=500, description="What you need to know.")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    scope: Scope
    args: type[BaseModel]
    action: ActionKind | None = None

    def parse(self, raw: Mapping[str, Any] | None) -> BaseModel:
        try:
            return self.args.model_validate(dict(raw or {}))
        except ValidationError as exc:
            raise _argument_error(self.name, exc) from exc

    def json_schema(self) -> dict[str, Any]:
        return clean_schema(self.args.model_json_schema())


def _argument_error(tool: str, exc: ValidationError) -> ToolArgumentError:
    parts = [
        f"{'.'.join(str(x) for x in e['loc']) or '(arguments)'}: {e['msg']}" for e in exc.errors()
    ]
    return ToolArgumentError(f"invalid arguments for {tool}: " + "; ".join(parts))


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "get_case",
        "Get the dispute's observable features and the episode status.",
        Scope.READ,
        NoArgs,
    ),
    ToolSpec(
        "list_evidence",
        "List the evidence documents available for this dispute (ids, kinds, titles).",
        Scope.READ,
        NoArgs,
    ),
    ToolSpec("get_evidence", "Read one evidence document by id.", Scope.READ, GetEvidenceArgs),
    ToolSpec(
        "get_ledger_view",
        "Read-only view of the journal for this transaction: captures, refunds, "
        "chargeback debits and fees, with balances.",
        Scope.READ,
        NoArgs,
    ),
    ToolSpec(
        "request_info",
        "Ask for more information before acting. May return nothing new.",
        Scope.INFO,
        RequestInfoArgs,
    ),
    ToolSpec(
        "accept_liability",
        "Accept the chargeback: the cardholder keeps the disputed amount. Final.",
        Scope.ACTION,
        ReasonArgs,
        ActionKind.ACCEPT_LIABILITY,
    ),
    ToolSpec(
        "refund",
        "Refund the cardholder and close the dispute. Final.",
        Scope.ACTION,
        ReasonArgs,
        ActionKind.REFUND,
    ),
    ToolSpec(
        "represent",
        "Contest the chargeback (representment) with evidence. Final. Losing a "
        "representment costs fees.",
        Scope.ACTION,
        RepresentArgs,
        ActionKind.REPRESENT,
    ),
    ToolSpec(
        "escalate",
        "Send the dispute to a human analyst. Final. Costs analyst time.",
        Scope.ACTION,
        ReasonArgs,
        ActionKind.ESCALATE,
    ),
    ToolSpec(
        "submit_decision",
        "Finish the episode with a short explanation of what you did and why. "
        "Returns the verifier scorecard.",
        Scope.SUBMIT,
        Decision,
    ),
)

TOOL_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}
TOOL_NAMES: tuple[str, ...] = tuple(TOOL_BY_NAME)


def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline `$ref`s and drop titles: some model APIs reject `$defs`."""
    defs: dict[str, Any] = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged)
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in ("$defs", "title"):
                continue
            if k == "properties":
                out[k] = {name: walk(sub) for name, sub in v.items()}
            else:
                out[k] = walk(v)
        return out

    cleaned: dict[str, Any] = walk(schema)
    cleaned.setdefault("properties", {})
    cleaned["type"] = "object"
    cleaned["additionalProperties"] = False
    return cleaned


def anthropic_tools() -> list[dict[str, Any]]:
    """Tool definitions for the Anthropic Messages API."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in TOOLS
    ]


def openai_tools() -> list[dict[str, Any]]:
    """Tool definitions for OpenAI-compatible Chat Completions (function calling)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.json_schema(),
            },
        }
        for t in TOOLS
    ]
