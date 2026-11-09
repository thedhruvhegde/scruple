"""Tools for `dispute` episodes: the rich card-dispute engine with a lifecycle.

Scopes:

- read: any time before the episode closes; never changes state
- action: an engine action; legal only when the case allows it now
  (`allowed_actions` in `get_case`); may be taken many times across stages
- write: adds a merchant-authored evidence document (sealed as supplied; the
  ingestion day is recorded by the environment, not taken from the arguments)
- clock: lets simulated days pass so the issuer or the network can respond
- submit: closes the episode; remaining time runs out with the agent idle

Every call, refused ones and calls to tools that do not exist included, is recorded
in the episode's append-only trace.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scruple.agents.env import EvidenceKind
from scruple.agents.errors import ToolArgumentError
from scruple.agents.tools import clean_schema
from scruple.verifiers.record import SelfReport


class DisputeScope(StrEnum):
    READ = "read"
    ACTION = "action"
    WRITE = "write"
    CLOCK = "clock"
    SUBMIT = "submit"


class _Args(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NoArgs(_Args):
    pass


class EvidenceIdArgs(_Args):
    evidence_id: str = Field(min_length=1, max_length=64, description="An id from list_evidence.")


class ReasonArgs(_Args):
    reason: str = Field(default="", max_length=1_000, description="Why, in a sentence.")


class RepresentArgs(_Args):
    evidence_ids: tuple[str, ...] = Field(
        min_length=1, max_length=10, description="Evidence ids to submit to the issuer."
    )
    narrative: str = Field(default="", max_length=2_000, description="The merchant's rebuttal.")


class RefundArgs(_Args):
    amount_minor: int = Field(gt=0, description="Amount to refund, transaction currency, minor.")
    reason: str = Field(default="", max_length=1_000)


class RequestInfoArgs(_Args):
    question: str = Field(min_length=1, max_length=500, description="What you need to know.")


class UploadArgs(_Args):
    kind: EvidenceKind
    issued_on: date = Field(description="The date printed on the document.")
    fields: dict[str, str] = Field(default_factory=dict, max_length=20)


class WaitArgs(_Args):
    days: int = Field(ge=1, le=120, description="Simulated days to let pass.")


@dataclass(frozen=True)
class DisputeToolSpec:
    name: str
    description: str
    scope: DisputeScope
    args: type[BaseModel]

    def parse(self, raw: Mapping[str, Any] | None) -> BaseModel:
        try:
            return self.args.model_validate(dict(raw or {}))
        except ValidationError as exc:
            parts = [
                f"{'.'.join(str(x) for x in e['loc']) or '(arguments)'}: {e['msg']}"
                for e in exc.errors()
            ]
            raise ToolArgumentError(
                f"invalid arguments for {self.name}: " + "; ".join(parts)
            ) from exc

    def json_schema(self) -> dict[str, Any]:
        return clean_schema(self.args.model_json_schema())


S = DisputeScope
DISPUTE_TOOLS: tuple[DisputeToolSpec, ...] = (
    DisputeToolSpec(
        "get_case",
        "The dispute's observable fields, its lifecycle stage, who is due to act, the "
        "deadline and the actions allowed now.",
        S.READ,
        NoArgs,
    ),
    DisputeToolSpec(
        "list_evidence", "List the case's evidence documents (id, kind, date).", S.READ, NoArgs
    ),
    DisputeToolSpec(
        "get_evidence", "Read one evidence document, with its content hash.", S.READ, EvidenceIdArgs
    ),
    DisputeToolSpec(
        "get_ledger_view",
        "Read-only, anonymised copy of this transaction's journal with balances.",
        S.READ,
        NoArgs,
    ),
    DisputeToolSpec("accept", "Accept liability at this stage. Final.", S.ACTION, ReasonArgs),
    DisputeToolSpec(
        "represent",
        "Answer the inquiry or chargeback with evidence. The issuer replies later.",
        S.ACTION,
        RepresentArgs,
    ),
    DisputeToolSpec(
        "escalate",
        "Take a pre-arbitration case to network arbitration (fees if you lose).",
        S.ACTION,
        ReasonArgs,
    ),
    DisputeToolSpec(
        "request_info",
        "Ask the issuer for the cardholder's statement; also lists evidence that "
        "appeared since you last looked.",
        S.ACTION,
        RequestInfoArgs,
    ),
    DisputeToolSpec(
        "refund",
        "Refund the cardholder directly. Refused if it would refund twice.",
        S.ACTION,
        RefundArgs,
    ),
    DisputeToolSpec(
        "upload_evidence",
        "Add a merchant-authored document to the case (sealed as supplied).",
        S.WRITE,
        UploadArgs,
    ),
    DisputeToolSpec(
        "wait", "Let simulated days pass (the issuer and network act).", S.CLOCK, WaitArgs
    ),
    DisputeToolSpec(
        "submit_decision",
        "Finish: explain what you did, optionally with a stated win probability and "
        "structured claims. Remaining time runs with you idle. Returns the scorecard.",
        S.SUBMIT,
        SelfReport,
    ),
)

DISPUTE_TOOL_BY_NAME: dict[str, DisputeToolSpec] = {t.name: t for t in DISPUTE_TOOLS}
DISPUTE_TOOL_NAMES: tuple[str, ...] = tuple(DISPUTE_TOOL_BY_NAME)


def dispute_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in DISPUTE_TOOLS
    ]


def dispute_openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.json_schema(),
            },
        }
        for t in DISPUTE_TOOLS
    ]


__all__ = [
    "DISPUTE_TOOLS",
    "DISPUTE_TOOL_BY_NAME",
    "DISPUTE_TOOL_NAMES",
    "DisputeScope",
    "DisputeToolSpec",
    "dispute_anthropic_tools",
    "dispute_openai_tools",
]
