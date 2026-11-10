"""Tools for `ach` episodes: work one returned ACH debit on the originator's behalf.

Scopes follow `scruple.agents.dispute_tools.DisputeScope`: `read` tools never change
state, `action` tools ask the engine to act (refused when the entry does not allow it
now), `clock` lets banking days pass, `submit` closes the episode. Every call, refused
ones included, is recorded in the episode's hash-chained trace.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from scruple.agents.dispute_tools import DisputeScope, DisputeToolSpec, NoArgs, _Args
from scruple.verifiers.ach_record import AchReport


class ReturnIdArgs(_Args):
    return_id: str = Field(min_length=1, max_length=32, description="An id from get_case.")


class CloseArgs(_Args):
    reason: str = Field(default="", max_length=1_000, description="Why, in a sentence.")


class DishonorArgs(_Args):
    return_id: str = Field(min_length=1, max_length=32, description="The return to dishonor.")
    code: Literal["R67", "R68"] = Field(description="R67 duplicate return, R68 untimely return.")
    reason: str = Field(default="", max_length=1_000)


class ReinitiateArgs(_Args):
    company_entry_description: str = Field(
        min_length=1, max_length=10, description="Company entry description of the new entry."
    )
    reason: str = Field(default="", max_length=1_000)


class AchWaitArgs(_Args):
    days: int = Field(ge=1, le=60, description="Calendar days to let pass.")


S = DisputeScope
ACH_TOOLS: tuple[DisputeToolSpec, ...] = (
    DisputeToolSpec(
        "get_case",
        "The original debit entry, every return received so far, the receiver's billing "
        "profile and authorization on file, the bank holiday calendar, today's date, "
        "what is pending and the actions allowed now.",
        S.READ,
        NoArgs,
    ),
    DisputeToolSpec("get_return", "Read one return entry in full.", S.READ, ReturnIdArgs),
    DisputeToolSpec(
        "get_ledger_view", "This entry's journal on the originator's books.", S.READ, NoArgs
    ),
    DisputeToolSpec(
        "accept",
        "Accept the return: concede the debit and close the receivable (contra revenue).",
        S.ACTION,
        CloseArgs,
    ),
    DisputeToolSpec(
        "write_off",
        "Write the receivable off as bad debt: owed, but not collectable through ACH.",
        S.ACTION,
        CloseArgs,
    ),
    DisputeToolSpec(
        "dishonor",
        "Dishonor an improper return (R67 duplicate, R68 untimely). Settles next banking "
        "day; the receiving bank may contest it within two banking days.",
        S.ACTION,
        DishonorArgs,
    ),
    DisputeToolSpec(
        "reinitiate",
        "Send the debit again. Settles next banking day; it may be returned again.",
        S.ACTION,
        ReinitiateArgs,
    ),
    DisputeToolSpec("wait", "Let simulated days pass (banks act).", S.CLOCK, AchWaitArgs),
    DisputeToolSpec(
        "submit_decision",
        "Finish: explain what you did, optionally with structured claims. Remaining time "
        "runs with you idle. Returns the scorecard.",
        S.SUBMIT,
        AchReport,
    ),
)
ACH_TOOL_BY_NAME: dict[str, DisputeToolSpec] = {t.name: t for t in ACH_TOOLS}
ACH_TOOL_NAMES: tuple[str, ...] = tuple(ACH_TOOL_BY_NAME)
ACH_ACTIONS = ("accept", "write_off", "dishonor", "reinitiate")


def ach_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in ACH_TOOLS
    ]


__all__ = [
    "ACH_ACTIONS",
    "ACH_TOOLS",
    "ACH_TOOL_BY_NAME",
    "ACH_TOOL_NAMES",
    "AchWaitArgs",
    "CloseArgs",
    "DishonorArgs",
    "ReinitiateArgs",
    "ReturnIdArgs",
    "ach_anthropic_tools",
]
